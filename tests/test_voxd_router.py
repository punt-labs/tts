"""Tests for punt_vox.voxd.router -- WebSocket message routing."""
# pyright: reportPrivateUsage=false

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, cast, final
from unittest.mock import MagicMock

import pytest

from punt_vox.voxd.chimes import ChimeResolver
from punt_vox.voxd.dedup import ChimeDedup, OnceDedup
from punt_vox.voxd.health import DaemonHealth
from punt_vox.voxd.playback import PlaybackQueue
from punt_vox.voxd.programs.wiring import ProgramSubsystem
from punt_vox.voxd.record_handler import RecordHandler
from punt_vox.voxd.router import WebSocketRouter
from punt_vox.voxd.speech_handlers import SynthesizeHandler
from punt_vox.voxd.synthesis import SynthesisPipeline
from punt_vox.voxd.system_handlers import ChimeHandler, HealthHandler, VoicesHandler
from punt_vox.voxd.types import MessageHandler

if TYPE_CHECKING:
    from pathlib import Path

    from punt_vox.voxd.programs.part import Part
    from punt_vox.voxd.programs.producer import PartSpec


@final
class _UnusedProducer:
    """A producer the router tests never invoke (they exercise no generation)."""

    __slots__ = ()

    async def produce(self, spec: PartSpec, target: Path) -> Part:
        """Never called by router tests -- routing does not generate audio."""
        raise NotImplementedError


def _make_router(
    *,
    auth_token: str | None = None,
) -> WebSocketRouter:
    """Build a WebSocketRouter with the real handler wiring for routing tests.

    Not filesystem-isolated: it resolves user-home paths (default_output_dir /
    recordings_dir) and ProgramSubsystem scans the on-disk catalog at init. The
    router tests exercise message dispatch, not audio or file effects.
    """
    from punt_vox.dirs import default_output_dir
    from punt_vox.paths import recordings_dir
    from punt_vox.voxd.record_store import RecordStore

    pb = PlaybackQueue()
    hl = DaemonHealth(pb, lambda: 0, 0)
    syn = SynthesisPipeline(playback_mutex=pb.mutex)
    programs = ProgramSubsystem(
        default_output_dir() / "programs",
        _UnusedProducer(),
        default_output_dir() / "mpv.sock",
    )

    handlers: dict[str, MessageHandler] = {
        "synthesize": SynthesizeHandler(
            synthesis=syn,
            playback=pb,
            once_dedup=OnceDedup(),
        ),
        "record": RecordHandler(synthesis=syn, store=RecordStore(recordings_dir())),
        "chime": ChimeHandler(
            chimes=ChimeResolver(),
            chime_dedup=ChimeDedup(),
            playback=pb,
        ),
        "voices": VoicesHandler(),
        "health": HealthHandler(health=hl),
        **programs.handlers(),
    }
    return WebSocketRouter(
        handlers=handlers,
        auth_token=auth_token,
    )


