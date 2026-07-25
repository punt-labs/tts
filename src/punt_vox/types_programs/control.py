"""The request and result value objects the surfaces exchange with the daemon.

The ProgramGateway vocabulary -- the ``StartRequest``/``SelectionRequest`` inputs,
the applied/rejected :class:`CommandOutcome`, and the :class:`ProgramSummary`
catalogue entry -- plain value objects with no I/O, shared by CLI, MCP, and fakes.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Self, final

from punt_vox.types_programs.prompts import PromptSet
from punt_vox.types_programs.vibe_label import VibeLabel

__all__ = ["CommandOutcome", "ProgramSummary", "SelectionRequest", "StartRequest"]


@final
@dataclass(frozen=True, slots=True)
class StartRequest:
    """The authoring input for turning a Program on (the ``music on`` command).

    All fields optional: ``style`` persists, ``vibe`` is the session mood tag,
    ``name`` binds a curated album, ``prompts`` carries the agent base + per-slot
    variations (``None`` => a minimal literal fallback -- absence is the contract).
    """

    style: str | None = None
    vibe: str | None = None  # session mood; None falls back to the style tag
    name: str | None = None
    prompts: PromptSet | None = None


@final
@dataclass(frozen=True, slots=True)
class SelectionRequest:
    """The replay input for playing a Selection (the ``music play`` command).

    ``id`` is the *specific-album* axis -- resolved id-or-name like the positional
    play path, distinct from the ``style``/``vibe``/``name`` tag axes and never
    folded into the tag filter. All fields optional: an all-``None`` request
    replays every album (the cross-genre radio).
    """

    style: str | None = None
    vibe: str | None = None
    name: str | None = None
    id: str | None = None  # specific album, resolved id-or-name; never a tag axis

    def resolved_style(self, catalog: Iterable[ProgramSummary]) -> str | None:
        """Return the single genre this replay selects, else ``None`` for a union.

        Names the *actual* album's style -- what is now playing -- rather than the
        tool's ``style`` arg, which is absent on an id/name/vibe replay. A match
        spanning more than one genre (or no match at all) has no single style to
        name: ``None``, so the caller clears the register instead of naming a wrong
        genre. An explicit ``style`` is returned outright only when no ``id`` is
        present; an ``id`` takes precedence, resolving the genre from the catalog.
        """
        if self.style is not None and self.id is None:
            return self.style
        return self._single_style(s for s in catalog if self._selects(s))

    def _selects(self, summary: ProgramSummary) -> bool:
        """Return whether *summary* is this replay's target: specific album, else tags.

        The ``id`` axis resolves id-or-name, as the positional play path does, so a
        name in the ``id`` slot still resolves its one album (and names its style)
        rather than matching nothing and clearing the register like a union radio.
        With no id each absent tag is a wildcard, the vibe bounded via ``VibeLabel``.
        """
        if self.id is not None:
            return self.id in (summary.id, summary.name)
        return (self.vibe is None or summary.vibe == VibeLabel(self.vibe).value) and (
            self.name is None or summary.name == self.name
        )

    @staticmethod
    def _single_style(matches: Iterable[ProgramSummary]) -> str | None:
        """Return the one distinct style among *matches*, else ``None``."""
        return next(iter(s)) if len(s := {m.style for m in matches}) == 1 else None


@final
@dataclass(frozen=True, slots=True)
class CommandOutcome:
    """Whether the daemon applied a command, and a line describing the result.

    The live protocol acks at *enqueue* with a bare reply (no ``applied`` flag),
    so a command always parses to ``applied=True`` with an empty ``message``; the
    rejected ``applied=False`` path is caught-and-logged in the daemon's async
    apply and not yet sent. The flag and forward-looking parse are kept so
    surfacing rejection later stays a daemon-side change, not a client-contract
    change.
    """

    applied: bool
    message: str

    @classmethod
    def ok(cls, message: str) -> Self:
        """Return an applied outcome carrying ``message``."""
        return cls(applied=True, message=message)

    @classmethod
    def rejected(cls, message: str) -> Self:
        """Return a rejected outcome (forward-looking; unused by the live daemon)."""
        return cls(applied=False, message=message)

    def display(self, applied_default: str) -> str:
        """Return the daemon's reason, or ``applied_default`` when it is silent."""
        return self.message or applied_default


@final
@dataclass(frozen=True, slots=True)
class ProgramSummary:
    """One catalog album as a ``list`` renders it: id, tags, and part counts."""

    id: str
    style: str
    vibe: str
    format: str  # the human surface label -- "music" for a playlist
    ready: int  # playable Parts
    total: int = 0  # all Parts incl. failed; defaults to ready when unspecified
    name: str | None = None  # the curated handle, or None for a tag-addressed album

    def display_line(self) -> str:
        """Return a human-readable one-line summary of the album."""
        parts = max(self.total, self.ready)  # total counts ready+failed, or 0 unset
        handle = self.name or f"{self.style}--{self.vibe}"
        return f"{handle} [{self.id}] — {self.ready}/{parts} part(s) [{self.format}]"
