"""In-memory ``Player``/``PlayHandle`` doubles for the loop and suspension tests.

The design's testing seam is the ``Player``/``MpvClient`` boundary: the loop is
driven with a fake player whose ended-futures and control calls a test observes,
so the advance/interrupt/pause/crash logic is exercised without a real mpv. The
fake records every ``loadfile`` (part + paused flag), every pause/resume/stop, and
lets a test resolve a load's end with any :class:`EndFileReason` -- including the
synthetic ``crashed`` -- or make the next load fail like a wedged connection.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Self, final

from punt_vox.types_programs.mpv_event import EndFileReason

if TYPE_CHECKING:
    from punt_vox.voxd.programs.part import Part


@final
class FakePlayHandle:
    """One load: the ended-future the loop awaits, resolved by the test."""

    __slots__ = ("_ended",)
    _ended: asyncio.Future[EndFileReason]

    def __new__(cls, ended: asyncio.Future[EndFileReason]) -> Self:
        self = super().__new__(cls)
        self._ended = ended
        return self

    async def ended(self) -> EndFileReason:
        """Await this load's end (the loop races this against the interrupt)."""
        return await self._ended

    def finish(self, reason: EndFileReason = EndFileReason.EOF) -> None:
        """Resolve the load with ``reason`` -- a natural end drives the loop."""
        if not self._ended.done():
            self._ended.set_result(reason)

    def crash(self) -> None:
        """Resolve the load with the synthetic ``crashed`` reason (a socket EOF)."""
        self.finish(EndFileReason.CRASHED)


@final
class FakePlayer:
    """A ``Player`` double: records loads and controls, hands back live handles."""

    __slots__ = (
        "_pending_errors",
        "_ready",
        "_spawned",
        "handles",
        "parts",
        "paused_flags",
        "pauses",
        "resumes",
        "stops",
    )
    parts: list[Part]
    paused_flags: list[bool]
    handles: list[FakePlayHandle]
    pauses: int
    resumes: int
    stops: int
    _ready: asyncio.Event
    _spawned: asyncio.Event
    _pending_errors: list[Exception]

    def __new__(cls) -> Self:
        self = super().__new__(cls)
        self.parts = []
        self.paused_flags = []
        self.handles = []
        self.pauses = 0
        self.resumes = 0
        self.stops = 0
        self._ready = asyncio.Event()
        self._ready.set()  # ready by default; a test clears it to model a crash gap
        self._spawned = asyncio.Event()
        self._pending_errors = []
        return self

    async def await_ready(self) -> None:
        """Park until ready -- a test clears readiness to model the WaitReady gate."""
        await self._ready.wait()

    async def play(self, part: Part, *, paused: bool) -> FakePlayHandle:
        """Record the load (part + paused flag) and return a controllable handle."""
        if self._pending_errors:
            raise self._pending_errors.pop(0)
        loop = asyncio.get_running_loop()
        handle = FakePlayHandle(loop.create_future())
        self.parts.append(part)
        self.paused_flags.append(paused)
        self.handles.append(handle)
        self._spawned.set()
        return handle

    def pause(self) -> None:
        """Record a pause control command."""
        self.pauses += 1

    def resume(self) -> None:
        """Record a resume control command."""
        self.resumes += 1

    def stop(self) -> None:
        """Record a stop control command."""
        self.stops += 1

    def fail_next_load(self, exc: Exception) -> None:
        """Make the next ``play`` raise ``exc`` (a wedged/lost connection)."""
        self._pending_errors.append(exc)

    def become_not_ready(self) -> None:
        """Close the readiness gate so ``await_ready`` blocks (a crash gap)."""
        self._ready.clear()

    def become_ready(self) -> None:
        """Open the readiness gate so ``await_ready`` returns (mpv reconnected)."""
        self._ready.set()

    async def wait_for(self, count: int) -> None:
        """Block until at least ``count`` loads have been issued."""
        while len(self.parts) < count:
            self._spawned.clear()
            if len(self.parts) >= count:
                return
            await asyncio.wait_for(self._spawned.wait(), timeout=2.0)
