"""Tests for punt_vox.voxd.system_handlers -- the chime handler's INFO budget."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from pathlib import Path
from typing import TYPE_CHECKING, cast

import pytest
from starlette.websockets import WebSocketDisconnect

from punt_vox.voxd.chimes import ChimeResolver
from punt_vox.voxd.dedup import ChimeDedup
from punt_vox.voxd.playback import PlaybackQueue
from punt_vox.voxd.system_handlers import ChimeHandler, VoicesHandler

if TYPE_CHECKING:
    from collections.abc import Iterator

    from starlette.websockets import WebSocket


class _CollectingWs:
    """A fake websocket that records the frames the handler sends."""

    def __init__(self) -> None:
        self.sent: list[dict[str, object]] = []

    async def send_json(self, data: dict[str, object]) -> None:
        self.sent.append(data)


class TestChimeInfoBudget:
    """A chime emits exactly one INFO line across the whole path."""

    @pytest.fixture
    def _silent_playback(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> Iterator[PlaybackQueue]:
        """A PlaybackQueue whose consumer runs but plays no real audio."""

        async def _noop(_self: PlaybackQueue, _path: Path) -> None:
            return None

        monkeypatch.setattr(PlaybackQueue, "play_audio", _noop)
        yield PlaybackQueue()

    @pytest.mark.asyncio
    async def test_chime_emits_single_info(
        self, _silent_playback: PlaybackQueue, caplog: pytest.LogCaptureFixture
    ) -> None:
        pb = _silent_playback
        handler = ChimeHandler(
            chimes=ChimeResolver(), chime_dedup=ChimeDedup(), playback=pb
        )
        consumer = asyncio.create_task(pb.consumer())
        ws = _CollectingWs()
        try:
            with caplog.at_level(logging.INFO, logger="punt_vox.voxd"):
                await asyncio.wait_for(
                    handler({"signal": "done"}, cast("WebSocket", ws)), timeout=5.0
                )
        finally:
            consumer.cancel()
            # Await the cancellation so the loop reaps the task -- no leaked
            # "Task was destroyed but it is pending" warning.
            with contextlib.suppress(asyncio.CancelledError):
                await consumer

        infos = [
            r
            for r in caplog.records
            if r.levelno == logging.INFO and r.name == "punt_vox.voxd.system_handlers"
        ]
        assert len(infos) == 1
        assert infos[0].getMessage() == "played chime: done"

    @pytest.mark.asyncio
    async def test_deduped_chime_logs_no_info(
        self, _silent_playback: PlaybackQueue, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A duplicate chime is a DEBUG no-op, not an INFO line."""
        pb = _silent_playback
        dedup = ChimeDedup()
        dedup.should_play("done")  # first call arms the dedup window
        handler = ChimeHandler(chimes=ChimeResolver(), chime_dedup=dedup, playback=pb)
        ws = _CollectingWs()
        with caplog.at_level(logging.INFO, logger="punt_vox.voxd"):
            await handler({"signal": "done"}, cast("WebSocket", ws))

        infos = [
            r
            for r in caplog.records
            if r.levelno == logging.INFO and r.name == "punt_vox.voxd.system_handlers"
        ]
        assert infos == []  # deduped -> DEBUG only
        assert ws.sent == [{"type": "done", "id": ""}]


class TestVoicesHandler:
    """The voices handler rejects a malformed provider without tearing the socket."""

    @pytest.mark.asyncio
    async def test_non_string_provider_is_a_rejected_op(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A non-string ``provider`` is a rejected client request, not a fault.

        The ValueError from the parse is caught and replied as an id-stamped error
        frame -- the parse must not escape the handler and tear the connection
        down -- and it audits at WARNING "rejected op", never ERROR "operation
        failed", because the client sent a bad request, not the daemon failing.
        """
        ws = _CollectingWs()
        with caplog.at_level(logging.WARNING, logger="punt_vox.voxd.wire_reply"):
            await VoicesHandler()({"id": "v1", "provider": 123}, cast("WebSocket", ws))

        assert ws.sent[-1]["type"] == "error"
        assert ws.sent[-1]["id"] == "v1"
        assert "must be a string" in str(ws.sent[-1]["message"])
        assert any("rejected op" in r.getMessage() for r in caplog.records)
        assert not any("operation failed" in r.getMessage() for r in caplog.records)

    @pytest.mark.asyncio
    async def test_operational_failure_is_a_fault_not_a_rejection(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """An OSError resolving the provider is a server fault, not a rejected op.

        A provider that cannot be resolved or listed (an I/O or lookup fault) is a
        server-side failure, so the reply routes through WireReply.fault -- an
        id-stamped clean error frame the client can read, audited at ERROR
        "operation failed", never WARNING "rejected op" (which would blame the
        client). Reaching the assertions without a raise is the connection-intact
        check.
        """

        def boom(*_args: object, **_kwargs: object) -> object:
            raise OSError("voice service unreachable")

        monkeypatch.setattr("punt_vox.voxd.system_handlers.get_provider", boom)
        ws = _CollectingWs()
        with caplog.at_level(logging.WARNING, logger="punt_vox.voxd.wire_reply"):
            await VoicesHandler()(
                {"id": "v2", "provider": "polly"}, cast("WebSocket", ws)
            )

        assert ws.sent[-1]["type"] == "error"
        assert ws.sent[-1]["id"] == "v2"
        assert "voice service unreachable" in str(ws.sent[-1]["message"])
        assert any(
            r.levelno == logging.ERROR and "operation failed" in r.getMessage()
            for r in caplog.records
        )
        assert not any("rejected op" in r.getMessage() for r in caplog.records)

    @pytest.mark.asyncio
    async def test_gone_peer_during_send_does_not_raise(self) -> None:
        """A peer gone while the handler replies is absorbed by WireReply.

        The reply routes through WireReply's disconnect-safe send, so a socket
        that raises WebSocketDisconnect mid-reply yields a clean no-op instead of
        a traceback escaping the handler (which would tear the connection down).
        Reaching the end of the call without an exception is the assertion.
        """

        class _GoneWs:
            async def send_json(self, _payload: dict[str, object]) -> None:
                raise WebSocketDisconnect(code=1006)

        # A non-string provider drives the error reply; the gone peer must not
        # turn that reply into a raise escaping the handler.
        await VoicesHandler()(
            {"id": "v1", "provider": 123}, cast("WebSocket", _GoneWs())
        )
