"""Tests for AlbumListScene: the element tree it projects for a known catalog."""

from __future__ import annotations

from typing import TYPE_CHECKING

from punt_vox.voxd.music_player.player_view import PlayerView
from punt_vox.voxd.music_player.scene import AlbumListScene

if TYPE_CHECKING:
    from collections.abc import Callable

    from punt_vox.types_programs.status import ProgramStatus
    from punt_vox.voxd.programs.catalog import Album

    type AlbumFactory = Callable[..., Album]
    type PlayingFactory = Callable[[Album, int, int], ProgramStatus]


def test_scene_header_controls_and_one_row_per_album(
    album_of: AlbumFactory, playing_of: PlayingFactory
) -> None:
    first = album_of("aa11bb", name="Techno Mix")
    second = album_of("cc22dd", name="Ambient Drift")
    view = PlayerView.from_status(playing_of(first, 1, 3), (first, second))

    request = AlbumListScene((first, second), view).render_request()
    elements = request.elements

    assert request.scene_id == "vox.music"
    assert request.title == "Music"
    assert elements[0] == {
        "kind": "markdown",
        "id": "music.header",
        "content": "## Music",
    }
    assert elements[1]["kind"] == "text"
    assert "Techno Mix" in str(elements[1]["content"])
    assert "1 of 3" in str(elements[1]["content"])
    assert elements[2]["kind"] == "button"
    assert elements[2]["id"] == "music.stop"
    assert elements[3]["kind"] == "separator"

    rows = elements[4:]
    assert [row["id"] for row in rows] == ["music.row.aa11bb", "music.row.cc22dd"]


def test_playing_row_is_marked_and_carries_its_play_button(
    album_of: AlbumFactory, playing_of: PlayingFactory
) -> None:
    first = album_of("aa11bb", name="Techno Mix")
    second = album_of("cc22dd", name="Ambient Drift")
    view = PlayerView.from_status(playing_of(first, 2, 3), (first, second))

    rows = AlbumListScene((first, second), view).render_request().elements[4:]
    playing_children = list(rows[0]["children"])  # type: ignore[call-overload]
    idle_children = list(rows[1]["children"])  # type: ignore[call-overload]

    assert playing_children[0]["content"] == "▶ Techno Mix"
    assert playing_children[1] == {
        "kind": "button",
        "id": "music.play.aa11bb",
        "label": "Play",
    }
    assert idle_children[0]["content"] == "Ambient Drift"  # unmarked, not playing


def test_idle_scene_says_nothing_playing_and_disables_stop(
    album_of: AlbumFactory,
) -> None:
    album = album_of("aa11bb", name="Techno Mix")
    request = AlbumListScene((album,), PlayerView.idle()).render_request()

    assert request.elements[1]["content"] == "Nothing playing"
    assert request.elements[2] == {
        "kind": "button",
        "id": "music.stop",
        "label": "Stop",
        "disabled": True,
    }


def test_unnamed_album_falls_back_to_its_id(album_of: AlbumFactory) -> None:
    album = album_of("aa11bb", name=None)
    rows = AlbumListScene((album,), PlayerView.idle()).render_request().elements[4:]
    children = list(rows[0]["children"])  # type: ignore[call-overload]
    assert children[0]["content"] == "album aa11bb"
