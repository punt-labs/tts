"""Tests for punt_vox.types."""

from __future__ import annotations

from pathlib import Path

import pytest

from punt_vox.types import (
    SUPPORTED_LANGUAGES,
    AudioProviderId,
    HealthCheck,
    MergeStrategy,
    MusicProvider,
    MusicRequest,
    MusicResult,
    SynthesisRequest,
    SynthesisResult,
    VoiceNotFoundError,
    generate_filename,
    result_to_dict,
    validate_language,
)


class TestHealthCheck:
    def test_defaults_to_required(self) -> None:
        check = HealthCheck(passed=True, message="ok")
        assert check.required is True

    def test_optional_check(self) -> None:
        check = HealthCheck(passed=False, message="fail", required=False)
        assert check.required is False

    def test_frozen(self) -> None:
        check = HealthCheck(passed=True, message="ok")
        with pytest.raises(AttributeError):
            check.passed = False  # type: ignore[misc]


class TestMergeStrategy:
    def test_separate_value(self) -> None:
        assert MergeStrategy.ONE_FILE_PER_INPUT.value == "separate"

    def test_single_value(self) -> None:
        assert MergeStrategy.ONE_FILE_PER_BATCH.value == "single"


class TestSynthesisRequest:
    def test_default_rate(self) -> None:
        req = SynthesisRequest(text="hello", voice="joanna")
        assert req.rate is None

    def test_custom_rate(self) -> None:
        req = SynthesisRequest(text="hello", voice="joanna", rate=100)
        assert req.rate == 100

    def test_frozen(self) -> None:
        req = SynthesisRequest(text="hello", voice="joanna")
        with pytest.raises(AttributeError):
            req.text = "world"  # type: ignore[misc]

    def test_voice_is_string(self) -> None:
        req = SynthesisRequest(text="hello", voice="hans")
        assert req.voice == "hans"

    def test_language_default_none(self) -> None:
        req = SynthesisRequest(text="hello", voice="joanna")
        assert req.language is None

    def test_language_set(self) -> None:
        req = SynthesisRequest(text="Guten Tag", voice="daniel", language="de")
        assert req.language == "de"


class TestSynthesisResult:
    def test_to_dict(self) -> None:
        result = SynthesisResult(
            path=Path("/tmp/test.mp3"),
            text="hello",
            provider=AudioProviderId.openai,
            voice="Joanna",
        )
        d = result_to_dict(result)
        assert d["path"] == "/tmp/test.mp3"
        assert d["text"] == "hello"
        assert d["voice"] == "Joanna"
        assert "language" not in d

    def test_to_dict_with_language(self) -> None:
        result = SynthesisResult(
            path=Path("/tmp/test.mp3"),
            text="Guten Tag",
            provider=AudioProviderId.openai,
            voice="Daniel",
            language="de",
        )
        d = result_to_dict(result)
        assert d["language"] == "de"

    def test_language_default_none(self) -> None:
        result = SynthesisResult(
            path=Path("/tmp/test.mp3"),
            text="hello",
            provider=AudioProviderId.openai,
            voice="Joanna",
        )
        assert result.language is None


class TestValidateLanguage:
    def test_valid_code(self) -> None:
        assert validate_language("de") == "de"

    def test_normalizes_case(self) -> None:
        assert validate_language("DE") == "de"

    def test_strips_whitespace(self) -> None:
        assert validate_language(" fr ") == "fr"

    def test_rejects_too_long(self) -> None:
        with pytest.raises(ValueError, match="Invalid language code"):
            validate_language("deu")

    def test_rejects_too_short(self) -> None:
        with pytest.raises(ValueError, match="Invalid language code"):
            validate_language("d")

    def test_rejects_empty(self) -> None:
        with pytest.raises(ValueError, match="Invalid language code"):
            validate_language("")

    def test_rejects_digits(self) -> None:
        with pytest.raises(ValueError, match="Invalid language code"):
            validate_language("d1")

    def test_rejects_non_ascii(self) -> None:
        with pytest.raises(ValueError, match="Invalid language code"):
            validate_language("dé")


class TestSupportedLanguages:
    def test_has_common_languages(self) -> None:
        for code in ("de", "en", "es", "fr", "ja", "ko", "ru", "zh"):
            assert code in SUPPORTED_LANGUAGES

    def test_values_are_strings(self) -> None:
        for name in SUPPORTED_LANGUAGES.values():
            assert isinstance(name, str)
            assert len(name) > 0


