"""A fake :class:`~punt_vox.voxd.conversation_mode.playback_sink.PlaybackSink`.

Records every operation and, critically, *detects reentrancy*: if two
operations are ever in flight on the same fake at once, :meth:`FakeSink._enter`
raises. That is what makes the race tests real proof rather than theatre --
a fake that only recorded calls would pass even if
:class:`~punt_vox.voxd.conversation_mode.playback_sink_actor.PlaybackSinkActor`
let two commands interleave; this one cannot.
"""

from __future__ import annotations

import asyncio
from typing import Self, final

from punt_vox.voxd.conversation_mode.playback_sink import SinkStatus

__all__ = ["FakeSink", "SinkReentrancyError"]


class SinkReentrancyError(RuntimeError):
    """Raised when a second operation starts while one is already in flight.

    Proves a serialization violation actually happened -- the sole purpose
    of this fake beyond recording calls (see the module docstring).
    """


@final
class FakeSink:
    """An in-memory sink whose operations yield mid-call, exposing races.

    Each of :meth:`write`, :meth:`clear`, and :meth:`close` awaits
    ``asyncio.sleep(0)`` partway through -- a real yield point back to the
    event loop, standing in for the real I/O a concrete sink would await.
    Without a caller serializing access (:class:`~punt_vox.voxd.
    conversation_mode.playback_sink_actor.PlaybackSinkActor`), two
    concurrently-running coroutines calling these methods directly on the
    same instance can and will interleave at that yield point -- exactly
    the race the actor exists to prevent. :meth:`_enter`/:meth:`_exit`
    detect that interleaving and raise :class:`SinkReentrancyError`.
    """

    __slots__ = ("_buffer", "_busy", "_current_op", "_history", "_status")
    _buffer: bytearray
    _status: SinkStatus
    _busy: bool
    _current_op: str | None
    _history: list[str]

    def __new__(cls) -> Self:
        self = super().__new__(cls)
        self._buffer = bytearray()
        self._status = SinkStatus.IDLE
        self._busy = False
        self._current_op = None
        self._history = []
        return self

    @property
    def status(self) -> SinkStatus:
        return self._status

    @property
    def buffered_bytes(self) -> int:
        """Return how many bytes are currently buffered (unplayed/uncleared)."""
        return len(self._buffer)

    @property
    def history(self) -> tuple[str, ...]:
        """Return every completed operation, in the order it finished."""
        return tuple(self._history)

    async def write(self, chunk: bytes) -> None:
        self._enter("write")
        await asyncio.sleep(0)
        if self._status is SinkStatus.CLOSED:
            self._exit()
            msg = "write on a closed sink"
            raise ValueError(msg)
        self._buffer.extend(chunk)
        self._status = SinkStatus.WRITING
        self._history.append(f"write:{len(chunk)}")
        self._exit()

    async def clear(self) -> None:
        self._enter("clear")
        await asyncio.sleep(0)
        if self._status is SinkStatus.CLOSED:
            self._exit()
            msg = "clear on a closed sink"
            raise ValueError(msg)
        self._buffer.clear()
        self._status = SinkStatus.IDLE
        self._history.append("clear")
        self._exit()

    async def close(self) -> None:
        self._enter("close")
        await asyncio.sleep(0)
        self._buffer.clear()
        self._status = SinkStatus.CLOSED
        self._history.append("close")
        self._exit()

    def _enter(self, operation: str) -> None:
        if self._busy:
            msg = (
                f"reentrant sink access: {operation} started during {self._current_op}"
            )
            raise SinkReentrancyError(msg)
        self._busy = True
        self._current_op = operation

    def _exit(self) -> None:
        self._busy = False
        self._current_op = None
