"""Tests for punt_vox.providers.espeak."""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from conftest import _get_valid_mp3_bytes  # pyright: ignore[reportPrivateUsage]

from punt_vox.providers.convert import rate_to_wpm
from punt_vox.providers.espeak import (
    EspeakProvider,
    EspeakVoiceConfig,
)
from punt_vox.providers.local_play import EspeakDirectPlayer
from punt_vox.types import AudioProviderId, SynthesisRequest, VoiceNotFoundError


class TestEspeakVoiceConfig:
    def test_frozen(self) -> None:
        cfg = EspeakVoiceConfig(name="english", language="en")
        with pytest.raises(AttributeError):
            cfg.name = "german"  # type: ignore[misc]


class TestRateToWpm:
    def test_normal(self) -> None:
        assert rate_to_wpm(100) == 175

    def test_slow(self) -> None:
        assert rate_to_wpm(90) == 157

    def test_half(self) -> None:
        assert rate_to_wpm(50) == 87

    def test_zero_clamps_to_one(self) -> None:
        assert rate_to_wpm(0) == 1


class TestEspeakProviderGuard:
    def test_no_binary_raises(self) -> None:
        with (
            patch(
                "punt_vox.providers.espeak._find_espeak_binary",
                return_value=None,
            ),
            pytest.raises(ValueError, match="espeak-ng or espeak not found"),
        ):
            EspeakProvider()

    def test_binary_found_succeeds(self) -> None:
        with patch(
            "punt_vox.providers.espeak._find_espeak_binary",
            return_value="/usr/bin/espeak-ng",
        ):
            provider = EspeakProvider()
            assert provider.name == "espeak"


class TestEspeakProviderName:
    def test_name(self, espeak_provider: EspeakProvider) -> None:
        assert espeak_provider.name == "espeak"


def _make_provider_with_voices(fake_output: str) -> EspeakProvider:
    """Create a fresh EspeakProvider whose _fetch_voices parses fake_output."""
    with patch(
        "punt_vox.providers.espeak._find_espeak_binary",
        return_value="/usr/bin/espeak-ng",
    ):
        provider = EspeakProvider()
    provider._voices._cache.clear()  # pyright: ignore[reportPrivateUsage]
    provider._voices._loaded_at = 0.0  # pyright: ignore[reportPrivateUsage]
    with (
        patch(
            "punt_vox.providers.espeak._find_espeak_binary",
            return_value="/usr/bin/espeak-ng",
        ),
        patch(
            "punt_vox.providers.espeak.subprocess.run",
            return_value=subprocess.CompletedProcess(
                ["espeak-ng", "--voices"],
                0,
                stdout=fake_output,
                stderr="",
            ),
        ),
    ):
        # Trigger the fetch through the resolver
        provider._voices._do_load()  # pyright: ignore[reportPrivateUsage]
    return provider


class TestEspeakBareIsoFallback:
    """Verify that _fetch_voices registers bare ISO 639-1 keys."""

    def test_bare_en_registered(self) -> None:
        fake = (
            "Pty  Language  Age/Gender  VoiceName   File\n"
            " 5     en-us          M  english-us   other/en-us\n"
            " 5     en-gb          M  english-gb   other/en-gb\n"
            " 5     de             M  german       other/de\n"
        )
        prov = _make_provider_with_voices(fake)
        cache = prov._voices._cache  # pyright: ignore[reportPrivateUsage]
        assert "en" in cache
        assert cache["en"].language == "en"
        assert "de" in cache

    def test_bare_entry_wins_when_listed_first(self) -> None:
        fake = (
            "Pty  Language  Age/Gender  VoiceName   File\n"
            " 5     en             M  english      default\n"
            " 5     en-us          M  english-us   other/en-us\n"
        )
        prov = _make_provider_with_voices(fake)
        cache = prov._voices._cache  # pyright: ignore[reportPrivateUsage]
        assert cache["en"].name == "en"

    def test_bare_entry_wins_when_listed_after(self) -> None:
        fake = (
            "Pty  Language  Age/Gender  VoiceName   File\n"
            " 5     en-us          M  english-us   other/en-us\n"
            " 5     en             M  english      default\n"
        )
        prov = _make_provider_with_voices(fake)
        cache = prov._voices._cache  # pyright: ignore[reportPrivateUsage]
        assert cache["en"].name == "en"


