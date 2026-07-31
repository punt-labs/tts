"""The ``ProgramService`` composition root -- the daemon's one playback seam.

``ProgramService`` owns the whole live orchestration: the single-writer
:class:`ControlChannel` over the active :class:`PlaybackSource`, the background
:class:`Filler`, the :class:`ProgramLoop` and its player, the :class:`Catalog`
built once from ``store.scan()``, and the :class:`ActiveContext` that names which
source is animated. It is an *orchestrator, not an algorithm*: the
catalog owns query resolution (``by_name``/``resume``/``select``/``by_id``); the
service owns only the mint side-effect (a domain object must not call
``store.create``), seeds a source, and posts one serialized signal.
"""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING, Final, Self, final

from punt_vox.types_programs.prompts import PromptSet
from punt_vox.types_programs.status import ProgramStatus
from punt_vox.types_programs.status_views import NowPlaying
from punt_vox.voxd.programs.active_context import (
    ActiveContext,
    ActiveProgram,
    ActiveSelection,
)
from punt_vox.voxd.programs.album_binder import AlbumBinder
from punt_vox.voxd.programs.album_tags import AlbumTags, PromptFingerprint, TagQuery
from punt_vox.voxd.programs.catalog import Album, Catalog
from punt_vox.voxd.programs.change_signal import ChangeSignal
from punt_vox.voxd.programs.control_channel import ControlChannel
from punt_vox.voxd.programs.fill_reconciler import FillReconciler
from punt_vox.voxd.programs.filler import Filler
from punt_vox.voxd.programs.lifecycle_signal import TurnOff
from punt_vox.voxd.programs.loop import ProgramLoop
from punt_vox.voxd.programs.playback_health import PlaybackHealth
from punt_vox.voxd.programs.playback_signal import StepBack, StepForward
from punt_vox.voxd.programs.program import Program
from punt_vox.voxd.programs.rotate_policy import RotatePolicy
from punt_vox.voxd.programs.select_signal import SwitchSelection
from punt_vox.voxd.programs.selection import Selection
from punt_vox.voxd.programs.selection_playback import SelectionPlayback
from punt_vox.voxd.programs.state import ProgramState
from punt_vox.voxd.programs.subprocess_player import SubprocessPlayer
from punt_vox.voxd.programs.suspension import PlaybackSuspension
from punt_vox.voxd.programs.switch_signal import SwitchProgram

if TYPE_CHECKING:
    from pathlib import Path

    from punt_vox.voxd.programs.active_context import ActiveSource
    from punt_vox.voxd.programs.album_id import AlbumId
    from punt_vox.voxd.programs.filler import FillPlan
    from punt_vox.voxd.programs.part import Part
    from punt_vox.voxd.programs.playback_source import PlaybackSource
    from punt_vox.voxd.programs.producer import Producer
    from punt_vox.voxd.programs.sleeper import Sleeper
    from punt_vox.voxd.programs.store import ProgramStore

__all__ = ["ProgramService"]

_DEFAULT_STYLE: Final = "ambient"
_RADIO_LABEL: Final = "radio"


