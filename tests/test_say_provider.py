"""Tests for punt_vox.providers.say."""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from conftest import _get_valid_mp3_bytes  # pyright: ignore[reportPrivateUsage]

from punt_vox.providers.convert import rate_to_wpm
from punt_vox.providers.local_play import SayDirectPlayer
from punt_vox.providers.say import (
    SayProvider,
    SayVoiceConfig,
    _locale_to_iso,  # pyright: ignore[reportPrivateUsage]
)
from punt_vox.types import AudioProviderId, SynthesisRequest, VoiceNotFoundError


class TestSayVoiceConfig:
    def test_frozen(self) -> None:
        cfg = SayVoiceConfig(name="Fred", locale="en_US")
        with pytest.raises(AttributeError):
            cfg.name = "Samantha"  # type: ignore[misc]


class TestLocaleToIso:
    def test_english(self) -> None:
        assert _locale_to_iso("en_US") == "en"

    def test_german(self) -> None:
        assert _locale_to_iso("de_DE") == "de"

    def test_japanese(self) -> None:
        assert _locale_to_iso("ja_JP") == "ja"


class TestRateToWpm:
    def test_normal(self) -> None:
        assert rate_to_wpm(100) == 175

    def test_slow(self) -> None:
        assert rate_to_wpm(90) == 157

    def test_half(self) -> None:
        assert rate_to_wpm(50) == 87

    def test_zero_clamps_to_one(self) -> None:
        assert rate_to_wpm(0) == 1

    def test_fast(self) -> None:
        assert rate_to_wpm(200) == 350


class TestSayProviderPlatformGuard:
    def test_non_darwin_raises(self) -> None:
        with (
            patch("punt_vox.providers.say.platform") as mock_platform,
            patch("punt_vox.providers.say.shutil"),
        ):
            mock_platform.system.return_value = "Linux"
            with pytest.raises(ValueError, match="requires macOS"):
                SayProvider()

    def test_no_say_command_raises(self) -> None:
        with (
            patch("punt_vox.providers.say.platform") as mock_platform,
            patch("punt_vox.providers.say.shutil") as mock_shutil,
        ):
            mock_platform.system.return_value = "Darwin"
            mock_shutil.which.return_value = None
            with pytest.raises(ValueError, match="say command not found"):
                SayProvider()

    def test_darwin_with_say_succeeds(self) -> None:
        with (
            patch("punt_vox.providers.say.platform") as mock_platform,
            patch("punt_vox.providers.say.shutil") as mock_shutil,
        ):
            mock_platform.system.return_value = "Darwin"
            mock_shutil.which.return_value = "/usr/bin/say"
            provider = SayProvider()
            assert provider.name == "say"


class TestSayProviderName:
    def test_name(self, say_provider: SayProvider) -> None:
        assert say_provider.name == "say"


class TestSayDefaultVoiceDynamic:
    def test_prefers_samantha(self, say_provider: SayProvider) -> None:
        assert say_provider.default_voice == "samantha"

    def test_falls_back_to_alex(self, say_provider: SayProvider) -> None:
        cache = say_provider._voices._cache  # pyright: ignore[reportPrivateUsage]
        cache.clear()
        cache["alex"] = SayVoiceConfig(name="Alex", locale="en_US")
        cache["anna"] = SayVoiceConfig(name="Anna", locale="de_DE")
        assert say_provider.default_voice == "alex"

    def test_falls_back_to_first_english(
        self,
        say_provider: SayProvider,
    ) -> None:
        cache = say_provider._voices._cache  # pyright: ignore[reportPrivateUsage]
        cache.clear()
        cache["karen"] = SayVoiceConfig(name="Karen", locale="en_AU")
        cache["anna"] = SayVoiceConfig(name="Anna", locale="de_DE")
        assert say_provider.default_voice == "karen"

    def test_falls_back_to_first_voice(
        self,
        say_provider: SayProvider,
    ) -> None:
        cache = say_provider._voices._cache  # pyright: ignore[reportPrivateUsage]
        cache.clear()
        cache["anna"] = SayVoiceConfig(name="Anna", locale="de_DE")
        assert say_provider.default_voice == "anna"

    def test_empty_voices_returns_samantha(
        self,
        say_provider: SayProvider,
    ) -> None:
        cache = say_provider._voices._cache  # pyright: ignore[reportPrivateUsage]
        cache.clear()
        assert say_provider.default_voice == "samantha"