class TestEspeakDefaultVoiceDynamic:
    def test_prefers_bare_en(self, espeak_provider: EspeakProvider) -> None:
        assert espeak_provider.default_voice == "en"

    def test_falls_back_to_en_us(
        self,
        espeak_provider: EspeakProvider,
    ) -> None:
        cache = espeak_provider._voices._cache  # pyright: ignore[reportPrivateUsage]
        cache.clear()
        cache["en-us"] = EspeakVoiceConfig(name="en-us", language="en")
        cache["de"] = EspeakVoiceConfig(name="de", language="de")
        assert espeak_provider.default_voice == "en-us"

    def test_falls_back_to_en_gb(
        self,
        espeak_provider: EspeakProvider,
    ) -> None:
        cache = espeak_provider._voices._cache  # pyright: ignore[reportPrivateUsage]
        cache.clear()
        cache["en-gb"] = EspeakVoiceConfig(name="en-gb", language="en")
        assert espeak_provider.default_voice == "en-gb"

    def test_falls_back_to_first_en_variant(
        self,
        espeak_provider: EspeakProvider,
    ) -> None:
        cache = espeak_provider._voices._cache  # pyright: ignore[reportPrivateUsage]
        cache.clear()
        cache["en-au"] = EspeakVoiceConfig(name="en-au", language="en")
        assert espeak_provider.default_voice == "en-au"

    def test_falls_back_to_first_voice(
        self,
        espeak_provider: EspeakProvider,
    ) -> None:
        cache = espeak_provider._voices._cache  # pyright: ignore[reportPrivateUsage]
        cache.clear()
        cache["de"] = EspeakVoiceConfig(name="de", language="de")
        assert espeak_provider.default_voice == "de"

    def test_empty_voices_returns_en(
        self,
        espeak_provider: EspeakProvider,
    ) -> None:
        cache = espeak_provider._voices._cache  # pyright: ignore[reportPrivateUsage]
        cache.clear()
        assert espeak_provider.default_voice == "en"


class TestEspeakProviderResolveVoice:
    def test_resolve_cached(self, espeak_provider: EspeakProvider) -> None:
        assert espeak_provider.resolve_voice("english") == "en"

    def test_resolve_by_language_code(
        self,
        espeak_provider: EspeakProvider,
    ) -> None:
        assert espeak_provider.resolve_voice("en") == "en"

    def test_resolve_case_insensitive(
        self,
        espeak_provider: EspeakProvider,
    ) -> None:
        assert espeak_provider.resolve_voice("ENGLISH") == "en"

    def test_unknown_voice_raises(
        self,
        espeak_provider: EspeakProvider,
    ) -> None:
        espeak_provider._voices._cache.clear()  # pyright: ignore[reportPrivateUsage]
        espeak_provider._voices._loaded_at = 1.0  # pyright: ignore[reportPrivateUsage]
        with pytest.raises(VoiceNotFoundError) as exc_info:
            espeak_provider.resolve_voice("nonexistent")
        assert exc_info.value.voice_name == "nonexistent"

    def test_matching_language(
        self,
        espeak_provider: EspeakProvider,
    ) -> None:
        assert espeak_provider.resolve_voice("english", language="en") == "en"

    def test_mismatching_language(
        self,
        espeak_provider: EspeakProvider,
    ) -> None:
        with pytest.raises(ValueError, match="does not support language 'de'"):
            espeak_provider.resolve_voice("english", language="de")


