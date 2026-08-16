"""Tests for punt_vox.voxd.synthesis -- SynthesisPipeline and helpers."""
# pyright: reportPrivateUsage=false

from __future__ import annotations

import asyncio
import logging
import os
import tempfile
from collections.abc import Callable
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from conftest import _get_valid_mp3_bytes  # pyright: ignore[reportPrivateUsage]

from punt_vox.types_synthesis import SynthesisSpec
from punt_vox.voxd.playback import PlaybackQueue
from punt_vox.voxd.synthesis import SynthesisPipeline
from punt_vox.voxd.synthesis_result import SynthesisOutcome


def _default_spec(**overrides: object) -> SynthesisSpec:
    """Build a SynthesisSpec with sensible test defaults."""
    defaults: dict[str, object] = {
        "voice": None,
        "provider": "espeak",
        "model": None,
        "language": None,
        "rate": None,
        "vibe_tags": None,
        "stability": None,
        "similarity": None,
        "style": None,
        "speaker_boost": None,
        "api_key": None,
    }
    defaults.update(overrides)
    return SynthesisSpec(**defaults)  # type: ignore[arg-type]


def _make_pipeline() -> SynthesisPipeline:
    """Build a SynthesisPipeline with a fresh PlaybackQueue mutex."""
    return SynthesisPipeline(playback_mutex=PlaybackQueue().mutex)


def _record_result(results: list[dict[str, object]]) -> Callable[..., None]:
    """Return a record_result callback that appends to results."""
    import time

    def _record(
        *,
        path: Path,
        rc: int,
        elapsed: float,
        stderr: str,
    ) -> None:
        results.append(
            {
                "file": str(path),
                "rc": rc,
                "elapsed_s": round(elapsed, 4),
                "stderr": stderr,
                "ts": time.time(),
            }
        )

    return _record