class TestSayProviderResolveVoice:
    def test_resolve_cached_voice(self, say_provider: SayProvider) -> None:
        assert say_provider.resolve_voice("fred") == "Fred"

    def test_resolve_case_insensitive(self, say_provider: SayProvider) -> None:
        assert say_provider.resolve_voice("FRED") == "Fred"

    def test_resolve_german_voice(self, say_provider: SayProvider) -> None:
        assert say_provider.resolve_voice("anna") == "Anna"

    def test_unknown_voice_raises(self, say_provider: SayProvider) -> None:
        say_provider._voices._cache.clear()  # pyright: ignore[reportPrivateUsage]
        # Prevent reload from system
        say_provider._voices._loaded_at = 1.0  # pyright: ignore[reportPrivateUsage]
        with pytest.raises(VoiceNotFoundError) as exc_info:
            say_provider.resolve_voice("nonexistent")
        assert exc_info.value.voice_name == "nonexistent"

    def test_matching_language(self, say_provider: SayProvider) -> None:
        assert say_provider.resolve_voice("fred", language="en") == "Fred"

    def test_mismatching_language(self, say_provider: SayProvider) -> None:
        with pytest.raises(ValueError, match="does not support language 'de'"):
            say_provider.resolve_voice("fred", language="de")


class TestSayProviderSynthesize:
    def _mock_subprocess(self, mp3_bytes: bytes) -> MagicMock:
        def side_effect(
            args: list[str],
            **kwargs: object,
        ) -> subprocess.CompletedProcess[bytes]:
            if args[0] == "say":
                output_idx = args.index("-o")
                aiff_path = Path(args[output_idx + 1])
                aiff_path.write_bytes(b"FORM\x00\x00\x00\x00AIFF")
                return subprocess.CompletedProcess(args, 0)
            if args[0] == "ffmpeg":
                output_path = Path(args[-1])
                output_path.write_bytes(mp3_bytes)
                return subprocess.CompletedProcess(args, 0, b"", b"")
            msg = f"Unexpected subprocess call: {args}"
            raise ValueError(msg)

        return MagicMock(side_effect=side_effect)

    def test_synthesize_creates_file(
        self,
        say_provider: SayProvider,
        tmp_output_dir: Path,
    ) -> None:
        mp3_bytes = _get_valid_mp3_bytes()
        out = tmp_output_dir / "test.mp3"
        mock = self._mock_subprocess(mp3_bytes)
        with patch("punt_vox.providers.say.subprocess.run", mock):
            result = say_provider.synthesize(
                SynthesisRequest(text="hello", voice="fred"),
                out,
            )
        assert result.path == out
        assert out.exists()

    def test_synthesize_result_metadata(
        self,
        say_provider: SayProvider,
        tmp_output_dir: Path,
    ) -> None:
        mp3_bytes = _get_valid_mp3_bytes()
        out = tmp_output_dir / "test.mp3"
        mock = self._mock_subprocess(mp3_bytes)
        with patch("punt_vox.providers.say.subprocess.run", mock):
            result = say_provider.synthesize(
                SynthesisRequest(text="hello", voice="fred"),
                out,
            )
        assert result.provider == AudioProviderId.say
        assert result.voice == "Fred"
        assert result.language == "en"
        assert result.text == "hello"

    def test_synthesize_say_args(
        self,
        say_provider: SayProvider,
        tmp_output_dir: Path,
    ) -> None:
        mp3_bytes = _get_valid_mp3_bytes()
        out = tmp_output_dir / "test.mp3"
        mock_run = self._mock_subprocess(mp3_bytes)
        with patch("punt_vox.providers.say.subprocess.run", mock_run):
            say_provider.synthesize(
                SynthesisRequest(text="hello", voice="fred", rate=90),
                out,
            )
        say_call = mock_run.call_args_list[0]
        say_args = say_call[0][0]
        assert say_args[0] == "say"
        assert say_args[say_args.index("-v") + 1] == "Fred"
        assert say_args[say_args.index("-r") + 1] == "157"

    def test_synthesize_ffmpeg_via_convert(
        self,
        say_provider: SayProvider,
        tmp_output_dir: Path,
    ) -> None:
        mp3_bytes = _get_valid_mp3_bytes()
        out = tmp_output_dir / "test.mp3"
        mock_run = self._mock_subprocess(mp3_bytes)
        with patch("punt_vox.providers.say.subprocess.run", mock_run):
            say_provider.synthesize(
                SynthesisRequest(text="hello", voice="fred"),
                out,
            )
        ffmpeg_call = mock_run.call_args_list[1]
        ffmpeg_args = ffmpeg_call[0][0]
        assert ffmpeg_args[0] == "ffmpeg"
        assert "libmp3lame" in ffmpeg_args

    def test_synthesize_cleans_up_aiff(
        self,
        say_provider: SayProvider,
        tmp_output_dir: Path,
    ) -> None:
        mp3_bytes = _get_valid_mp3_bytes()
        out = tmp_output_dir / "test.mp3"
        aiff_paths: list[Path] = []
        original_mock = self._mock_subprocess(mp3_bytes)

        def tracking(
            args: list[str],
            **kwargs: object,
        ) -> subprocess.CompletedProcess[bytes]:
            if args[0] == "say":
                aiff_paths.append(Path(args[args.index("-o") + 1]))
            return original_mock(args, **kwargs)  # type: ignore[no-any-return]

        with patch(
            "punt_vox.providers.say.subprocess.run",
            side_effect=tracking,
        ):
            say_provider.synthesize(
                SynthesisRequest(text="hello", voice="fred"),
                out,
            )
        assert len(aiff_paths) == 1
        assert not aiff_paths[0].exists()

    def test_synthesize_infers_language(
        self,
        say_provider: SayProvider,
        tmp_output_dir: Path,
    ) -> None:
        mp3_bytes = _get_valid_mp3_bytes()
        out = tmp_output_dir / "test.mp3"
        mock = self._mock_subprocess(mp3_bytes)
        with patch("punt_vox.providers.say.subprocess.run", mock):
            result = say_provider.synthesize(
                SynthesisRequest(text="Hallo", voice="anna"),
                out,
            )
        assert result.language == "de"

    def test_synthesize_explicit_language(
        self,
        say_provider: SayProvider,
        tmp_output_dir: Path,
    ) -> None:
        mp3_bytes = _get_valid_mp3_bytes()
        out = tmp_output_dir / "test.mp3"
        mock = self._mock_subprocess(mp3_bytes)
        with patch("punt_vox.providers.say.subprocess.run", mock):
            result = say_provider.synthesize(
                SynthesisRequest(text="hello", voice="fred", language="en"),
                out,
            )
        assert result.language == "en"


