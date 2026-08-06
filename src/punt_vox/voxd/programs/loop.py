"""The playback loop -- play the current Part, auto-advance, race controls.

``ProgramLoop`` owns the mpv player and nothing else: it waits for mpv to be
ready, loads ``program.playing``, and when the part ends *posts a Rotate message*
(never mutating the Program directly) so the single :class:`ControlChannel`
writer advances the cursor, then loads the new ``program.playing``. It never
generates. A skip / play-a-part / off interrupts the current part at once (the
channel's ``interrupt`` event); a retune does not -- the current part finishes
first, then the loop plays the new pool's Part (finish-current-then-switch).

The blocking point is the mpv-``ready`` gate (:meth:`Player.await_ready`), the
loop's single ``WaitReady`` step -- it replaces the deleted suspension gate. On
the synthetic ``crashed`` reason the loop does not advance: it waits for mpv to
come back and replays the **current** part, honouring the paused flag (I6). The
explicit ``is_paused`` guard at the advance decision stays (Z ``T3``): an ``eof``
mpv buffered in the instant before a pause must not advance the cursor.

A ``loadfile`` that mpv will not accept -- a wedged connection (timeout) or a
crash mid-send (``ConnectionError``) -- is recorded observably and followed by a
bounded backoff, so a persistent fault cannot spin the loop hot; the process-level
mpv fault the supervisor raises is the authoritative surface.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Self, final

from punt_vox.types_programs.playback_fault import PlaybackFaultKind
from punt_vox.voxd.programs.interrupt_race import InterruptRace
from punt_vox.voxd.programs.playback_signal import Rotate
from punt_vox.voxd.wire_text import SafeText

if TYPE_CHECKING:
    from punt_vox.voxd.programs.control_channel import ControlChannel
    from punt_vox.voxd.programs.part import Part
    from punt_vox.voxd.programs.playback_health import PlaybackHealth
    from punt_vox.voxd.programs.player import Player
    from punt_vox.voxd.programs.sleeper import Sleeper
    from punt_vox.voxd.programs.suspension import PlaybackSuspension
    from punt_vox.voxd.programs.track_end import TrackEnd

__all__ = ["ProgramLoop"]

logger = logging.getLogger(__name__)

_LOAD_BACKOFF_SECONDS = 2.0
"""Bounded pause before retrying a load mpv would not accept (no CPU spin)."""


@final
class ProgramLoop:
    """Play ``program.playing`` over mpv and advance when the part ends."""

    __slots__ = (
        "_channel",
        "_health",
        "_player",
        "_race",
        "_sleeper",
        "_suspension",
    )
    _channel: ControlChannel
    _player: Player
    _race: InterruptRace
    _sleeper: Sleeper
    _health: PlaybackHealth
    _suspension: PlaybackSuspension

    def __new__(
        cls,
        channel: ControlChannel,
        player: Player,
        sleeper: Sleeper,
        health: PlaybackHealth,
        suspension: PlaybackSuspension,
    ) -> Self:
        self = super().__new__(cls)
        self._channel = channel
        self._player = player
        self._race = InterruptRace(channel.interrupt)
        self._sleeper = sleeper
        self._health = health
        self._suspension = suspension
        return self

    @property
    def health(self) -> PlaybackHealth:
        """Return the player-health surface the loop writes (status reads it)."""
        return self._health

    async def run(self) -> None:
        """Run the loop for the lifetime of the daemon.

        The top-level guard is the last line of defence: an unexpected error in
        one step (a raising player, a bug) is logged at ERROR and the loop
        continues, so playback never stops on a silent task death.
        """
        while True:
            try:
                await self._step()
            except Exception:
                logger.exception("playback loop: unexpected error in a step")

    async def _step(self) -> None:
        """Play the current Part, or idle mpv and wait for one to become playable.

        The cursor is read fresh each step, so a ``prev``/``next`` that moved it
        (while paused or between parts) plays the newly-cursored Part. When
        nothing is playable -- off, or an empty ``generating_first`` pool -- the
        loop stops mpv (returning it to idle) and blocks on ``channel.changed``.
        """
        target = self._channel.source.playing
        if target is not None:
            await self._play(target)
            return
        self._player.stop()
        await self._wait_for_playable()

    async def _wait_for_playable(self) -> None:
        """Block until a Part becomes playable (first track, or a retune)."""
        self._channel.changed.clear()
        if self._channel.source.playing is not None:
            return  # became available between the read and the clear
        await self._channel.changed.wait()

    async def _play(self, target: Part) -> None:
        """Wait for mpv, load ``target``, settle its end, then act (F3).

        ``WaitReady`` parks until mpv is up (startup or post-crash); the load is
        paused per the suspension flag (Fork B / I6). A load mpv will not accept
        becomes an observable fault plus a bounded backoff rather than a raise
        into ``run``'s guard, which would spin on the same unplayable target.
        The post-settle decision is ``_finish``.
        """
        self._channel.interrupt.clear()
        await self._player.await_ready()
        try:
            handle = await self._player.play(target, paused=self._suspension.is_paused)
        except OSError as exc:  # ConnectionError/TimeoutError are OSError subclasses
            await self._back_off_load(target, exc)
            return
        except ValueError:
            # The player's containment gate refused the part path before loadfile
            # -- a hostile/corrupt manifest whose file field is a symlink or
            # escapes the album dir. Treat it exactly like a bad file: record it
            # observably and advance past it, so one poisoned part can neither
            # open a file outside the album nor hot-spin the loop.
            await self._skip_unplayable(target)
            return
        self._health.clear()
        end = await self._race.settle(handle)
        await self._finish(target, end)

    async def _skip_unplayable(self, target: Part) -> None:
        """Record a part the player refused before loadfile, then advance past it.

        Shares the bad-file transition (``_note_error_fault`` + ``_advance_after``)
        a mpv ``end-file`` ``error`` reason takes, so a containment-refused part
        and a decode-failed part leave the loop in the same advanced state.
        """
        await self._note_error_fault(target)
        await self._advance_after(target)

    async def _finish(self, target: Part, end: TrackEnd) -> None:
        """Act on how the part stopped: replay, hold, idle, or advance the cursor.

        A crash does not advance -- the next ``_step`` replays the current part
        via ``WaitReady`` honouring pause (I6). The paused guard fires next: an
        ``eof`` mpv buffered just before a pause must not advance (Z ``T3``); the
        next ``_step`` reloads the current part paused. A user interrupt does not
        advance -- the next source decides. A clean or bad-file end advances -- a
        bad file is recorded observably first (F3), never a silent skip.
        """
        if end.crashed:
            return  # cursor unmoved; _step replays the current part after WaitReady
        if self._suspension.is_paused:
            return  # T3: a buffered eof under pause must not advance
        if end.interrupted:
            return  # skip / off / switch; _step plays the new source or idles mpv
        if end.faulted:
            await self._note_error_fault(target)
        await self._advance_after(target)

    async def _back_off_load(self, target: Part, exc: Exception) -> None:
        """Record a load mpv would not accept observably, then pause so it cannot spin.

        The status-surfaced reason is :class:`SafeText`-sanitized so a host path
        never crosses the wire; the raw exception rides ``exc_info`` to the log.
        The supervisor's process-level fault is the authoritative surface -- this
        per-part note keeps the loop's own retry observable and bounded.
        """
        reason = SafeText.of(str(exc)).text
        self._health.record(
            target,
            f"mpv did not accept the load: {reason}",
            PlaybackFaultKind.PLAYER_UNAVAILABLE,
        )
        logger.error(
            "mpv load failed for part %s; backing off %ss",
            target.index,
            _LOAD_BACKOFF_SECONDS,
            exc_info=exc,
        )
        await self._sleeper.sleep(_LOAD_BACKOFF_SECONDS)

    async def _note_error_fault(self, target: Part) -> None:
        """Record a bad/corrupt part observably, then pause so it cannot spin.

        mpv reported the part could not be played (``end-file`` reason ``error``),
        so the loop advances past it; the backoff bounds the rate at which a
        wholly-corrupt pool would rotate.
        """
        self._health.record(
            target,
            "mpv could not play the part file",
            PlaybackFaultKind.TRACK_ERROR,
        )
        logger.warning("mpv reported a bad file for part %s", target.index)
        await self._sleeper.sleep(_LOAD_BACKOFF_SECONDS)

    async def _advance_after(self, target: Part) -> None:
        """After a natural part end, post the advance (or play the retune target).

        If ``playing`` is still the part that just ended, post a Rotate and wait
        for the single writer to apply it; the loop then plays the advanced Part.
        If ``playing`` already changed (a retune finished mid-part), the loop
        re-reads and plays the new pool's Part -- no advance. The advance gate is
        source-agnostic: ``source.advances_on_end`` is the Program mode gate for a
        generate pool and ``playing is not None`` for a replay Selection, so a
        radio auto-advances on part-end exactly as a generate pool does.
        """
        source = self._channel.source
        if source.playing == target and source.advances_on_end:
            self._channel.changed.clear()
            self._channel.post(Rotate())
            await self._channel.changed.wait()