class TestGenerateFilename:
    def test_deterministic(self) -> None:
        name1 = generate_filename("hello")
        name2 = generate_filename("hello")
        assert name1 == name2

    def test_different_text_different_name(self) -> None:
        name1 = generate_filename("hello")
        name2 = generate_filename("world")
        assert name1 != name2

    def test_ends_with_mp3(self) -> None:
        name = generate_filename("test")
        assert name.endswith(".mp3")

    def test_prefix(self) -> None:
        name = generate_filename("test", prefix="pair_")
        assert name.startswith("pair_")
        assert name.endswith(".mp3")

    def test_no_prefix(self) -> None:
        name = generate_filename("test")
        assert not name.startswith("pair_")


class TestMusicRequest:
    def test_required_fields(self) -> None:
        req = MusicRequest(prompt="ambient techno", duration_ms=120000)
        assert req.prompt == "ambient techno"
        assert req.duration_ms == 120000

    def test_optional_fields_default_none(self) -> None:
        req = MusicRequest(prompt="jazz", duration_ms=60000)
        assert req.style is None
        assert req.vibe is None
        assert req.vibe_tags is None

    def test_all_fields(self) -> None:
        req = MusicRequest(
            prompt="techno music, happy mood",
            duration_ms=120000,
            style="techno",
            vibe="happy",
            vibe_tags="[cheerful]",
        )
        assert req.style == "techno"
        assert req.vibe == "happy"
        assert req.vibe_tags == "[cheerful]"

    def test_frozen(self) -> None:
        req = MusicRequest(prompt="test", duration_ms=60000)
        with pytest.raises(AttributeError):
            req.prompt = "changed"  # type: ignore[misc]


class TestMusicResult:
    def test_required_fields(self) -> None:
        result = MusicResult(
            path=Path("/home/user/vox-output/music/track.mp3"),
            duration_ms=120000,
            prompt="ambient techno",
        )
        assert result.path == Path("/home/user/vox-output/music/track.mp3")
        assert result.duration_ms == 120000
        assert result.prompt == "ambient techno"

    def test_frozen(self) -> None:
        result = MusicResult(
            path=Path("/tmp/track.mp3"),
            duration_ms=60000,
            prompt="test",
        )
        with pytest.raises(AttributeError):
            result.path = Path("/other.mp3")  # type: ignore[misc]


class TestMusicProvider:
    def test_protocol_is_runtime_checkable(self) -> None:
        assert isinstance(MusicProvider, type)

    def test_conforming_class_is_recognized(self) -> None:
        class FakeMusic:
            async def generate_track(
                self, prompt: str, duration_ms: int, output_path: Path
            ) -> Path:
                return output_path

        assert isinstance(FakeMusic(), MusicProvider)

    def test_non_conforming_class_is_rejected(self) -> None:
        class NotMusic:
            def unrelated(self) -> None: ...

        assert not isinstance(NotMusic(), MusicProvider)


class TestVoiceNotFoundError:
    """The F5 error carries structured fields and renders a user-facing sentence.

    Both matter separately: a wire surface renders via ``str(exc)``, while a
    programmatic caller inspects the properties. The rendering was silently
    a tuple repr before ``__str__`` was overridden -- a substring assertion
    would have passed either way, which is why these tests check the whole
    string.
    """

    def test_str_renders_the_f5_sentence(self) -> None:
        exc = VoiceNotFoundError("bella", ["alloy", "ash", "ballad"])

        assert str(exc) == "bella (available: alloy, ash, ballad)"

    def test_str_is_not_the_tuple_repr(self) -> None:
        """Regression: without ``__str__`` this would render as the args tuple.

        The defect this test guards against: ``BaseException.__init__`` runs
        after ``__new__`` and overwrites ``args`` with the constructor's
        original positional arguments, so a message stashed in
        ``super().__new__(cls, msg)`` round-trips out as
        ``"('bella', ['alloy', ...])"``. The ``__str__`` override is the fix;
        this test names the wrong shape so a future edit removing the
        override fails visibly.
        """
        rendered = str(VoiceNotFoundError("bella", ["alloy"]))

        assert not rendered.startswith("(")
        assert "'bella'" not in rendered  # the tuple repr quotes 'bella'
        assert "available" in rendered

    def test_properties_expose_the_structured_fields(self) -> None:
        exc = VoiceNotFoundError("bella", ["alloy", "ash"])

        assert exc.voice_name == "bella"
        assert exc.available == ["alloy", "ash"]

    def test_available_returns_a_copy(self) -> None:
        """The property returns a copy so a caller cannot mutate the error's state."""
        available = ["alloy"]
        exc = VoiceNotFoundError("bella", available)

        got = exc.available
        got.append("mutation")
        assert exc.available == ["alloy"]
