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
from punt_vox.voxd.health import DaemonHealth
from punt_vox.voxd.playback import PlaybackQueue
from punt_vox.voxd.system_handlers import (
    ChimeHandler,
    HealthHandler,
    VoicesHandler,
)

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


class _GoneWs:
    """A websocket whose every send raises as though the peer had disconnected."""

    async def send_json(self, _payload: dict[str, object]) -> None:
        raise WebSocketDisconnect(code=1006)


class TestChimeReply:
    """The chime handler replies through WireReply -- classified, gone-peer-safe."""

    @pytest.mark.asyncio
    async def test_unknown_chime_is_a_rejected_error_frame(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """An unknown chime signal returns an id-stamped error frame, audited.

        Routed through WireReply.error, it audits at WARNING "rejected op" (the
        client asked for a chime that does not exist) and never enqueues.
        """
        handler = ChimeHandler(
            chimes=ChimeResolver(), chime_dedup=ChimeDedup(), playback=PlaybackQueue()
        )
        ws = _CollectingWs()
        with caplog.at_level(logging.WARNING, logger="punt_vox.voxd.wire_reply"):
            await handler(
                {"id": "c9", "signal": "no-such-chime"}, cast("WebSocket", ws)
            )

        assert ws.sent[-1]["type"] == "error"
        assert ws.sent[-1]["id"] == "c9"
        assert "unknown chime" in str(ws.sent[-1]["message"])
        assert any("rejected op" in r.getMessage() for r in caplog.records)

    @pytest.mark.asyncio
    async def test_gone_peer_during_reply_does_not_raise(self) -> None:
        """A peer gone while the handler replies is absorbed by WireReply.

        An unknown chime drives the reply; the disconnect-safe send turns the
        gone peer into a clean no-op instead of a raw send_json raising into the
        router's broad guard. Reaching the end without an exception is the check.
        """
        await ChimeHandler(
            chimes=ChimeResolver(), chime_dedup=ChimeDedup(), playback=PlaybackQueue()
        )({"id": "c1", "signal": "no-such-chime"}, cast("WebSocket", _GoneWs()))

    @pytest.mark.asyncio
    async def test_chime_enqueues_and_survives_gone_peer(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A known chime still enqueues even when the peer has gone mid-reply.

        The playback item is queued before the 'playing'/'done' frames, so a
        disconnected peer must not stop the chime from playing -- the gone-peer
        send is a no-op, and the enqueued item still reaches the consumer.
        """
        played: list[Path] = []

        async def _record(_self: PlaybackQueue, path: Path) -> None:
            played.append(path)

        monkeypatch.setattr(PlaybackQueue, "play_audio", _record)
        pb = PlaybackQueue()
        consumer = asyncio.create_task(pb.consumer())
        try:
            await asyncio.wait_for(
                ChimeHandler(
                    chimes=ChimeResolver(), chime_dedup=ChimeDedup(), playback=pb
                )({"signal": "done"}, cast("WebSocket", _GoneWs())),
                timeout=5.0,
            )
        finally:
            consumer.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await consumer

        assert played  # the chime reached playback despite the gone peer


class TestHealthReply:
    """The health handler replies through WireReply -- gone-peer-safe."""

    @pytest.mark.asyncio
    async def test_health_reports_through_wire_reply(self) -> None:
        """A health request returns an id-stamped health payload."""
        health = DaemonHealth(PlaybackQueue(), lambda: 0, 0)
        ws = _CollectingWs()
        await HealthHandler(health=health)({"id": "h1"}, cast("WebSocket", ws))

        assert ws.sent[-1]["type"] == "health"
        assert ws.sent[-1]["id"] == "h1"

    @pytest.mark.asyncio
    async def test_gone_peer_during_health_send_does_not_raise(self) -> None:
        """A peer gone while health replies is absorbed instead of tearing down.

        Reaching the end of the call without an exception is the assertion.
        """
        health = DaemonHealth(PlaybackQueue(), lambda: 0, 0)
        await HealthHandler(health=health)({"id": "h2"}, cast("WebSocket", _GoneWs()))


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
        # VoicesHandler catches the OSError in its own (ValueError, LookupError,
        # OSError) clause and replies via reject_or_fault -> WireReply.fault as a
        # SafeFault; it never escapes to the router's broad-except. The OSError has
        # no in-jail filename, so the wire carries the generic verdict while the
        # raw "voice service unreachable" stays in the log.
        assert ws.sent[-1]["message"] == "operation failed"
        assert any(
            r.levelno == logging.ERROR and "operation failed" in r.getMessage()
            for r in caplog.records
        )
        assert not any("rejected op" in r.getMessage() for r in caplog.records)

    @pytest.mark.asyncio
    async def test_unexpected_provider_exception_is_a_fault_not_a_teardown(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A provider exception outside the trio is caught, faulted, and logged.

        A vendor SDK can raise outside ValueError/LookupError/OSError (boto3's
        ClientError, an ElevenLabs client error). The router awaits this handler
        with no guard of its own, so the broad boundary catch must convert the
        escape into an id-stamped fault frame -- connection intact -- and log the
        traceback, never let it propagate and tear the shared socket down.
        Reaching the assertions without a raise is the connection-intact check.
        The wire message stays generic; the vendor detail lives in the log only.
        """

        def boom(*_args: object, **_kwargs: object) -> object:
            raise RuntimeError("unexpected vendor failure")

        monkeypatch.setattr("punt_vox.voxd.system_handlers.get_provider", boom)
        ws = _CollectingWs()
        with caplog.at_level(logging.ERROR, logger="punt_vox.voxd"):
            await VoicesHandler()(
                {"id": "v3", "provider": "polly"}, cast("WebSocket", ws)
            )

        assert ws.sent[-1]["type"] == "error"
        assert ws.sent[-1]["id"] == "v3"
        # Generic on the wire -- the RuntimeError text never leaks to the client.
        assert ws.sent[-1]["message"] == "operation failed"
        # The full traceback is logged for the audit trail (logger.exception),
        # and the cause is never silently lost -- the exception detail is attached.
        assert any(
            r.levelno == logging.ERROR
            and r.name == "punt_vox.voxd.system_handlers"
            and r.exc_info is not None
            and "unexpected vendor failure" in str(r.exc_info[1])
            for r in caplog.records
        )
        # ...and it audits as an operational fault, never a client rejection.
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

    @pytest.mark.asyncio
    async def test_missing_provider_field_is_a_rejected_op(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A wire message with no ``provider`` field is a rejected client op.

        State is the sole authority on which provider voxd runs (design
        §3.7); the parse for ``provider`` is ``parse_required_str`` and
        a missing field must not fall through to a daemon guess. The
        rejection audits as WARNING "rejected op", not ERROR "operation
        failed" -- the client sent an incomplete request, and the
        message names the missing field.
        """
        ws = _CollectingWs()
        with caplog.at_level(logging.WARNING, logger="punt_vox.voxd.wire_reply"):
            await VoicesHandler()({"id": "v0"}, cast("WebSocket", ws))

        assert ws.sent[-1]["type"] == "error"
        assert ws.sent[-1]["id"] == "v0"
        assert "provider" in str(ws.sent[-1]["message"])
        assert any("rejected op" in r.getMessage() for r in caplog.records)

    @pytest.mark.asyncio
    async def test_uncredentialed_provider_crosses_verbatim_via_error(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """The F2 wire promise on the voices op.

        When ``get_provider`` raises :class:`ProviderUnavailableError`
        (the daemon has no credentials for a known provider), the
        message must reach the client verbatim through
        ``WireReply.error`` -- NOT through fault() and NOT laundered
        to "operation failed". Same shape ``mic:unmute`` gets for the
        same underlying condition, so ``mic:voice`` and ``mic:unmute``
        return one text for one fact.

        This test would have caught the round-1 defect where the type
        was defined and caught but never raised: driving a real
        ``ProviderUnavailableError`` through the handler ensures the
        end-to-end path works, not just its pieces.
        """
        from punt_vox.types_provider_errors import ProviderUnavailableError

        detail = (
            "provider 'polly' is configured but voxd has no AWS credentials "
            "(AWS_PROFILE, or AWS_ACCESS_KEY_ID + AWS_SECRET_ACCESS_KEY); "
            "run `vox doctor`"
        )

        def boom(*_args: object, **_kwargs: object) -> object:
            raise ProviderUnavailableError("polly", detail)

        monkeypatch.setattr("punt_vox.voxd.system_handlers.get_provider", boom)
        ws = _CollectingWs()
        with caplog.at_level(logging.WARNING, logger="punt_vox.voxd.wire_reply"):
            await VoicesHandler()(
                {"id": "vf2", "provider": "polly"}, cast("WebSocket", ws)
            )

        assert ws.sent[-1]["type"] == "error"
        assert ws.sent[-1]["id"] == "vf2"
        assert ws.sent[-1]["message"] == detail
        # Never through fault() / "operation failed" for this class.
        assert not any("operation failed" in r.getMessage() for r in caplog.records)
        assert any("rejected op" in r.getMessage() for r in caplog.records)

    @pytest.mark.asyncio
    async def test_voice_not_found_crosses_verbatim_via_error(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """F5 on the voices op: an unknown-voice error crosses verbatim.

        The bead's originally reported incident was ``bella`` (an
        ElevenLabs voice) reaching the OpenAI provider and coming back
        as "operation failed". The synthesize path catches this
        explicitly; ``VoicesHandler`` catches it via the ValueError
        branch of ``reject_or_fault``. Either way the sentence must
        cross the wire.
        """
        from punt_vox.types_errors import VoiceNotFoundError

        def list_voices() -> list[str]:
            raise VoiceNotFoundError("bella", ["alloy", "ash", "ballad"])

        provider = type(
            "_StubProvider", (), {"list_voices": staticmethod(list_voices)}
        )()

        def factory(*_args: object, **_kwargs: object) -> object:
            return provider

        monkeypatch.setattr("punt_vox.voxd.system_handlers.get_provider", factory)
        ws = _CollectingWs()
        with caplog.at_level(logging.WARNING, logger="punt_vox.voxd.wire_reply"):
            await VoicesHandler()(
                {"id": "vf5", "provider": "openai"}, cast("WebSocket", ws)
            )

        assert ws.sent[-1]["type"] == "error"
        assert ws.sent[-1]["id"] == "vf5"
        assert ws.sent[-1]["message"] == "bella (available: alloy, ash, ballad)"
        assert not any("operation failed" in r.getMessage() for r in caplog.records)
