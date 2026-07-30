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
    from punt_lux.hub_client import ConnectHandler

    from punt_vox.voxd.music_player.hub_ports import HubListener

    type _ConnectHub = Callable[
        [EventHandler, CallbackHandler, ConnectHandler], HubListener
    ]


async def _fire(on_connect: ConnectHandler) -> None:
    """Invoke a (sync-or-async) on_connect hook, awaiting a coroutine result."""
    result = on_connect()
    if result is not None:
        await result


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
    """A HubListener double: fires on_connect (the handshake), then finishes."""

    def __init__(self) -> None:
        self.subscribed: tuple[str, ...] = ()
        self.listens = 0
        self._on_connect: ConnectHandler | None = None

    def bind(self, on_connect: ConnectHandler) -> None:
        """Capture the on_connect hook the real client fires on each handshake."""
        self._on_connect = on_connect

    def subscribe(self, *topics: str) -> None:
        self.subscribed = self.subscribed + topics

    async def listen(self) -> None:
        self.listens += 1
        if self._on_connect is not None:
            await _fire(self._on_connect)  # the handshake fires on_connect

    def stop(self) -> None:
        pass


@final
class _RaisingListener:
    """A HubListener whose ``listen`` raises as a skewed protocol frame would.

    ``LuxHubClient.listen`` validates each server frame; a frame that fails
    validation raises ``pydantic.ValidationError`` -- a ``ValueError`` subtype -- out
    of ``listen``, uncaught by its ``(OSError, WebSocketException)`` reconnect guard.
    This double raises the same base type to pin the subscription's guarded restart.
    The handshake succeeds first -- on_connect fires and registers the menu -- and only
    then does a later frame fail validation, exactly as the real session sequences it.
    """

    def __init__(self) -> None:
        self.subscribed: tuple[str, ...] = ()
        self._on_connect: ConnectHandler | None = None

    def bind(self, on_connect: ConnectHandler) -> None:
        """Capture the on_connect hook the real client fires on each handshake."""
        self._on_connect = on_connect

    def subscribe(self, *topics: str) -> None:
        self.subscribed = self.subscribed + topics

    async def listen(self) -> None:
        if self._on_connect is not None:
            await _fire(self._on_connect)  # handshake succeeds, menu registers
        msg = "skewed frame failed validation"
        raise ValueError(msg)

    def stop(self) -> None:
        pass


def _sequence_connect(
    listeners: list[_RecordingListener | _RaisingListener], handed: list[int]
) -> _ConnectHub:
    """Return a connect_hub that hands out ``listeners`` in order, one per connect."""

    def connect(
        on_event: EventHandler,
        on_callback: CallbackHandler,
        on_connect: ConnectHandler,
    ) -> HubListener:
        listener = listeners[len(handed)]
        handed.append(1)
        listener.bind(on_connect)  # the client fires this on the handshake
        return listener

    return connect


def _connect(listener: _RecordingListener, log: list[int]) -> _ConnectHub:
    """Return a connect_hub that hands out ``listener`` and counts its calls."""

    def connect(
        on_event: EventHandler,
        on_callback: CallbackHandler,
        on_connect: ConnectHandler,
    ) -> HubListener:
        log.append(1)
        listener.bind(on_connect)  # the client fires this on the handshake
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

    def connect(
        on_event: EventHandler,
        on_callback: CallbackHandler,
        on_connect: ConnectHandler,
    ) -> HubListener:
        attempts.append(1)
        if len(attempts) == 1:
            raise HubUnavailableError("luxd down")
        listener.bind(on_connect)
        return listener

    sub = LuxSubscription(_FakeCommands(), _FakeOpener(), _FakeMenu(), connect)

    await sub.run()

    assert len(attempts) == 2  # retried once, then one connection
    assert listener.subscribed == ("music.play", "music.stop")