class TestTryDirectPlay:
    """Voxd dispatches to provider.play_directly for local providers."""

    def _run(
        self,
        provider: MagicMock,
        pipeline: SynthesisPipeline,
        results: list[dict[str, object]],
    ) -> int | Exception | None:
        with patch("punt_vox.voxd.synthesis.get_provider", return_value=provider):
            return asyncio.run(
                pipeline.try_direct_play(
                    "hello",
                    _default_spec(),
                    record_result=_record_result(results),
                )
            )

    def test_returns_provider_rc_on_success(self) -> None:
        pipeline = _make_pipeline()
        results: list[dict[str, object]] = []
        provider = MagicMock()
        provider.play_directly = MagicMock(return_value=0)

        rc = self._run(provider, pipeline, results)

        assert rc == 0
        assert len(results) == 1
        assert results[0]["rc"] == 0
        provider.play_directly.assert_called_once()

    def test_returns_none_for_cloud_provider(self) -> None:
        """A provider lacking play_directly opts out of the direct-play path."""
        pipeline = _make_pipeline()
        results: list[dict[str, object]] = []
        provider = MagicMock(spec=["name", "synthesize"])

        rc = self._run(provider, pipeline, results)

        assert rc is None
        assert len(results) == 0

    def test_nonzero_rc_logs_error(self, caplog: pytest.LogCaptureFixture) -> None:
        pipeline = _make_pipeline()
        results: list[dict[str, object]] = []
        provider = MagicMock()
        provider.play_directly = MagicMock(return_value=2)

        with caplog.at_level(logging.ERROR, logger="punt_vox.voxd"):
            rc = self._run(provider, pipeline, results)

        assert rc == 2
        assert "Direct-play FAILED" in caplog.text
        assert len(results) == 1
        assert results[0]["rc"] == 2

    def test_get_provider_exception_returned(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Provider construction failure surfaces as an Exception, not a crash."""
        pipeline = _make_pipeline()
        results: list[dict[str, object]] = []

        with (
            caplog.at_level(logging.ERROR, logger="punt_vox.voxd"),
            patch(
                "punt_vox.voxd.synthesis.get_provider",
                side_effect=ValueError("unknown provider"),
            ),
        ):
            result = asyncio.run(
                pipeline.try_direct_play(
                    "hello",
                    _default_spec(),
                    record_result=_record_result(results),
                )
            )

        assert isinstance(result, ValueError)
        assert "unknown provider" in str(result)
        assert "Direct-play raised" in caplog.text

    def test_newline_in_provider_cannot_forge_a_log_line(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A newline in the raw provider field is escaped at the raised sink.

        ``provider`` is a client-controlled wire value; logged with ``%r`` an
        embedded newline renders as ``\\n`` inside quotes, so the record stays a
        single physical line and cannot smuggle a forged second entry.
        """
        pipeline = _make_pipeline()
        results: list[dict[str, object]] = []
        spec = _default_spec(provider="espeak\nFATAL forged entry")

        with (
            caplog.at_level(logging.ERROR, logger="punt_vox.voxd"),
            patch(
                "punt_vox.voxd.synthesis.get_provider",
                side_effect=ValueError("boom"),
            ),
        ):
            asyncio.run(
                pipeline.try_direct_play(
                    "hello", spec, record_result=_record_result(results)
                )
            )

        raised = [r for r in caplog.records if "Direct-play raised" in r.getMessage()]
        assert len(raised) == 1
        message = raised[0].getMessage()
        assert "\n" not in message, "the provider newline must be escaped, not raw"
        assert "\\n" in message  # rendered visibly by %r
        assert message.splitlines() == [message]  # exactly one physical line

    def test_no_api_key_skips_env_lock(self) -> None:
        """Local providers without an API key must not block on _env_lock."""
        pipeline = _make_pipeline()
        results: list[dict[str, object]] = []
        provider = MagicMock()
        provider.play_directly = MagicMock(return_value=0)

        sentinel_lock = MagicMock(wraps=asyncio.Lock())
        sentinel_lock.__aenter__ = AsyncMock()
        sentinel_lock.__aexit__ = AsyncMock(return_value=None)

        with patch("punt_vox.voxd.synthesis.get_provider", return_value=provider):
            old_lock = pipeline._env_lock
            pipeline._env_lock = sentinel_lock
            try:
                asyncio.run(
                    pipeline.try_direct_play(
                        "hello",
                        _default_spec(),
                        record_result=_record_result(results),
                    )
                )
            finally:
                pipeline._env_lock = old_lock

        sentinel_lock.__aenter__.assert_not_called()

    def test_api_key_acquires_env_lock_for_cloud_provider(self) -> None:
        """API-key path must serialize via _env_lock to protect os.environ."""
        pipeline = _make_pipeline()
        results: list[dict[str, object]] = []
        provider = MagicMock()
        provider.play_directly = MagicMock(return_value=0)

        sentinel_lock = MagicMock()
        sentinel_lock.__aenter__ = AsyncMock()
        sentinel_lock.__aexit__ = AsyncMock(return_value=None)

        with patch("punt_vox.voxd.synthesis.get_provider", return_value=provider):
            old_lock = pipeline._env_lock
            pipeline._env_lock = sentinel_lock
            try:
                asyncio.run(
                    pipeline.try_direct_play(
                        "hello",
                        _default_spec(provider="elevenlabs", api_key="secret"),
                        record_result=_record_result(results),
                    )
                )
            finally:
                pipeline._env_lock = old_lock

        sentinel_lock.__aenter__.assert_called_once()


class TestDirectPlayProtocol:
    """Direct-play capability is detected via the DirectPlayProvider protocol."""

    def test_polly_is_not_direct_play(self) -> None:
        from punt_vox.providers.polly import PollyProvider
        from punt_vox.types import DirectPlayProvider

        with patch("punt_vox.providers.polly.boto3.client"):
            provider = PollyProvider()
        assert not isinstance(provider, DirectPlayProvider)

    def test_openai_is_not_direct_play(self) -> None:
        from punt_vox.providers.openai import OpenAIProvider
        from punt_vox.types import DirectPlayProvider

        with patch.dict("os.environ", {"OPENAI_API_KEY": "sk-test"}):
            provider = OpenAIProvider()
        assert not isinstance(provider, DirectPlayProvider)

    def test_elevenlabs_is_not_direct_play(self) -> None:
        from punt_vox.providers.elevenlabs import ElevenLabsProvider
        from punt_vox.types import DirectPlayProvider

        with patch.dict("os.environ", {"ELEVENLABS_API_KEY": "test"}):
            provider = ElevenLabsProvider()
        assert not isinstance(provider, DirectPlayProvider)

    def test_espeak_provider_is_not_direct_play(self) -> None:
        """EspeakProvider is not a DirectPlayProvider."""
        from punt_vox.providers.espeak import EspeakProvider
        from punt_vox.types import DirectPlayProvider

        with patch(
            "punt_vox.providers.espeak._find_espeak_binary",
            return_value="/usr/bin/espeak-ng",
        ):
            provider = EspeakProvider()
        assert not isinstance(provider, DirectPlayProvider)

    def test_espeak_direct_player_is_direct_play(self) -> None:
        from punt_vox.providers.espeak import EspeakProvider
        from punt_vox.providers.local_play import EspeakDirectPlayer
        from punt_vox.types import DirectPlayProvider

        with patch(
            "punt_vox.providers.espeak._find_espeak_binary",
            return_value="/usr/bin/espeak-ng",
        ):
            provider = EspeakProvider()
        player = EspeakDirectPlayer(
            binary=provider._binary,  # pyright: ignore[reportPrivateUsage]
            voices=provider._voices,  # pyright: ignore[reportPrivateUsage]
        )
        assert isinstance(player, DirectPlayProvider)

    def test_say_direct_player_is_direct_play(self) -> None:
        from punt_vox.providers.local_play import SayDirectPlayer
        from punt_vox.providers.say import SayProvider
        from punt_vox.types import DirectPlayProvider

        with (
            patch("punt_vox.providers.say.platform") as mock_platform,
            patch("punt_vox.providers.say.shutil") as mock_shutil,
        ):
            mock_platform.system.return_value = "Darwin"
            mock_shutil.which.return_value = "/usr/bin/say"
            provider = SayProvider()
        player = SayDirectPlayer(voices=provider._voices)  # pyright: ignore[reportPrivateUsage]
        assert isinstance(player, DirectPlayProvider)


class TestDirectPlaySerialization:
    """Direct-play and queued playback share _playback_mutex."""

    def test_concurrent_direct_plays_serialize(self) -> None:
        """Two concurrent direct-play calls must not run in parallel."""
        import time as time_module

        pipeline = _make_pipeline()
        results: list[dict[str, object]] = []

        starts: list[float] = []
        ends: list[float] = []

        def slow_play(_request: object) -> int:
            starts.append(time_module.monotonic())
            time_module.sleep(0.1)
            ends.append(time_module.monotonic())
            return 0

        provider = MagicMock()
        provider.play_directly = slow_play

        async def _drive() -> None:
            with patch("punt_vox.voxd.synthesis.get_provider", return_value=provider):
                await asyncio.gather(
                    pipeline.try_direct_play(
                        "one",
                        _default_spec(),
                        record_result=_record_result(results),
                    ),
                    pipeline.try_direct_play(
                        "two",
                        _default_spec(),
                        record_result=_record_result(results),
                    ),
                )

        asyncio.run(_drive())

        assert len(starts) == 2
        assert len(ends) == 2
        first_end = min(ends)
        second_start = max(starts)
        assert second_start >= first_end, (
            f"direct-play calls overlapped: "
            f"second_start={second_start}, first_end={first_end}"
        )


class TestApiKeyPassthroughIntegration:
    """End-to-end api_key flow: WebSocket -> _handle_record -> provider."""

    def _build_stub_provider(self, observed: list[str | None]) -> type:
        """Return a stub TTSProvider class that records the observed api key."""
        from punt_vox.types import (
            AudioProviderId,
            AudioRequest,
            AudioResult,
            HealthCheck,
        )

        valid_mp3 = _get_valid_mp3_bytes()

        class _StubProvider:
            name = "elevenlabs"
            default_voice = "matilda"
            supports_expressive_tags = False

            def __init__(self) -> None:
                observed.append(os.environ.get("ELEVENLABS_API_KEY"))

            def synthesize(
                self, request: AudioRequest, output_path: Path
            ) -> AudioResult:
                output_path.write_bytes(valid_mp3)
                return AudioResult(
                    path=output_path,
                    text=request.text,
                    provider=AudioProviderId.elevenlabs,
                    voice="matilda",
                )

            def generate_audio(self, request: AudioRequest) -> AudioResult:
                raise NotImplementedError

            def resolve_voice(self, name: str, language: str | None = None) -> str:
                return name or "matilda"

            def get_default_voice(self, language: str) -> str:
                return "matilda"

            def list_voices(self, language: str | None = None) -> list[str]:
                return ["matilda"]

            def infer_language_from_voice(self, voice: str) -> str | None:
                return None

            def check_health(self) -> list[HealthCheck]:
                return []

        return _StubProvider

    def _run_record(
        self,
        api_key: str | None,
        observed: list[str | None],
        monkeypatch: pytest.MonkeyPatch,
    ) -> dict[str, object]:
        """Send a record message over a real WebSocket and return the response."""
        from starlette.testclient import TestClient

        from punt_vox.voxd import build_app

        stub_provider_cls = self._build_stub_provider(observed)

        def fake_get_provider(
            name: str,
            *,
            config_dir: object = None,
            model: str | None = None,
        ) -> object:
            assert name == "elevenlabs"
            return stub_provider_cls()

        monkeypatch.setattr("punt_vox.voxd.synthesis.get_provider", fake_get_provider)

        # Disable the cache so every call reaches the stub factory.
        from punt_vox.cache import CacheKey as _CacheKey

        def _cache_miss(_key: _CacheKey) -> Path | None:
            return None

        def _cache_noop(_key: _CacheKey, _path: Path) -> None:
            return None

        monkeypatch.setattr("punt_vox.voxd.synthesis.cache_get", _cache_miss)
        monkeypatch.setattr("punt_vox.voxd.synthesis.cache_put", _cache_noop)

        app = build_app()

        with tempfile.TemporaryDirectory() as out_dir:
            msg: dict[str, object] = {
                "type": "record",
                "id": "test-rec",
                "text": "billable synthesis",
                "provider": "elevenlabs",
                "voice": "matilda",
                "output_dir": out_dir,
            }
            if api_key is not None:
                msg["api_key"] = api_key

            with (
                TestClient(app) as client,
                client.websocket_connect("/ws") as ws,
            ):
                ws.send_json(msg)
                # The daemon acks with 'recording' before the terminal 'audio'.
                response: dict[str, object] = ws.receive_json()
                while response.get("type") == "recording":
                    response = ws.receive_json()

        return response

    def test_first_call_key_reaches_provider(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("ELEVENLABS_API_KEY", raising=False)
        observed: list[str | None] = []

        response = self._run_record("test-key-alpha-billing", observed, monkeypatch)

        assert response.get("type") == "audio"
        assert len(observed) == 1
        assert observed[0] == "test-key-alpha-billing"

    def test_second_call_key_does_not_leak_from_first(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("ELEVENLABS_API_KEY", raising=False)
        observed: list[str | None] = []

        self._run_record("test-key-alpha-billing", observed, monkeypatch)
        self._run_record("test-key-bravo-billing", observed, monkeypatch)

        assert observed == [
            "test-key-alpha-billing",
            "test-key-bravo-billing",
        ]
        assert os.environ.get("ELEVENLABS_API_KEY") is None

    def test_no_api_key_falls_back_to_ambient_env(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("ELEVENLABS_API_KEY", "ambient-fallback-key")
        observed: list[str | None] = []

        self._run_record(None, observed, monkeypatch)

        assert len(observed) == 1
        assert observed[0] == "ambient-fallback-key"
        assert os.environ.get("ELEVENLABS_API_KEY") == "ambient-fallback-key"

    def test_previously_set_ambient_key_restored_after_per_call_key(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("ELEVENLABS_API_KEY", "ambient-persistent-key")
        observed: list[str | None] = []

        self._run_record("per-call-override", observed, monkeypatch)

        assert observed == ["per-call-override"]
        assert os.environ.get("ELEVENLABS_API_KEY") == "ambient-persistent-key"


class TestCacheApiKeyBypass:
    """Cache bypass when ``api_key`` is set -- vox-a3e billing isolation."""

    def _build_counting_provider(self, calls: list[str | None]) -> type:
        """Stub provider that appends to ``calls`` on every construction."""
        from punt_vox.types import (
            AudioProviderId,
            AudioRequest,
            AudioResult,
            HealthCheck,
        )

        valid_mp3 = _get_valid_mp3_bytes()

        class _CountingProvider:
            name = "elevenlabs"
            default_voice = "matilda"
            supports_expressive_tags = False

            def __init__(self) -> None:
                calls.append(os.environ.get("ELEVENLABS_API_KEY"))

            def synthesize(
                self, request: AudioRequest, output_path: Path
            ) -> AudioResult:
                output_path.write_bytes(valid_mp3)
                return AudioResult(
                    path=output_path,
                    text=request.text,
                    provider=AudioProviderId.elevenlabs,
                    voice="matilda",
                )

            def generate_audio(self, request: AudioRequest) -> AudioResult:
                raise NotImplementedError

            def resolve_voice(self, name: str, language: str | None = None) -> str:
                return name or "matilda"

            def get_default_voice(self, language: str) -> str:
                return "matilda"

            def list_voices(self, language: str | None = None) -> list[str]:
                return ["matilda"]

            def infer_language_from_voice(self, voice: str) -> str | None:
                return None

            def check_health(self) -> list[HealthCheck]:
                return []

        return _CountingProvider

    def _run_record(
        self,
        *,
        api_key: str | None,
        text: str,
        calls: list[str | None],
        monkeypatch: pytest.MonkeyPatch,
    ) -> dict[str, object]:
        """Send a ``record`` message over a real WebSocket."""
        from starlette.testclient import TestClient

        from punt_vox.voxd import build_app

        provider_cls = self._build_counting_provider(calls)

        def fake_get_provider(
            name: str,
            *,
            config_dir: object = None,
            model: str | None = None,
        ) -> object:
            assert name == "elevenlabs"
            return provider_cls()

        monkeypatch.setattr("punt_vox.voxd.synthesis.get_provider", fake_get_provider)

        app = build_app()

        with tempfile.TemporaryDirectory() as out_dir:
            msg: dict[str, object] = {
                "type": "record",
                "id": "test-rec",
                "text": text,
                "provider": "elevenlabs",
                "voice": "matilda",
                "output_dir": out_dir,
            }
            if api_key is not None:
                msg["api_key"] = api_key

            with (
                TestClient(app) as client,
                client.websocket_connect("/ws") as ws,
            ):
                ws.send_json(msg)
                # The daemon acks with 'recording' before the terminal 'audio'.
                response: dict[str, object] = ws.receive_json()
                while response.get("type") == "recording":
                    response = ws.receive_json()

        return response

    def test_api_key_call_bypasses_cache_get_and_put(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        monkeypatch.setattr("punt_vox.cache.CACHE_DIR", tmp_path / "cache")
        monkeypatch.setenv("ELEVENLABS_API_KEY", "")

        get_calls: list[tuple[object, ...]] = []
        put_calls: list[tuple[object, ...]] = []

        from punt_vox.cache import CacheKey as _CacheKey2

        def spy_cache_get(_key: _CacheKey2) -> Path | None:
            get_calls.append((_key,))
            return None

        def spy_cache_put(_key: _CacheKey2, source: Path) -> Path | None:
            put_calls.append((_key, source))
            assert isinstance(source, Path)
            return source

        monkeypatch.setattr("punt_vox.voxd.synthesis.cache_get", spy_cache_get)
        monkeypatch.setattr("punt_vox.voxd.synthesis.cache_put", spy_cache_put)

        calls: list[str | None] = []

        resp1 = self._run_record(
            api_key=None,
            text="bypass test phrase",
            calls=calls,
            monkeypatch=monkeypatch,
        )
        assert resp1.get("type") == "audio"
        assert len(get_calls) == 1
        assert len(put_calls) == 1

        resp2 = self._run_record(
            api_key="sk_bypass_test",
            text="bypass test phrase",
            calls=calls,
            monkeypatch=monkeypatch,
        )
        assert resp2.get("type") == "audio"
        assert len(get_calls) == 1
        assert len(put_calls) == 1
        assert len(calls) == 2

    def test_api_key_call_does_not_poison_anonymous_cache(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        monkeypatch.delenv("ELEVENLABS_API_KEY", raising=False)
        monkeypatch.setattr("punt_vox.cache.CACHE_DIR", tmp_path / "cache")
        calls: list[str | None] = []

        resp1 = self._run_record(
            api_key=None,
            text="shared anon phrase",
            calls=calls,
            monkeypatch=monkeypatch,
        )
        assert resp1.get("type") == "audio"
        assert len(calls) == 1

        resp2 = self._run_record(
            api_key=None,
            text="shared anon phrase",
            calls=calls,
            monkeypatch=monkeypatch,
        )
        assert resp2.get("type") == "audio"
        assert len(calls) == 1

        resp3 = self._run_record(
            api_key="sk_poison_test",
            text="shared anon phrase",
            calls=calls,
            monkeypatch=monkeypatch,
        )
        assert resp3.get("type") == "audio"
        assert len(calls) == 2

        resp4 = self._run_record(
            api_key=None,
            text="shared anon phrase",
            calls=calls,
            monkeypatch=monkeypatch,
        )
        assert resp4.get("type") == "audio"
        assert len(calls) == 2

    def test_per_call_keys_reach_provider_distinct(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        monkeypatch.setenv("ELEVENLABS_API_KEY", "")
        monkeypatch.setattr("punt_vox.cache.CACHE_DIR", tmp_path / "cache")
        calls: list[str | None] = []

        resp1 = self._run_record(
            api_key="sk_A",
            text="billable phrase",
            calls=calls,
            monkeypatch=monkeypatch,
        )
        assert resp1.get("type") == "audio"

        resp2 = self._run_record(
            api_key="sk_B",
            text="billable phrase",
            calls=calls,
            monkeypatch=monkeypatch,
        )
        assert resp2.get("type") == "audio"

        assert calls == ["sk_A", "sk_B"]


class TestSynthesizeFailFast:
    """0-byte synthesis must raise and skip cache_put to avoid poisoning."""

    def test_zero_byte_output_raises_and_skips_cache(self, tmp_path: Path) -> None:
        pipeline = _make_pipeline()

        captured_temp: dict[str, Path] = {}

        def fake_synth(request: object, output_path: Path) -> None:
            output_path.write_bytes(b"")
            captured_temp["path"] = output_path

        provider = MagicMock()
        provider.name = "espeak"

        with (
            patch("punt_vox.voxd.synthesis.get_provider", return_value=provider),
            patch("punt_vox.voxd.synthesis.cache_get", return_value=None),
            patch("punt_vox.voxd.synthesis.cache_put") as mock_cache_put,
            patch("punt_vox.voxd.synthesis.TTSClient") as mock_client_cls,
        ):
            mock_client = MagicMock()
            mock_client.synthesize = fake_synth
            mock_client_cls.return_value = mock_client

            with pytest.raises(RuntimeError, match="missing or empty"):
                asyncio.run(
                    pipeline.synthesize_to_file(
                        "hello",
                        _default_spec(),
                    )
                )

        mock_cache_put.assert_not_called()
        assert "path" in captured_temp
        assert not captured_temp["path"].exists()


class TestSynthesizeCacheObservability:
    """synthesize_to_file reports cache hit/miss via SynthesisOutcome.cached."""

    @staticmethod
    def _run(
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        requests: list[tuple[str, SynthesisSpec]],
    ) -> tuple[list[SynthesisOutcome], list[str]]:
        """Drive synthesize_to_file over a real temp cache with a counting synth.

        Returns the ordered outcomes and the list of texts actually
        synthesized (one entry per real TTS call, so a cache hit adds none).
        """
        monkeypatch.setattr("punt_vox.cache.CACHE_DIR", tmp_path)
        synth_calls: list[str] = []
        mp3 = _get_valid_mp3_bytes()

        def fake_synth(request: object, output_path: Path) -> None:
            synth_calls.append(getattr(request, "text", ""))
            output_path.write_bytes(mp3)

        mock_client = MagicMock()
        mock_client.synthesize = fake_synth
        pipeline = _make_pipeline()

        async def _drive() -> list[SynthesisOutcome]:
            return [await pipeline.synthesize_to_file(t, s) for t, s in requests]

        with (
            patch("punt_vox.voxd.synthesis.get_provider", return_value=MagicMock()),
            patch("punt_vox.voxd.synthesis.TTSClient", return_value=mock_client),
        ):
            outcomes = asyncio.run(_drive())
        return outcomes, synth_calls

    def test_miss_then_hit_flips_cached_without_resynth(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        spec = _default_spec(provider="openai")
        outcomes, synth_calls = self._run(
            tmp_path, monkeypatch, [("hello", spec), ("hello", spec)]
        )

        assert outcomes[0].cached is False
        assert outcomes[1].cached is True
        # The hit is served from the cache dir, not re-synthesized: the miss
        # returns its temp file, the hit returns the copied-in cache entry.
        assert outcomes[1].path.parent == tmp_path
        assert synth_calls == ["hello"]

    def test_different_voice_is_a_miss(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        outcomes, synth_calls = self._run(
            tmp_path,
            monkeypatch,
            [
                ("hello", _default_spec(provider="openai", voice="roger")),
                ("hello", _default_spec(provider="openai", voice="alice")),
            ],
        )

        # A different voice is a different cache key -> both miss and synth.
        assert outcomes[0].cached is False
        assert outcomes[1].cached is False
        assert len(synth_calls) == 2

    def test_per_call_api_key_bypasses_cache_is_a_miss(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        spec = _default_spec(provider="openai", api_key="sk-scope")
        outcomes, synth_calls = self._run(
            tmp_path, monkeypatch, [("hello", spec), ("hello", spec)]
        )

        # Per-call api_key bypasses the cache on both calls: always a miss.
        assert outcomes[0].cached is False
        assert outcomes[1].cached is False
        assert len(synth_calls) == 2

    def test_synthesize_emits_single_info(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """A fresh anonymous synth logs exactly one plain-sentence INFO."""
        spec = _default_spec(provider="openai")
        with caplog.at_level(logging.INFO, logger="punt_vox.voxd"):
            outcomes, _ = self._run(tmp_path, monkeypatch, [("hello", spec)])

        assert outcomes[0].cached is False
        infos = [r.getMessage() for r in caplog.records if r.levelno == logging.INFO]
        assert infos == ["synthesized 5 chars with openai, cached"]

    def test_non_anonymous_miss_is_not_marked_cached(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """A per-call api_key request is not cached, so no ", cached" suffix."""
        spec = _default_spec(provider="openai", api_key="sk-scope")
        with caplog.at_level(logging.INFO, logger="punt_vox.voxd"):
            outcomes, _ = self._run(tmp_path, monkeypatch, [("hello", spec)])

        assert outcomes[0].cached is False
        infos = [r.getMessage() for r in caplog.records if r.levelno == logging.INFO]
        assert infos == ["synthesized 5 chars with openai"]
        assert not any("cached" in m for m in infos)


class TestModelSupportsExpressiveTags:
    """model_supports_expressive_tags lookup for vibe-tag gating."""

    def test_elevenlabs_v3_supported(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("TTS_MODEL", raising=False)
        assert (
            SynthesisPipeline.model_supports_expressive_tags("elevenlabs", "eleven_v3")
            is True
        )

    def test_elevenlabs_flash_does_not_support(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("TTS_MODEL", raising=False)
        assert (
            SynthesisPipeline.model_supports_expressive_tags(
                "elevenlabs", "eleven_flash_v2_5"
            )
            is False
        )

    def test_elevenlabs_default_supported(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("TTS_MODEL", raising=False)
        assert (
            SynthesisPipeline.model_supports_expressive_tags("elevenlabs", None) is True
        )

    def test_polly_never_supports(self) -> None:
        assert SynthesisPipeline.model_supports_expressive_tags("polly", None) is False
        assert (
            SynthesisPipeline.model_supports_expressive_tags("polly", "neural") is False
        )

    def test_openai_never_supports(self) -> None:
        assert SynthesisPipeline.model_supports_expressive_tags("openai", None) is False
        assert (
            SynthesisPipeline.model_supports_expressive_tags("openai", "tts-1-hd")
            is False
        )

    def test_say_never_supports(self) -> None:
        assert SynthesisPipeline.model_supports_expressive_tags("say", None) is False

    def test_espeak_never_supports(self) -> None:
        assert SynthesisPipeline.model_supports_expressive_tags("espeak", None) is False

    def test_unknown_provider_never_supports(self) -> None:
        assert (
            SynthesisPipeline.model_supports_expressive_tags("future_provider", "any")
            is False
        )


class TestApplyVibeForSynthesis:
    """apply_vibe_for_synthesis gates vibe-tag handling on capability."""

    _apply = staticmethod(SynthesisPipeline.apply_vibe_for_synthesis)

    def test_v3_preserves_vibe_tags(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("TTS_MODEL", raising=False)
        result = self._apply("Hello world", "[excited]", "elevenlabs", "eleven_v3")
        assert result == "[excited] Hello world"

    def test_expressive_model_passthrough_when_no_vibe_tags(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("TTS_MODEL", raising=False)
        result = self._apply("Hello world", None, "elevenlabs", "eleven_v3")
        assert result == "Hello world"

    def test_v3_preserves_user_tags_in_text(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("TTS_MODEL", raising=False)
        result = self._apply("[whisper] Quiet message", None, "elevenlabs", "eleven_v3")
        assert result == "[whisper] Quiet message"

    def test_non_expressive_model_drops_vibe_tags(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("TTS_MODEL", raising=False)
        result = self._apply(
            "Hello world", "[serious]", "elevenlabs", "eleven_flash_v2_5"
        )
        assert result == "Hello world"
        assert "[serious]" not in result
        assert "serious" not in result

    def test_non_expressive_model_strips_user_tags_from_text(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("TTS_MODEL", raising=False)
        result = self._apply(
            "[serious] Hello world", None, "elevenlabs", "eleven_flash_v2_5"
        )
        assert result == "Hello world"
        assert "serious" not in result

    def test_non_expressive_model_strips_user_tags_even_with_vibe_set(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("TTS_MODEL", raising=False)
        result = self._apply(
            "[whisper] Quiet message",
            "[excited]",
            "elevenlabs",
            "eleven_flash_v2_5",
        )
        assert result == "Quiet message"
        assert "whisper" not in result
        assert "excited" not in result

    def test_polly_strips_user_tags(self) -> None:
        result = self._apply("[serious] Important", "[calm]", "polly", None)
        assert result == "Important"

    def test_openai_strips_user_tags(self) -> None:
        result = self._apply("[whisper] Hello", None, "openai", "tts-1")
        assert result == "Hello"

    def test_say_strips_user_tags(self) -> None:
        result = self._apply("[excited] Greetings", "[happy]", "say", None)
        assert result == "Greetings"

    def test_espeak_strips_user_tags(self) -> None:
        result = self._apply("[serious] Notice", None, "espeak", None)
        assert result == "Notice"

    def test_degenerate_text_only_tags_non_expressive_returns_empty(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.delenv("TTS_MODEL", raising=False)
        result = self._apply("[serious]", None, "elevenlabs", "eleven_flash_v2_5")
        assert result == ""

    def test_degenerate_text_only_tags_v3_preserves(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.delenv("TTS_MODEL", raising=False)
        result = self._apply("[sighs]", None, "elevenlabs", "eleven_v3")
        assert result == "[sighs]"

    def test_normalizes_body_through_production_call_path(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.delenv("TTS_MODEL", raising=False)
        result = self._apply("[serious] my_function works", None, "polly", None)
        assert result == "my function works"
        assert "serious" not in result
        assert "[" not in result
        assert "_" not in result

    def test_v3_normalizes_body_and_preserves_tags(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.delenv("TTS_MODEL", raising=False)
        result = self._apply(
            "[whisper] my_function works", None, "elevenlabs", "eleven_v3"
        )
        assert result == "[whisper] my function works"

    def test_trailing_tags_stripped_non_expressive(self) -> None:
        result = self._apply("hello [alert] [serious]", None, "polly", None)
        assert result == "hello"
        assert "alert" not in result
        assert "serious" not in result

    def test_inline_tags_stripped_non_expressive(self) -> None:
        result = self._apply("hello [serious] world", None, "polly", None)
        assert result == "hello world"
        assert "serious" not in result

    def test_leading_tags_still_stripped_non_expressive(self) -> None:
        result = self._apply("[serious] hello", None, "polly", None)
        assert result == "hello"
        assert "serious" not in result

    def _patch_expressive(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Force model_supports_expressive_tags to return True."""

        def _always_expressive(_provider: str, _model: str | None) -> bool:
            return True

        monkeypatch.setattr(
            "punt_vox.voxd.synthesis.SynthesisPipeline.model_supports_expressive_tags",
            _always_expressive,
        )

    def test_trailing_tags_preserved_expressive(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._patch_expressive(monkeypatch)
        result = self._apply("hello [serious]", None, "elevenlabs", "eleven_v3")
        assert "[serious]" in result
        assert "hello" in result

    def test_inline_tags_preserved_expressive(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._patch_expressive(monkeypatch)
        result = self._apply("hello [serious] world", None, "elevenlabs", "eleven_v3")
        assert "[serious]" in result
        assert "hello" in result
        assert "world" in result

    def test_session_vibe_tags_prepended_expressive(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._patch_expressive(monkeypatch)
        result = self._apply("hello", "[warm]", "elevenlabs", "eleven_v3")
        assert result == "[warm] hello"
