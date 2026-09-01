"""Tests for punt_vox.normalize — programmer string normalization."""

from __future__ import annotations

import pytest

from punt_vox.normalize import normalize_for_speech, strip_vibe_tags

# ---------------------------------------------------------------------------
# snake_case splitting
# ---------------------------------------------------------------------------


class TestSnakeCase:
    def test_simple_snake_case(self) -> None:
        assert normalize_for_speech("hello_world") == "hello world"

    def test_eof_received(self) -> None:
        assert normalize_for_speech("eof_received") == "E O F received"

    def test_multiple_underscores(self) -> None:
        assert normalize_for_speech("on_data_received") == "on data received"

    def test_leading_underscore(self) -> None:
        # Leading underscore → empty first part, skipped
        assert normalize_for_speech("_private") == "private"

    def test_trailing_underscore(self) -> None:
        assert normalize_for_speech("value_") == "value"

    def test_all_caps_snake(self) -> None:
        assert normalize_for_speech("MAX_RETRY_COUNT") == "MAX RETRY COUNT"

    def test_mixed_snake_camel(self) -> None:
        assert normalize_for_speech("get_fileName") == "get file name"

    def test_mixed_snake_camel_with_abbreviation(self) -> None:
        assert normalize_for_speech("read_stdin") == "read standard input"


# ---------------------------------------------------------------------------
# camelCase / PascalCase splitting
# ---------------------------------------------------------------------------


class TestCamelCase:
    def test_simple_camel(self) -> None:
        assert normalize_for_speech("fileName") == "file name"

    def test_pascal_case(self) -> None:
        assert normalize_for_speech("FileName") == "file name"

    def test_multiple_humps(self) -> None:
        assert normalize_for_speech("onDataReceived") == "on data received"

    def test_acronym_prefix(self) -> None:
        # HTMLParser → HTML Parser → H T M L parser
        assert normalize_for_speech("HTMLParser") == "H T M L parser"

    def test_acronym_suffix(self) -> None:
        assert normalize_for_speech("parseHTML") == "parse H T M L"


# ---------------------------------------------------------------------------
# Abbreviation expansion
# ---------------------------------------------------------------------------


class TestAbbreviations:
    def test_stderr(self) -> None:
        assert normalize_for_speech("stderr") == "standard error"

    def test_stdout(self) -> None:
        assert normalize_for_speech("stdout") == "standard output"

    def test_stdin(self) -> None:
        assert normalize_for_speech("stdin") == "standard input"

    def test_eof_standalone(self) -> None:
        assert normalize_for_speech("eof") == "E O F"

    def test_case_insensitive(self) -> None:
        assert normalize_for_speech("STDERR") == "standard error"

    def test_abbreviation_in_sentence(self) -> None:
        assert (
            normalize_for_speech("Check stderr for errors")
            == "Check standard error for errors"
        )

    def test_abbreviation_with_punctuation(self) -> None:
        assert normalize_for_speech("(stderr)") == "standard error"

    def test_repo(self) -> None:
        assert normalize_for_speech("repo") == "repository"

    def test_config(self) -> None:
        assert normalize_for_speech("config") == "configuration"

    def test_lol(self) -> None:
        assert normalize_for_speech("lol") == "laughing out loud"

    def test_rofl(self) -> None:
        assert normalize_for_speech("ROFL") == "rolling on the floor laughing"

    def test_lmao(self) -> None:
        assert normalize_for_speech("lmao") == "laughing my ass off"

    def test_lmfao(self) -> None:
        assert normalize_for_speech("lmfao") == "laughing my head off"

    def test_smh(self) -> None:
        assert normalize_for_speech("smh") == "shaking my head"

    def test_omg(self) -> None:
        assert normalize_for_speech("OMG") == "oh my god"

    def test_wtf(self) -> None:
        assert normalize_for_speech("WTF") == "what the heck"


# ---------------------------------------------------------------------------
# File paths — should be left alone
# ---------------------------------------------------------------------------


class TestFilePaths:
    def test_absolute_path(self) -> None:
        assert normalize_for_speech("/usr/local/bin") == "/usr/local/bin"

    def test_home_path(self) -> None:
        assert normalize_for_speech("~/Documents") == "~/Documents"

    def test_relative_path(self) -> None:
        assert normalize_for_speech("./src/main.py") == "./src/main.py"

    def test_path_in_sentence(self) -> None:
        result = normalize_for_speech("Check /usr/local/bin for binaries")
        assert "/usr/local/bin" in result


# ---------------------------------------------------------------------------
# Mixed text
# ---------------------------------------------------------------------------