class TestSayDirectPlayerPlayDirectly:
    def test_spawns_without_o_flag(self, say_provider: SayProvider) -> None:
        player = SayDirectPlayer(voices=say_provider._voices)  # pyright: ignore[reportPrivateUsage]
        mock = MagicMock(
            return_value=subprocess.CompletedProcess([], 0, b"", b""),
        )
        with patch("punt_vox.providers.local_play.subprocess.run", mock):
            rc = player.play_directly(
                SynthesisRequest(text="hello", voice="fred"),
            )
        assert rc == 0
        args = mock.call_args[0][0]
        assert args[0] == "say"
        assert "-o" not in args

    def test_nonzero_rc_returned(self, say_provider: SayProvider) -> None:
        player = SayDirectPlayer(voices=say_provider._voices)  # pyright: ignore[reportPrivateUsage]
        mock = MagicMock(
            return_value=subprocess.CompletedProcess([], 5, b"", b"oops"),
        )
        with patch("punt_vox.providers.local_play.subprocess.run", mock):
            rc = player.play_directly(
                SynthesisRequest(text="hello", voice="fred"),
            )
        assert rc == 5

    def test_strips_vibe_tags(self, say_provider: SayProvider) -> None:
        player = SayDirectPlayer(voices=say_provider._voices)  # pyright: ignore[reportPrivateUsage]
        mock = MagicMock(
            return_value=subprocess.CompletedProcess([], 0, b"", b""),
        )
        with patch("punt_vox.providers.local_play.subprocess.run", mock):
            player.play_directly(
                SynthesisRequest(text="[serious] Hello world", voice="fred"),
            )
        args = mock.call_args[0][0]
        assert "Hello world" in args
        assert "[serious]" not in " ".join(args)


