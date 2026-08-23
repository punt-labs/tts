"""Tests for :class:`SessionDiscovery`'s ``claude agents --json`` parsing.

Mocks the subprocess boundary (PL-TT-5-style: no real ``claude`` binary
needed) and asserts the parsing contract: every candidate is returned, never
silently narrowed to one -- the ADR found silent auto-pick unsafe, so this
class must never make that choice itself.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from punt_vox.voxd.conversation_mode.session_discovery import (
    SessionCandidate,
    SessionDiscovery,
    SessionDiscoveryError,
)


def _fake_process(stdout: bytes, stderr: bytes = b"", returncode: int = 0) -> AsyncMock:
    process = AsyncMock()
    process.communicate.return_value = (stdout, stderr)
    process.returncode = returncode
    return process


async def test_returns_every_candidate_never_just_one() -> None:
    payload = (
        b'[{"id": "session-a", "cwd": "/repo"}, {"id": "session-b", "cwd": "/repo"}]'
    )
    with patch("asyncio.create_subprocess_exec", return_value=_fake_process(payload)):
        discovery = SessionDiscovery()
        candidates = await discovery.discover(Path("/repo"))
    assert candidates == (
        SessionCandidate(session_id="session-a", cwd="/repo"),
        SessionCandidate(session_id="session-b", cwd="/repo"),
    )


async def test_no_active_sessions_returns_empty_tuple() -> None:
    with patch("asyncio.create_subprocess_exec", return_value=_fake_process(b"[]")):
        discovery = SessionDiscovery()
        candidates = await discovery.discover(Path("/repo"))
    assert candidates == ()


async def test_nonzero_exit_raises_session_discovery_error() -> None:
    process = _fake_process(b"", b"claude: command not found", returncode=127)
    with patch("asyncio.create_subprocess_exec", return_value=process):
        discovery = SessionDiscovery()
        with pytest.raises(SessionDiscoveryError, match="127"):
            await discovery.discover(Path("/repo"))


async def test_malformed_json_raises_session_discovery_error() -> None:
    with patch(
        "asyncio.create_subprocess_exec", return_value=_fake_process(b"not json")
    ):
        discovery = SessionDiscovery()
        with pytest.raises(SessionDiscoveryError, match="invalid JSON"):
            await discovery.discover(Path("/repo"))


async def test_entry_missing_id_raises_session_discovery_error() -> None:
    with patch(
        "asyncio.create_subprocess_exec",
        return_value=_fake_process(b'[{"cwd": "/repo"}]'),
    ):
        discovery = SessionDiscovery()
        with pytest.raises(SessionDiscoveryError, match="missing an id or cwd"):
            await discovery.discover(Path("/repo"))


async def test_missing_binary_raises_session_discovery_error() -> None:
    """Regression: every other failure mode here raises SessionDiscoveryError;
    a missing ``claude`` binary used to leak a raw FileNotFoundError instead."""
    with patch("asyncio.create_subprocess_exec", side_effect=FileNotFoundError):
        discovery = SessionDiscovery()
        with pytest.raises(SessionDiscoveryError, match="not found on PATH"):
            await discovery.discover(Path("/repo"))
