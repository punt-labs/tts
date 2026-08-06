"""Tests for ``TransportRow`` -- the prev / play-pause / next / stop control bar.

The row's glyphs, publish topics, and disabled flags are pure functions of the
:class:`PlayerView`, so these assert the modelled transport properties by name:

* ``T6`` -- the play/pause glyph is ``⏸`` iff playing and ``⏵`` iff paused.
* ``T5`` -- prev is disabled at part 1 and next is disabled at part M.
* the idle-inert rule -- every button is disabled when nothing plays.
"""

from __future__ import annotations

from punt_vox.types_programs.status_views import NowPlaying
from punt_vox.voxd.music_player.player_view import PlayerMode, PlayerView
from punt_vox.voxd.music_player.transport_row import TransportRow
from punt_vox.voxd.programs.album_id import AlbumId

_ALBUM = AlbumId("aa11bb")

_PAUSE_GLYPH = "⏸"
_PLAY_GLYPH = "⏵"
_PREV_GLYPH = "⏮"
_NEXT_GLYPH = "⏭"
_STOP_GLYPH = "⏹"


def _playing(index: int, of: int) -> PlayerView:
    cursor = NowPlaying(index=index, of=of)
    return PlayerView(mode=PlayerMode.PLAYING, album=_ALBUM, now_playing=cursor)


def _paused(index: int, of: int) -> PlayerView:
    cursor = NowPlaying(index=index, of=of)
    return PlayerView(mode=PlayerMode.PAUSED, album=_ALBUM, now_playing=cursor)


def _buttons(view: PlayerView) -> list[dict[str, object]]:
    children = TransportRow(view).to_dict()["children"]
    assert isinstance(children, list)
    return children


def test_row_is_a_columns_group_of_four_buttons() -> None:
    row = TransportRow(_playing(2, 3)).to_dict()
    assert row["kind"] == "group"
    assert row["id"] == "music.transport"
    assert row["layout"] == "columns"
    assert [b["id"] for b in _buttons(_playing(2, 3))] == [
        "music.transport.prev",
        "music.transport.playpause",
        "music.transport.next",
        "music.transport.stop",
    ]


def test_t6_glyph_is_pause_iff_playing() -> None:
    play_pause = _buttons(_playing(2, 3))[1]
    assert play_pause["label"] == _PAUSE_GLYPH
    assert play_pause["publish"] == {"topic": "music.pause"}
    assert play_pause["tooltip"] == "Pause"


def test_t6_glyph_is_play_iff_paused() -> None:
    play_pause = _buttons(_paused(2, 3))[1]
    assert play_pause["label"] == _PLAY_GLYPH
    assert play_pause["publish"] == {"topic": "music.resume"}
    assert play_pause["tooltip"] == "Play"


def test_prev_and_next_carry_their_glyphs_and_topics() -> None:
    prev, _, nxt, stop = _buttons(_playing(2, 3))
    assert prev["label"] == _PREV_GLYPH
    assert prev["publish"] == {"topic": "music.prev"}
    assert nxt["label"] == _NEXT_GLYPH
    assert nxt["publish"] == {"topic": "music.next"}
    assert stop["label"] == _STOP_GLYPH
    assert stop["publish"] == {"topic": "music.stop"}


def test_t5_prev_disabled_at_part_one() -> None:
    prev, _, nxt, _ = _buttons(_playing(1, 3))
    assert prev["disabled"] is True  # prev at part 1 is a no-op
    assert nxt["disabled"] is False


def test_t5_next_disabled_at_part_m() -> None:
    prev, _, nxt, _ = _buttons(_playing(3, 3))
    assert nxt["disabled"] is True  # next at part M is a no-op
    assert prev["disabled"] is False


def test_prev_and_next_enabled_in_the_interior() -> None:
    prev, _, nxt, _ = _buttons(_playing(2, 3))
    assert prev["disabled"] is False
    assert nxt["disabled"] is False


def test_bounds_hold_while_paused_too() -> None:
    # The prev/next bounds are cursor-driven, independent of playing vs paused.
    prev, _, nxt, _ = _buttons(_paused(1, 3))
    assert prev["disabled"] is True
    assert nxt["disabled"] is False


def test_transport_is_inert_when_idle() -> None:
    buttons = _buttons(PlayerView.idle())
    assert all(button["disabled"] is True for button in buttons)
    # Idle wears the play glyph, ready to resume nothing.
    assert buttons[1]["label"] == _PLAY_GLYPH
    assert buttons[1]["publish"] == {"topic": "music.resume"}