class TestSayProviderCheckHealth:
    def test_darwin_with_say(self) -> None:
        with (
            patch("punt_vox.providers.say.platform") as mock_platform,
            patch("punt_vox.providers.say.shutil") as mock_shutil,
        ):
            mock_platform.system.return_value = "Darwin"
            mock_shutil.which.return_value = "/usr/bin/say"
            provider = SayProvider()
            voice = SayVoiceConfig(name="Samantha", locale="en_US")
            provider._voices._cache["samantha"] = voice  # pyright: ignore[reportPrivateUsage]
            provider._voices._loaded_at = 1.0  # pyright: ignore[reportPrivateUsage]
            checks = provider.check_health()
        assert len(checks) == 2
        assert checks[0].passed
        assert "/usr/bin/say" in checks[0].message
        assert checks[1].passed
        assert "default voice: Samantha" in checks[1].message

    def test_default_voice_unavailable(self) -> None:
        with (
            patch("punt_vox.providers.say.platform") as mock_platform,
            patch("punt_vox.providers.say.shutil") as mock_shutil,
        ):
            mock_platform.system.return_value = "Darwin"
            mock_shutil.which.return_value = "/usr/bin/say"
            provider = SayProvider()
        # Replace the loader so _ensure_loaded returns empty results
        provider._voices._loader = dict  # pyright: ignore[reportPrivateUsage]
        provider._voices._cache.clear()  # pyright: ignore[reportPrivateUsage]
        provider._voices._loaded_at = 0.0  # pyright: ignore[reportPrivateUsage]
        with (
            patch("punt_vox.providers.say.platform") as mock_platform,
            patch("punt_vox.providers.say.shutil") as mock_shutil,
        ):
            mock_platform.system.return_value = "Darwin"
            mock_shutil.which.return_value = "/usr/bin/say"
            checks = provider.check_health()
        assert len(checks) == 2
        assert not checks[1].passed
        assert "default voice unavailable" in checks[1].message

    def test_non_darwin(self) -> None:
        with (
            patch("punt_vox.providers.say.platform") as mock_platform,
            patch("punt_vox.providers.say.shutil") as mock_shutil,
        ):
            mock_platform.system.return_value = "Darwin"
            mock_shutil.which.return_value = "/usr/bin/say"
            provider = SayProvider()
        with patch("punt_vox.providers.say.platform") as mock_platform:
            mock_platform.system.return_value = "Linux"
            checks = provider.check_health()
        assert len(checks) == 1
        assert not checks[0].passed
        assert "requires macOS" in checks[0].message

    def test_darwin_without_say(self) -> None:
        with (
            patch("punt_vox.providers.say.platform") as mock_platform,
            patch("punt_vox.providers.say.shutil") as mock_shutil,
        ):
            mock_platform.system.return_value = "Darwin"
            mock_shutil.which.return_value = "/usr/bin/say"
            provider = SayProvider()
        with (
            patch("punt_vox.providers.say.platform") as mock_platform,
            patch("punt_vox.providers.say.shutil") as mock_shutil,
        ):
            mock_platform.system.return_value = "Darwin"
            mock_shutil.which.return_value = None
            checks = provider.check_health()
        assert len(checks) == 1
        assert not checks[0].passed


class TestSayProviderGetDefaultVoice:
    def test_english(self, say_provider: SayProvider) -> None:
        assert say_provider.get_default_voice("en") == "samantha"

    def test_german(self, say_provider: SayProvider) -> None:
        assert say_provider.get_default_voice("de") == "anna"

    def test_unknown_language(self, say_provider: SayProvider) -> None:
        with pytest.raises(ValueError, match="No default voice"):
            say_provider.get_default_voice("xx")

    def test_case_insensitive(self, say_provider: SayProvider) -> None:
        assert say_provider.get_default_voice("EN") == "samantha"


class TestSayProviderListVoices:
    def test_all_voices(self, say_provider: SayProvider) -> None:
        voices = say_provider.list_voices()
        assert "fred" in voices
        assert "anna" in voices
        assert "samantha" in voices

    def test_filter_by_language(self, say_provider: SayProvider) -> None:
        voices = say_provider.list_voices(language="en")
        assert "fred" in voices
        assert "anna" not in voices

    def test_filter_by_german(self, say_provider: SayProvider) -> None:
        voices = say_provider.list_voices(language="de")
        assert "anna" in voices
        assert "fred" not in voices

    def test_sorted(self, say_provider: SayProvider) -> None:
        voices = say_provider.list_voices()
        assert voices == sorted(voices)


class TestSayProviderInferLanguage:
    def test_english_voice(self, say_provider: SayProvider) -> None:
        assert say_provider.infer_language_from_voice("fred") == "en"

    def test_german_voice(self, say_provider: SayProvider) -> None:
        assert say_provider.infer_language_from_voice("anna") == "de"


# The old ``TestAutoDetectSayFallback`` covered ``auto_detect_provider``,
# which is deleted (design §3.3): the daemon never invents a provider,
# state names it. Preference (the ``vox enable`` proposal) is answered
# by :meth:`ProviderCredentials.preferred`, covered by
# tests/test_credentials.py.
