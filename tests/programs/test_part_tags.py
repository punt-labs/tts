"""Tests for :class:`PartTags` -- the ID3v2 frames a generated Part carries.

The authored album title reaches a real music player only if it lands in the
mp3's ID3 frames. ``write_to`` is driven against a real on-disk file and the
frames are read back with mutagen, so the ``TALB`` (album) and ``TIT2`` (title)
bytes a player groups and labels tracks by are asserted directly, not mocked.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from mutagen.id3 import ID3

from punt_vox.voxd.programs.part_tags import PartTags

if TYPE_CHECKING:
    from pathlib import Path


def _read_back(
    path: Path,
    *,
    title: str,
    album: str,
    genre: str = "synthwave",
    index: int = 1,
    total: int = 12,
) -> ID3:
    """Write a :class:`PartTags` onto ``path`` and read the ID3 frames back."""
    PartTags(title=title, album=album, genre=genre, index=index, total=total).write_to(
        path
    )
    return ID3(path)


def test_album_title_lands_in_the_talb_frame(tmp_path: Path) -> None:
    """The authored album title rides the ``TALB`` frame a player groups by."""
    frames = _read_back(tmp_path / "001.mp3", title="dawn", album="Midnight Drive")
    assert frames["TALB"].text == ["Midnight Drive"]


def test_track_title_lands_in_the_tit2_frame(tmp_path: Path) -> None:
    """The per-track variation clause rides the ``TIT2`` title frame."""
    frames = _read_back(tmp_path / "001.mp3", title="neon rain", album="Midnight Drive")
    assert frames["TIT2"].text == ["neon rain"]


def test_track_number_lands_in_the_trck_frame(tmp_path: Path) -> None:
    """The ``index``/``total`` position renders the ``TRCK`` frame (``3/12``)."""
    frames = _read_back(
        tmp_path / "003.mp3", title="drift", album="Midnight Drive", index=3
    )
    assert frames["TRCK"].text == ["3/12"]


def test_genre_lands_in_the_tcon_frame(tmp_path: Path) -> None:
    """The album style rides the ``TCON`` genre frame."""
    frames = _read_back(tmp_path / "001.mp3", title="drift", album="Midnight Drive")
    assert frames["TCON"].text == ["synthwave"]


def test_unicode_title_round_trips(tmp_path: Path) -> None:
    """A non-ASCII album title survives the UTF-8 ID3 write/read round trip."""
    frames = _read_back(
        tmp_path / "001.mp3", title="夜", album="néon 夜", genre="ambient"
    )
    assert frames["TALB"].text == ["néon 夜"]