class TestWsRoutePeerClose:
    """handle_connection must exit quietly when the peer closes mid-receive."""

    @pytest.mark.asyncio
    async def test_state_check_preempts_disconnected_receive(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """State check preempts receive_text on a peer-closed socket."""
        from starlette.websockets import WebSocketState

        router = _make_router()

        async def _accept() -> None:
            return None

        async def _close(code: int = 1000) -> None:
            return None

        receive_calls = 0

        async def _receive_text() -> str:
            nonlocal receive_calls
            receive_calls += 1
            raise AssertionError(
                "receive_text must not be called once state is DISCONNECTED"
            )

        fake_ws = MagicMock()
        fake_ws.headers = {}
        fake_ws.query_params = {}
        fake_ws.accept = _accept
        fake_ws.close = _close
        fake_ws.receive_text = _receive_text
        fake_ws.application_state = WebSocketState.DISCONNECTED

        with caplog.at_level(logging.ERROR, logger="punt_vox.voxd"):
            await router.handle_connection(cast("object", fake_ws))  # type: ignore[arg-type]

        ws_error_records = [
            rec
            for rec in caplog.records
            if rec.levelno == logging.ERROR
            and rec.name == "punt_vox.voxd.router"
            and "WebSocket error" in rec.getMessage()
        ]
        assert ws_error_records == []
        assert receive_calls == 0
        assert router.client_count == 0

    @pytest.mark.asyncio
    async def test_logs_error_when_receive_raises_unexpected_runtimeerror(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Unexpected RuntimeError from receive_text still logs an error."""
        from starlette.websockets import WebSocketState

        router = _make_router()

        async def _accept() -> None:
            return None

        async def _close(code: int = 1000) -> None:
            return None

        async def _receive_text() -> str:
            raise RuntimeError("something else entirely")

        fake_ws = MagicMock()
        fake_ws.headers = {}
        fake_ws.query_params = {}
        fake_ws.accept = _accept
        fake_ws.close = _close
        fake_ws.receive_text = _receive_text
        fake_ws.application_state = WebSocketState.CONNECTED

        with caplog.at_level(logging.ERROR, logger="punt_vox.voxd"):
            await router.handle_connection(cast("object", fake_ws))  # type: ignore[arg-type]

        ws_error_records = [
            rec
            for rec in caplog.records
            if rec.levelno == logging.ERROR
            and rec.name == "punt_vox.voxd.router"
            and "WebSocket error" in rec.getMessage()
        ]
        assert len(ws_error_records) == 1
        assert router.client_count == 0


class TestWsRouteAuth:
    """A rejected connection is closed and logged without leaking the token."""

    @pytest.mark.asyncio
    async def test_rejected_connection_logs_peer_not_token(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Bad-token connect: closed 1008, WARNING carries the peer, never a token."""
        expected = "expected-server-token"
        supplied = "attacker-supplied-token"
        router = _make_router(auth_token=expected)

        closed: list[int] = []

        async def _close(code: int = 1000) -> None:
            closed.append(code)

        fake_ws = MagicMock()
        fake_ws.query_params = {"token": supplied}
        fake_ws.client = ("198.51.100.7", 4242)
        fake_ws.close = _close

        with caplog.at_level(logging.WARNING, logger="punt_vox.voxd"):
            await router.handle_connection(cast("object", fake_ws))  # type: ignore[arg-type]

        assert closed == [1008]
        warnings = [
            r
            for r in caplog.records
            if r.levelno == logging.WARNING and "Auth rejected" in r.getMessage()
        ]
        assert len(warnings) == 1
        assert "198.51.100.7" in warnings[0].getMessage()
        blob = " ".join(r.getMessage() for r in caplog.records)
        assert expected not in blob
        assert supplied not in blob


class TestWsRouteOrigin:
    """A browser-origin handshake is refused; native no-Origin clients proceed."""

    @pytest.mark.asyncio
    async def test_origin_header_rejected_before_auth(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Present Origin: closed 1008, token never inspected, no token leaked."""
        token = "expected-server-token"
        router = _make_router(auth_token=token)

        closed: list[int] = []
        accepted = False

        async def _close(code: int = 1000) -> None:
            closed.append(code)

        async def _accept() -> None:
            nonlocal accepted
            accepted = True

        fake_ws = MagicMock()
        fake_ws.headers = {"origin": "https://evil.example"}
        # A *valid* token must still be refused when Origin is present.
        fake_ws.query_params = {"token": token}
        fake_ws.client = ("203.0.113.9", 5555)
        fake_ws.close = _close
        fake_ws.accept = _accept

        with caplog.at_level(logging.WARNING, logger="punt_vox.voxd"):
            await router.handle_connection(cast("object", fake_ws))  # type: ignore[arg-type]

        assert closed == [1008]
        assert accepted is False
        assert router.client_count == 0
        warnings = [
            r
            for r in caplog.records
            if r.levelno == logging.WARNING and "Origin" in r.getMessage()
        ]
        assert len(warnings) == 1
        assert "203.0.113.9" in warnings[0].getMessage()
        blob = " ".join(r.getMessage() for r in caplog.records)
        assert token not in blob

    @pytest.mark.asyncio
    async def test_no_origin_valid_token_accepted(self) -> None:
        """Absent Origin + valid token: handshake accepted, never closed 1008."""
        from starlette.websockets import WebSocketDisconnect, WebSocketState

        token = "expected-server-token"
        router = _make_router(auth_token=token)

        accepted = False
        closed: list[int] = []

        async def _accept() -> None:
            nonlocal accepted
            accepted = True

        async def _close(code: int = 1000) -> None:
            closed.append(code)

        async def _receive_text() -> str:
            raise WebSocketDisconnect(1000)

        fake_ws = MagicMock()
        fake_ws.headers = {}
        fake_ws.query_params = {"token": token}
        fake_ws.accept = _accept
        fake_ws.close = _close
        fake_ws.receive_text = _receive_text
        fake_ws.application_state = WebSocketState.CONNECTED

        await router.handle_connection(cast("object", fake_ws))  # type: ignore[arg-type]

        assert accepted is True
        assert 1008 not in closed
        assert router.client_count == 0

    @pytest.mark.asyncio
    async def test_no_origin_bad_token_rejected(self) -> None:
        """Absent Origin + bad token: falls through to auth, closed 1008."""
        router = _make_router(auth_token="expected-server-token")

        closed: list[int] = []

        async def _close(code: int = 1000) -> None:
            closed.append(code)

        fake_ws = MagicMock()
        fake_ws.headers = {}
        fake_ws.query_params = {"token": "attacker-supplied-token"}
        fake_ws.client = ("198.51.100.7", 4242)
        fake_ws.close = _close

        await router.handle_connection(cast("object", fake_ws))  # type: ignore[arg-type]

        assert closed == [1008]
        assert router.client_count == 0


class TestHandlerRegistration:
    """All handlers are registered in the router."""

    def test_all_expected_handlers_registered(self) -> None:
        router = _make_router()
        expected = {
            "synthesize",
            "chime",
            "record",
            "voices",
            "health",
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
        assert set(router.handlers.keys()) == expected
