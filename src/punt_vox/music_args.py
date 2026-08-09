"""The bundled ``music`` tool arguments -- one frozen value object per call.

:class:`MusicArgs` is the input value object the single ``music`` MCP tool builds
from a call and hands to the matching subcommand handler. It is kept apart from
:class:`~punt_vox.server_music_tool.MusicTool` (the dispatcher) so each concern --
the arguments and their canonicalisation, versus the routing -- owns its own
module and neither grows the other past the size threshold.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import final

from punt_vox.types_programs.control import StartRequest

__all__ = ["MusicArgs"]


@final
@dataclass(frozen=True, slots=True)
class MusicArgs:
    """The raw ``music`` tool arguments bundled for a subcommand handler.

    One frozen value object per call (PY-OO-3) instead of a fan of loose
    parameters threaded through eight handlers; each handler reads only the
    fields it needs. The tag fields carry their own canonicalisation so a
    blank/whitespace tag is absent (``None``), never an explicit ``""`` the
    daemon would store while the panel reads it as no tag.
    """

    subcommand: str
    style: str | None = None
    vibe: str | None = None
    name: str | None = None
    album_id: str | None = None
    base_prompt: str | None = None
    # Wire-shaped optional list: the agent's 12-entry pool for ``on``, or absent
    # (PY-TS-14 -- the tool schema needs the list shape FastMCP builds).
    variations: list[str] | None = None
    dest: str | None = None
    # The human album title the authoring verbs (``on``/``new``) give the album
    # they create; it becomes the album's unique ``name`` and rides the ID3.
    title: str | None = None

    @property
    def canonical_style(self) -> str | None:
        """Return the style tag trimmed, or None when blank/absent."""
        return StartRequest.canonical_tag(self.style)

    @property
    def canonical_title(self) -> str | None:
        """Return the authored album title trimmed, or None when blank/absent.

        The authoring verbs (``on``/``new``) title the album they create; the
        title becomes the album's unique ``name`` and rides the ID3 ``TALB``
        frame. ``play`` addresses an *existing* album by ``name`` instead -- a
        different act, so the two words do not share one field.
        """
        return StartRequest.canonical_tag(self.title)

    @property
    def authored(self) -> bool:
        """Return whether the agent supplied an authored variation pool."""
        return bool(self.variations)
