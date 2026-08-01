"""``MpvClient`` -- one live connection to the persistent mpv process.

The client owns a single mpv JSON-IPC connection: the stream pair, the reader
coroutine, request/response correlation, and the *one* ended-future the playback
loop awaits for the currently-loaded part. It knows nothing about restarts or
process spawning -- that is the ``MpvSupervisor``'s job. When the socket closes
(mpv crashed), the reader fails every pending command
future *and* resolves the loop's ended-future with the synthetic
:class:`~punt_vox.types_programs.mpv_event.EndFileReason` ``crashed`` -- the two
halves of the I7 guarantee that a crash leaves no ``await`` orphaned.

Framing is safe without a mutex: this runs on one event loop in one thread, and
each command is written with a single :meth:`asyncio.StreamWriter.write` call,
which appends the whole frame contiguously and cannot be spliced by another
coroutine. The "send lock" the design names is realised by that single-call
framing, not a thread lock (there are no threads to race).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import TYPE_CHECKING, Self, final

from punt_vox.types_programs.mpv_event import (
    EndFileReason,
    MpvEvent,
    MpvResponse,
)
from punt_vox.types_programs.wire import JsonObject
from punt_vox.voxd.wire_text import SafeText

if TYPE_CHECKING:
    from collections.abc import Callable

    from punt_vox.types_programs.mpv_event import MpvCommand

__all__ = ["MpvClient"]

logger = logging.getLogger(__name__)

_COMMAND_TIMEOUT = 5.0
"""Seconds a command awaits its response before surfacing a wedged mpv as a fault."""

_END_FILE = "end-file"


@final
class MpvClient:
    """One mpv IPC connection: correlated commands and the loop's ended-future."""

    __slots__ = (
        "_closed",
        "_ended",
        "_next_id",
        "_on_crash",
        "_pending",
        "_reader",
        "_reader_task",
        "_writer",
    )
    _reader: asyncio.StreamReader
    _writer: asyncio.StreamWriter
    _on_crash: Callable[[], None]
    _pending: dict[int, asyncio.Future[MpvResponse]]
    _ended: asyncio.Future[EndFileReason] | None
    _next_id: int
    _closed: bool
    _reader_task: asyncio.Task[None] | None

    def __new__(
        cls,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        on_crash: Callable[[], None],
    ) -> Self:
        self = super().__new__(cls)
        self._reader = reader
        self._writer = writer
        self._on_crash = on_crash
        self._pending = {}
        self._ended = None
        self._next_id = 1
        self._closed = False
        self._reader_task = None
        return self

    def start(self) -> None:
        """Launch the reader coroutine (called once, right after connecting)."""
        self._reader_task = asyncio.create_task(self._read_loop())

    @property
    def is_ready(self) -> bool:
        """Return whether the connection is live (commands may be issued)."""
        return not self._closed

    def arm_ended(self) -> asyncio.Future[EndFileReason]:
        """Arm and return the ended-future for the load about to be issued.

        A prior armed future, if any, is dropped: the loop only re-arms after it
        has abandoned the previous one (an interrupt) or seen it resolve, so the
        replaced future is never awaited.
        """
        loop = asyncio.get_running_loop()
        ended: asyncio.Future[EndFileReason] = loop.create_future()
        self._ended = ended
        return ended

    async def request(self, command: MpvCommand) -> MpvResponse:
        """Send ``command`` and await mpv's reply, or raise on timeout/crash.

        A wedged-but-alive mpv surfaces as :class:`TimeoutError` (never a hang);
        a crash mid-flight surfaces as :class:`ConnectionError` when the reader
        fails the pending future. Either way the caller decides -- the loop backs
        off, the supervisor restarts.
        """
        request_id = self._next_id
        self._next_id += 1
        future: asyncio.Future[MpvResponse] = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        try:
            self._writer.write(command.framed(request_id))
            await self._writer.drain()
            return await asyncio.wait_for(future, _COMMAND_TIMEOUT)
        finally:
            self._pending.pop(request_id, None)

    def write_command(self, command: MpvCommand) -> None:
        """Fire ``command`` without awaiting a reply (pause/resume/stop control).

        The response, if any, is ignored (no future registered). A single
        ``write`` keeps the frame atomic; a closed transport raises, which the
        caller (the player) suppresses because recovery honours the flag.
        """
        request_id = self._next_id
        self._next_id += 1
        self._writer.write(command.framed(request_id))

    async def close(self) -> None:
        """Close the connection deliberately (graceful shutdown, not a crash).

        Setting ``_closed`` first makes the reader's terminal ``_on_eof`` a no-op,
        so a deliberate close never fires the crash callback.
        """
        self._closed = True
        if self._reader_task is not None:
            self._reader_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._reader_task
        self._writer.close()
        with contextlib.suppress(Exception):
            await self._writer.wait_closed()

    async def _read_loop(self) -> None:
        """Read newline-framed messages until EOF, then fail every awaiter (I7)."""
        try:
            while True:
                line = await self._reader.readline()
                if not line:
                    break
                self._dispatch(line)
        except (ConnectionError, OSError):
            pass  # a broken socket is an EOF by another name
        finally:
            self._on_eof()

    def _dispatch(self, line: bytes) -> None:
        """Route one line to its handler, dropping any line mpv should not have sent.

        A non-conformant line raises ``ValueError`` from the wire accessors --
        unparseable JSON, a present-but-wrong-typed ``event``/``request_id``
        discriminant, an end-file missing its reason, or a response missing
        ``error``. It is logged and dropped here so a single bad line never exits
        the reader loop and fires a spurious ``_on_eof`` crash (a needless
        supervisor restart). This one boundary now covers the response and
        classification paths, symmetric with the event drop it subsumes.
        """
        raw = line.decode(errors="replace")
        try:
            self._classify(JsonObject.parse(raw, "mpv"))
        except ValueError:
            logger.warning(
                "mpv: dropping non-conformant line: %s", SafeText.of(raw).text
            )

    def _classify(self, obj: JsonObject) -> None:
        """Hand a parsed line to the event or response handler, or skip its shape."""
        if obj.opt_str("event") is not None:
            self._on_event(obj)
        elif obj.opt_int("request_id") is not None:
            self._on_response(obj)
        else:
            logger.debug("mpv: skipping unclassified line")

    def _on_response(self, obj: JsonObject) -> None:
        """Resolve the pending future for a command reply bearing its request id."""
        response = MpvResponse.from_object(obj)
        future = self._pending.get(response.request_id)
        if future is not None and not future.done():
            future.set_result(response)

    def _on_event(self, obj: JsonObject) -> None:
        """Resolve the loop's ended-future on an advancing end; drop teardown noise.

        Only an advancing end-file drives the loop, so only such an event resolves
        the ended-future. ``stop``/``redirect``/``quit`` are our own teardown -- a
        ``stop`` command or a ``loadfile`` replace -- and resolving them would
        spuriously reload the current part, so they are dropped. An unrecognized
        reason folds to the advancing ``eof`` class in :meth:`MpvEvent.from_object`,
        so a newer mpv's ``unknown`` advances rather than hanging the part; a
        genuinely malformed event raises out to ``_dispatch``, which drops it.
        """
        event = MpvEvent.from_object(obj)
        if event.name != _END_FILE or event.reason is None or not event.reason.advances:
            return
        self._resolve_ended(event.reason)

    def _resolve_ended(self, reason: EndFileReason) -> None:
        """Resolve the current ended-future once (consumed on resolution)."""
        ended = self._ended
        if ended is not None and not ended.done():
            ended.set_result(reason)

    def _on_eof(self) -> None:
        """On socket EOF fail every pending command and crash-resolve the load (I7)."""
        if self._closed:
            return
        self._closed = True
        lost = ConnectionError("mpv connection lost")
        for future in self._pending.values():
            if not future.done():
                future.set_exception(lost)
        self._pending.clear()
        self._resolve_ended(EndFileReason.CRASHED)
        self._on_crash()
