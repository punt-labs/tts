"""Tests for AlbumListScene: the three-region element tree it projects.

The scene is a flat, id-keyed list of wire elements in three regions -- now-playing,
transport, and the album table under its count label. These tests pin the shape by
element id (not list position, which shifts between the idle one-line now-playing and
the active two-element one): the now-playing block for a playing and a paused source,
the table columns, its sortable flag, and its ``music.play`` row-selection publish,
and the idle greying of the transport. The publish assertion is the offline substitute
for a live click -- it pins the exact wire contract :class:`LuxSubscription` decodes on
the other leg.
"""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING

from punt_vox.types_programs.status import ProgramStatus
from punt_vox.voxd.music_player.album_display import AlbumDisplay
from punt_vox.voxd.music_player.album_roster import AlbumRoster
from punt_vox.voxd.music_player.now_playing_block import MarkdownText
from punt_vox.voxd.music_player.playback_notice import PlaybackNotice
from punt_vox.voxd.music_player.player_view import PlayerView
from punt_vox.voxd.music_player.scene import AlbumListScene

if TYPE_CHECKING:
    from collections.abc import Callable

    from punt_vox.voxd.programs.catalog import Album

    type AlbumFactory = Callable[..., Album]
    type PlayingFactory = Callable[[Album, int, int], ProgramStatus]


def _scene(
    albums: tuple[Album, ...],
    view: PlayerView,
    notice: PlaybackNotice | None = None,
) -> AlbumListScene:
    """Build the scene the way the player does: from a roster read at its seam."""
    roster = AlbumRoster.read(albums)
    if notice is None:
        return AlbumListScene(roster, view)
    return AlbumListScene(roster, view, notice)


def _by_id(elements: list[dict[str, object]], elem_id: str) -> dict[str, object]:
    """Return the scene element with ``elem_id`` (the scene is a flat id-keyed list)."""
    return next(element for element in elements if element["id"] == elem_id)


def _children(element: dict[str, object]) -> list[dict[str, object]]:
    """Return a container element's children, asserting the wire shape is a list."""
    children = element["children"]
    assert isinstance(children, list)
    return children


def _table(elements: list[dict[str, object]]) -> dict[str, object]:
    """Return the album table -- a top-level element now, not nested under a header."""
    return _by_id(elements, "music.albums")


def _paused(status: ProgramStatus) -> ProgramStatus:
    """Return a copy of a playing status suspended in place (transport pause)."""
    return replace(status, paused=True)


def test_scene_region_order_when_idle(album_of: AlbumFactory) -> None:
    # The idle roster is the active roster: same ids, same order, same count. That
    # is what lets pressing play refresh the widget instead of re-installing it.
    request = _scene((album_of("aa11bb"),), PlayerView.idle()).render_request()

    assert request.scene_id == "vox.music"
    assert request.title == "Music"
    # No "music.header": the lux frame is already titled "Music".
    assert [element["id"] for element in request.elements] == [
        "music.now.album",
        "music.now.position",
        "music.status",
        "music.transport",
        "music.sep",
        "music.albums.label",
        "music.albums",
    ]


def test_now_playing_block_shows_album_and_track_position(
    album_of: AlbumFactory, playing_of: PlayingFactory
) -> None:
    album = album_of("aa11bb", name="Techno Mix")
    view = PlayerView.from_status(playing_of(album, 1, 3), (album,))

    elements = _scene((album,), view).render_request().elements

    assert _by_id(elements, "music.now.album") == {
        "kind": "markdown",
        "id": "music.now.album",
        "content": "### Techno Mix",
    }
    # The block carries only album + position: no song-title line (it held the
    # generation prompt, not a song name) and no progress bar.
    assert _by_id(elements, "music.now.position") == {
        "kind": "text",
        "id": "music.now.position",
        "content": "1 of 3",
    }
    assert not any(e["id"] == "music.now.line" for e in elements)
    assert not any(e["id"] == "music.now.track" for e in elements)
    assert not any(e["id"] == "music.now.progress" for e in elements)


def test_now_playing_album_name_is_markdown_escaped(
    album_of: AlbumFactory, playing_of: PlayingFactory
) -> None:
    # The album name is agent/user-influenced. A name carrying markdown
    # metacharacters must render inert under the heading: the ``### `` marker
    # keeps its prominence, the name injects no heading, emphasis, or link.
    album = album_of("aa11bb", name="Pwn *x* #h [a](b)")
    view = PlayerView.from_status(playing_of(album, 1, 3), (album,))

    elements = _scene((album,), view).render_request().elements

    content = _by_id(elements, "music.now.album")["content"]
    assert content == "### Pwn \\*x\\* \\#h \\[a\\]\\(b\\)"
    # The only unescaped ``#`` is the heading marker itself -- the name's own
    # ``#`` is backslash-escaped, so no second heading is forged.
    assert isinstance(content, str)
    assert "\\#h" in content  # the name's # is escaped, not a heading


