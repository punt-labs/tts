"""Tests for :class:`SessionDiscovery`'s ``claude agents --json`` parsing.

Mocks the subprocess boundary (PL-TT-5-style: no real ``claude`` binary
needed) and asserts the parsing contract: every candidate is returned, never
silently narrowed to one -- the ADR found silent auto-pick unsafe, so this
class must never make that choice itself.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from punt_vox.voxd.conversation_mode import (
    session_discovery as session_discovery_module,
)
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


async def test_entry_with_an_empty_id_raises_session_discovery_error() -> None:
    """Regression: an empty ``id`` used to pass ``_first_str`` as a "found"
    value and only fail 120s later as an opaque timeout, once
    :class:`~.claude_session_attach.ClaudeSessionAttach` refused it -- with
    no view of the malformed ``claude agents --json`` entry that caused it.
    Rejecting it here surfaces the failure at discovery time instead, naming
    the entry.
    """
    with patch(
        "asyncio.create_subprocess_exec",
        return_value=_fake_process(b'[{"id": "", "cwd": "/repo"}]'),
    ):
        discovery = SessionDiscovery()
        with pytest.raises(SessionDiscoveryError, match="missing an id or cwd"):
            await discovery.discover(Path("/repo"))


async def test_payload_that_is_not_a_list_raises_session_discovery_error() -> None:
    """The parse-error branch for a well-formed JSON value of the wrong shape
    (an object, not an array) -- distinct from ``test_malformed_json_raises_
    session_discovery_error``, which covers JSON that fails to parse at all.
    """
    with patch(
        "asyncio.create_subprocess_exec",
        return_value=_fake_process(b'{"id": "session-a", "cwd": "/repo"}'),
    ):
        discovery = SessionDiscovery()
        with pytest.raises(SessionDiscoveryError, match="a dict, not a list"):
            await discovery.discover(Path("/repo"))


async def test_entry_that_is_not_an_object_raises_session_discovery_error() -> None:
    """Each list entry must itself be an object -- a bare string or number in
    the array is exactly as malformed as the top level being the wrong shape.
    """
    with patch(
        "asyncio.create_subprocess_exec",
        return_value=_fake_process(b'["not-an-object"]'),
    ):
        discovery = SessionDiscovery()
        with pytest.raises(SessionDiscoveryError, match="a str, not an object"):
            await discovery.discover(Path("/repo"))


async def test_key_aliases_are_tried_in_order() -> None:
    """``_first_str`` must accept ``sessionId``/``session_id`` and
    ``workingDirectory``/``working_directory`` as aliases for ``id``/``cwd``
    -- ``claude agents --json``'s exact field naming is not contractually
    fixed.
    """
    payload = (
        b'[{"sessionId": "session-camel", "workingDirectory": "/camel"},'
        b' {"session_id": "session-snake", "working_directory": "/snake"}]'
    )
    with patch("asyncio.create_subprocess_exec", return_value=_fake_process(payload)):
        discovery = SessionDiscovery()
        candidates = await discovery.discover(Path("/repo"))
    assert candidates == (
        SessionCandidate(session_id="session-camel", cwd="/camel"),
        SessionCandidate(session_id="session-snake", cwd="/snake"),
    )


async def test_missing_binary_raises_session_discovery_error() -> None:
    """Regression: every other failure mode here raises SessionDiscoveryError;
    a missing ``claude`` binary used to leak a raw FileNotFoundError instead."""
    with patch("asyncio.create_subprocess_exec", side_effect=FileNotFoundError):
        discovery = SessionDiscovery()
        with pytest.raises(SessionDiscoveryError, match="not found on PATH"):
            await discovery.discover(Path("/repo"))


async def test_spawn_strips_anthropic_api_key_from_the_subprocess_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression: SessionDiscovery had the identical ANTHROPIC_API_KEY
    vulnerability ClaudeSessionAttach's fix addressed, and it runs BEFORE
    that fixed code path -- a stale key inherited here fails claude agents
    --json's own auth the same way, before the call state machine, the
    lock, or any user-facing feedback exists.
    """
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-stale-org-key")
    with patch(
        "asyncio.create_subprocess_exec", return_value=_fake_process(b"[]")
    ) as mock_exec:
        discovery = SessionDiscovery()
        await discovery.discover(Path("/repo"))
    spawned_env = mock_exec.call_args.kwargs["env"]
    assert "ANTHROPIC_API_KEY" not in spawned_env


class _HangingProcess:
    """A subprocess double whose ``communicate()`` never returns -- simulates
    a `claude agents --json` stuck in the same auth-retry loop that
    motivated the timeout in the first place."""

    def __init__(self) -> None:
        self.returncode: int | None = None
        self.kill = MagicMock()
        self.wait = AsyncMock()

    async def communicate(self) -> tuple[bytes, bytes]:
        await asyncio.sleep(10)
        return (b"[]", b"")  # pragma: no cover -- the timeout fires first


async def test_discover_times_out_and_kills_the_process() -> None:
    process = _HangingProcess()
    with (
        patch("asyncio.create_subprocess_exec", return_value=process),
        patch.object(session_discovery_module, "_DISCOVERY_TIMEOUT_S", 0.05),
    ):
        discovery = SessionDiscovery()
        with pytest.raises(SessionDiscoveryError, match="did not finish within"):
            await discovery.discover(Path("/repo"))
    process.kill.assert_called_once()
    process.wait.assert_awaited()
