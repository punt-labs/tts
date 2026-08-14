"""Tests for punt_vox.voxd.speech_handlers -- synthesize and record handlers."""
# pyright: reportPrivateUsage=false

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from punt_vox.types_errors import VoiceNotFoundError
from punt_vox.types_provider_errors import (
    ProviderAuthError,
    ProviderUnavailableError,
    UnknownProviderError,
)
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


class TestSynthesizeHandlerTypedErrorRouting:
    """The F2/F3/F5 wire promise: diagnosable errors cross verbatim through
    :meth:`WireReply.error`, never through :meth:`WireReply.fault` (which
    launders the message to ``"operation failed"``).

    Each test drives the real ``SynthesizeHandler`` end-to-end -- parsing
    the wire message, entering the synthesis path, catching the typed
    error, sending the frame -- and asserts BOTH halves: the sentence
    reaches the peer, AND it arrives as an ``error`` frame with an
    ``id`` (the WARNING ``rejected op`` audit shape), never as a
    ``fault`` frame carrying ``"operation failed"``.

    The tests exist because catching a type in isolation and testing
    the type in isolation both pass while nothing actually raises it --
    the exact defect the review found in this bead's first round: three
    honest artifacts (the catch clause, the test suite, the commit
    message) each described a wired path that had no raise site behind
    it. Handler-level tests close the gap.
    """

    @staticmethod
    def _handler_that_raises(
        exc: Exception,
    ) -> tuple[SynthesizeHandler, list[dict[str, object]]]:
        """Build a SynthesizeHandler whose synthesize_to_file raises *exc*."""
        sent: list[dict[str, object]] = []

        async def _raising(*_a: object, **_k: object) -> SynthesisOutcome:
            raise exc

        mock_synth = MagicMock(spec=SynthesisPipeline)
        mock_synth.synthesize_to_file = _raising
        handler = _make_synthesize_handler(synthesis=mock_synth)
        return handler, sent

    @staticmethod
    async def _drive(
        handler: SynthesizeHandler,
    ) -> list[dict[str, object]]:
        """Send one wire message through *handler*; return the frames sent."""
        ws = MagicMock()
        sent: list[dict[str, object]] = []

        async def _capture(frame: dict[str, object]) -> None:
            sent.append(frame)

        ws.send_json = _capture
        await handler(
            {
                "type": "synthesize",
                "id": "req-1",
                "text": "hi",
                "provider": "elevenlabs",
            },
            ws,
        )
        return sent

    @pytest.mark.asyncio
    async def test_f2_provider_unavailable_crosses_verbatim_via_error(self) -> None:
        # F2: the daemon has no credentials for the named provider.
        # Wire frame must be type=error carrying the message verbatim,
        # NOT type=fault carrying "operation failed" -- the whole point
        # of the taxonomy is that a diagnosable failure reaches the
        # caller with a message they can act on.
        detail = (
            "provider 'polly' is configured but voxd has no AWS credentials "
            "(AWS_PROFILE, or AWS_ACCESS_KEY_ID + AWS_SECRET_ACCESS_KEY); "
            "run `vox doctor`"
        )
        handler, _ = self._handler_that_raises(
            ProviderUnavailableError("polly", detail)
        )

        sent = await self._drive(handler)

        assert sent, "expected at least one frame on the wire"
        assert sent[-1]["type"] == "error"
        assert sent[-1]["message"] == detail
        assert not any(
            f.get("type") == "fault" or f.get("message") == "operation failed"
            for f in sent
        ), "F2 must never route through fault()"

    @pytest.mark.asyncio
    async def test_f3_provider_auth_crosses_verbatim_via_error(self) -> None:
        # F3: the credentials were present at the readiness check but
        # the provider rejected them at the SDK call. Same routing
        # promise as F2: through error(), not fault().
        handler, _ = self._handler_that_raises(ProviderAuthError("elevenlabs", 401))

        sent = await self._drive(handler)

        assert sent[-1]["type"] == "error"
        assert sent[-1]["message"] == (
            "provider 'elevenlabs' rejected the credentials (HTTP 401); "
            "run `vox doctor`"
        )
        assert not any(
            f.get("type") == "fault" or f.get("message") == "operation failed"
            for f in sent
        ), "F3 must never route through fault()"

    @pytest.mark.asyncio
    async def test_f4_unknown_provider_crosses_verbatim_via_error(self) -> None:
        # F4: a hand-edited ``vox.md`` naming ``provider: ploly``. The
        # registry raises ``UnknownProviderError`` (a ``ValueError``
        # subclass, so ``WireReply.reject_or_fault`` on the voices path
        # renders it verbatim). The synthesize handler catches it in
        # the same tuple as F2/F3/F5 -- widening to plain ValueError
        # would swallow genuine daemon-side bugs, which is why the
        # typed class exists. Without this catch, F4 falls through to
        # the broad except and reaches the caller as "operation
        # failed" -- the F3 twin the review round-2 caught.
        handler, _ = self._handler_that_raises(
            UnknownProviderError("ploly", ["elevenlabs", "openai", "polly"])
        )

        sent = await self._drive(handler)

        assert sent[-1]["type"] == "error"
        assert sent[-1]["message"] == (
            "Unknown provider 'ploly'. Available: elevenlabs, openai, polly"
        )
        assert not any(
            f.get("type") == "fault" or f.get("message") == "operation failed"
            for f in sent
        ), "F4 must never route through fault()"

    @pytest.mark.asyncio
    async def test_f5_voice_not_found_crosses_verbatim_via_error(self) -> None:
        # F5: the bead's ORIGINALLY reported incident. Before this PR
        # the path produced "operation failed" instead of the voice
        # list; this test pins that the __str__ override PR 1 added
        # plus the pre-broad-guard catch this PR adds together make
        # the sentence cross the wire.
        handler, _ = self._handler_that_raises(
            VoiceNotFoundError("bella", ["alloy", "ash", "ballad"])
        )

        sent = await self._drive(handler)

        assert sent[-1]["type"] == "error"
        assert sent[-1]["message"] == "bella (available: alloy, ash, ballad)"
        assert not any(
            f.get("type") == "fault" or f.get("message") == "operation failed"
            for f in sent
        ), "F5 must never route through fault()"

    @pytest.mark.asyncio
    async def test_reject_log_escapes_newlines_in_caller_supplied_strings(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        # The reject-path WARNING interpolates the exception message
        # through ``%r`` on ``str(exc)`` so a caller-supplied string
        # in the message (a voice name that carries a newline, a
        # hostile provider name in a hand-edited vox.md) cannot forge
        # a second audit line. Same discipline
        # AwsRequirement.satisfied uses on the boto3 exception it
        # swallows, and the shape this test pins so a future change
        # cannot regress it back to ``%s``.
        #
        # VoiceNotFoundError renders the voice name verbatim into
        # str(exc), so a forged newline in the voice name would land
        # directly at the log sink without the ``%r`` escape.
        handler, _ = self._handler_that_raises(
            VoiceNotFoundError("b\nFATAL forged audit line", ["alloy"])
        )

        with caplog.at_level(logging.WARNING, logger="punt_vox.voxd"):
            await self._drive(handler)

        rejected = [r for r in caplog.records if "Rejected synthesis" in r.getMessage()]
        assert len(rejected) == 1
        message = rejected[0].getMessage()
        # The raw newline is escaped as ``\n`` inside the quoted repr;
        # never a literal newline character at the log sink.
        assert "\n" not in message
        assert "\\n" in message
        assert message.splitlines() == [message]

    @pytest.mark.asyncio
    async def test_unexpected_exception_still_routes_through_fault(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        # A genuine daemon-side bug (a KeyError deep in the pipeline, a
        # RuntimeError from an SDK we did not classify as auth) is not
        # a caller-side rejection and must still land on ``WireReply.fault``
        # with the laundered "operation failed" message -- the SafeFault
        # path exists to keep host paths off the wire. Both frame shapes
        # are ``type=error`` on the wire (that is what WireReply sends);
        # the caller-visible distinction is the ERROR audit line
        # containing "operation failed", NOT a WARNING "rejected op".
        # Asserting on the log line is the honest test: the message on
        # the wire is what the caller sees, and this asserts the wire
        # message did NOT preserve the internal detail.
        handler, _ = self._handler_that_raises(RuntimeError("unclassified"))

        with caplog.at_level(logging.ERROR, logger="punt_vox.voxd.wire_reply"):
            sent = await self._drive(handler)

        assert sent[-1]["type"] == "error"
        assert sent[-1]["message"] == "operation failed"
        # The wire message is laundered; the raw detail stays in the log.
        # Never a "rejected op" audit -- that would blame the client for
        # a daemon-side fault.
        assert any(
            r.levelno == logging.ERROR and "operation failed" in r.getMessage()
            for r in caplog.records
        )
        assert not any("rejected op" in r.getMessage() for r in caplog.records)
