"""Tests for punt_vox.voxd.speech_handlers -- synthesize and record handlers."""
# pyright: reportPrivateUsage=false

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from punt_vox.voxd.dedup import OnceDedup
from punt_vox.voxd.playback import PlaybackItem, PlaybackQueue
from punt_vox.voxd.speech_handlers import SynthesizeHandler
from punt_vox.voxd.synthesis import SynthesisPipeline
from punt_vox.voxd.synthesis_result import SynthesisOutcome


def _make_synthesize_handler(
    *,
    synthesis: SynthesisPipeline | None = None,
    playback: PlaybackQueue | None = None,
    once_dedup: OnceDedup | None = None,
) -> SynthesizeHandler:
    """Build a SynthesizeHandler for testing."""
    pb = playback or PlaybackQueue()
    syn = synthesis or SynthesisPipeline(playback_mutex=pb.mutex)
    od = once_dedup or OnceDedup()
    return SynthesizeHandler(synthesis=syn, playback=pb, once_dedup=od)


class TestHandleSynthesizeShortCircuit:
    """SynthesizeHandler skips try_direct_play for cloud providers."""

    def test_cloud_provider_skips_direct_play(self) -> None:
        mock_synth = MagicMock(spec=SynthesisPipeline)
        mock_synth.try_direct_play = AsyncMock(return_value=None)
        mock_synth.synthesize_to_file = AsyncMock(side_effect=RuntimeError("stop here"))
        handler = _make_synthesize_handler(synthesis=mock_synth)
        websocket = MagicMock()
        websocket.send_json = AsyncMock()
        msg: dict[str, object] = {
            "id": "1",
            "text": "hello",
            "provider": "elevenlabs",
        }

        asyncio.run(handler(msg, websocket))

        mock_synth.try_direct_play.assert_not_called()

    def test_local_provider_calls_direct_play(self) -> None:
        mock_synth = MagicMock(spec=SynthesisPipeline)
        mock_synth.try_direct_play = AsyncMock(return_value=0)
        handler = _make_synthesize_handler(synthesis=mock_synth)
        websocket = MagicMock()
        websocket.send_json = AsyncMock()
        msg: dict[str, object] = {
            "id": "2",
            "text": "hello",
            "provider": "espeak",
        }

        asyncio.run(handler(msg, websocket))

        mock_synth.try_direct_play.assert_called_once()
        call_args = mock_synth.try_direct_play.call_args
        # spec is the second positional argument
        spec = call_args[0][1]
        assert spec.provider == "espeak"


class TestSynthesizeParseGuard:
    """A malformed wire field is a clean error frame, not a torn connection."""

    @pytest.mark.asyncio
    async def test_non_string_voice_is_a_clean_error(self) -> None:
        """A non-string typed field yields an id-stamped error frame; the parse
        must not escape the handler and tear the connection down."""
        mock_synth = MagicMock(spec=SynthesisPipeline)
        mock_synth.synthesize_to_file = AsyncMock()
        handler = _make_synthesize_handler(synthesis=mock_synth)
        sent: list[dict[str, object]] = []
        ws = MagicMock()
        ws.send_json = AsyncMock(side_effect=sent.append)

        msg: dict[str, object] = {"id": "1", "text": "hello", "voice": 123}
        await handler(msg, ws)

        assert sent[-1]["type"] == "error"
        assert sent[-1]["id"] == "1"
        assert "must be a string" in str(sent[-1]["message"])
        mock_synth.synthesize_to_file.assert_not_called()

    @pytest.mark.asyncio
    async def test_non_string_text_is_a_clean_error(self) -> None:
        """A non-string required text yields an id-stamped error frame; 123 must
        not coerce to "123", synthesis must not run, and the connection stays up."""
        mock_synth = MagicMock(spec=SynthesisPipeline)
        mock_synth.synthesize_to_file = AsyncMock()
        handler = _make_synthesize_handler(synthesis=mock_synth)
        sent: list[dict[str, object]] = []
        ws = MagicMock()
        ws.send_json = AsyncMock(side_effect=sent.append)

        msg: dict[str, object] = {"id": "1", "text": 123}
        await handler(msg, ws)

        assert sent[-1]["type"] == "error"
        assert sent[-1]["id"] == "1"
        assert "text must be a string" in str(sent[-1]["message"])
        mock_synth.synthesize_to_file.assert_not_called()


