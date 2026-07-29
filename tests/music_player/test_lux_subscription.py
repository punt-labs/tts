"""Tests for LuxSubscription: the Z-model invariants of voxd's receive leg.

Each test names the ``docs/vox-music-player.tex`` invariant it pins:

* I  -- at most one live connection/subscription;
* II -- an event received while subscribed is dispatched exactly once;
* III -- register-fresh restart: reconnect re-identifies, re-subscribes, and relies
  on no surviving Hub state (the buffer is luxd's, swept on lease expiry);
* V  -- each ``music.play``/``music.stop`` maps to exactly one playback transition.

The suite uses fakes; it needs no running luxd.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, final

from punt_lux import HubUnavailableError

from punt_vox.voxd.music_player.lux_subscription import LuxSubscription
from punt_vox.voxd.programs.album_id import AlbumId

if TYPE_CHECKING:
    from collections.abc import Callable

    import pytest
    from punt_lux import CallbackHandler, EventHandler

    from punt_vox.voxd.music_player.hub_ports import HubListener


@final
class _FakeCommands:
    """A PlayerCommands double recording each replay/off the leg applies."""

    def __init__(self) -> None:
        self.played: list[AlbumId] = []
        self.stops = 0

    def replay_album(self, album_id: AlbumId) -> None:
        self.played.append(album_id)

    def off(self) -> None:
        self.stops += 1


@final
class _FakeOpener:
    """A ChangeListener double counting the scene re-pushes a menu click drives."""

    def __init__(self, *, boom: bool = False) -> None:
        self.opens = 0
        self._boom = boom

    def notify_changed(self) -> None:
        self.opens += 1
        if self._boom:
            msg = "projection blew up"
            raise RuntimeError(msg)


@final
class _FakeMenu:
    """A MenuRegistrar double recording each menu registration."""

    def __init__(self) -> None:
        self.registered: list[tuple[str, str]] = []

    async def register(self, callback_id: str, label: str) -> None:
        self.registered.append((callback_id, label))


@final
class _RecordingListener:
    """A HubListener double: records its one subscription, then finishes listening."""

    def __init__(self) -> None:
        self.subscribed: tuple[str, ...] = ()
        self.listens = 0

    def subscribe(self, *topics: str) -> None:
        self.subscribed = self.subscribed + topics

    async def listen(self) -> None:
        self.listens += 1

    def stop(self) -> None:
        pass


def _connect(
    listener: HubListener, log: list[int]
) -> Callable[[EventHandler, CallbackHandler], HubListener]:
    """Return a connect_hub that hands out ``listener`` and counts its calls."""

    def connect(on_event: EventHandler, on_callback: CallbackHandler) -> HubListener:
        log.append(1)
        return listener

    return connect


def _subscription(
    *,
    service: _FakeCommands | None = None,
    opener: _FakeOpener | None = None,
) -> LuxSubscription:
    """Build a subscription with inert wiring for the direct-handler tests."""
    listener = _RecordingListener()
    return LuxSubscription(
        service or _FakeCommands(),
        opener or _FakeOpener(),
        _FakeMenu(),
        _connect(listener, []),
    )


async def test_run_registers_the_menu_and_subscribes_once() -> None:
    # Invariant I: one connection, one subscription to exactly the two topics.
    service = _FakeCommands()
    menu = _FakeMenu()
    listener = _RecordingListener()
    calls: list[int] = []
    sub = LuxSubscription(service, _FakeOpener(), menu, _connect(listener, calls))

    await sub.run()

    assert menu.registered == [("music", "Music")]
    assert listener.subscribed == ("music.play", "music.stop")
    assert len(calls) == 1  # exactly one connection built
    assert listener.listens == 1


async def test_run_retries_until_luxd_is_up_then_registers_fresh(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Invariant III: a down luxd is retried; when up, one fresh connection is built
    # and subscribed -- voxd depends on no surviving Hub state.
    monkeypatch.setattr(
        "punt_vox.voxd.music_player.lux_subscription._RETRY_SECONDS", 0.001
    )
    listener = _RecordingListener()
    attempts: list[int] = []

    def connect(on_event: EventHandler, on_callback: CallbackHandler) -> HubListener:
        attempts.append(1)
        if len(attempts) == 1:
            raise HubUnavailableError("luxd down")
        return listener

    sub = LuxSubscription(_FakeCommands(), _FakeOpener(), _FakeMenu(), connect)

    await sub.run()

    assert len(attempts) == 2  # retried once, then one connection
    assert listener.subscribed == ("music.play", "music.stop")


async def test_on_event_play_dispatches_to_replay_album() -> None:
    # The offline substitute for a live click: play -> replay_album (invariant V).
    service = _FakeCommands()
    sub = _subscription(service=service)

    await sub.on_event("music.play", {"album_id": "aa11bb"})

    assert service.played == [AlbumId("aa11bb")]
    assert service.stops == 0


async def test_on_event_stop_dispatches_to_off() -> None:
    service = _FakeCommands()
    sub = _subscription(service=service)

    await sub.on_event("music.stop", {})

    assert service.stops == 1
    assert service.played == []


async def test_on_event_dispatches_each_event_exactly_once() -> None:
    # Invariant II: an event received while subscribed is dispatched exactly once.
    service = _FakeCommands()
    sub = _subscription(service=service)

    await sub.on_event("music.play", {"album_id": "aa11bb"})
    await sub.on_event("music.stop", {})

    assert service.played == [AlbumId("aa11bb")]
    assert service.stops == 1


async def test_on_event_drops_a_malformed_frame_without_raising(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # A malformed frame must never tear down the single connection (invariant I).
    service = _FakeCommands()
    sub = _subscription(service=service)

    with caplog.at_level(logging.ERROR):
        await sub.on_event("music.play", {})  # missing the album id

    assert service.played == []  # dropped, not applied
    assert any("dropping music event" in r.getMessage() for r in caplog.records)


async def test_on_event_drops_a_playback_refusal_without_raising() -> None:
    # A play whose album is unknown/empty raises in replay_album; the leg survives.
    @final
    class _Refusing:
        def replay_album(self, album_id: AlbumId) -> None:
            msg = "no album with that id"
            raise ValueError(msg)

        def off(self) -> None:  # pragma: no cover - unused here
            raise NotImplementedError

    sub = LuxSubscription(
        _Refusing(), _FakeOpener(), _FakeMenu(), _connect(_RecordingListener(), [])
    )

    await sub.on_event("music.play", {"album_id": "aa11bb"})  # must not raise


async def test_on_callback_opens_the_scene_for_the_music_menu() -> None:
    opener = _FakeOpener()
    sub = _subscription(opener=opener)

    await sub.on_callback("music")

    assert opener.opens == 1  # the menu click re-pushes the scene


async def test_on_callback_ignores_an_unrelated_callback() -> None:
    opener = _FakeOpener()
    sub = _subscription(opener=opener)

    await sub.on_callback("beads")

    assert opener.opens == 0


async def test_on_callback_survives_a_failing_open(
    caplog: pytest.LogCaptureFixture,
) -> None:
    opener = _FakeOpener(boom=True)
    sub = _subscription(opener=opener)

    with caplog.at_level(logging.ERROR):
        await sub.on_callback("music")  # must not raise

    assert any("music menu open failed" in r.getMessage() for r in caplog.records)
