"""Tests for PlayerEventCodec: decode, and the anchor-name -> album-id resolution.

The offline substitute for a live click: a synthesized ``music.play`` (a row
selection, its anchor the clicked album's name) or ``music.stop`` decodes into a
typed event, resolving the play anchor against the catalog since voxd owns the
name-to-id mapping. Each decoded event maps to exactly one daemon transition (the Z
model's total ``dispatch``, invariant V), exercised without a running luxd.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, final

import pytest

from punt_vox.voxd.music_player.player_event_codec import PlayerEventCodec
from punt_vox.voxd.music_player.player_events import (
    Next,
    Pause,
    PlayAlbum,
    Prev,
    Resume,
    StopMusic,
)
from punt_vox.voxd.programs.album_id import AlbumId

if TYPE_CHECKING:
    from collections.abc import Callable

    from punt_vox.voxd.music_player.player_events import PlayerEvent
    from punt_vox.voxd.programs.catalog import Album

    type AlbumFactory = Callable[..., Album]


@final
class _FakeCommands:
    """A PlayerCommands double recording each replay/off the events apply."""

    def __init__(self) -> None:
        self.played: list[AlbumId] = []
        self.stops = 0

    def replay_album(self, album_id: AlbumId) -> None:
        self.played.append(album_id)

    def stop(self) -> None:
        self.stops += 1

    def advance(self) -> None:  # pragma: no cover - unused here
        raise NotImplementedError

    def prev(self) -> None:  # pragma: no cover - unused here
        raise NotImplementedError

    def pause(self) -> None:  # pragma: no cover - unused here
        raise NotImplementedError

    def resume(self) -> None:  # pragma: no cover - unused here
        raise NotImplementedError


def test_decode_play_resolves_the_anchor_name_to_the_album_id(
    album_of: AlbumFactory,
) -> None:
    # The anchor is the clicked row's Album cell -- the display name; the codec
    # resolves it to the album's id against the fresh catalog.
    album = album_of("aa11bb", name="Techno Mix")
    event = PlayerEventCodec().decode("music.play", {"anchor": "Techno Mix"}, (album,))
    assert event == PlayAlbum(AlbumId("aa11bb"))


def test_decode_play_resolves_a_marked_anchor(album_of: AlbumFactory) -> None:
    # The playing album's cell wears the ▶ cue, so its anchor arrives marked; the
    # codec resolves it to the same id as the unmarked form.
    album = album_of("aa11bb", name="Techno Mix")
    codec = PlayerEventCodec()
    event = codec.decode("music.play", {"anchor": "▶ Techno Mix"}, (album,))
    assert event == PlayAlbum(AlbumId("aa11bb"))


def test_decode_play_resolves_an_unnamed_albums_fallback(
    album_of: AlbumFactory,
) -> None:
    # An unnamed album renders as ``album <id>``; that fallback cell resolves too.
    album = album_of("aa11bb", name=None)
    event = PlayerEventCodec().decode(
        "music.play", {"anchor": "album aa11bb"}, (album,)
    )
    assert event == PlayAlbum(AlbumId("aa11bb"))


def test_decode_play_tolerates_extra_payload_keys(album_of: AlbumFactory) -> None:
    # lux carries siblings of ``anchor`` (row_ids, and shapes still settling in
    # lux-r4pp); the codec reads only ``anchor`` and ignores the rest.
    album = album_of("aa11bb", name="Techno Mix")
    payload = {"anchor": "Techno Mix", "row_ids": ["Techno Mix"], "kind": "sel"}
    event = PlayerEventCodec().decode("music.play", payload, (album,))
    assert event == PlayAlbum(AlbumId("aa11bb"))


def test_decode_empty_play_payload_is_inert(album_of: AlbumFactory) -> None:
    # Until the lux publish-payload passthrough lands (lux-r4pp) the click carries no
    # anchor; the codec raises so the boundary drops it -- the click is inert, never
    # a silent wrong-album play.
    album = album_of("aa11bb", name="Techno Mix")
    with pytest.raises(ValueError, match="carries no 'anchor'"):
        PlayerEventCodec().decode("music.play", {}, (album,))


def test_decode_rejects_a_non_string_anchor(album_of: AlbumFactory) -> None:
    album = album_of("aa11bb", name="Techno Mix")
    with pytest.raises(ValueError, match="carries no 'anchor'"):
        PlayerEventCodec().decode("music.play", {"anchor": 123}, (album,))


def test_decode_rejects_an_anchor_naming_no_album(album_of: AlbumFactory) -> None:
    album = album_of("aa11bb", name="Techno Mix")
    with pytest.raises(ValueError, match="names no catalogued album"):
        PlayerEventCodec().decode("music.play", {"anchor": "Ghost Album"}, (album,))


def test_decode_stop_builds_a_stop_music() -> None:
    assert PlayerEventCodec().decode("music.stop", {}, ()) == StopMusic()


def test_decode_rejects_an_unknown_topic() -> None:
    with pytest.raises(ValueError, match="unknown music topic"):
        PlayerEventCodec().decode("music.bogus", {}, ())


def test_decode_transport_topics_build_their_events() -> None:
    codec = PlayerEventCodec()
    assert codec.decode("music.prev", {}, ()) == Prev()
    assert codec.decode("music.next", {}, ()) == Next()
    assert codec.decode("music.pause", {}, ()) == Pause()
    assert codec.decode("music.resume", {}, ()) == Resume()


def test_a_decoded_event_dispatches_to_a_single_transition(
    album_of: AlbumFactory,
) -> None:
    # Invariant V: each decoded event maps to exactly one playback transition.
    album = album_of("aa11bb", name="Techno Mix")
    service = _FakeCommands()
    events: list[PlayerEvent] = [
        PlayerEventCodec().decode("music.play", {"anchor": "Techno Mix"}, (album,)),
        PlayerEventCodec().decode("music.stop", {}, ()),
    ]
    for event in events:
        event.apply(service)
    assert service.played == [AlbumId("aa11bb")]
    assert service.stops == 1