class TestSynthesisFaultClassification:
    """Synthesis and direct-play failures audit as faults, not client rejections.

    A failed synthesis or a nonzero/exception direct-play exit is a server-side
    operational failure -- it reaches the client on the error frame but routes
    through WireReply.fault (ERROR "operation failed"), never WARNING "rejected
    op", since the client's request was well-formed and the daemon-side work is
    what broke. Parse rejections (TestSynthesizeParseGuard) stay errors.
    """

    @staticmethod
    def _capturing_ws() -> tuple[MagicMock, list[dict[str, object]]]:
        sent: list[dict[str, object]] = []
        ws = MagicMock()
        ws.send_json = AsyncMock(side_effect=sent.append)
        return ws, sent

    @staticmethod
    def _assert_faulted(
        caplog: pytest.LogCaptureFixture,
        sent: list[dict[str, object]],
        detail: str,
    ) -> None:
        # The wire carries only the generic verdict -- a non-OSError fault (or an
        # rc) has no relative form -- while the raw *detail* stays in the log.
        assert sent[-1]["type"] == "error"
        assert sent[-1]["message"] == "operation failed"
        assert any(
            r.levelno == logging.ERROR
            and "operation failed" in r.getMessage()
            and detail in r.getMessage()
            for r in caplog.records
        )
        assert not any("rejected op" in r.getMessage() for r in caplog.records)

    @pytest.mark.asyncio
    async def test_synthesis_failure_is_a_fault(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        mock_synth = MagicMock(spec=SynthesisPipeline)
        mock_synth.try_direct_play = AsyncMock(return_value=None)
        mock_synth.synthesize_to_file = AsyncMock(
            side_effect=RuntimeError("provider 500")
        )
        handler = _make_synthesize_handler(synthesis=mock_synth)
        ws, sent = self._capturing_ws()
        msg: dict[str, object] = {"id": "s1", "text": "hi", "provider": "elevenlabs"}
        with caplog.at_level(logging.WARNING, logger="punt_vox.voxd.wire_reply"):
            await handler(msg, ws)
        self._assert_faulted(caplog, sent, "provider 500")

    @pytest.mark.asyncio
    async def test_local_play_exception_is_a_fault(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        mock_synth = MagicMock(spec=SynthesisPipeline)
        mock_synth.try_direct_play = AsyncMock(
            return_value=RuntimeError("espeak crash")
        )
        handler = _make_synthesize_handler(synthesis=mock_synth)
        ws, sent = self._capturing_ws()
        msg: dict[str, object] = {"id": "s2", "text": "hi", "provider": "espeak"}
        with caplog.at_level(logging.WARNING, logger="punt_vox.voxd.wire_reply"):
            await handler(msg, ws)
        self._assert_faulted(caplog, sent, "espeak crash")

    @pytest.mark.asyncio
    async def test_local_play_nonzero_rc_is_a_fault(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        mock_synth = MagicMock(spec=SynthesisPipeline)
        mock_synth.try_direct_play = AsyncMock(return_value=3)
        handler = _make_synthesize_handler(synthesis=mock_synth)
        ws, sent = self._capturing_ws()
        msg: dict[str, object] = {"id": "s3", "text": "hi", "provider": "espeak"}
        with caplog.at_level(logging.WARNING, logger="punt_vox.voxd.wire_reply"):
            await handler(msg, ws)
        self._assert_faulted(caplog, sent, "rc=3")


class TestHandleSynthesizeOnceFlag:
    """Integration tests for SynthesizeHandler with the once flag."""

    @staticmethod
    def _make_stubbed_handler(
        monkeypatch: pytest.MonkeyPatch,
    ) -> tuple[SynthesizeHandler, list[str]]:
        """Build a handler with fake synthesis and instant playback."""
        synthesis_calls: list[str] = []

        async def fake_synthesize(*args: object, **_kwargs: object) -> SynthesisOutcome:
            synthesis_calls.append(str(args[0]))
            return SynthesisOutcome(path=Path("/tmp/fake.mp3"), cached=False)

        mock_synth = MagicMock(spec=SynthesisPipeline)
        mock_synth.synthesize_to_file = fake_synthesize

        monkeypatch.setattr(
            "punt_vox.voxd.speech_handlers._LOCAL_PROVIDERS", set[str]()
        )
        # Provider is now required on the wire (design §3.7): tests must
        # send it, so the daemon-side ``auto_detect_provider`` monkeypatch
        # this fixture used to install is gone with the function itself.

        handler = _make_synthesize_handler(synthesis=mock_synth)

        class _InstantPlaybackQueue:
            async def put(self, item: PlaybackItem) -> None:
                item.notify.set()

        handler._playback._queue = _InstantPlaybackQueue()  # type: ignore[assignment]
        return handler, synthesis_calls

    @pytest.mark.asyncio
    async def test_once_null_does_not_dedupe(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Without once, identical requests both proceed (regression)."""
        handler, synthesis_calls = self._make_stubbed_handler(monkeypatch)
        ws = MagicMock()
        ws.send_json = AsyncMock()

        msg: dict[str, object] = {
            "type": "synthesize",
            "id": "a",
            "text": "hello",
            "provider": "elevenlabs",
        }
        await handler(msg, ws)
        msg2: dict[str, object] = {
            "type": "synthesize",
            "id": "b",
            "text": "hello",
            "provider": "elevenlabs",
        }
        await handler(msg2, ws)

        assert len(synthesis_calls) == 2

    @pytest.mark.asyncio
    async def test_once_set_dedups_identical_text(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """With once=600, the second identical request returns deduped."""
        handler, synthesis_calls = self._make_stubbed_handler(monkeypatch)
        ws = MagicMock()
        ws.send_json = AsyncMock()

        msg: dict[str, object] = {
            "type": "synthesize",
            "id": "a",
            "text": "wall msg",
            "provider": "elevenlabs",
            "once": 600,
        }
        await handler(msg, ws)
        msg2: dict[str, object] = {
            "type": "synthesize",
            "id": "b",
            "text": "wall msg",
            "provider": "elevenlabs",
            "once": 600,
        }
        await handler(msg2, ws)

        assert len(synthesis_calls) == 1

        all_calls = ws.send_json.call_args_list
        sent_msgs = [c[0][0] for c in all_calls]
        deduped_msgs = [m for m in sent_msgs if m.get("deduped") is True]
        assert len(deduped_msgs) == 1
        deduped = deduped_msgs[0]
        assert deduped["id"] == "b"
        assert deduped["type"] == "done"
        assert "original_played_at" in deduped
        assert "ttl_seconds_remaining" in deduped
        assert deduped["ttl_seconds_remaining"] > 0

    @pytest.mark.asyncio
    async def test_once_zero_does_not_dedupe(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """once=0 is treated as null per the spec -- must not dedupe."""
        handler, synthesis_calls = self._make_stubbed_handler(monkeypatch)
        ws = MagicMock()
        ws.send_json = AsyncMock()

        msg: dict[str, object] = {
            "type": "synthesize",
            "id": "a",
            "text": "hello",
            "provider": "elevenlabs",
            "once": 0,
        }
        await handler(msg, ws)
        msg2: dict[str, object] = {
            "type": "synthesize",
            "id": "b",
            "text": "hello",
            "provider": "elevenlabs",
            "once": 0,
        }
        await handler(msg2, ws)

        assert len(synthesis_calls) == 2


class TestHandleSynthesizeCachedSignal:
    """SynthesizeHandler rides the cache hit/miss flag on the 'playing' response."""

    @staticmethod
    def _drive_with_cached(
        monkeypatch: pytest.MonkeyPatch,
        *,
        cached: bool,
    ) -> list[dict[str, object]]:
        """Run the handler once with a stubbed outcome; return sent messages."""

        async def fake_synthesize(*_a: object, **_k: object) -> SynthesisOutcome:
            return SynthesisOutcome(path=Path("/tmp/fake.mp3"), cached=cached)

        mock_synth = MagicMock(spec=SynthesisPipeline)
        mock_synth.synthesize_to_file = fake_synthesize
        monkeypatch.setattr(
            "punt_vox.voxd.speech_handlers._LOCAL_PROVIDERS", set[str]()
        )
        # Provider is now required on the wire (design §3.7): tests must
        # send it, so the daemon-side ``auto_detect_provider`` monkeypatch
        # this fixture used to install is gone with the function itself.
        handler = _make_synthesize_handler(synthesis=mock_synth)

        class _InstantPlaybackQueue:
            async def put(self, item: PlaybackItem) -> None:
                item.notify.set()

        handler._playback._queue = _InstantPlaybackQueue()  # type: ignore[assignment]

        ws = MagicMock()
        ws.send_json = AsyncMock()
        asyncio.run(
            handler(
                {
                    "type": "synthesize",
                    "id": "x",
                    "text": "hi",
                    "provider": "elevenlabs",
                },
                ws,
            )
        )
        return [call[0][0] for call in ws.send_json.call_args_list]

    def test_playing_reports_cache_hit(self, monkeypatch: pytest.MonkeyPatch) -> None:
        sent = self._drive_with_cached(monkeypatch, cached=True)
        playing = [m for m in sent if m.get("type") == "playing"]
        assert len(playing) == 1
        assert playing[0]["cached"] is True

    def test_playing_reports_cache_miss(self, monkeypatch: pytest.MonkeyPatch) -> None:
        sent = self._drive_with_cached(monkeypatch, cached=False)
        playing = [m for m in sent if m.get("type") == "playing"]
        assert len(playing) == 1
        assert playing[0]["cached"] is False


class TestDedupHitLogIsInjectionSafe:
    """The dedup-hit sink escapes the raw client id so it cannot forge a line."""

    @pytest.mark.asyncio
    async def test_newline_in_id_cannot_forge_a_log_line(
        self,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """A newline in the client ``id`` renders as ``\\n`` at the dedup sink.

        ``request_id`` is a raw wire field; logged with ``%r`` an embedded
        newline stays inside quotes, so the "Dedup hit" record is a single
        physical line and cannot smuggle a forged second entry.
        """
        handler, _ = TestHandleSynthesizeOnceFlag._make_stubbed_handler(monkeypatch)
        ws = MagicMock()
        ws.send_json = AsyncMock()

        first: dict[str, object] = {
            "type": "synthesize",
            "id": "a",
            "text": "hi",
            "provider": "elevenlabs",
            "once": 600,
        }
        forged: dict[str, object] = {
            "type": "synthesize",
            "id": "b\nFATAL forged entry",
            "text": "hi",
            "provider": "elevenlabs",
            "once": 600,
        }
        await handler(first, ws)
        with caplog.at_level(logging.INFO, logger="punt_vox.voxd"):
            await handler(forged, ws)

        hits = [r for r in caplog.records if "Dedup hit" in r.getMessage()]
        assert len(hits) == 1
        message = hits[0].getMessage()
        assert "\n" not in message, "the id newline must be escaped, not raw"
        assert "\\n" in message  # rendered visibly by %r
        assert message.splitlines() == [message]  # exactly one physical line
