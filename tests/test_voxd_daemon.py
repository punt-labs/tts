"""Integration test for ``VoxDaemon._lifespan`` -- the background-task wiring.

The lifespan starts three background tasks (playback consumer, the sole control
writer, and the playback loop). This drives the real lifespan context and asserts
(a) all three tasks are announced started and (b) a command posted to the service
is *applied end-to-end* by the running control writer -- the daemon-level guard
that a posted transition is actually listened to, never dropped on the floor.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Self, final

from punt_vox.types_programs.mode import Mode
from punt_vox.voxd.config import DaemonConfig
from punt_vox.voxd.daemon import VoxDaemon
from punt_vox.voxd.health import DaemonHealth
from punt_vox.voxd.playback import PlaybackQueue
from punt_vox.voxd.programs.wiring import ProgramSubsystem
from punt_vox.voxd.router import WebSocketRouter
from punt_vox.voxd.synthesis import SynthesisPipeline

if TYPE_CHECKING:
    from pathlib import Path

    import pytest
    from punt_lux import CallbackHandler, EventHandler, LuxClient
    from punt_lux.hub_client import ConnectHandler

    from punt_vox.voxd.music_player.hub_ports import HubListener
    from punt_vox.voxd.programs.part import Part
    from punt_vox.voxd.programs.producer import PartSpec


@final
class _BlockingProducer:
    """A producer whose generation never completes -- the pool stays generating.

    Keeps the Program parked in ``generating_first`` (nothing playing) so the test
    asserts the control writer applied ``turn_on`` without the playback loop trying
    to spawn a real player for a produced Part.
    """

    __slots__ = ()

    async def produce(self, spec: PartSpec, target: Path) -> Part:
        """Block forever; a fill awaiting this never delivers a Part."""
        await asyncio.Event().wait()
        raise AssertionError  # unreachable -- the await never returns


@final
class _CountingLux:
    """A LuxClientFactory double that records leg requests and never connects.

    The lifespan composes the music player, whose subscription leg asks for a
    hub within a few hundred milliseconds of the scene task starting. With no
    factory injected the subsystem builds ``VoxLuxClients()`` -- the production
    identity resolved against whatever luxd is running -- so the suite reached
    the operator's live hub and published a ``vox.music`` scene from its own
    scratch store, standing a second "Music" frame beside the real one on their
    display.

    Both methods raise after counting, which is what a down luxd looks like to
    the subsystem: its legs are guarded and retry, so a raise exercises that
    path rather than wedging the lifespan. The counts are the assertion --
    raising alone proves nothing, because both legs run inside tasks whose
    exceptions are discarded when the lifespan cancels them.
    """

    __slots__ = ("client_calls", "hub_calls")
    client_calls: int
    hub_calls: int

    def __new__(cls) -> Self:
        self = super().__new__(cls)
        self.client_calls = 0
        self.hub_calls = 0
        return self

    def client(self) -> LuxClient:
        self.client_calls += 1
        msg = "test double: no lux client leg is built"
        raise RuntimeError(msg)

    def hub(
        self,
        on_event: EventHandler,
        on_callback: CallbackHandler,
        on_connect: ConnectHandler,
    ) -> HubListener:
        self.hub_calls += 1
        msg = "test double: no lux hub leg is built"
        raise RuntimeError(msg)


def _daemon(tmp_path: Path, lux: _CountingLux | None = None) -> VoxDaemon:
    """Build a VoxDaemon with a blocking producer so no real player spawns."""
    lux = lux if lux is not None else _CountingLux()
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    config = DaemonConfig(run_dir=run_dir, config_dir=tmp_path, log_dir=tmp_path)
    playback = PlaybackQueue()
    synthesis = SynthesisPipeline(playback_mutex=playback.mutex)
    programs = ProgramSubsystem(
        tmp_path / "programs", _BlockingProducer(), tmp_path / "mpv.sock"
    )
    health = DaemonHealth(playback, lambda: 0, 0)
    router = WebSocketRouter(handlers=programs.handlers(), auth_token=None)
    return VoxDaemon(
        config=config,
        playback=playback,
        synthesis=synthesis,
        programs=programs,
        health=health,
        router=router,
        lux_clients=lux,
    )


async def _wait_for_hub_leg(lux: _CountingLux) -> None:
    """Yield until the subscription asks ``lux`` for a hub leg, or give up.

    Polling rather than sleeping a fixed span: the leg is requested as soon as
    the scene task runs, so a wall-clock wait is both slower than needed and
    flaky under load. Returning without the leg is not an error here -- the
    caller's assertion is what reports it, with the diagnosis.
    """
    for _ in range(2000):
        if lux.hub_calls:
            return
        await asyncio.sleep(0)


async def _wait_for_mode(daemon: VoxDaemon, mode: Mode) -> bool:
    """Poll the daemon's status until it reaches ``mode`` (or give up)."""
    service = daemon._programs.service  # pyright: ignore[reportPrivateUsage]
    for _ in range(500):
        if service.status().mode is mode:
            return True
        await asyncio.sleep(0)
    return False


