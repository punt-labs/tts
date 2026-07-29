"""Tests for AlbumRow: the columns group of a name cell and a Play button."""

from __future__ import annotations

from punt_vox.voxd.music_player.album_row import AlbumRow


def test_album_row_nests_a_name_cell_beside_its_play_button() -> None:
    row = AlbumRow(album_id="aa11bb", label="▶ Techno Mix").to_dict()

    assert row["kind"] == "group"
    assert row["id"] == "music.row.aa11bb"
    assert row["layout"] == "columns"

    children = row["children"]
    assert isinstance(children, list)
    assert children[0]["kind"] == "text"
    assert children[0]["id"] == "music.name.aa11bb"
    assert children[0]["content"] == "▶ Techno Mix"
    assert children[1] == {
        "kind": "button",
        "id": "play-aa11bb",
        "label": "Play",
        "publish": {"topic": "music.play", "payload": {"album_id": "aa11bb"}},
    }
