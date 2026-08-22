"""Tests for the player events: the polymorphic apply and failure double-dispatch.

Each event maps to exactly one daemon command -- the Z model's total ``dispatch``
(invariant V) -- and names its own failure surface (double dispatch), so the
subscription never branches on the topic. Decoding lives with
:class:`PlayerEventCodec` in ``test_player_event_codec``.
"""

from __future__ import annotations

from typing import final

from punt_vox.voxd.music_player.player_events import (
    Next,
    Pause,
    PlayAlbum,
    Prev,
    Resume,
    StopMusic,
)
from punt_vox.voxd.programs.album_id import AlbumId


@final
class _FakeCommands:
    """A PlayerCommands double recording each replay/off the events apply."""

    def __init__(self) -> None:
        self.played: list[AlbumId] = []
        self.stops = 0
        self.nexts = 0
        self.prevs = 0
        self.pauses = 0
        self.resumes = 0

    def replay_album(self, album_id: AlbumId) -> None:
        self.played.append(album_id)

    def stop(self) -> None:
        self.stops += 1

    def advance(self) -> None:
        self.nexts += 1

    def prev(self) -> None:
        self.prevs += 1

    def pause(self) -> None:
        self.pauses += 1

    def resume(self) -> None:
        self.resumes += 1


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


def test_transport_events_each_apply_to_one_daemon_call() -> None:
    # Invariant V: each transport event maps to exactly one daemon transition.
    service = _FakeCommands()
    Prev().apply(service)
    Next().apply(service)
    Pause().apply(service)
    Resume().apply(service)
    assert (service.prevs, service.nexts, service.pauses, service.resumes) == (
        1,
        1,
        1,
        1,
    )
    assert service.played == []  # transport never replays or stops
    assert service.stops == 0


@final
class _FakePresenter:
    """A FailurePresenter double recording which failure each event surfaces."""

    def __init__(self) -> None:
        self.play_failures: list[AlbumId] = []
        self.stop_failures = 0
        self.transport_failures = 0
        self.resolve_failures: list[str] = []

    def present_play_failure(self, album: AlbumId) -> None:
        self.play_failures.append(album)

    def present_stop_failure(self) -> None:
        self.stop_failures += 1

    def present_transport_failure(self) -> None:
        self.transport_failures += 1

    def present_resolve_failure(self, anchor: str) -> None:
        self.resolve_failures.append(anchor)


def test_play_album_surfaces_its_own_play_failure() -> None:
    # Double dispatch: the event names its failure, no topic branch in the caller.
    presenter = _FakePresenter()
    PlayAlbum(AlbumId("aa11bb")).surface_failure(presenter)
    assert presenter.play_failures == [AlbumId("aa11bb")]
    assert presenter.stop_failures == 0


def test_stop_music_surfaces_its_own_stop_failure() -> None:
    presenter = _FakePresenter()
    StopMusic().surface_failure(presenter)
    assert presenter.stop_failures == 1
    assert presenter.play_failures == []


def test_transport_events_surface_a_transport_failure() -> None:
    presenter = _FakePresenter()
    for event in (Prev(), Next(), Pause(), Resume()):
        event.surface_failure(presenter)
    assert presenter.transport_failures == 4
    assert presenter.play_failures == []
    assert presenter.stop_failures == 0
