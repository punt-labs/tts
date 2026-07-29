"""Tests for AlbumListScene: the element tree it projects for a known catalog.

The Play/Stop button assertions are the offline substitute for the (PR-3-gated)
live click: they pin the exact ``publish`` attribute each button carries, which is
the wire contract :class:`LuxSubscription` decodes on the other leg.
"""

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
    assert elements[2] == {
        "kind": "button",
        "id": "stop",
        "label": "Stop",
        "publish": {"topic": "music.stop"},
    }
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
    playing_children = rows[0]["children"]
    idle_children = rows[1]["children"]
    assert isinstance(playing_children, list)  # each row nests its cells in a list
    assert isinstance(idle_children, list)

    assert playing_children[0]["content"] == "▶ Techno Mix"
    assert playing_children[1] == {
        "kind": "button",
        "id": "play-aa11bb",
        "label": "Play",
        "publish": {"topic": "music.play", "payload": {"album_id": "aa11bb"}},
    }
    assert idle_children[0]["content"] == "Ambient Drift"  # unmarked, not playing


def test_stop_button_always_publishes_even_when_idle(album_of: AlbumFactory) -> None:
    album = album_of("aa11bb", name="Techno Mix")
    request = AlbumListScene((album,), PlayerView.idle()).render_request()

    assert request.elements[1]["content"] == "Nothing playing"
    # Stop always carries its publish and is never disabled: a stop-while-idle is a
    # harmless no-op (Z model PlayerStop), so no mode-dependent button state is needed.
    assert request.elements[2] == {
        "kind": "button",
        "id": "stop",
        "label": "Stop",
        "publish": {"topic": "music.stop"},
    }


def test_unnamed_album_falls_back_to_its_id(album_of: AlbumFactory) -> None:
    album = album_of("aa11bb", name=None)
    rows = AlbumListScene((album,), PlayerView.idle()).render_request().elements[4:]
    children = rows[0]["children"]
    assert isinstance(children, list)  # each row nests its cells in a list
    assert children[0]["content"] == "album aa11bb"
