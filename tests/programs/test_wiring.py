"""Tests for the ProgramSubsystem facade -- the daemon's one Programs seam."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock

from punt_vox.voxd.programs.service import ProgramService
from punt_vox.voxd.programs.wiring import ProgramSubsystem

from .conftest import QuietProducer, seed_album

if TYPE_CHECKING:
    from pathlib import Path


def _subsystem(root: Path) -> ProgramSubsystem:
    """Build a subsystem with a fake producer (Fix #5e: producer is injectable)."""
    return ProgramSubsystem(root, QuietProducer())


_EXPECTED_HANDLERS = frozenset(
    {
        "program_on",
        "program_off",
        "program_next",
        "program_prev",
        "program_pause",
        "program_resume",
        "program_select",
        "program_list",
        "program_status",
        "music_new",
        "music_manifest",
        "music_remove",
    }
)


class TestHandlerRoster:
    """handlers() exposes the program_* playback and music_* catalog adapters."""

    def test_exactly_the_expected_handlers(self, tmp_path: Path) -> None:
        subsystem = _subsystem(tmp_path / "programs")
        assert set(subsystem.handlers()) == _EXPECTED_HANDLERS

    def test_service_is_a_program_service(self, tmp_path: Path) -> None:
        subsystem = _subsystem(tmp_path / "programs")
        assert isinstance(subsystem.service, ProgramService)


class TestHandlersBoundToService:
    """The handlers read and drive the subsystem's own service, live."""

    def test_status_handler_reports_idle_before_any_command(
        self, tmp_path: Path
    ) -> None:
        subsystem = _subsystem(tmp_path / "programs")
        ws = MagicMock()
        ws.send_json = AsyncMock()

        asyncio.run(subsystem.handlers()["program_status"]({"id": "s1"}, ws))

        reply = ws.send_json.await_args.args[0]
        assert reply["type"] == "program_status"
        assert reply["status"]["mode"] == "off"

    def test_list_handler_reports_albums(self, tmp_path: Path) -> None:
        root = tmp_path / "programs"
        seed_album(root, 1, 2, style="ambient", vibe="calm", album_id="a3f1c9")
        subsystem = _subsystem(root)
        ws = MagicMock()
        ws.send_json = AsyncMock()

        asyncio.run(subsystem.handlers()["program_list"]({"id": "l1"}, ws))

        reply = ws.send_json.await_args.args[0]
        assert reply["type"] == "program_list"
        assert [p["id"] for p in reply["programs"]] == ["a3f1c9"]
