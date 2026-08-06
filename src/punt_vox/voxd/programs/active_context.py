"""The backing context of the one active playback source -- what fills and plays it.

The daemon animates a single source at a time -- a generate :class:`ActiveProgram`
or a consume-only :class:`ActiveSelection`. Both resolve a Part to its on-disk
``Path`` through :meth:`locate`, so the player asks the context, not
the source, where a track lives. Only a program has a fill plan and a store to
grow; a selection resolves each part's opaque locator to a directory under root.
The mutable :class:`ActiveContext` is the one slot the single control-channel
writer swaps, so the player and the fill never see a half-swapped context.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Final, Self, final

from punt_vox.types_programs.identifiers import ProgramName
from punt_vox.voxd.containment import ContainmentRoot
from punt_vox.voxd.programs.filler import FillPlan

if TYPE_CHECKING:
    from pathlib import Path

    from punt_vox.types_programs.prompts import PromptSet
    from punt_vox.voxd.programs.album_id import AlbumId
    from punt_vox.voxd.programs.album_tags import AlbumTags
    from punt_vox.voxd.programs.part import Part
    from punt_vox.voxd.programs.selection import Selection
    from punt_vox.voxd.programs.store import PartStore

__all__ = ["ActiveContext", "ActiveProgram", "ActiveSelection"]

_PART_LABEL: Final = "part name"


@final
@dataclass(frozen=True, slots=True)
class ActiveProgram:
    """The immutable context backing an active generate Program (its fill + player).

    ``prompts`` is the pool's :class:`PromptSet`; the fill composes each Part's
    generation prompt and title from it. ``directory`` is where the player finds
    the Part audio. A program's :meth:`locate` returns its single directory joined
    with the Part's file identity.
    """

    album_id: AlbumId
    store: PartStore
    tags: AlbumTags
    directory: Path
    prompts: PromptSet

    def to_plan(self) -> FillPlan:
        """Return the fill plan the background fill grows this album from."""
        return FillPlan(store=self.store, tags=self.tags, prompts=self.prompts)

    def locate(self, part: Part) -> Path:
        """Return ``part``'s contained regular file in this album's directory (F2).

        The Part identity is untrusted -- it is the manifest ``file`` field. It is
        resolved through the shared :class:`ContainmentRoot` gate: a name that is a
        symlink, a directory, absent, or escapes the album directory raises
        ``ValueError`` before the path can reach ``loadfile``, mirroring the
        ``music get`` fetch path so no crafted manifest opens a file outside the
        album.
        """
        return ContainmentRoot(self.directory, _PART_LABEL).contained_regular_file(
            part.identity
        )

    @property
    def name(self) -> ProgramName:
        """Return the album's directory-name handle (the status surface)."""
        return ProgramName(self.directory.name)


@final
class ActiveSelection:
    """The immutable context backing a consume-only replay over a Selection.

    A selection has no fill and no store to grow: it only resolves each
    :class:`SelectedPart`'s opaque locator to a directory under ``root``. The
    per-part path map is precomputed once, keyed by each part's selection-unique
    ``playable`` identity, so :meth:`locate` never parses a string.
    """

    __slots__ = ("_label", "_parts")
    # Each playable Part -> (its album directory, its untrusted file identity).
    # The album directory (from the catalog locator) is trusted; the identity is
    # the manifest ``file`` field and is gated at :meth:`locate`, not precomputed
    # into a raw path, so a crafted manifest is caught at play time.
    _parts: dict[Part, tuple[Path, str]]
    _label: str

    def __new__(cls, root: Path, selection: Selection, label: str) -> Self:
        self = super().__new__(cls)
        self._parts = {
            selected.playable: (root / selected.locator, selected.part.identity)
            for selected in selection
        }
        self._label = label
        return self

    def locate(self, part: Part) -> Path:
        """Return ``part``'s contained regular file in its album directory (F2).

        The file identity is untrusted (the manifest ``file`` field); it is
        resolved through the shared :class:`ContainmentRoot` gate, so a symlink,
        a directory, an absent name, or one escaping the album directory raises
        ``ValueError`` before the path can reach ``loadfile`` -- the same refusal
        the ``music get`` fetch path applies.
        """
        album_dir, identity = self._parts[part]
        return ContainmentRoot(album_dir, _PART_LABEL).contained_regular_file(identity)

    @property
    def name(self) -> ProgramName:
        """Return the replay's display handle (the status surface)."""
        return ProgramName(self._label)


type ActiveSource = ActiveProgram | ActiveSelection


@final
class ActiveContext:
    """The single mutable slot holding the active source's backing context.

    Written only by the control-channel's sole consumer (via a switch signal),
    so the swap is atomic against every read -- the fill reconciliation asks for
    :meth:`plan` and the player for :meth:`locate` between the writer's serialised
    commands, never mid-swap.
    """

    __slots__ = ("_current",)
    _current: ActiveSource | None

    def __new__(cls) -> Self:
        self = super().__new__(cls)
        self._current = None
        return self

    @property
    def current(self) -> ActiveSource | None:
        """Return the active context, or ``None`` when the daemon is idle."""
        return self._current

    def switch(self, active: ActiveSource) -> None:
        """Make ``active`` the backing context of the one animated source."""
        self._current = active

    def clear(self) -> None:
        """Drop the active context back to idle (a replay Selection's ``off``)."""
        self._current = None

    def plan(self) -> FillPlan:
        """Return the active fill plan, raising unless a generate program is active.

        A selection never fills, so ``wants_generation`` is false and the
        reconciler never asks for a plan while a selection is active; this raise
        is an unreachable guard, not a control path.
        """
        active = self._require()
        if not isinstance(active, ActiveProgram):
            msg = "no fill plan: the active source is a consume-only selection"
            raise RuntimeError(msg)
        return active.to_plan()

    def locate(self, part: Part) -> Path:
        """Return the active source's on-disk path for ``part`` (the player's seam)."""
        return self._require().locate(part)

    def name(self) -> ProgramName | None:
        """Return the active source's display handle, or ``None`` when idle."""
        return None if self._current is None else self._current.name

    def _require(self) -> ActiveSource:
        """Return the active context or raise -- callers run only while one is live."""
        if self._current is None:
            msg = "no active source: the daemon holds nothing to fill or play"
            raise RuntimeError(msg)
        return self._current
