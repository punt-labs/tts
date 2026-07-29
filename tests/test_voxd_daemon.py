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
from typing import TYPE_CHECKING, final

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


def _daemon(tmp_path: Path) -> VoxDaemon:
    """Build a VoxDaemon with a blocking producer so no real player spawns."""
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    config = DaemonConfig(run_dir=run_dir, config_dir=tmp_path, log_dir=tmp_path)
    playback = PlaybackQueue()
    synthesis = SynthesisPipeline(playback_mutex=playback.mutex)
    programs = ProgramSubsystem(tmp_path / "programs", _BlockingProducer())
    health = DaemonHealth(playback, lambda: 0, 0)
    router = WebSocketRouter(handlers=programs.handlers(), auth_token=None)
    return VoxDaemon(
        config=config,
        playback=playback,
        synthesis=synthesis,
        programs=programs,
        health=health,
        router=router,
    )


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
            # (a) the background tasks (incl. the scene publisher) were announced.
            assert any(
                "control writer, loop, and scene publisher up" in r.getMessage()
                for r in caplog.records
            )
            # (b) a posted command is applied end-to-end by the running writer.
            assert service.status().mode is Mode.OFF
            service.turn_on(style="techno", vibe="calm", name="mix", prompts=None)
            applied = await _wait_for_mode(daemon, Mode.GENERATING_FIRST)

    assert applied  # the control writer listened and applied turn_on


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