class TestMarkdownText:
    """The escaper that makes an untrusted album name inert in a markdown block."""

    def test_backslash_escapes_markdown_punctuation(self) -> None:
        assert MarkdownText("*x* #h [a](b)").text == "\\*x\\* \\#h \\[a\\]\\(b\\)"

    def test_folds_line_breaks_so_a_name_cannot_open_a_new_block(self) -> None:
        # A newline would let the name escape the heading line into a fresh
        # markdown block (a forged heading); it is folded to a space.
        assert "\n" not in MarkdownText("Evil\n# Pwned").text
        assert MarkdownText("a\r\nb").text == "a b"

    def test_plain_text_and_spaces_survive_unescaped(self) -> None:
        assert MarkdownText("Techno Mix").text == "Techno Mix"


def test_playing_transport_shows_the_pause_glyph_and_topic(
    album_of: AlbumFactory, playing_of: PlayingFactory
) -> None:
    album = album_of("aa11bb", name="Techno Mix")
    view = PlayerView.from_status(playing_of(album, 1, 3), (album,))

    elements = _scene((album,), view).render_request().elements
    play_pause = _children(_by_id(elements, "music.transport"))[1]

    assert play_pause["label"] == "⏸"  # playing -> press to pause
    assert play_pause["publish"] == {"topic": "music.pause"}


def test_paused_transport_shows_the_play_glyph_and_topic(
    album_of: AlbumFactory, playing_of: PlayingFactory
) -> None:
    # The paused-vs-playing distinction the scene surfaces: one button flips off the
    # wire ``paused`` flag (ProgramStatus.paused) -- ⏵ + music.resume while paused.
    album = album_of("aa11bb", name="Techno Mix")
    view = PlayerView.from_status(_paused(playing_of(album, 1, 3)), (album,))

    elements = _scene((album,), view).render_request().elements
    play_pause = _children(_by_id(elements, "music.transport"))[1]

    assert play_pause["label"] == "⏵"  # paused -> press to resume
    assert play_pause["publish"] == {"topic": "music.resume"}


def test_idle_reads_nothing_playing_and_greys_the_transport(
    album_of: AlbumFactory,
) -> None:
    request = _scene((album_of("aa11bb"),), PlayerView.idle()).render_request()

    assert (
        _by_id(request.elements, "music.now.album")["content"] == "### Nothing playing"
    )
    assert _by_id(request.elements, "music.now.position")["content"] == ""
    transport = _children(_by_id(request.elements, "music.transport"))
    assert all(child["disabled"] is True for child in transport)


def test_album_table_columns_key_column_and_play_publish(
    album_of: AlbumFactory,
) -> None:
    albums = (album_of("aa11bb", name="Techno Mix"), album_of("cc22dd", name="Ambient"))
    table = _table(_scene(albums, PlayerView.idle()).render_request().elements)

    assert table["kind"] == "table"
    assert table["columns"] == ["Album", "Genre", "Tracks"]  # no id column
    assert table["key_column"] == 0  # the Album (name) column is the row-id source
    assert table["selection_mode"] == "single"
    # Selecting a row publishes music.play; the ``publish`` sugar key IS the decorator
    # declaration. lux delivers the clicked row's key cell (the album name) as
    # payload['anchor'], which voxd resolves back to an id.
    assert table["handlers"] == [
        {"event": "row_selection_changed", "publish": ["music.play"]}
    ]


def test_album_table_is_sortable(album_of: AlbumFactory) -> None:
    # The sortable flag turns on ImGui's Display-local column sort; the default
    # borders/row-backgrounds ride alongside it.
    table = _table(
        _scene((album_of("aa11bb"),), PlayerView.idle()).render_request().elements
    )
    assert table["flags"] == ["borders", "row_bg", "sortable"]


def test_album_table_rows_carry_name_genre_and_track_count(
    album_of: AlbumFactory,
) -> None:
    album = album_of("aa11bb", name="Techno Mix", tracks=7)
    scene = _scene((album,), PlayerView.idle())
    table = _table(scene.render_request().elements)

    assert table["rows"] == [["Techno Mix", "techno", 7]]


def test_the_tracks_cell_agrees_with_the_position_line(
    album_of: AlbumFactory, playing_of: PlayingFactory
) -> None:
    # The coherence invariant, and the reported bug's regression test: for the
    # playing album, the Tracks cell IS the M in "N of M". Both count the same live
    # ready-Part set, so a widget showing 0 next to "3 of 12" is the frozen
    # creation-time snapshot leaking into a cell that must be live.
    album = album_of("aa11bb", name="Techno Mix", tracks=0, on_disk=12)
    view = PlayerView.from_status(playing_of(album, 3, 12), (album,))
    elements = _scene((album,), view).render_request().elements

    rows = _table(elements)["rows"]
    assert isinstance(rows, list)
    assert _by_id(elements, "music.now.position")["content"] == "3 of 12"
    assert rows[0][2] == 12