class TestEspeakProviderSynthesize:
    def _mock_subprocess(self, mp3_bytes: bytes) -> MagicMock:
        def side_effect(
            args: list[str],
            **kwargs: object,
        ) -> subprocess.CompletedProcess[bytes]:
            cmd = args[0]
            if "espeak" in cmd:
                w_idx = args.index("-w")
                Path(args[w_idx + 1]).write_bytes(b"RIFF\x00\x00\x00\x00WAVE")
                return subprocess.CompletedProcess(args, 0)
            if cmd == "ffmpeg":
                Path(args[-1]).write_bytes(mp3_bytes)
                return subprocess.CompletedProcess(args, 0, b"", b"")
            msg = f"Unexpected subprocess call: {args}"
            raise ValueError(msg)

        return MagicMock(side_effect=side_effect)

    def test_synthesize_creates_file(
        self,
        espeak_provider: EspeakProvider,
        tmp_output_dir: Path,
    ) -> None:
        mp3 = _get_valid_mp3_bytes()
        out = tmp_output_dir / "test.mp3"
        mock = self._mock_subprocess(mp3)
        with patch("punt_vox.providers.espeak.subprocess.run", mock):
            result = espeak_provider.synthesize(
                SynthesisRequest(text="hello", voice="en"),
                out,
            )
        assert result.path == out
        assert out.exists()

    def test_synthesize_result_metadata(
        self,
        espeak_provider: EspeakProvider,
        tmp_output_dir: Path,
    ) -> None:
        mp3 = _get_valid_mp3_bytes()
        out = tmp_output_dir / "test.mp3"
        mock = self._mock_subprocess(mp3)
        with patch("punt_vox.providers.espeak.subprocess.run", mock):
            result = espeak_provider.synthesize(
                SynthesisRequest(text="hello", voice="en"),
                out,
            )
        assert result.provider == AudioProviderId.espeak
        assert result.voice == "en"
        assert result.language == "en"

    def test_synthesize_espeak_args(
        self,
        espeak_provider: EspeakProvider,
        tmp_output_dir: Path,
    ) -> None:
        mp3 = _get_valid_mp3_bytes()
        out = tmp_output_dir / "test.mp3"
        mock_run = self._mock_subprocess(mp3)
        with patch("punt_vox.providers.espeak.subprocess.run", mock_run):
            espeak_provider.synthesize(
                SynthesisRequest(text="hello", voice="en", rate=90),
                out,
            )
        espeak_args = mock_run.call_args_list[0][0][0]
        assert espeak_args[espeak_args.index("-v") + 1] == "en"
        assert espeak_args[espeak_args.index("-s") + 1] == "157"

    def test_synthesize_cleans_up_wav(
        self,
        espeak_provider: EspeakProvider,
        tmp_output_dir: Path,
    ) -> None:
        mp3 = _get_valid_mp3_bytes()
        out = tmp_output_dir / "test.mp3"
        wav_paths: list[Path] = []
        original = self._mock_subprocess(mp3)

        def tracking(
            args: list[str],
            **kwargs: object,
        ) -> subprocess.CompletedProcess[bytes]:
            if "espeak" in args[0]:
                wav_paths.append(Path(args[args.index("-w") + 1]))
            return original(args, **kwargs)  # type: ignore[no-any-return]

        with patch(
            "punt_vox.providers.espeak.subprocess.run",
            side_effect=tracking,
        ):
            espeak_provider.synthesize(
                SynthesisRequest(text="hello", voice="en"),
                out,
            )
        assert len(wav_paths) == 1
        assert not wav_paths[0].exists()

    def test_synthesize_infers_language(
        self,
        espeak_provider: EspeakProvider,
        tmp_output_dir: Path,
    ) -> None:
        mp3 = _get_valid_mp3_bytes()
        out = tmp_output_dir / "test.mp3"
        mock = self._mock_subprocess(mp3)
        with patch("punt_vox.providers.espeak.subprocess.run", mock):
            result = espeak_provider.synthesize(
                SynthesisRequest(text="Hallo", voice="german"),
                out,
            )
        assert result.language == "de"


class TestEspeakDirectPlayerPlayDirectly:
    def test_spawns_without_w_flag(
        self,
        espeak_provider: EspeakProvider,
    ) -> None:
        player = EspeakDirectPlayer(
            binary=espeak_provider._binary,  # pyright: ignore[reportPrivateUsage]
            voices=espeak_provider._voices,  # pyright: ignore[reportPrivateUsage]
        )
        mock = MagicMock(
            return_value=subprocess.CompletedProcess([], 0, b"", b""),
        )
        with patch("punt_vox.providers.local_play.subprocess.run", mock):
            rc = player.play_directly(
                SynthesisRequest(text="hello", voice="en"),
            )
        assert rc == 0
        args = mock.call_args[0][0]
        assert "-w" not in args

    def test_nonzero_rc_returned(
        self,
        espeak_provider: EspeakProvider,
    ) -> None:
        player = EspeakDirectPlayer(
            binary=espeak_provider._binary,  # pyright: ignore[reportPrivateUsage]
            voices=espeak_provider._voices,  # pyright: ignore[reportPrivateUsage]
        )
        mock = MagicMock(
            return_value=subprocess.CompletedProcess([], 3, b"", b"boom"),
        )
        with patch("punt_vox.providers.local_play.subprocess.run", mock):
            rc = player.play_directly(
                SynthesisRequest(text="hello", voice="en"),
            )
        assert rc == 3

    def test_strips_vibe_tags(
        self,
        espeak_provider: EspeakProvider,
    ) -> None:
        player = EspeakDirectPlayer(
            binary=espeak_provider._binary,  # pyright: ignore[reportPrivateUsage]
            voices=espeak_provider._voices,  # pyright: ignore[reportPrivateUsage]
        )
        mock = MagicMock(
            return_value=subprocess.CompletedProcess([], 0, b"", b""),
        )
        with patch("punt_vox.providers.local_play.subprocess.run", mock):
            player.play_directly(
                SynthesisRequest(text="[serious] Hello world", voice="en"),
            )
        args = mock.call_args[0][0]
        assert "Hello world" in args
        assert "[serious]" not in " ".join(args)

    def test_binary_missing_returns_error(
        self,
        espeak_provider: EspeakProvider,
    ) -> None:
        player = EspeakDirectPlayer(
            binary=espeak_provider._binary,  # pyright: ignore[reportPrivateUsage]
            voices=espeak_provider._voices,  # pyright: ignore[reportPrivateUsage]
        )
        with patch(
            "punt_vox.providers.local_play.subprocess.run",
            side_effect=FileNotFoundError("no espeak"),
        ):
            rc = player.play_directly(
                SynthesisRequest(text="hello", voice="en"),
            )
        assert rc == 1


