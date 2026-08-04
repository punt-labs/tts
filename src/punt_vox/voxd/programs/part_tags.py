"""The ID3 tags written onto a generated Part's mp3 -- its player-facing metadata.

A music player reads these ID3v2 frames to group and label tracks; without them
every Part imports as "Unknown Artist / Unknown Album" with its bare ``NNN``
filename as the title. :class:`PartTags` is the value object one Part carries
from the authoring seam -- where the variation, album name, and style are known --
to the write that lands right after the audio bytes.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, final

from mutagen.id3 import ID3, TALB, TCON, TIT2, TPE1, TPE2, TRCK

if TYPE_CHECKING:
    from pathlib import Path

__all__ = ["PartTags"]

_ARTIST = "vox"
_UTF8 = 3  # ID3 text-encoding byte for UTF-8 (mutagen Encoding.UTF8).


@final
@dataclass(frozen=True, slots=True)
class PartTags:
    """The ID3v2 frames one generated Part carries to its mp3 on disk.

    ``title`` is the variation clause that generated the Part (the base prompt
    for a fallback pool); ``album`` is the Program name; ``genre`` is its style;
    ``index``/``total`` render the ``TRCK`` position (``1/12``). Artist and album
    artist are always ``vox`` -- the fixed identity of every generated pool.
    """

    title: str
    album: str
    genre: str
    index: int
    total: int

    def write_to(self, path: Path) -> None:
        """Write these ID3v2 frames onto the mp3 at ``path`` (UTF-8 text)."""
        tags = ID3()
        tags.add(TPE1(encoding=_UTF8, text=[_ARTIST]))
        tags.add(TPE2(encoding=_UTF8, text=[_ARTIST]))
        tags.add(TALB(encoding=_UTF8, text=[self.album]))
        tags.add(TIT2(encoding=_UTF8, text=[self.title]))
        tags.add(TCON(encoding=_UTF8, text=[self.genre]))
        tags.add(TRCK(encoding=_UTF8, text=[f"{self.index}/{self.total}"]))
        tags.save(path)