class TestMixedText:
    def test_natural_english_unchanged(self) -> None:
        text = "This is a normal sentence."
        assert normalize_for_speech(text) == text

    def test_mixed_programmer_and_english(self) -> None:
        result = normalize_for_speech("The eof_received handler uses stderr")
        assert result == "The E O F received handler uses standard error"

    def test_empty_string(self) -> None:
        assert normalize_for_speech("") == ""

    def test_whitespace_only(self) -> None:
        assert normalize_for_speech("   ") == ""

    def test_single_word(self) -> None:
        assert normalize_for_speech("hello") == "hello"

    def test_preserves_numbers(self) -> None:
        assert normalize_for_speech("port 8080") == "port 8080"


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------


class TestIdempotency:
    @pytest.mark.parametrize(
        "text",
        [
            "eof_received",
            "fileName",
            "stderr",
            "The eof_received handler uses stderr",
            "normal sentence",
            "/usr/local/bin",
        ],
    )
    def test_idempotent(self, text: str) -> None:
        once = normalize_for_speech(text)
        twice = normalize_for_speech(once)
        assert once == twice


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


class TestEdgeCases:
    def test_single_char(self) -> None:
        assert normalize_for_speech("x") == "x"

    def test_all_caps_short(self) -> None:
        # Two-letter ALL_CAPS: spaced unless in pronounceable allowlist
        assert normalize_for_speech("IO") == "I O"

    def test_number_in_identifier(self) -> None:
        assert normalize_for_speech("value2key") == "value 2 key"

    def test_snake_with_abbreviation_parts(self) -> None:
        assert normalize_for_speech("stdin_reader") == "standard input reader"

    def test_punctuation_preserved(self) -> None:
        # Non-speech symbols (parens) stripped; prosody punctuation (comma) kept
        assert normalize_for_speech("(fileName),") == "file name,"

    def test_expression_tags_stripped(self) -> None:
        # Brackets are non-speech symbols — normalize drops them,
        # keeping the inner word.
        result = normalize_for_speech("[warm] Hello world")
        assert result == "warm Hello world"


# ---------------------------------------------------------------------------
# Acronym spacing
# ---------------------------------------------------------------------------


class TestAcronymSpacing:
    def test_ocr_spaced(self) -> None:
        assert normalize_for_speech("OCR") == "O C R"

    def test_mcp_spaced(self) -> None:
        assert normalize_for_speech("MCP") == "M C P"

    def test_tts_spaced(self) -> None:
        assert normalize_for_speech("TTS") == "T T S"

    def test_cli_spaced(self) -> None:
        assert normalize_for_speech("CLI") == "C L I"

    def test_html_spaced(self) -> None:
        assert normalize_for_speech("HTML") == "H T M L"

    def test_api_spaced(self) -> None:
        assert normalize_for_speech("API") == "A P I"

    def test_json_pronounceable(self) -> None:
        assert normalize_for_speech("JSON") == "JSON"

    def test_nats_pronounceable(self) -> None:
        assert normalize_for_speech("NATS") == "NATS"

    def test_sql_pronounceable(self) -> None:
        assert normalize_for_speech("SQL") == "SQL"

    def test_aws_pronounceable(self) -> None:
        assert normalize_for_speech("AWS") == "AWS"

    def test_io_spaced(self) -> None:
        assert normalize_for_speech("IO") == "I O"

    def test_ok_pronounceable(self) -> None:
        assert normalize_for_speech("OK") == "OK"

    def test_long_caps_unchanged(self) -> None:
        assert normalize_for_speech("SIGNAL") == "SIGNAL"

    def test_mp3_spaced(self) -> None:
        # camelCase split "MP" + "3", then MP gets spaced → "M P 3"
        assert normalize_for_speech("MP3") == "M P 3"

    def test_acronym_in_camel(self) -> None:
        assert normalize_for_speech("HTMLParser") == "H T M L parser"

    def test_acronym_with_punctuation(self) -> None:
        assert normalize_for_speech("(CLI)") == "C L I"

    def test_max_pronounceable(self) -> None:
        assert normalize_for_speech("MAX") == "MAX"

    def test_idempotent_spaced(self) -> None:
        once = normalize_for_speech("OCR")
        twice = normalize_for_speech(once)
        assert once == twice == "O C R"

    def test_idempotent_eof(self) -> None:
        once = normalize_for_speech("eof")
        twice = normalize_for_speech(once)
        assert once == twice == "E O F"

    def test_vibe_tag_with_acronym(self) -> None:
        result = normalize_for_speech("[warm] The OCR failed")
        assert "warm" in result
        assert "O C R" in result


# ---------------------------------------------------------------------------
# Non-speech symbol stripping
# ---------------------------------------------------------------------------


