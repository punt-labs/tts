"""Tests for AlbumDisplay: the album's cells, its ▶ marker, and name->album resolve."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from punt_vox.voxd.music_player.album_display import AlbumDisplay
from punt_vox.voxd.music_player.player_view import PlayerView

if TYPE_CHECKING:
    from collections.abc import Callable

    from punt_vox.types_programs.status import ProgramStatus
    from punt_vox.voxd.programs.catalog import Album

    type AlbumFactory = Callable[..., Album]
    type PlayingFactory = Callable[[Album, int, int], ProgramStatus]


def test_cells_come_from_the_manifest(album_of: AlbumFactory) -> None:
    display = AlbumDisplay(album_of("aa11bb", name="Techno Mix", tracks=9))
    assert display.id == "aa11bb"
    assert display.name == "Techno Mix"
    assert display.genre == "techno"
    assert display.track_count == 9


def test_unnamed_album_name_falls_back_to_a_unique_id_string(
    album_of: AlbumFactory,
) -> None:
    # The fallback embeds the id, so an unnamed album's cell stays unique/resolvable.
    assert AlbumDisplay(album_of("aa11bb", name=None)).name == "album aa11bb"


def test_marked_name_prefixes_the_cue_only_for_the_playing_album(
    album_of: AlbumFactory, playing_of: PlayingFactory
) -> None:
    playing = album_of("aa11bb", name="Techno Mix")
    idle = album_of("cc22dd", name="Ambient Drift")
    view = PlayerView.from_status(playing_of(playing, 1, 3), (playing, idle))

    assert AlbumDisplay(playing).marked_name(view) == "▶ Techno Mix"
    assert AlbumDisplay(idle).marked_name(view) == "Ambient Drift"


def test_resolve_finds_the_album_by_its_plain_name(album_of: AlbumFactory) -> None:
    first = album_of("aa11bb", name="Techno Mix")
    second = album_of("cc22dd", name="Ambient Drift")
    assert AlbumDisplay.resolve("Ambient Drift", (first, second)) is second


def test_resolve_finds_the_album_by_its_marked_name(album_of: AlbumFactory) -> None:
    # The playing album's cell arrives with the ▶ cue; resolve handles both forms.
    album = album_of("aa11bb", name="Techno Mix")
    assert AlbumDisplay.resolve("▶ Techno Mix", (album,)) is album


def test_resolve_finds_an_unnamed_album_by_its_fallback_cell(
    album_of: AlbumFactory,
) -> None:
    album = album_of("aa11bb", name=None)
    assert AlbumDisplay.resolve("album aa11bb", (album,)) is album


def test_resolve_raises_when_no_album_matches(album_of: AlbumFactory) -> None:
    with pytest.raises(ValueError, match="names no catalogued album"):
        AlbumDisplay.resolve("Ghost Album", (album_of("aa11bb", name="Techno Mix"),))
