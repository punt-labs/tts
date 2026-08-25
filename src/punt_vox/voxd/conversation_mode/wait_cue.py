"""Background wait cue: repeats a bundled chime while a call's reply is pending.

A latency mitigation: the per-turn ``claude`` subprocess spawn measured
13-25s median. No existing audio-playback primitive in this
codebase fits a genuine ambient loop -- the daemon's two bundled chime assets
(``voxd/chimes.py``'s ``_CHIME_MAP``) are short one-shot notification sounds,
and the full music-Program subsystem (``src/punt_vox/voxd/music_player/``)
needs either a fresh ElevenLabs generation (measured minutes -- far too slow
to trigger per-turn) or a pre-authored catalog album (none exists for this
purpose), plus carries playlist/Format state this bounded, short-lived cue
has no use for. Repeating the existing bundled chime on an interval reuses a
real, already-tested daemon primitive with zero new audio-playback machinery
and zero per-repeat synthesis cost, at the cost of being a periodic ping
rather than a true ambient bed -- an acceptable stopgap trade for a latency
mitigation, not a redesign.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncGenerator
from typing import Protocol, Self, final, runtime_checkable

logger = logging.getLogger(__name__)

__all__ = ["ChimeFn", "WaitCue"]

# How often the cue repeats during the wait.
_WAIT_CHIME_INTERVAL_S = 8.0


@runtime_checkable
class ChimeFn(Protocol):
    """Play the background wait cue once and return.

    Takes no text (unlike :class:`~.call_session.SpeakFn`) -- it always plays
    the same bundled chime asset, never synthesized speech, so repeating it
    costs no ElevenLabs credits. Async for the same "must not stall the
    call's event loop" reason as ``SpeakFn`` (see that Protocol's docstring).
    """

    async def __call__(self) -> None: ...


@final
class WaitCue:
    """Repeats *chime* on an interval for as long as :meth:`active` stays open.

    A no-op (no background task at all) when constructed with ``chime=None``
    -- the scripted (``--script``) path and every existing test that doesn't
    pass one. Absence is a legitimate default, not a deferred decision: a
    scripted call has no human waiting through real ``claude`` latency to
    reassure.
    """

    __slots__ = ("_chime",)
    _chime: ChimeFn | None

    def __new__(cls, chime: ChimeFn | None) -> Self:
        self = super().__new__(cls)
        self._chime = chime
        return self

    @contextlib.asynccontextmanager
    async def active(self) -> AsyncGenerator[None]:
        """Repeat the cue for as long as the ``async with`` body runs.

        Starts a task that awaits :data:`_WAIT_CHIME_INTERVAL_S` then chimes,
        in a loop, for the duration of the body (the real ``claude`` round
        trip); cancelled and awaited on exit either way, so a reply that
        arrives before the first interval elapses never chimes at all, and
        the task never outlives this method.
        """
        chime = self._chime
        if chime is None:
            yield
            return
        task = asyncio.create_task(self._repeat(chime))
        try:
            yield
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    @staticmethod
    async def _repeat(chime: ChimeFn) -> None:
        """Call *chime* every :data:`_WAIT_CHIME_INTERVAL_S`, forever.

        The caller cancels once the wait ends. A failing *chime* is logged
        and swallowed, never left to propagate out of this loop: this task
        races the real turn's own ``send_turn`` inside :meth:`active`'s
        ``finally: await task`` cleanup, and an uncaught chime failure
        arriving around the same time as a genuine turn failure could win
        that race and mask the turn's own exception -- the one the caller
        actually needs to see.
        """
        while True:
            await asyncio.sleep(_WAIT_CHIME_INTERVAL_S)
            try:
                await chime()
            except Exception:
                logger.exception("wait cue chime failed")
