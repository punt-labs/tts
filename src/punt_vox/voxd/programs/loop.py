"""The playback loop -- play the current Part, auto-advance, race controls.

``ProgramLoop`` owns the player and nothing else: it plays ``program.playing``,
and when the track ends it *posts a Rotate message* (never mutating the Program
directly) so the single :class:`ControlChannel` writer advances the cursor, then
plays the new ``program.playing``. It never generates. A skip / play-a-part /
off interrupts the current track at once (the channel's ``interrupt`` event); a
retune does not -- the current track finishes first, then the loop plays the new
pool's Part (finish-current-then-switch). The auto-advance is a real,
listened-to transition, proven by a test that asserts the loop spawned a
*different* file on track-end.

The interrupt-vs-natural-end race lives in :class:`InterruptRace`; a player
*spawn* failure (a missing ``afplay``/``ffplay`` binary, or an OS resource limit)
is handled here: it is not a Program transition -- the Part stays ready and the
cursor unmoved -- so it is recorded on the shared :class:`PlaybackHealth` (which
status surfaces, never only a log) and followed by a bounded backoff so a
persistent failure cannot spin the loop hot.
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
    from punt_vox.voxd.programs.player import Player, PlayerProcess
    from punt_vox.voxd.programs.sleeper import Sleeper
    from punt_vox.voxd.programs.suspension import PlaybackSuspension
    from punt_vox.voxd.programs.track_end import TrackEnd

__all__ = ["ProgramLoop"]

logger = logging.getLogger(__name__)

_SPAWN_BACKOFF_SECONDS = 2.0
"""Bounded pause before retrying a player that could not spawn (no CPU spin)."""


@final
class ProgramLoop:
    """Play ``program.playing`` and advance when the track ends."""

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
        """Park while paused, then play the current Part or wait for a playable one.

        The suspension gate is consulted *first*, before the cursor is read: while
        paused there is no running player, so the loop parks here and spawns
        nothing (Z ``T3`` -- a paused album does not auto-advance). On resume the
        cursor is read fresh, so a ``prev``/``next`` that moved it while paused
        plays the newly-cursored Part (Z Fork~B).
        """
        await self._suspension.wait_resumed()
        target = self._channel.source.playing
        if target is not None:
            await self._play(target)
            return
        await self._wait_for_playable()

    async def _wait_for_playable(self) -> None:
        """Block until a Part becomes playable (first track, or a retune)."""
        self._channel.changed.clear()
        if self._channel.source.playing is not None:
            return  # became available between the read and the clear
        await self._channel.changed.wait()

    async def _play(self, target: Part) -> None:
        """Spawn ``target`` at its resume offset, settle its end, then act (F3).

        The spawn seeks to the suspension's resume offset -- the frozen offset when
        this is the paused part being resumed, 0 for a fresh part -- and attaches
        the handle with that offset so the position accounting stays correct across
        pause/resume cycles. A spawn failure becomes an observable fault plus a
        bounded backoff rather than a raise into ``run``'s guard, which would spin
        on the same unplayable target. The post-settle decision is ``_finish``.
        """
        self._channel.interrupt.clear()
        offset = self._suspension.seek_for(target)
        try:
            proc = await self._player.play(target, offset)
        except OSError as exc:  # FileNotFoundError (no player) + EMFILE/ENOMEM
            await self._back_off_spawn(target, exc)
            return
        self._suspension.attach(proc, target, offset)
        self._health.clear()
        try:
            end = await self._race.settle(proc)
        finally:
            self._suspension.detach()
        await self._finish(target, proc, end)

    async def _finish(self, target: Part, proc: PlayerProcess, end: TrackEnd) -> None:
        """Act on how the track stopped: park, kill, or advance the cursor.

        The paused check fires first: a ``pause`` gracefully stopped the player, so
        the cursor is held and the loop returns without advancing (Z ``T3``); the
        next ``_step`` parks on the gate until resume. A user interrupt kills and
        does not advance. A clean or faulted end advances -- a fault is recorded
        observably first (F3), never a WARNING-only log that skips silently.
        """
        if self._suspension.is_paused:
            await proc.kill()  # reap the gracefully-stopped player; do NOT advance
            return
        if end.interrupted:
            await proc.kill()
            return
        if end.faulted:
            await self._note_exit_fault(target, end.exit_code)
        await self._advance_after(target)

    async def _back_off_spawn(self, target: Part, exc: OSError) -> None:
        """Record the spawn failure observably, then pause so it cannot spin.

        The status-surfaced reason is :class:`SafeText`-sanitized so the missing
        binary's absolute path never crosses the wire; the raw exception rides
        ``exc_info`` to the host-local log.
        """
        reason = SafeText.of(str(exc)).text
        self._health.record(target, reason, PlaybackFaultKind.SPAWN)
        logger.error(
            "player spawn failed for part %s; backing off %ss",
            target.index,
            _SPAWN_BACKOFF_SECONDS,
            exc_info=exc,
        )
        await self._sleeper.sleep(_SPAWN_BACKOFF_SECONDS)

    async def _note_exit_fault(self, target: Part, exit_code: int | None) -> None:
        """Record a non-zero player exit observably, then pause so it cannot spin.

        The track spawned but exited non-zero, so the loop advances past it; the
        backoff bounds the rate at which a wholly-corrupt pool would rotate.
        """
        reason = f"player exited with code {exit_code}"
        self._health.record(target, reason, PlaybackFaultKind.TRACK_EXIT)
        logger.warning("player exited non-zero for part %s: %s", target.index, reason)
        await self._sleeper.sleep(_SPAWN_BACKOFF_SECONDS)

    async def _advance_after(self, target: Part) -> None:
        """After a natural track end, post the advance (or play the retune target).

        If ``playing`` is still the track that just ended, post a Rotate and
        wait for the single writer to apply it; the loop then plays the advanced
        Part. If ``playing`` already changed (a retune finished mid-track), the
        loop simply re-reads and plays the new pool's Part -- no advance. The
        advance gate is source-agnostic: ``source.advances_on_end`` is the
        Program mode gate for a generate pool and ``playing is not None`` for a
        replay Selection, so a radio auto-advances on track-end exactly as a
        generate pool does.
        """
        source = self._channel.source
        if source.playing == target and source.advances_on_end:
            self._channel.changed.clear()
            self._channel.post(Rotate())
            await self._channel.changed.wait()