class TestSymbolStripping:
    def test_parentheses_stripped(self) -> None:
        assert normalize_for_speech("only (10m TTL)") == "only 10 m T T L"

    def test_brackets_stripped(self) -> None:
        assert normalize_for_speech("[calm] hello") == "calm hello"

    def test_curly_braces_stripped(self) -> None:
        assert normalize_for_speech("{key: value}") == "key: value"

    def test_prosody_comma_kept(self) -> None:
        assert normalize_for_speech("hello, world") == "hello, world"

    def test_prosody_question_mark_kept(self) -> None:
        assert normalize_for_speech("really?") == "really?"

    def test_prosody_exclamation_kept(self) -> None:
        assert normalize_for_speech("done!") == "done!"

    def test_prosody_colon_kept(self) -> None:
        assert normalize_for_speech("value:") == "value:"

    def test_prosody_period_kept(self) -> None:
        assert normalize_for_speech("end.") == "end."

    def test_mixed_symbols_and_prosody(self) -> None:
        # Parens stripped, comma kept
        assert normalize_for_speech("(fileName),") == "file name,"

    def test_slash_command_preserved(self) -> None:
        # Leading / is a file path prefix — token skipped entirely
        assert normalize_for_speech("/wall") == "/wall"

    def test_wrapped_file_path(self) -> None:
        # Parens stripped, file path core preserved
        assert normalize_for_speech("(/usr/local/bin)") == "/usr/local/bin"

    def test_wrapped_file_path_with_prosody(self) -> None:
        assert normalize_for_speech("(/usr/local/bin),") == "/usr/local/bin,"

    def test_punctuation_only_token_dropped(self) -> None:
        assert normalize_for_speech("hello () world") == "hello world"


# ---------------------------------------------------------------------------
# strip_vibe_tags
# ---------------------------------------------------------------------------


class TestStripVibeTags:
    """Tests for strip_vibe_tags — removal of ElevenLabs expressive tags."""

    def test_single_word_tag(self) -> None:
        assert strip_vibe_tags("[serious] Hello world") == "Hello world"

    def test_two_word_tag_preserved(self) -> None:
        """Two-word bracketed tokens are NOT stripped."""
        text = "[slow breath] Hello world"
        assert strip_vibe_tags(text) == text

    def test_tag_at_end(self) -> None:
        assert strip_vibe_tags("Hello world [warm]") == "Hello world"

    def test_tag_in_middle(self) -> None:
        assert strip_vibe_tags("Hello [excited] world") == "Hello world"

    def test_multiple_tags(self) -> None:
        assert strip_vibe_tags("[serious] [warm] Hello world") == "Hello world"

    def test_no_tags_passthrough(self) -> None:
        assert strip_vibe_tags("Hello world") == "Hello world"

    def test_empty_string(self) -> None:
        assert strip_vibe_tags("") == ""

    def test_all_tags_returns_original(self) -> None:
        """When stripping removes all content, return the original text."""
        assert strip_vibe_tags("[serious] [warm]") == "[serious] [warm]"

    def test_single_tag_only_returns_original(self) -> None:
        assert strip_vibe_tags("[calm]") == "[calm]"

    def test_preserves_uppercase_brackets(self) -> None:
        assert strip_vibe_tags("[IMPORTANT] Hello") == "[IMPORTANT] Hello"

    def test_preserves_numbered_brackets(self) -> None:
        assert strip_vibe_tags("[1] Hello") == "[1] Hello"

    def test_preserves_figure_reference(self) -> None:
        assert strip_vibe_tags("[Figure 1] Hello") == "[Figure 1] Hello"

    def test_preserves_mixed_case_brackets(self) -> None:
        assert strip_vibe_tags("[Figure A] Hello") == "[Figure A] Hello"

    def test_preserves_three_word_brackets(self) -> None:
        text = "[citation needed here] Hello"
        assert strip_vibe_tags(text) == text

    def test_tag_with_digits_not_stripped(self) -> None:
        assert strip_vibe_tags("[excited2] Hello") == "[excited2] Hello"

    def test_sighs_tag(self) -> None:
        assert strip_vibe_tags("[sighs] Hello world") == "Hello world"

    def test_multiple_positions(self) -> None:
        result = strip_vibe_tags("[warm] Start [excited] middle [calm] end")
        assert result == "Start middle end"

    def test_whitespace_collapsed(self) -> None:
        result = strip_vibe_tags("[warm]  Hello  [calm]  world")
        assert result == "Hello world"

    @pytest.mark.parametrize(
        ("input_text", "expected"),
        [
            ("[serious] Hello", "Hello"),
            ("Hello [warm]", "Hello"),
            ("[slow breath] text", "[slow breath] text"),
            ("[sighs] sigh", "sigh"),
            ("no tags here", "no tags here"),
            ("[Figure 1] caption", "[Figure 1] caption"),
            ("[1] item", "[1] item"),
            ("[LOUD] text", "[LOUD] text"),
        ],
    )
    def test_parametrized(self, input_text: str, expected: str) -> None:
        assert strip_vibe_tags(input_text) == expected
