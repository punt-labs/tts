"""Tests for the extracted mpv doctor sub-check (``MpvCheck``)."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from punt_vox.doctor_mpv import MPV_HINTS, MPV_MIN_STR, MpvCheck


class TestMpvCheckRun:
    @staticmethod
    def _proc(stdout: str) -> MagicMock:
        return MagicMock(stdout=stdout)

    @patch("punt_vox.doctor_mpv.shutil.which", return_value=None)
    def test_missing_binary_fails(self, _which: MagicMock) -> None:
        result = MpvCheck().run()
        assert result.passed is False
        assert result.symbol == "✗"
        assert "mpv" in result.message
        assert "not found" in result.message

    @patch("punt_vox.doctor_mpv.shutil.which", return_value="/opt/mpv")
    def test_present_recent_passes(self, _which: MagicMock) -> None:
        with patch(
            "punt_vox.doctor_mpv.subprocess.run",
            return_value=self._proc("mpv 0.40.0 Copyright"),
        ):
            result = MpvCheck().run()
        assert result.passed is True
        assert "0.40.0" in result.message
        assert "/opt/mpv" not in result.message  # out-of-jail path never leaks

    @patch("punt_vox.doctor_mpv.shutil.which", return_value="/opt/mpv")
    def test_too_old_fails_with_hint(self, _which: MagicMock) -> None:
        with patch(
            "punt_vox.doctor_mpv.subprocess.run",
            return_value=self._proc("mpv 0.10.0 Copyright"),
        ):
            result = MpvCheck().run()
        assert result.passed is False
        assert "too old" in result.message
        assert MPV_MIN_STR in result.message

    @patch("punt_vox.doctor_mpv.shutil.which", return_value="/opt/mpv")
    def test_unreadable_version_fails(self, _which: MagicMock) -> None:
        with patch(
            "punt_vox.doctor_mpv.subprocess.run",
            return_value=self._proc("gibberish"),
        ):
            result = MpvCheck().run()
        assert result.passed is False
        assert "unreadable" in result.message
        assert MPV_MIN_STR in result.message


class TestMpvCheckParseVersion:
    @pytest.mark.parametrize(
        ("output", "expected"),
        [
            ("mpv 0.38.0 Copyright © 2000-2024", (0, 38, 0)),
            ("mpv v0.35.1 Copyright", (0, 35, 1)),
            ("mpv 0.37 Copyright", (0, 37, 0)),  # minor-only form pads patch=0
            ("mpv 0.40.0-git-abc123", (0, 40, 0)),  # git suffix is ignored
        ],
    )
    def test_parses_version(self, output: str, expected: tuple[int, int, int]) -> None:
        assert MpvCheck.parse_version(output) == expected

    def test_no_version_returns_none(self) -> None:
        assert MpvCheck.parse_version("banner without a version token") is None


class TestMpvHintsShape:
    """MPV_HINTS carries a per-platform install hint plus a ``default`` key."""

    def test_default_key_present(self) -> None:
        assert "default" in MPV_HINTS

    def test_every_hint_is_a_string(self) -> None:
        assert all(isinstance(hint, str) and hint for hint in MPV_HINTS.values())