async def test_run_restarts_and_recovers_after_a_listen_fault(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    # Invariant I: a fault out of ``listen`` (here a ValidationError-like error, as a
    # skewed protocol frame raises deep in the real ``listen``) is logged to the
    # daemon log and the whole cycle restarts on a fresh connection, rather than
    # leaving the receive leg silently dead. The leg recovers and listens again.
    monkeypatch.setattr(
        "punt_vox.voxd.music_player.lux_subscription._RETRY_SECONDS", 0.001
    )
    recovered = _RecordingListener()
    listeners: list[_RecordingListener | _RaisingListener] = [
        _RaisingListener(),
        recovered,
    ]
    handed: list[int] = []
    connect = _sequence_connect(listeners, handed)
    sub = LuxSubscription(_FakeCommands(), _FakeOpener(), _FakeMenu(), connect)

    with caplog.at_level(logging.ERROR):
        await sub.run()

    assert len(handed) == 2  # the faulted connection was replaced by a fresh one
    assert recovered.listens == 1  # the leg recovered and listened again
    assert any("music receive leg failed" in r.getMessage() for r in caplog.records)


async def test_run_re_registers_the_menu_on_reconnect_after_expiry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Invariant III (register-fresh, Z model §6.11): once a >30s outage lapses the
    # lease luxd sweeps the Music menu; the reconnect must register it fresh. The
    # registration rides the on_connect hook the client fires on every handshake, so
    # each fresh connection's handshake re-registers -- here two connections, two
    # registrations.
    monkeypatch.setattr(
        "punt_vox.voxd.music_player.lux_subscription._RETRY_SECONDS", 0.001
    )
    menu = _FakeMenu()
    listeners: list[_RecordingListener | _RaisingListener] = [
        _RaisingListener(),
        _RecordingListener(),
    ]
    sub = LuxSubscription(
        _FakeCommands(), _FakeOpener(), menu, _sequence_connect(listeners, [])
    )

    await sub.run()

    # Registered once per handshake -- the swept menu is restored on reconnect.
    assert menu.registered == [("music", "Music"), ("music", "Music")]


async def test_run_logs_connect_subscribe_connected_and_clean_stop(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # The connect/subscribe/connected/disconnect audit trail: one grep of [lux]
    # shows the whole receive-leg lifecycle in order.
    listener = _RecordingListener()
    sub = LuxSubscription(
        _FakeCommands(), _FakeOpener(), _FakeMenu(), _connect(listener, [])
    )

    with caplog.at_level(logging.INFO):
        await sub.run()

    lux = [r.getMessage() for r in caplog.records if "[lux]" in r.getMessage()]
    assert any("connecting" in m for m in lux)
    assert any("subscribed to topics music.play, music.stop" in m for m in lux)
    assert any("handshake complete" in m for m in lux)  # connected
    assert any("stopped cleanly" in m for m in lux)  # clean disconnect


async def test_run_logs_the_reconnect_when_luxd_is_down(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    # A down luxd is retried; the [lux] warning names the attempt and backoff so a
    # reconnect storm is legible in vox.log.
    monkeypatch.setattr(
        "punt_vox.voxd.music_player.lux_subscription._RETRY_SECONDS", 0.001
    )
    listener = _RecordingListener()
    attempts: list[int] = []

    def connect(
        on_event: EventHandler,
        on_callback: CallbackHandler,
        on_connect: ConnectHandler,
    ) -> HubListener:
        attempts.append(1)
        if len(attempts) == 1:
            raise HubUnavailableError("luxd down")
        listener.bind(on_connect)
        return listener

    sub = LuxSubscription(_FakeCommands(), _FakeOpener(), _FakeMenu(), connect)

    with caplog.at_level(logging.WARNING):
        await sub.run()

    assert any(
        "[lux]" in r.getMessage() and "luxd down" in r.getMessage()
        for r in caplog.records
    )


async def test_on_event_logs_the_inbound_play(
    caplog: pytest.LogCaptureFixture,
) -> None:
    sub = _subscription()
    with caplog.at_level(logging.INFO):
        await sub.on_event("music.play", {"album_id": "aa11bb"})
    assert any(
        "[lux]" in r.getMessage() and "received music.play" in r.getMessage()
        for r in caplog.records
    )


async def test_on_callback_logs_the_menu_click(
    caplog: pytest.LogCaptureFixture,
) -> None:
    sub = _subscription()
    with caplog.at_level(logging.INFO):
        await sub.on_callback("music")
    assert any(
        "[lux]" in r.getMessage() and "Music menu clicked" in r.getMessage()
        for r in caplog.records
    )


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


async def test_on_connect_registers_the_menu_and_repushes_the_scene() -> None:
    # The Bugbot fix (invariant III, register-fresh): a fresh handshake fires
    # on_connect, which BOTH re-registers the Music menu AND re-pushes the scene -- so
    # a >30s outage that lapses the lease is healed on the internal reconnect, not only
    # on an outer fault.
    menu = _FakeMenu()
    opener = _FakeOpener()
    sub = LuxSubscription(
        _FakeCommands(), opener, menu, _connect(_RecordingListener(), [])
    )

    await sub.on_connect()

    assert menu.registered == [("music", "Music")]  # menu re-registered
    assert opener.opens == 1  # scene re-pushed


async def test_on_connect_survives_a_failing_scene_push(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # A transient projection failure is logged, not lost, and never skips the
    # registration; on_connect must not raise (lux logs-and-continues if it does).
    menu = _FakeMenu()
    opener = _FakeOpener(boom=True)
    sub = LuxSubscription(
        _FakeCommands(), opener, menu, _connect(_RecordingListener(), [])
    )

    with caplog.at_level(logging.ERROR):
        await sub.on_connect()  # must not raise

    assert menu.registered == [("music", "Music")]  # registered despite the push fault
    assert any("scene projection on connect" in r.getMessage() for r in caplog.records)
