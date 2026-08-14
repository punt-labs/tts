"""Tests for punt_vox.providers.openai."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from punt_vox.core import (
    _split_at_words,  # pyright: ignore[reportPrivateUsage]
    split_text,
)
from punt_vox.providers.openai import OpenAIProvider
from punt_vox.types import SynthesisRequest, VoiceNotFoundError


class TestSplitText:
    def test_short_text_no_split(self) -> None:
        result = split_text("Hello world.", max_chars=4096)
        assert result == ["Hello world."]

    def test_exact_limit_no_split(self) -> None:
        text = "a" * 4096
        result = split_text(text, max_chars=4096)
        assert result == [text]

    def test_sentence_boundary_split(self) -> None:
        # Two sentences that together exceed the limit.
        s1 = "A" * 50 + "."
        s2 = "B" * 50 + "."
        text = f"{s1} {s2}"
        result = split_text(text, max_chars=60)
        assert len(result) == 2
        assert result[0] == s1
        assert result[1] == s2

    def test_multiple_sentence_accumulation(self) -> None:
        # Three short sentences: first two fit together, third forces new chunk.
        text = "One. Two. Three."
        result = split_text(text, max_chars=10)
        assert len(result) >= 2
        # All original text is preserved across chunks.
        assert "".join(result).replace(" ", "") == text.replace(" ", "")

    def test_word_boundary_fallback(self) -> None:
        # A single sentence exceeding the limit — no sentence breaks available.
        words = ["word"] * 20
        text = " ".join(words)  # 99 chars
        result = split_text(text, max_chars=30)
        assert len(result) > 1
        for chunk in result:
            assert len(chunk) <= 30

    def test_empty_string(self) -> None:
        result = split_text("", max_chars=4096)
        assert result == [""]

    def test_exclamation_and_question_splits(self) -> None:
        text = "Stop! Why? Because."
        result = split_text(text, max_chars=8)
        assert len(result) >= 2
        # Verify punctuation is preserved.
        combined = " ".join(result)
        assert "Stop!" in combined
        assert "Why?" in combined

    def test_oversized_word_gets_character_split(self) -> None:
        text = "a" * 100
        result = split_text(text, max_chars=30)
        assert len(result) == 4  # 30+30+30+10
        for chunk in result:
            assert len(chunk) <= 30
        assert "".join(result) == text

    def test_trailing_punctuation_whitespace_no_empty_chunks(self) -> None:
        # re.split() can yield empty strings at boundaries; verify none leak through.
        text = "Hello. World. "
        result = split_text(text, max_chars=8)
        for chunk in result:
            assert chunk  # no empty strings

    def test_all_chunks_within_limit(self) -> None:
        text = "short " + "x" * 50 + " words here"
        result = split_text(text, max_chars=20)
        for chunk in result:
            assert len(chunk) <= 20


class TestSplitAtWords:
    def test_oversized_word_split(self) -> None:
        result = _split_at_words("a" * 100, max_chars=30)
        assert len(result) == 4
        for chunk in result:
            assert len(chunk) <= 30
        assert "".join(result) == "a" * 100

    def test_mixed_normal_and_oversized(self) -> None:
        text = "hello " + "x" * 50 + " world"
        result = _split_at_words(text, max_chars=20)
        for chunk in result:
            assert len(chunk) <= 20
        assert "".join(c.strip() for c in result) == text.replace(" ", "")

    def test_normal_words_unchanged(self) -> None:
        result = _split_at_words("one two three", max_chars=20)
        assert result == ["one two three"]


class TestOpenAIProviderName:
    def test_name(self) -> None:
        provider = OpenAIProvider(client=MagicMock())
        assert provider.name == "openai"


class TestOpenAIProviderResolveVoice:
    def test_resolve_known_voice(self) -> None:
        provider = OpenAIProvider(client=MagicMock())
        assert provider.resolve_voice("alloy") == "alloy"

    def test_resolve_case_insensitive(self) -> None:
        provider = OpenAIProvider(client=MagicMock())
        assert provider.resolve_voice("NOVA") == "nova"

    def test_resolve_mixed_case(self) -> None:
        provider = OpenAIProvider(client=MagicMock())
        assert provider.resolve_voice("Shimmer") == "shimmer"

    def test_resolve_unknown_raises(self) -> None:
        provider = OpenAIProvider(client=MagicMock())
        with pytest.raises(VoiceNotFoundError) as exc_info:
            provider.resolve_voice("nonexistent")
        assert exc_info.value.voice_name == "nonexistent"
        assert isinstance(exc_info.value.available, list)
        assert len(exc_info.value.available) > 0

    def test_error_lists_available(self) -> None:
        provider = OpenAIProvider(client=MagicMock())
        with pytest.raises(VoiceNotFoundError) as exc_info:
            provider.resolve_voice("bad")
        assert "alloy" in exc_info.value.available


class TestOpenAIProviderSynthesize:
    def test_synthesize_creates_file(
        self,
        openai_provider: OpenAIProvider,
        tmp_output_dir: Path,
    ) -> None:
        request = SynthesisRequest(text="hello", voice="alloy", rate=90)
        out = tmp_output_dir / "test.mp3"

        result = openai_provider.synthesize(request, out)

        assert result.path == out
        assert out.exists()
        assert out.stat().st_size > 0

    def test_synthesize_rate_conversion(
        self,
        mock_openai_client: MagicMock,
        openai_provider: OpenAIProvider,
        tmp_output_dir: Path,
    ) -> None:
        """rate=90 should become speed=0.9."""
        request = SynthesisRequest(text="test", voice="echo", rate=90)
        out = tmp_output_dir / "rate.mp3"

        openai_provider.synthesize(request, out)

        call_kwargs = mock_openai_client.audio.speech.create.call_args.kwargs
        assert call_kwargs["speed"] == pytest.approx(0.9)  # pyright: ignore[reportUnknownMemberType]

    def test_synthesize_rate_clamped_low(
        self,
        mock_openai_client: MagicMock,
        openai_provider: OpenAIProvider,
        tmp_output_dir: Path,
    ) -> None:
        """rate=10 should be clamped to speed=0.25."""
        request = SynthesisRequest(text="test", voice="alloy", rate=10)
        out = tmp_output_dir / "slow.mp3"

        openai_provider.synthesize(request, out)

        call_kwargs = mock_openai_client.audio.speech.create.call_args.kwargs
        assert call_kwargs["speed"] == pytest.approx(0.25)  # pyright: ignore[reportUnknownMemberType]

    def test_synthesize_rate_clamped_high(
        self,
        mock_openai_client: MagicMock,
        openai_provider: OpenAIProvider,
        tmp_output_dir: Path,
    ) -> None:
        """rate=500 should be clamped to speed=4.0."""
        request = SynthesisRequest(text="test", voice="alloy", rate=500)
        out = tmp_output_dir / "fast.mp3"

        openai_provider.synthesize(request, out)

        call_kwargs = mock_openai_client.audio.speech.create.call_args.kwargs
        assert call_kwargs["speed"] == pytest.approx(4.0)  # pyright: ignore[reportUnknownMemberType]

    def test_synthesize_passes_model(
        self,
        mock_openai_client: MagicMock,
        tmp_output_dir: Path,
    ) -> None:
        provider = OpenAIProvider(model="tts-1-hd", client=mock_openai_client)
        request = SynthesisRequest(text="test", voice="alloy")
        out = tmp_output_dir / "model.mp3"

        provider.synthesize(request, out)

        call_kwargs = mock_openai_client.audio.speech.create.call_args.kwargs
        assert call_kwargs["model"] == "tts-1-hd"

    def test_synthesize_passes_voice(
        self,
        mock_openai_client: MagicMock,
        openai_provider: OpenAIProvider,
        tmp_output_dir: Path,
    ) -> None:
        request = SynthesisRequest(text="test", voice="coral")
        out = tmp_output_dir / "voice.mp3"

        openai_provider.synthesize(request, out)

        call_kwargs = mock_openai_client.audio.speech.create.call_args.kwargs
        assert call_kwargs["voice"] == "coral"

    def test_synthesize_result_metadata(
        self,
        openai_provider: OpenAIProvider,
        tmp_output_dir: Path,
    ) -> None:
        request = SynthesisRequest(text="hello world", voice="sage")
        out = tmp_output_dir / "meta.mp3"

        result = openai_provider.synthesize(request, out)

        assert result.text == "hello world"
        assert result.voice == "sage"
        assert result.path == out

    def test_synthesize_chunked_text(
        self,
        mock_openai_client: MagicMock,
        tmp_output_dir: Path,
    ) -> None:
        """Text exceeding 4096 chars should trigger chunked synthesis."""
        provider = OpenAIProvider(client=mock_openai_client)
        long_text = "Hello. " * 1000  # ~7000 chars
        request = SynthesisRequest(text=long_text.strip(), voice="alloy")
        out = tmp_output_dir / "chunked.mp3"

        result = provider.synthesize(request, out)

        assert result.path == out
        assert out.exists()
        # Should have been called multiple times (chunked).
        assert mock_openai_client.audio.speech.create.call_count > 1


class TestOpenAIProviderDefaultModel:
    def test_default_model(self) -> None:
        provider = OpenAIProvider(client=MagicMock())
        # Access internal to verify default — acceptable in tests.
        assert provider._model == "tts-1"  # pyright: ignore[reportPrivateUsage]

    def test_explicit_model(self) -> None:
        provider = OpenAIProvider(model="tts-1-hd", client=MagicMock())
        assert provider._model == "tts-1-hd"  # pyright: ignore[reportPrivateUsage]

    @patch.dict("os.environ", {"TTS_MODEL": "tts-1-hd"})
    def test_env_var_does_not_override_default(self) -> None:
        """State (or an explicit kwarg) is authoritative -- not the env.

        The ``TTS_MODEL`` env probe was the same substitution defect the
        provider gate closes, one field over (design §3.9). With no
        explicit ``model=`` and the env var set to a different value, the
        constructor still picks the provider's own ``_DEFAULT_MODEL``.
        """
        provider = OpenAIProvider(client=MagicMock())
        assert provider._model == "tts-1"  # pyright: ignore[reportPrivateUsage]

    @patch.dict("os.environ", {"TTS_MODEL": "tts-1-hd"})
    def test_explicit_wins_over_env(self) -> None:
        provider = OpenAIProvider(model="tts-1", client=MagicMock())
        assert provider._model == "tts-1"  # pyright: ignore[reportPrivateUsage]


class TestOpenAIProviderCheckHealth:
    @patch.dict("os.environ", {"OPENAI_API_KEY": "sk-test"})
    def test_all_pass(self, mock_openai_client: MagicMock) -> None:
        mock_openai_client.models.retrieve.return_value = MagicMock()

        provider = OpenAIProvider(client=mock_openai_client)
        checks = provider.check_health()

        assert len(checks) == 2
        assert all(c.passed for c in checks)
        assert "API key: set" in checks[0].message
        assert "model access" in checks[1].message

    @patch.dict("os.environ", {}, clear=True)
    def test_missing_api_key(self) -> None:
        # Ensure OPENAI_API_KEY is not set.
        import os

        os.environ.pop("OPENAI_API_KEY", None)

        provider = OpenAIProvider(client=MagicMock())
        checks = provider.check_health()

        assert len(checks) == 1
        assert not checks[0].passed
        assert "not set" in checks[0].message

    @patch.dict("os.environ", {"OPENAI_API_KEY": "sk-test"})
    def test_auth_error(self, mock_openai_client: MagicMock) -> None:
        import openai

        mock_openai_client.models.retrieve.side_effect = openai.AuthenticationError(
            message="invalid key",
            response=MagicMock(status_code=401),
            body=None,
        )

        provider = OpenAIProvider(client=mock_openai_client)
        checks = provider.check_health()

        assert len(checks) == 2
        assert checks[0].passed  # Key is set.
        assert not checks[1].passed
        assert "invalid API key" in checks[1].message

    @patch.dict("os.environ", {"OPENAI_API_KEY": "sk-test"})
    def test_model_not_found(self, mock_openai_client: MagicMock) -> None:
        import openai

        mock_openai_client.models.retrieve.side_effect = openai.NotFoundError(
            message="not found",
            response=MagicMock(status_code=404),
            body=None,
        )

        provider = OpenAIProvider(client=mock_openai_client)
        checks = provider.check_health()

        assert len(checks) == 2
        assert checks[0].passed
        assert not checks[1].passed
        assert "not found" in checks[1].message


class TestOpenAIProviderLanguageSupport:
    def test_resolve_voice_with_language(self) -> None:
        provider = OpenAIProvider(client=MagicMock())
        assert provider.resolve_voice("nova", language="de") == "nova"

    def test_get_default_voice_any_language(self) -> None:
        provider = OpenAIProvider(client=MagicMock())
        assert provider.get_default_voice("de") == "nova"
        assert provider.get_default_voice("ja") == "nova"

    def test_list_voices_ignores_language(self) -> None:
        provider = OpenAIProvider(client=MagicMock())
        all_voices = provider.list_voices()
        filtered = provider.list_voices(language="de")
        assert all_voices == filtered

    def test_list_voices_sorted(self) -> None:
        provider = OpenAIProvider(client=MagicMock())
        voices = provider.list_voices()
        assert voices == sorted(voices)
        assert "alloy" in voices
        assert "nova" in voices

    def test_infer_language_returns_none(self) -> None:
        provider = OpenAIProvider(client=MagicMock())
        assert provider.infer_language_from_voice("nova") is None

    def test_synthesize_preserves_language(
        self,
        openai_provider: OpenAIProvider,
        tmp_output_dir: Path,
    ) -> None:
        request = SynthesisRequest(text="Guten Tag", voice="nova", language="de")
        result = openai_provider.synthesize(request, tmp_output_dir / "test.mp3")
        assert result.language == "de"

    def test_synthesize_no_language(
        self,
        openai_provider: OpenAIProvider,
        tmp_output_dir: Path,
    ) -> None:
        request = SynthesisRequest(text="hello", voice="nova")
        result = openai_provider.synthesize(request, tmp_output_dir / "test.mp3")
        assert result.language is None