@final
class ProgramService:
    """Own and drive the one active source; the handler-facing daemon seam."""

    __slots__ = (
        "_binder",
        "_catalog",
        "_changes",
        "_channel",
        "_context",
        "_filler",
        "_health",
        "_loop",
        "_root",
        "_store",
        "_suspension",
    )
    _store: ProgramStore
    _root: Path
    _catalog: Catalog
    _binder: AlbumBinder
    _context: ActiveContext
    _channel: ControlChannel
    _filler: Filler
    _health: PlaybackHealth
    _loop: ProgramLoop
    _changes: ChangeSignal
    _suspension: PlaybackSuspension

    def __new__(
        cls, producer: Producer, store: ProgramStore, root: Path, sleeper: Sleeper
    ) -> Self:
        self = super().__new__(cls)
        self._store = store
        self._root = root
        self._catalog = Catalog(store.scan())
        self._binder = AlbumBinder(self._catalog, store)
        self._context = ActiveContext()
        self._channel = ControlChannel(cls._idle_program())
        self._filler = Filler(producer, self._channel, sleeper)
        self._channel.attach_reconciler(FillReconciler(self._filler, self))
        self._health = PlaybackHealth()
        # The suspension is shared by the loop (which suspends the live player on
        # pause) and this service (which drives pause/resume and reports paused).
        self._suspension = PlaybackSuspension()
        self._loop = ProgramLoop(
            self._channel,
            SubprocessPlayer(self),
            sleeper,
            self._health,
            self._suspension,
        )
        # One change signal drives the scene re-push: the single-writer fires it
        # after each applied command, the catalog after each new/remove.
        self._changes = ChangeSignal()
        self._channel.attach_change_signal(self._changes)
        self._catalog.attach_change_signal(self._changes)
        return self

    @property
    def changes(self) -> ChangeSignal:
        """Return the change signal the music player subscribes to (PY-DP-8)."""
        return self._changes

    # -- injected seams (FillPlanSource + PlayerDirectory) ------------------

    def current_plan(self) -> FillPlan:
        """Return the active fill plan -- the fill reconciliation's source."""
        return self._context.plan()

    def locate(self, part: Part) -> Path:
        """Return the on-disk path of ``part`` for the active source (the player)."""
        return self._context.locate(part)

    # -- daemon lifecycle --------------------------------------------------

    async def serve_control(self) -> None:
        """Run the sole control-channel writer for the daemon's lifetime."""
        await self._channel.serve()

    async def run_playback(self) -> None:
        """Run the playback loop for the daemon's lifetime."""
        await self._loop.run()

    async def run_once(self) -> None:
        """Apply exactly one queued command -- the test seam for the writer."""
        await self._channel.apply_next()

    def shutdown(self) -> None:
        """Cancel any in-flight fill on daemon stop (no orphaned generation)."""
        self._filler.cancel()

    # -- observation (authoritative, read per call, never cached) ----------

    def status(self) -> ProgramStatus:
        """Return the daemon's authoritative status, read fresh per call.

        A generate Program reports the full Program status; a replay Selection
        reports the consume-only radio status; an idle daemon reports idle. The
        transport ``paused`` flag rides every active status (never idle), read
        authoritatively from the one suspension, so a client's PlayerView can tell
        a held source from a progressing one.
        """
        active = self._context.current
        if active is None:
            return ProgramStatus.idle()
        return replace(self._active_status(active), paused=self._suspension.is_paused)

    def _active_status(self, active: ActiveSource) -> ProgramStatus:
        """Return the active source's base status (before the paused overlay)."""
        source = self._channel.source
        if isinstance(source, Program):
            return source.to_status(active.name, self._health.fault)
        if isinstance(source, SelectionPlayback):
            return ProgramStatus.radio(
                active.name, self._radio_now_playing(source), self._health.fault
            )
        return ProgramStatus.idle()

    def catalog_albums(self) -> tuple[Album, ...]:
        """Return every catalog album, newest first (the ``list`` view)."""
        return self._catalog.by_tags(TagQuery())

    @property
    def catalog(self) -> Catalog:
        """Return the one catalog the play machine and the library both mutate."""
        return self._catalog

    def active_backing_locators(self) -> frozenset[str]:
        """Return the album locators whose Parts back the active source (D-2).

        ``MusicRemove`` refuses an album exactly when its locator is here: a
        generate Program contributes its own album while it is *live* -- playing
        a ready Part (``advances_on_end``) or generating into its directory
        (``wants_generation``) -- so the first track being written in
        ``generating_first`` (an empty pool) is protected too, and removing it
        cannot corrupt the in-flight generation; a Radio contributes every album
        its Selection spans; an idle daemon none. A Program stopped by ``off``
        is neither playing nor generating, so its retained pool -- kept only for
        a later re-``on`` -- backs nothing and must not block removal, mirroring
        how Radio ``off`` clears its selection.
        """
        active = self._context.current
        if active is None:
            return frozenset()
        return self._backing_locators(self._channel.source, active)

    def _backing_locators(
        self, source: PlaybackSource, active: ActiveSource
    ) -> frozenset[str]:
        """Dispatch D-2 backing by source kind: a generate Program vs a replay Radio.

        A generate Program backs its album while it is live -- playing a ready
        Part or generating one. A stopped Program (``off``) is neither, so its
        retained pool contributes nothing and the album becomes removable.
        """
        if isinstance(source, Program):
            backs = source.advances_on_end or source.wants_generation
            return frozenset({active.name.value}) if backs else frozenset()
        if isinstance(source, SelectionPlayback):
            return frozenset(selected.locator for selected in source.selection)
        return frozenset()

    # -- handler-facing mutators (each POSTs one serialized command) --------

    def turn_on(
        self,
        *,
        style: str | None,
        vibe: str | None,
        name: str | None,
        prompts: PromptSet | None,
    ) -> None:
        """Bind an album by tags/name (+ fingerprint) or mint one, then generate.

        Resolution is the catalog's: ``--name`` resumes the named album (or mints
        an auto-suffixed one on a fresh name); otherwise the newest album matching
        the ``(style, vibe)`` tags *and* the incoming prompt fingerprint resumes,
        and a fingerprint mismatch mints a fresh album rather than growing a
        foreign pool. The recorded vibe tag is the session vibe, not the style.
        """
        clean_style = AlbumTags.canonical(style or "") or _DEFAULT_STYLE
        clean_vibe = AlbumTags.canonical(vibe or "") or clean_style
        prompt_set = (
            prompts if prompts is not None else PromptSet.fallback(clean_style, "")
        )
        fingerprint = PromptFingerprint.from_prompts(
            prompt_set.base, prompt_set.variations
        )
        album = self._binder.bind(clean_style, clean_vibe, name, fingerprint)
        active = ActiveProgram(
            album_id=album.id,
            store=self._store.open(album.locator),
            tags=album.manifest.tags,
            directory=self._root / album.locator,
            prompts=prompt_set,
        )
        # Seed the pool from the freshly-opened store, never a catalog snapshot:
        # a re-``on`` of a filled album must restore its live parts, or the fill
        # would see disk already full, start nothing, and the loop would hang.
        program = Program(
            ProgramState.restored(
                album.manifest.format, frozenset(active.store.ready_parts())
            ),
            RotatePolicy(),
        )
        # A turn-on displaces whatever was active, so any held pause is dropped.
        self._suspension.reset()
        self._channel.post(
            SwitchProgram(self._channel, self._context, program, active, target=None)
        )

    def replay(self, query: TagQuery) -> None:
        """Replay the union Selection of every album matching ``query`` (no fill)."""
        self._start_replay(self._catalog.select(query), _RADIO_LABEL)

    def replay_album(self, album_id: AlbumId) -> None:
        """Replay a single album resolved by its id -- a direct lookup.

        Distinguishes an unknown id from a known-but-empty album: a resolved
        album with zero ready tracks reports "no playable tracks yet" rather than
        the generic tag-miss message, which would misread as an unknown album.
        """
        album = self._catalog.by_id(album_id)
        if album is None:
            msg = f"no album with id {album_id.value!r}"
            raise ValueError(msg)
        selection = Selection.from_albums([(album.locator, album.ready_parts())])
        if not selection:
            msg = f"album {album_id.value!r} has no playable tracks yet"
            raise ValueError(msg)
        self._start_replay(selection, album.locator)

    def advance(self) -> None:
        """User transport next: step a replay forward, or skip a generate Program.

        The user's next (Z ``Next``) is distinct from the loop's end-of-part
        auto-advance (Z ``AutoAdvance``, which the loop posts as its own ``Rotate``):
        on a single-album replay it walks the ordered pool by one and stalls at the
        last part, rather than wrapping.
        """
        self._channel.post(StepForward())

    def prev(self) -> None:
        """User transport prev: step a replay cursor back, floored at the first part.

        Only a replay Selection has an ordered position; a generate Program has no
        previous, so the signal is rejected as a lost race there (Z ``Prev``).
        """
        self._channel.post(StepBack())

    def pause(self) -> None:
        """Suspend the active source in place (Z ``Pause``): only while active.

        Pause holds the player where it is -- the cursor stays put and never
        auto-advances -- so it is meaningful only when a source is active; against
        an idle daemon it is a no-op. Idempotent while already paused. The scene
        re-push rides the change signal directly, since pause mutates no source
        cursor and so posts no control command.
        """
        if self._context.current is None:
            return
        self._suspension.pause()
        self._changes.emit()

    def resume(self) -> None:
        """Continue a suspended source (Z ``Resume``): only from paused.

        Idempotent while already playing; the change signal re-pushes the scene.
        """
        self._suspension.resume()
        self._changes.emit()

    def off(self) -> None:
        """Stop the active source (a Program keeps its pool; a replay goes idle).

        Resetting the suspension first returns the player to the not-paused, unheld
        state, so ``off`` from a paused source lands idle exactly as from a playing
        one (Z ``Stop`` from either active mode), leaving no paused residue.
        """
        self._suspension.reset()
        self._channel.post(TurnOff(self._channel, self._context, self._idle_program()))

    # -- internals ---------------------------------------------------------

    def _start_replay(self, selection: Selection, label: str) -> None:
        """Seed a replay over ``selection`` and post the switch, rejecting empty.

        A switch is start-or-switch (Z Fork A, SWITCH): from idle it starts, and
        from a playing *or* paused source it displaces the active album and begins
        the new one at part 1, mode playing. Resetting the suspension first drops
        any held/paused state so the newly chosen album plays rather than inheriting
        the displaced one's pause.
        """
        if not selection:
            msg = "no albums match the selection"
            raise ValueError(msg)
        self._suspension.reset()
        playback = SelectionPlayback(selection, RotatePolicy())
        active = ActiveSelection(self._root, selection, label)
        self._channel.post(
            SwitchSelection(self._channel, self._context, playback, active)
        )

    @staticmethod
    def _radio_now_playing(source: SelectionPlayback) -> NowPlaying | None:
        """Return the replay cursor's "Part N of M" view, or ``None`` when idle.

        ``N`` is the playing track's 1-based *position* in the selection and ``M``
        is the selection's size, so ``N <= M`` always holds -- the same
        position-of-count contract the generate-Program status uses. The cursor is
        read O(1) from the source, never rescanned over an uncapped selection.
        """
        position = source.position
        if position is None:
            return None
        return NowPlaying(index=position, of=len(source.selection))

    @staticmethod
    def _idle_program() -> Program:
        """Return a fresh idle Program -- the off/initial source (mode off)."""
        return Program(ProgramState.initial(), RotatePolicy())
