"""Tests for punt_vox.voxd.wire_text -- the free-form path-token sanitizer.

Player stderr, an ``OSError`` string, and a generation reason all embed absolute
host paths -- the home directory and the username inside it. ``SafeText`` scans a
free-form string for absolute-path tokens and rewrites each in place: an in-jail
path to its labeled relative form, an out-of-jail path to ``<path>``. These tests
pin that no absolute prefix, no ``/Users/`` prefix, and no host-binary path
survives, while non-path text is untouched.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from punt_vox import dirs, paths
from punt_vox.voxd.wire_text import SafeText


@pytest.fixture
def roots(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    """Point both data roots at isolated, resolved tmp directories."""
    state = (tmp_path / "state").resolve()
    output = (tmp_path / "output").resolve()
    state.mkdir()
    output.mkdir()
    monkeypatch.setattr(paths, "user_state_dir", lambda: state)
    monkeypatch.setattr(dirs, "default_output_dir", lambda: output)
    return state, output


class TestInJailTokens:
    """An in-jail path token is rewritten to its labeled relative form."""

    def test_state_path_token_relativized(self, roots: tuple[Path, Path]) -> None:
        state, _output = roots
        raw = f"[Errno 13] Permission denied: '{state / 'recordings' / 'foo.mp3'}'"
        safe = SafeText.of(raw).text
        assert "recordings/foo.mp3" in safe
        assert str(state) not in safe
        assert "/Users/" not in safe

    def test_output_path_token_relativized(self, roots: tuple[Path, Path]) -> None:
        _state, output = roots
        raw = f"cannot open {output / 'album-1' / 'part-2.mp3'} for playback"
        safe = SafeText.of(raw).text
        assert "album-1/part-2.mp3" in safe
        assert str(output) not in safe

    def test_no_absolute_prefix_survives(self, roots: tuple[Path, Path]) -> None:
        state, _output = roots
        raw = f"ffplay: {state / 'cache' / 'ab' / 'cd.mp3'}: No such file"
        safe = SafeText.of(raw).text
        assert "/" in safe  # the relative path keeps its inner separators
        assert not any(part.startswith(str(state)) for part in safe.split())


class TestOutOfJailTokens:
    """An out-of-jail path token is stripped to a redaction placeholder."""

    def test_home_path_redacted(self, roots: tuple[Path, Path]) -> None:
        raw = "spawn failed: /Users/someone/.local/bin/ffplay not found"
        safe = SafeText.of(raw).text
        assert "/Users/" not in safe
        assert "<path>" in safe
        assert "spawn failed:" in safe

    def test_host_binary_path_redacted(self, roots: tuple[Path, Path]) -> None:
        raw = "FileNotFoundError: [Errno 2] No such file: '/opt/homebrew/bin/afplay'"
        safe = SafeText.of(raw).text
        assert "/opt/homebrew/bin/afplay" not in safe
        assert "<path>" in safe


class TestNonPathText:
    """Non-path text -- and single-segment words with a slash -- pass through."""

    def test_plain_text_unchanged(self, roots: tuple[Path, Path]) -> None:
        raw = "player exited rc=1 (elapsed 0.050s)"
        assert SafeText.of(raw).text == raw

    def test_and_or_word_not_mangled(self, roots: tuple[Path, Path]) -> None:
        """A single-segment ``a/b`` word is not an absolute host path."""
        raw = "retry and/or give up after 3 attempts"
        assert SafeText.of(raw).text == raw


class TestCap:
    """A runaway string is capped so it cannot bloat a reply."""

    def test_long_text_is_capped(self, roots: tuple[Path, Path]) -> None:
        raw = "x" * 5000
        safe = SafeText.of(raw).text
        assert len(safe) < 5000
        assert safe.startswith("x")

    def test_short_text_not_capped(self, roots: tuple[Path, Path]) -> None:
        raw = "short reason"
        assert SafeText.of(raw).text == raw


class TestRegexDosBounded:
    """A pathological many-segment input cannot hang the scan (input capped first)."""

    def test_long_many_segment_input_returns_fast(
        self, roots: tuple[Path, Path]
    ) -> None:
        """A 200k-char '/a/a/.../a X' string sanitizes in well under a second.

        The ``_ABSOLUTE_PATH`` pattern backtracks super-linearly, so scanning the
        raw input would take minutes; capping the input before the regex bounds
        the cost to ``cap`` chars regardless of the caller's input length.
        """
        import time

        raw = "/a" * 100_000 + " X"  # ~200k chars, one giant many-segment token
        start = time.monotonic()
        safe = SafeText.of(raw).text
        assert time.monotonic() - start < 1.0  # bounded, not O(n^2) on 200k chars
        assert len(safe) <= 2100  # capped, with room for the truncation suffix

    def test_capped_input_still_sanitizes(self, roots: tuple[Path, Path]) -> None:
        """Capping the input first does not defeat relativize/strip on what remains."""
        state, _output = roots
        rec = state / "recordings" / "foo.mp3"
        raw = f"denied: {rec} then " + "x" * 5000  # in-jail path within the first cap
        safe = SafeText.of(raw).text
        assert "recordings/foo.mp3" in safe
        assert str(state) not in safe
