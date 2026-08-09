"""``SavedAlbums`` -- the saved crate as every music surface reports it.

Three verbs show the crate: ``list`` announces it, and a bare ``play`` with no
history appends it to the reject so the caller has something to pick -- on both
the ``music`` MCP tool and the ``vox music`` CLI. Each used to build its own
lines and its own wire records, which is how the CLI's ``list`` came to omit the
``format`` field the tool reported. The projection lives here once instead, so
an album cannot read one way on one surface and another way on the next.

Held beside :class:`~punt_vox.music_state_view.MusicStateView`, its counterpart
for the running Program: what is *saved* and what is *playing* are the two
things a music surface reports, and each is projected in exactly one place.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Self, final

if TYPE_CHECKING:
    from collections.abc import Iterable

    from punt_vox.types_programs.control import ProgramSummary

__all__ = ["SavedAlbums"]


@final
class SavedAlbums:
    """The albums on disk, rendered however a caller needs to see them."""

    __slots__ = ("_marker", "_summaries")
    _summaries: tuple[ProgramSummary, ...]
    _marker: str

    def __new__(cls, summaries: Iterable[ProgramSummary], marker: str = "") -> Self:
        self = super().__new__(cls)
        self._summaries = tuple(summaries)
        self._marker = marker
        return self

    @classmethod
    def marked(cls, summaries: Iterable[ProgramSummary]) -> Self:
        """Return the crate announced in the ``♪`` DJ voice the panel speaks in.

        The voice is the surface's -- the ``music`` tool marks its lines, the CLI
        prints them plainly -- but the listing it decorates is the same one.
        """
        return cls(summaries, "♪ ")

    def announced(self) -> str:
        """Return the crate as a ``list`` verb announces it."""
        if not self._summaries:
            return f"{self._marker}No saved albums."
        lines = [f"{self._marker}{len(self._summaries)} saved album(s):"]
        lines.extend(
            f"  {self._marker}{album.display_line()}" for album in self._summaries
        )
        return "\n".join(lines)

    def appended_to(self, message: str) -> str:
        """Return *message* with the crate listed beneath it, or bare if empty.

        Never marked, whatever the crate's voice: this is the body of an error
        envelope a caller must read, not a DJ announcement. An empty crate leaves
        the message untouched -- a caller told "nothing has played yet" is not
        helped by an empty list under it.
        """
        if not self._summaries:
            return message
        lines = [message, "saved albums:"]
        lines.extend(f"  {album.display_line()}" for album in self._summaries)
        return "\n".join(lines)

    def to_wire(self) -> list[dict[str, object]]:
        """Return the album records the ``programs`` field of a reply carries."""
        return [
            {
                "id": album.id,
                "style": album.style,
                "vibe": album.vibe,
                "name": album.name,
                "format": album.format,
                "ready": album.ready,
                "total": album.total,
            }
            for album in self._summaries
        ]
