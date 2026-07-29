"""Tests for the player events and codec: decode, and the polymorphic apply.

These are the offline substitute for the (PR-3-gated) live click: a synthesized
``music.play``/``music.stop`` decodes into a typed event that applies to exactly one
daemon command -- the Z model's total ``dispatch`` (invariant V), exercised without a
running luxd.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, final

import pytest

from punt_vox.voxd.music_player.player_events import (
    PlayAlbum,
    PlayerEventCodec,
    StopMusic,
)
from punt_vox.voxd.programs.album_id import AlbumId

if TYPE_CHECKING:
    from punt_vox.voxd.music_player.player_events import PlayerEvent


@final
class _FakeCommands:
    """A PlayerCommands double recording each replay/off the events apply."""

    def __init__(self) -> None:
        self.played: list[AlbumId] = []
        self.stops = 0

    def replay_album(self, album_id: AlbumId) -> None:
        self.played.append(album_id)

    def off(self) -> None:
        self.stops += 1


def test_decode_play_builds_a_play_album_with_the_album_id() -> None:
    event = PlayerEventCodec().decode("music.play", {"album_id": "aa11bb"})
    assert event == PlayAlbum(AlbumId("aa11bb"))


def test_decode_stop_builds_a_stop_music() -> None:
    assert PlayerEventCodec().decode("music.stop", {}) == StopMusic()


def test_decode_rejects_an_unknown_topic() -> None:
    with pytest.raises(ValueError, match="unknown music topic"):
        PlayerEventCodec().decode("music.pause", {})


def test_decode_rejects_a_play_missing_its_album_id() -> None:
    with pytest.raises(ValueError, match="missing a string 'album_id'"):
        PlayerEventCodec().decode("music.play", {})


def test_decode_rejects_a_non_string_album_id() -> None:
    with pytest.raises(ValueError, match="missing a string 'album_id'"):
        PlayerEventCodec().decode("music.play", {"album_id": 123})


def test_decode_rejects_a_malformed_album_id() -> None:
    # AlbumId validates hex shape at the boundary, so a non-hex id raises there.
    with pytest.raises(ValueError, match="album id"):
        PlayerEventCodec().decode("music.play", {"album_id": "not-hex!"})


def test_play_album_applies_exactly_one_replay() -> None:
    service = _FakeCommands()
    PlayAlbum(AlbumId("aa11bb")).apply(service)
    assert service.played == [AlbumId("aa11bb")]
    assert service.stops == 0


def test_stop_music_applies_exactly_one_off() -> None:
    service = _FakeCommands()
    StopMusic().apply(service)
    assert service.stops == 1
    assert service.played == []


def test_a_decoded_event_dispatches_to_a_single_transition() -> None:
    # Invariant V: each event maps to exactly one playback transition.
    service = _FakeCommands()
    events: list[PlayerEvent] = [
        PlayerEventCodec().decode("music.play", {"album_id": "aa11bb"}),
        PlayerEventCodec().decode("music.stop", {}),
    ]
    for event in events:
        event.apply(service)
    assert service.played == [AlbumId("aa11bb")]
    assert service.stops == 1
