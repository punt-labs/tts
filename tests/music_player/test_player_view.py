"""Tests for PlayerView, asserting the Z invariants I1/I2/I3 by name.

The model lives in ``docs/vox-music-player.tex``:

* I1 -- at most one album playing.
* I2 -- now-playing present iff playing.
* I3 -- a played album is catalogued (never names an unknown album).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from punt_vox.types_programs.status import ProgramStatus
from punt_vox.types_programs.status_views import NowPlaying
from punt_vox.voxd.music_player.player_view import PlayerMode, PlayerView

if TYPE_CHECKING:
    from collections.abc import Callable

    from punt_vox.voxd.programs.catalog import Album

    type AlbumFactory = Callable[..., Album]
    type PlayingFactory = Callable[[Album, int, int], ProgramStatus]
    type RadioFactory = Callable[[int, int], ProgramStatus]


def test_i1_at_most_one_album_playing(
    album_of: AlbumFactory, playing_of: PlayingFactory
) -> None:
    """I1: a playing view names exactly one album, never a set of several."""
    album = album_of("aa11bb", name="one")
    view = PlayerView.from_status(playing_of(album, 1, 3), (album,))
    assert view.mode is PlayerMode.PLAYING
    assert view.album == album.id  # a single id, not a collection


def test_i2_playing_iff_cursor_present(
    album_of: AlbumFactory, playing_of: PlayingFactory
) -> None:
    """I2: mode is playing exactly when an album and a now-playing cursor are set."""
    album = album_of("aa11bb")
    playing = PlayerView.from_status(playing_of(album, 2, 4), (album,))
    idle = PlayerView.from_status(ProgramStatus.idle(), (album,))

    assert (playing.mode is PlayerMode.PLAYING) is (playing.now_playing is not None)
    assert playing.now_playing == NowPlaying(index=2, of=4)
    assert idle.mode is PlayerMode.IDLE
    assert idle.now_playing is None
    assert idle.album is None


def test_i2_rejects_an_inconsistent_view(album_of: AlbumFactory) -> None:
    """I2: a view whose mode, album, and cursor disagree cannot be constructed."""
    album = album_of("aa11bb")
    with pytest.raises(ValueError, match="inconsistent PlayerView"):
        PlayerView(mode=PlayerMode.PLAYING, album=None, now_playing=None)
    with pytest.raises(ValueError, match="inconsistent PlayerView"):
        PlayerView(mode=PlayerMode.IDLE, album=album.id, now_playing=NowPlaying(1, 3))


def test_i3_only_names_a_catalogued_album(
    album_of: AlbumFactory, playing_of: PlayingFactory
) -> None:
    """I3: the playing album is drawn from the catalog; an unknown source is idle."""
    album = album_of("aa11bb", name="known")
    view = PlayerView.from_status(playing_of(album, 1, 3), (album,))
    assert view.album in {a.id for a in (album,)}


def test_i3_multi_album_radio_reads_idle(
    album_of: AlbumFactory, radio_of: RadioFactory
) -> None:
    """I3: a radio whose handle names no single catalogued album reads as idle."""
    album = album_of("aa11bb")
    view = PlayerView.from_status(radio_of(1, 5), (album,))
    assert view.mode is PlayerMode.IDLE
    assert view.album is None


def test_from_status_idle_when_nothing_plays(album_of: AlbumFactory) -> None:
    album = album_of("aa11bb")
    view = PlayerView.from_status(ProgramStatus.idle(), (album,))
    assert view == PlayerView.idle()