def test_the_projection_reads_the_store_not_at_all(album_of: AlbumFactory) -> None:
    # The gate's projection carve-out: AlbumListScene is a pure function of what it
    # is handed. Every album here has a store that raises on any read, and the
    # roster is built without one -- so a render that reaches for the disk fails
    # loudly instead of quietly costing a stat per album per repaint.
    album = album_of("aa11bb", name="Techno Mix", fails_with=OSError("no store reads"))
    roster = AlbumRoster((AlbumDisplay(album, 12),))

    request = AlbumListScene(roster, PlayerView.idle()).render_request()

    assert _table(request.elements)["rows"] == [["Techno Mix", "techno", 12]]


def test_album_table_leaves_the_playing_row_name_unmarked(
    album_of: AlbumFactory, playing_of: PlayingFactory
) -> None:
    # The playing album's name cell carries no ▶ cue: a marker would sort the row
    # to the bottom and corrupt the click key (the ▶-prefixed name resolves to no
    # album). The now-playing block above already names what is playing.
    first = album_of("aa11bb", name="Techno Mix")
    second = album_of("cc22dd", name="Ambient Drift")
    view = PlayerView.from_status(playing_of(first, 2, 3), (first, second))

    table = _table(_scene((first, second), view).render_request().elements)
    rows = table["rows"]
    assert isinstance(rows, list)

    assert rows[0][0] == "Techno Mix"  # the playing album's cell is a plain name
    assert rows[1][0] == "Ambient Drift"


def test_playing_album_row_is_selected(
    album_of: AlbumFactory, playing_of: PlayingFactory
) -> None:
    # The now-playing cue is a row selection, not a cell marker: the table's
    # authoritative selection names the playing album's key cell (its friendly
    # name), so lux highlights that row. Single-select carries exactly one id.
    first = album_of("aa11bb", name="Techno Mix")
    second = album_of("cc22dd", name="Ambient Drift")
    view = PlayerView.from_status(playing_of(second, 2, 3), (first, second))

    table = _table(_scene((first, second), view).render_request().elements)

    assert table["selection_mode"] == "single"
    assert table["selected_row_ids"] == ["Ambient Drift"]


def test_idle_scene_selects_no_row(album_of: AlbumFactory) -> None:
    # Idle highlights no row -- but the key is still emitted, empty. A field that
    # vanishes between renders is a shape change no patch can express, so an
    # omitted selection would strand the highlight on the row that stopped playing.
    table = _table(
        _scene((album_of("aa11bb", name="Techno Mix"),), PlayerView.idle())
        .render_request()
        .elements
    )

    assert table["selected_row_ids"] == []


def test_unnamed_album_row_titles_as_album(album_of: AlbumFactory) -> None:
    table = _table(
        _scene((album_of("aa11bb", name=None),), PlayerView.idle())
        .render_request()
        .elements
    )
    rows = table["rows"]
    assert isinstance(rows, list)
    assert rows[0][0] == "Album"  # sole unnamed album titles cleanly as "Album"


def test_album_count_label_sits_above_the_table(album_of: AlbumFactory) -> None:
    albums = (album_of("aa11bb"), album_of("cc22dd"), album_of("ee33ff"))
    elements = _scene(albums, PlayerView.idle()).render_request().elements

    assert _by_id(elements, "music.albums.label") == {
        "kind": "text",
        "id": "music.albums.label",
        "content": "Albums · 3 albums",
    }


def test_warning_notice_renders_the_status_line(album_of: AlbumFactory) -> None:
    # A failure notice the receive leg raised shows as the one-line status surface, so
    # a click that could not be applied is visible in the scene, not only the log.
    album = album_of("aa11bb", name="Techno Mix")
    warning = "⚠ couldn't play Techno Mix — it has no tracks yet"
    scene = _scene((album,), PlayerView.idle(), PlaybackNotice.warning(warning))

    assert _by_id(scene.render_request().elements, "music.status") == {
        "kind": "text",
        "id": "music.status",
        "content": warning,
    }


def test_silent_notice_leaves_the_status_line_empty(album_of: AlbumFactory) -> None:
    # The default silent notice keeps the slot present but empty -- the same scene
    # shape as a warning, so a later silent re-push clears a prior warning in place.
    scene = _scene((album_of("aa11bb"),), PlayerView.idle())

    assert _by_id(scene.render_request().elements, "music.status")["content"] == ""
