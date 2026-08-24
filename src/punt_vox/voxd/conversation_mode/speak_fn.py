"""The async speak-and-return-when-started-or-done contract every call cue uses."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

__all__ = ["SpeakFn"]


@runtime_checkable
class SpeakFn(Protocol):
    """Speak *text* aloud and return once playback has started or completed.

    Async, not sync: ``VoxClientSync.synthesize`` blocks its calling thread
    for the full round trip to the daemon, and this call happens inline
    inside :class:`~.call_session.CallSession`'s async methods, which run on
    the call's single event loop -- a synchronous ``SpeakFn`` would stall
    that loop for the duration of every utterance, during which the
    microphone's capture queue keeps filling and a pending ``/call stop``
    goes unnoticed. A caller backed by a blocking client wraps it in
    ``asyncio.to_thread`` (see :mod:`punt_vox.commands.call`).
    """

    async def __call__(self, text: str) -> None: ...
