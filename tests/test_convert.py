"""Tests for :mod:`punt_vox.providers.convert`."""

from __future__ import annotations

from punt_vox.providers.convert import estimate_speech_duration_s


class TestEstimateSpeechDurationS:
    def test_empty_string_is_zero(self) -> None:
        assert estimate_speech_duration_s("") == 0.0

    def test_whitespace_only_is_zero(self) -> None:
        assert estimate_speech_duration_s("   \n\t  ") == 0.0

    def test_normal_text_uses_word_count_at_default_wpm(self) -> None:
        # 175 WPM default -> 175 words takes 60s.
        text = " ".join(["word"] * 175)
        assert estimate_speech_duration_s(text) == 60.0

    def test_custom_wpm_scales_the_estimate(self) -> None:
        text = " ".join(["word"] * 100)
        # 100 WPM -> 100 words takes 60s.
        assert estimate_speech_duration_s(text, wpm=100) == 60.0

    def test_single_very_long_token_is_not_treated_as_one_short_word(self) -> None:
        # A 400-character unbroken token (e.g. a URL or file path) counts
        # as exactly one "word" under a naive whitespace split -- the
        # word-count estimate alone would report a fraction of a second.
        # The character-count floor must dominate instead.
        long_token = "a" * 400
        word_based = estimate_speech_duration_s("short")
        long_token_based = estimate_speech_duration_s(long_token)
        assert long_token_based > word_based
        # 400 chars at the character-count floor (~17.5 chars/s) is well
        # over 20 real seconds -- nowhere near the sub-second estimate a
        # pure word count would produce for "one word".
        assert long_token_based > 20.0

    def test_short_text_uses_word_based_estimate_not_char_floor(self) -> None:
        # For ordinary text the word-based estimate should already exceed
        # the character floor, so the max() picks the word-based value.
        text = "the quick brown fox jumps over the lazy dog"
        estimate = estimate_speech_duration_s(text)
        word_count = len(text.split())
        expected_word_based = word_count / (175 / 60.0)
        assert estimate == expected_word_based

    def test_negative_or_zero_wpm_not_exercised_default_only(self) -> None:
        # Boundary: a single word at the default rate is a small positive
        # duration, never zero or negative.
        estimate = estimate_speech_duration_s("hello")
        assert estimate > 0.0