async def test_lifespan_starts_tasks_and_applies_a_command(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    daemon = _daemon(tmp_path)
    app = daemon.build_app()
    service = daemon._programs.service  # pyright: ignore[reportPrivateUsage]

    with caplog.at_level(logging.INFO, logger="punt_vox.voxd.daemon"):
        async with daemon._lifespan(app):  # pyright: ignore[reportPrivateUsage]
            # (a) the background tasks (incl. the mpv supervisor and the scene
            # publisher) were announced. mpv is absent in the test sandbox, so its
            # spawn fails -- but the daemon stays up and applies commands anyway,
            # proving the program tier's absence never takes the daemon down.
            assert any(
                "control writer, loop, mpv, and scene up" in r.getMessage()
                for r in caplog.records
            )
            # (b) a posted command is applied end-to-end by the running writer.
            assert service.status().mode is Mode.OFF
            service.turn_on(style="techno", vibe="calm", name="mix", prompts=None)
            applied = await _wait_for_mode(daemon, Mode.GENERATING_FIRST)

    assert applied  # the control writer listened and applied turn_on


async def test_lifespan_asks_the_injected_factory_for_its_lux_legs(
    tmp_path: Path,
) -> None:
    """The lifespan's lux legs come from the injected factory, never production.

    The regression this guards is the whole reason the factory is a constructor
    argument. The lifespan composes the music player, whose subscription asks
    for a hub leg a few hundred milliseconds into the scene task. Before the
    injection that leg was ``VoxLuxClients()`` -- voxd's production identity
    against whatever luxd is running -- so this suite connected to the
    operator's live hub and published a ``vox.music`` scene built from its own
    scratch store. The row they saw was this module's own
    ``turn_on(style="techno", ..., name="mix")``: ``display_title`` title-cases
    ``mix`` to "Mix", the Genre cell is the style, and the blocking producer
    leaves Tracks at 0.

    The assertion is the call *count*, not an exception. Both legs run inside
    tasks the lifespan cancels on exit, and a task's exception is discarded
    unless awaited -- so a factory that merely raised would pass this test even
    if production clients were being built.
    """
    lux = _CountingLux()
    daemon = _daemon(tmp_path, lux)
    app = daemon.build_app()

    async with daemon._lifespan(app):  # pyright: ignore[reportPrivateUsage]
        await _wait_for_hub_leg(lux)

    assert lux.hub_calls >= 1, (
        "the lifespan never asked the injected factory for a hub leg -- either "
        "the subsystem stopped building one (make this test follow it) or it is "
        "building one from somewhere else, which means production clients"
    )


async def test_lifespan_cancels_tasks_on_exit(tmp_path: Path) -> None:
    """After the lifespan exits, the daemon's background tasks are stopped."""
    daemon = _daemon(tmp_path)
    app = daemon.build_app()
    before = len(asyncio.all_tasks())

    async with daemon._lifespan(app):  # pyright: ignore[reportPrivateUsage]
        await asyncio.sleep(0)
        assert len(asyncio.all_tasks()) > before  # tasks are live inside

    await asyncio.sleep(0)
    # The three tasks were cancelled on exit -- back to the baseline count.
    assert len(asyncio.all_tasks()) <= before + 1