class TestEspeakProviderCheckHealth:
    def test_binary_found(self) -> None:
        with patch(
            "punt_vox.providers.espeak._find_espeak_binary",
            return_value="/usr/bin/espeak-ng",
        ):
            provider = EspeakProvider()
        provider._voices._cache["en"] = EspeakVoiceConfig(  # pyright: ignore[reportPrivateUsage]
            name="en",
            language="en",
        )
        provider._voices._loaded_at = 1.0  # pyright: ignore[reportPrivateUsage]
        with patch(
            "punt_vox.providers.espeak._find_espeak_binary",
            return_value="/usr/bin/espeak-ng",
        ):
            checks = provider.check_health()
        assert len(checks) == 2
        assert checks[0].passed
        assert checks[1].passed
        assert "default voice: en (en)" in checks[1].message

    def test_default_voice_unavailable(self) -> None:
        with patch(
            "punt_vox.providers.espeak._find_espeak_binary",
            return_value="/usr/bin/espeak-ng",
        ):
            provider = EspeakProvider()
        provider._voices._loader = dict  # pyright: ignore[reportPrivateUsage]
        provider._voices._cache.clear()  # pyright: ignore[reportPrivateUsage]
        provider._voices._loaded_at = 0.0  # pyright: ignore[reportPrivateUsage]
        provider._voices._force_refreshed_at = 0.0  # pyright: ignore[reportPrivateUsage]
        with patch(
            "punt_vox.providers.espeak._find_espeak_binary",
            return_value="/usr/bin/espeak-ng",
        ):
            checks = provider.check_health()
        assert not checks[1].passed
        assert "default voice not available" in checks[1].message

    def test_binary_not_found(self) -> None:
        with patch(
            "punt_vox.providers.espeak._find_espeak_binary",
            return_value="/usr/bin/espeak-ng",
        ):
            provider = EspeakProvider()
        with patch(
            "punt_vox.providers.espeak._find_espeak_binary",
            return_value=None,
        ):
            checks = provider.check_health()
        assert len(checks) == 1
        assert not checks[0].passed


class TestEspeakProviderGetDefaultVoice:
    def test_english(self, espeak_provider: EspeakProvider) -> None:
        assert espeak_provider.get_default_voice("en") == "en"

    def test_german(self, espeak_provider: EspeakProvider) -> None:
        assert espeak_provider.get_default_voice("de") == "de"

    def test_unknown_language(self, espeak_provider: EspeakProvider) -> None:
        with pytest.raises(ValueError, match="No default voice"):
            espeak_provider.get_default_voice("xx")


class TestEspeakProviderListVoices:
    def test_all_voices(self, espeak_provider: EspeakProvider) -> None:
        voices = espeak_provider.list_voices()
        assert "english" in voices
        assert "en" in voices

    def test_filter_by_language(self, espeak_provider: EspeakProvider) -> None:
        voices = espeak_provider.list_voices(language="en")
        assert "english" in voices
        assert "german" not in voices

    def test_sorted(self, espeak_provider: EspeakProvider) -> None:
        voices = espeak_provider.list_voices()
        assert voices == sorted(voices)


class TestEspeakProviderInferLanguage:
    def test_english(self, espeak_provider: EspeakProvider) -> None:
        assert espeak_provider.infer_language_from_voice("english") == "en"

    def test_german(self, espeak_provider: EspeakProvider) -> None:
        assert espeak_provider.infer_language_from_voice("german") == "de"


# The old ``TestAutoDetectEspeakFallback`` covered ``auto_detect_provider``,
# which is deleted (design §3.3): the daemon never invents a provider.
# The espeak leg of the preference walk is now covered generically by
# tests/test_credentials.py (:class:`BinaryRequirement`).
