"""Tests for hook dispatchers (src/punt_vox/hooks.py)."""

from __future__ import annotations

import errno
import logging
import os
import sys
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import MagicMock, patch

from punt_vox.config import ConfigStore, VoxConfig
from punt_vox.dirs import find_config_dir
from punt_vox.hook_payload import NotificationPayload, StopPayload
from punt_vox.hooks import (
    _chime_via_voxd,  # pyright: ignore[reportPrivateUsage]
    _pick_notification_phrase,  # pyright: ignore[reportPrivateUsage]
    _read_hook_input,  # pyright: ignore[reportPrivateUsage]
    _repo_name_from_cwd,  # pyright: ignore[reportPrivateUsage]
    _speak_phrase,  # pyright: ignore[reportPrivateUsage]
    _with_repo_name,  # pyright: ignore[reportPrivateUsage]
    handle_notification,
    handle_pre_compact,
    handle_session_end,
    handle_stop,
    handle_subagent_start,
    handle_subagent_stop,
    handle_user_prompt_submit,
    handle_vibe_nudge,
    notification_cmd,
    stop_cmd,
)
from punt_vox.quips import (
    ACKNOWLEDGE_PHRASES,
    FAREWELL_PHRASES,
    PRE_COMPACT_PHRASES,
    STOP_PHRASES,
    SUBAGENT_START_PHRASES,
    SUBAGENT_STOP_PHRASES,
)
from punt_vox.vibe_nudge import DEFAULT_THRESHOLD, VIBE_NUDGE_REMINDER

if TYPE_CHECKING:
    import pytest

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_config(
    *,
    notify: str = "y",
    speak: str = "y",
    vibe_mode: str = "auto",
    vibe: str | None = None,
    vibe_nudge_turns: int = 0,
    voice: str | None = None,
    repo_name: str | None = None,
) -> VoxConfig:
    return VoxConfig(
        notify=notify,
        speak=speak,
        vibe_mode=vibe_mode,
        voice=voice,
        provider=None,
        model=None,
        vibe=vibe,
        vibe_tags=None,
        vibe_nudge_turns=vibe_nudge_turns,
        repo_name=repo_name,
    )


# ---------------------------------------------------------------------------
# handle_stop tests
# ---------------------------------------------------------------------------


_CONFIG_DIR = Path("/repo/.punt-labs/vox")


def _stop(*, active: bool = False) -> StopPayload:
    return StopPayload(stop_hook_active=active, cwd=None)


class TestHandleStop:
    def test_notify_disabled(self) -> None:
        config = _make_config(notify="n")
        assert handle_stop(_stop(), config, _CONFIG_DIR) is None

    def test_stop_hook_active(self) -> None:
        config = _make_config()
        assert handle_stop(_stop(active=True), config, _CONFIG_DIR) is None

    @patch("punt_vox.hooks._chime_via_voxd")
    def test_chime_mode(self, mock_enqueue: object) -> None:
        config = _make_config(speak="n")
        result = handle_stop(_stop(), config, _CONFIG_DIR)
        assert result is None

    def test_voice_mode_blocks_clean_reason(self) -> None:
        # The summary fires on the live conditions:
        # notify != n, speak != n, not stop_hook_active.
        config = _make_config()
        result = handle_stop(_stop(), config, _CONFIG_DIR)
        assert result is not None
        assert result["decision"] == "block"
        reason = str(result["reason"])
        # Reason is just the ♪ phrase — no data appended
        assert any(reason == phrase for phrase in STOP_PHRASES)
        assert "|" not in reason
        assert "vibe_tags" not in reason

    def test_stop_writes_nothing_to_config(self) -> None:
        # The agent sets vibe tags directly during the session; the stop hook
        # no longer resolves or persists anything.
        config = _make_config()
        with patch("punt_vox.hooks.ConfigStore") as mock_cs:
            result = handle_stop(_stop(), config, _CONFIG_DIR)
        assert result is not None
        mock_cs.return_value.write_fields.assert_not_called()
        mock_cs.return_value.write_field.assert_not_called()

    def test_continuous_mode_blocks(self) -> None:
        config = _make_config(notify="c")
        result = handle_stop(_stop(), config, _CONFIG_DIR)
        assert result is not None
        assert result["decision"] == "block"


# ---------------------------------------------------------------------------
# vibe authoritative-over-hooks boundary tests (regression: vox-73m5)
# ---------------------------------------------------------------------------


class TestVibeAuthoritativeBoundary:
    """A vibe change through config must survive hook write-backs.

    An authoritative ``/vibe auto`` clears the mood cluster; a later nudge
    write-back touches only the cadence counter and must not resurrect the
    cleared mood or flip the mode away from auto.
    """

    @staticmethod
    def _set_vibe_auto(config_dir: Path) -> None:
        """Persist an authoritative ``/vibe auto`` to disk (notify on)."""
        ConfigStore(config_dir).write_fields(
            {
                "notify": "y",
                "vibe": "",
                "vibe_tags": "",
                "vibe_nudge_turns": "0",
                "vibe_mode": "auto",
            }
        )

    def test_cleared_mood_stays_cleared_after_nudge_write(self, tmp_path: Path) -> None:
        self._set_vibe_auto(tmp_path)
        # A nudge write-back advances the cadence counter; nothing else.
        handle_vibe_nudge(ConfigStore(tmp_path).read(), tmp_path)
        assert ConfigStore(tmp_path).read().vibe is None

    def test_auto_mode_survives_nudge_write(self, tmp_path: Path) -> None:
        self._set_vibe_auto(tmp_path)
        handle_vibe_nudge(ConfigStore(tmp_path).read(), tmp_path)
        assert ConfigStore(tmp_path).read().vibe_mode == "auto"

    def test_committed_manual_does_not_resurrect_stale_mood(
        self, tmp_path: Path
    ) -> None:
        # Simulate a tracked vox.md carrying a stale vibe_mode after a checkout,
        # while the session holds a cleared, auto vibe in vox.local.md.
        (tmp_path / "vox.md").write_text('---\nvibe_mode: "manual"\nnotify: "y"\n---\n')
        (tmp_path / "vox.local.md").write_text('---\nvibe_mode: "auto"\n---\n')
        config = ConfigStore(tmp_path).read()
        assert config.vibe_mode == "auto"  # ephemeral wins; durable copy inert
        assert config.vibe is None


# ---------------------------------------------------------------------------
# handle_notification tests
# ---------------------------------------------------------------------------


class TestHandleNotification:
    @patch("punt_vox.hooks._speak_via_voxd")
    @patch("punt_vox.hooks._chime_via_voxd")
    def test_notify_disabled_does_nothing(
        self, mock_chime: MagicMock, mock_speak: MagicMock
    ) -> None:
        config = _make_config(notify="n")
        handle_notification(
            NotificationPayload(
                notification_type="permission_prompt",
                message="Needs your attention",
            ),
            config,
        )
        mock_chime.assert_not_called()
        mock_speak.assert_not_called()

    @patch("punt_vox.hooks._chime_via_voxd")
    def test_chime_mode_plays_chime(self, mock_chime: MagicMock) -> None:
        config = _make_config(speak="n")
        handle_notification(
            NotificationPayload(
                notification_type="permission_prompt",
                message="Needs your attention",
            ),
            config,
        )
        mock_chime.assert_called_once_with("prompt")

    @patch("punt_vox.hooks._speak_via_voxd")
    def test_voice_mode_speaks_via_voxd(self, mock_speak: MagicMock) -> None:
        config = _make_config(speak="y", voice="matilda")
        handle_notification(
            NotificationPayload(
                notification_type="permission_prompt",
                message="Needs your attention",
            ),
            config,
        )
        mock_speak.assert_called_once()

    @patch("punt_vox.hooks._speak_via_voxd")
    def test_idle_prompt_speaks_via_voxd(self, mock_speak: MagicMock) -> None:
        config = _make_config(speak="y")
        handle_notification(
            NotificationPayload(
                notification_type="idle_prompt",
                message="Needs your attention",
            ),
            config,
        )
        mock_speak.assert_called_once()

    @patch("punt_vox.hooks._speak_via_voxd")
    def test_unknown_type_speaks_via_voxd(self, mock_speak: MagicMock) -> None:
        config = _make_config(speak="y")
        handle_notification(
            NotificationPayload(
                notification_type="unknown",
                message="Needs your attention",
            ),
            config,
        )
        mock_speak.assert_called_once()


# ---------------------------------------------------------------------------
# handle_vibe_nudge tests
# ---------------------------------------------------------------------------


class TestHandleVibeNudge:
    def _seed(self, config_dir: Path, *, mode: str, turns: int) -> VoxConfig:
        """Write a vox.local.md with the given vibe mode and cadence, return config."""
        (config_dir / "vox.md").write_text('---\nnotify: "y"\n---\n')
        (config_dir / "vox.local.md").write_text(
            f'---\nvibe_mode: "{mode}"\nvibe_nudge_turns: "{turns}"\n---\n'
        )
        return ConfigStore(config_dir).read()

    def test_reminder_lands_on_nth_auto_prompt(self, tmp_path: Path) -> None:
        # Below the threshold every prompt stays silent and the counter climbs.
        for turns in range(DEFAULT_THRESHOLD - 1):
            config = self._seed(tmp_path, mode="auto", turns=turns)
            assert handle_vibe_nudge(config, tmp_path) is None
            assert ConfigStore(tmp_path).read().vibe_nudge_turns == turns + 1

        # The Nth prompt fires the reminder and resets the counter.
        config = self._seed(tmp_path, mode="auto", turns=DEFAULT_THRESHOLD - 1)
        result = handle_vibe_nudge(config, tmp_path)
        assert result is not None
        envelope = result["hookSpecificOutput"]
        assert isinstance(envelope, dict)
        assert envelope["hookEventName"] == "UserPromptSubmit"
        assert envelope["additionalContext"] == VIBE_NUDGE_REMINDER
        assert ConfigStore(tmp_path).read().vibe_nudge_turns == 0

    def test_never_emits_a_decision(self, tmp_path: Path) -> None:
        config = self._seed(tmp_path, mode="auto", turns=DEFAULT_THRESHOLD - 1)
        result = handle_vibe_nudge(config, tmp_path)
        assert result is not None
        assert "decision" not in result

    def test_manual_mode_stays_silent_and_leaves_counter(self, tmp_path: Path) -> None:
        config = self._seed(tmp_path, mode="manual", turns=DEFAULT_THRESHOLD - 1)
        assert handle_vibe_nudge(config, tmp_path) is None
        # Manual mode does not advance the cadence.
        assert ConfigStore(tmp_path).read().vibe_nudge_turns == DEFAULT_THRESHOLD - 1

    def test_off_mode_stays_silent(self, tmp_path: Path) -> None:
        config = self._seed(tmp_path, mode="off", turns=DEFAULT_THRESHOLD - 1)
        assert handle_vibe_nudge(config, tmp_path) is None

    def test_firing_prompt_emits_vibe_trace(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The nudge fire is greppable: the default sink writes [vibe-trace] to
        # <state>/logs/vibe-trace.log, which proves the auto-vibe link fired.
        monkeypatch.setattr("punt_vox.vibe_trace.log_dir", lambda: tmp_path)
        config = self._seed(tmp_path, mode="auto", turns=DEFAULT_THRESHOLD - 1)
        handle_vibe_nudge(config, tmp_path)
        lines = (tmp_path / "vibe-trace.log").read_text(encoding="utf-8").splitlines()
        assert any(line.startswith("[vibe-trace] nudge fired") for line in lines)

    def test_persist_failure_still_stays_silent(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        # An unwritable vox.local.md must not crash the nudge hook.
        config = self._seed(tmp_path, mode="auto", turns=0)
        with (
            patch.object(
                ConfigStore, "write_field", side_effect=OSError("read-only fs")
            ),
            caplog.at_level(logging.WARNING, logger="punt_vox.hooks"),
        ):
            assert handle_vibe_nudge(config, tmp_path) is None  # must not raise
        assert any("vibe-nudge" in r.getMessage() for r in caplog.records)

    def test_firing_prompt_stays_silent_when_reset_cannot_persist(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        # At the threshold with an unwritable counter, the reminder must NOT
        # fire: emitting it without resetting would re-hit the threshold and
        # nudge on every later prompt (reminder spam). Silent is the safe mode.
        config = self._seed(tmp_path, mode="auto", turns=DEFAULT_THRESHOLD - 1)
        with (
            patch.object(
                ConfigStore, "write_field", side_effect=OSError("read-only fs")
            ),
            caplog.at_level(logging.WARNING, logger="punt_vox.hooks"),
        ):
            # Every prompt in the failing-write regime stays silent — no spam.
            for _ in range(3):
                assert handle_vibe_nudge(config, tmp_path) is None
        assert any("vibe-nudge" in r.getMessage() for r in caplog.records)
        # The reset never landed, so the on-disk counter keeps its pre-fire value.
        assert ConfigStore(tmp_path).read().vibe_nudge_turns == DEFAULT_THRESHOLD - 1


# ---------------------------------------------------------------------------
# handle_pre_compact tests
# ---------------------------------------------------------------------------


class TestHandlePreCompact:
    @patch("punt_vox.hooks._chime_via_voxd")
    @patch("punt_vox.hooks._speak_via_voxd")
    def test_skip_when_notify_n(
        self, mock_speak: MagicMock, mock_chime: MagicMock
    ) -> None:
        """PreCompact does nothing when notify=n."""
        config = _make_config(notify="n")
        handle_pre_compact(config)
        mock_speak.assert_not_called()
        mock_chime.assert_not_called()

    @patch("punt_vox.hooks._chime_via_voxd")
    @patch("punt_vox.hooks._speak_via_voxd")
    def test_skip_when_notify_y(
        self, mock_speak: MagicMock, mock_chime: MagicMock
    ) -> None:
        """PreCompact does nothing when notify=y (on-demand only)."""
        config = _make_config(notify="y")
        handle_pre_compact(config)
        mock_speak.assert_not_called()
        mock_chime.assert_not_called()

    @patch("punt_vox.hooks._chime_via_voxd")
    def test_chime_mode_plays_chime(self, mock_chime: MagicMock) -> None:
        """PreCompact plays chime when notify=c and speak=n."""
        config = _make_config(notify="c", speak="n")
        handle_pre_compact(config)
        mock_chime.assert_called_once()

    @patch("punt_vox.hooks._speak_via_voxd")
    def test_voice_mode_speaks(self, mock_speak: MagicMock) -> None:
        """PreCompact speaks a phrase when notify=c and speak=y."""
        config = _make_config(notify="c", speak="y", voice="matilda")
        handle_pre_compact(config)
        mock_speak.assert_called_once()
        text = mock_speak.call_args[0][0]
        assert text in PRE_COMPACT_PHRASES

    @patch("punt_vox.hooks._speak_via_voxd")
    def test_voice_mode_no_voice_config(self, mock_speak: MagicMock) -> None:
        """PreCompact works without explicit voice config."""
        config = _make_config(notify="c", speak="y")
        handle_pre_compact(config)
        mock_speak.assert_called_once()


# ---------------------------------------------------------------------------
# _speak_phrase tests
# ---------------------------------------------------------------------------


class TestSpeakPhrase:
    @patch("punt_vox.hooks._chime_via_voxd")
    def test_chime_mode_plays_chime(self, mock_chime: MagicMock) -> None:
        """speak=n plays a chime instead of synthesizing speech."""
        config = _make_config(notify="c", speak="n")
        _speak_phrase(("Hello",), config, chime_signal="done")
        mock_chime.assert_called_once_with("done")

    @patch("punt_vox.hooks._speak_via_voxd")
    def test_voice_mode_speaks_via_voxd(self, mock_speak: MagicMock) -> None:
        """speak=y synthesizes speech via voxd."""
        config = _make_config(notify="c", speak="y", voice="matilda")
        _speak_phrase(("Hello there",), config, chime_signal="done")
        mock_speak.assert_called_once()
        text = mock_speak.call_args[0][0]
        assert text == "Hello there"

    @patch("punt_vox.hooks._speak_via_voxd")
    def test_picks_from_pool(self, mock_speak: MagicMock) -> None:
        """Spoken text is always drawn from the provided phrase pool."""
        phrases = ("Alpha", "Bravo", "Charlie")
        config = _make_config(notify="c", speak="y")
        _speak_phrase(phrases, config, chime_signal="done")
        text = mock_speak.call_args[0][0]
        assert text in phrases


# ---------------------------------------------------------------------------
# handle_user_prompt_submit tests
# ---------------------------------------------------------------------------


class TestHandleUserPromptSubmit:
    @patch("punt_vox.hooks._chime_via_voxd")
    @patch("punt_vox.hooks._speak_via_voxd")
    def test_skip_when_notify_n(
        self, mock_run: MagicMock, mock_enqueue: MagicMock
    ) -> None:
        """Does nothing when notify=n."""
        config = _make_config(notify="n")
        handle_user_prompt_submit(config)
        mock_run.assert_not_called()
        mock_enqueue.assert_not_called()

    @patch("punt_vox.hooks._chime_via_voxd")
    @patch("punt_vox.hooks._speak_via_voxd")
    def test_skip_when_notify_y(
        self, mock_run: MagicMock, mock_enqueue: MagicMock
    ) -> None:
        """Does nothing in on-demand mode (notify=y)."""
        config = _make_config(notify="y")
        handle_user_prompt_submit(config)
        mock_run.assert_not_called()
        mock_enqueue.assert_not_called()

    @patch("punt_vox.hooks._chime_via_voxd")
    def test_chime_mode(self, mock_enqueue: MagicMock) -> None:
        """Plays chime when notify=c and speak=n."""
        config = _make_config(notify="c", speak="n")
        handle_user_prompt_submit(config)
        mock_enqueue.assert_called_once()

    @patch("punt_vox.hooks._speak_via_voxd")
    def test_voice_mode_speaks(self, mock_speak: MagicMock) -> None:
        """Speaks an acknowledgment phrase when notify=c and speak=y."""
        config = _make_config(notify="c", speak="y", voice="matilda")
        handle_user_prompt_submit(config)
        mock_speak.assert_called_once()
        text = mock_speak.call_args[0][0]
        assert text in ACKNOWLEDGE_PHRASES


# ---------------------------------------------------------------------------
# handle_subagent_start tests
# ---------------------------------------------------------------------------


class TestHandleSubagentStart:
    @patch("punt_vox.hooks._chime_via_voxd")
    @patch("punt_vox.hooks._speak_via_voxd")
    def test_skip_when_notify_n(
        self, mock_run: MagicMock, mock_enqueue: MagicMock
    ) -> None:
        config = _make_config(notify="n")
        handle_subagent_start(config)
        mock_run.assert_not_called()
        mock_enqueue.assert_not_called()

    @patch("punt_vox.hooks._chime_via_voxd")
    @patch("punt_vox.hooks._speak_via_voxd")
    def test_skip_when_notify_y(
        self, mock_run: MagicMock, mock_enqueue: MagicMock
    ) -> None:
        config = _make_config(notify="y")
        handle_subagent_start(config)
        mock_run.assert_not_called()
        mock_enqueue.assert_not_called()

    @patch("punt_vox.hooks._chime_via_voxd")
    def test_chime_mode(self, mock_enqueue: MagicMock) -> None:
        config = _make_config(notify="c", speak="n")
        handle_subagent_start(config)
        mock_enqueue.assert_called_once()

    @patch("punt_vox.hooks._speak_via_voxd")
    def test_voice_mode_speaks(self, mock_speak: MagicMock) -> None:
        config = _make_config(notify="c", speak="y")
        handle_subagent_start(config)
        mock_speak.assert_called_once()
        text = mock_speak.call_args[0][0]
        assert text in SUBAGENT_START_PHRASES


# ---------------------------------------------------------------------------
# handle_subagent_stop tests
# ---------------------------------------------------------------------------


class TestHandleSubagentStop:
    @patch("punt_vox.hooks._chime_via_voxd")
    @patch("punt_vox.hooks._speak_via_voxd")
    def test_skip_when_notify_n(
        self, mock_run: MagicMock, mock_enqueue: MagicMock
    ) -> None:
        config = _make_config(notify="n")
        handle_subagent_stop(config)
        mock_run.assert_not_called()
        mock_enqueue.assert_not_called()

    @patch("punt_vox.hooks._chime_via_voxd")
    @patch("punt_vox.hooks._speak_via_voxd")
    def test_skip_when_notify_y(
        self, mock_run: MagicMock, mock_enqueue: MagicMock
    ) -> None:
        config = _make_config(notify="y")
        handle_subagent_stop(config)
        mock_run.assert_not_called()
        mock_enqueue.assert_not_called()

    @patch("punt_vox.hooks._chime_via_voxd")
    def test_chime_mode(self, mock_enqueue: MagicMock) -> None:
        config = _make_config(notify="c", speak="n")
        handle_subagent_stop(config)
        mock_enqueue.assert_called_once()

    @patch("punt_vox.hooks._speak_via_voxd")
    def test_voice_mode_speaks(self, mock_speak: MagicMock) -> None:
        config = _make_config(notify="c", speak="y")
        handle_subagent_stop(config)
        mock_speak.assert_called_once()
        text = mock_speak.call_args[0][0]
        assert text in SUBAGENT_STOP_PHRASES


# ---------------------------------------------------------------------------
# handle_session_end tests
# ---------------------------------------------------------------------------


class TestHandleSessionEnd:
    @patch("punt_vox.hooks._chime_via_voxd")
    @patch("punt_vox.hooks._speak_via_voxd")
    def test_skip_when_notify_n(
        self, mock_run: MagicMock, mock_enqueue: MagicMock
    ) -> None:
        config = _make_config(notify="n")
        handle_session_end(config, Path("/fake/.punt-labs/vox"))
        mock_run.assert_not_called()
        mock_enqueue.assert_not_called()

    @patch("punt_vox.hooks._chime_via_voxd")
    def test_chime_mode(self, mock_enqueue: MagicMock) -> None:
        """Fires for notify=y (not just continuous)."""
        config = _make_config(notify="y", speak="n")
        handle_session_end(config, Path("/fake/.punt-labs/vox"))
        mock_enqueue.assert_called_once()

    @patch("punt_vox.hooks._speak_via_voxd")
    def test_voice_mode_speaks(self, mock_speak: MagicMock) -> None:
        config = _make_config(notify="y", speak="y")
        handle_session_end(config, Path("/fake/.punt-labs/vox"))
        mock_speak.assert_called_once()
        text = mock_speak.call_args[0][0]
        assert text in FAREWELL_PHRASES

    @patch("punt_vox.hooks._speak_via_voxd")
    def test_continuous_mode_speaks(self, mock_speak: MagicMock) -> None:
        config = _make_config(notify="c", speak="y")
        handle_session_end(config, Path("/fake/.punt-labs/vox"))
        mock_speak.assert_called_once()

    @patch("punt_vox.hooks.ConfigStore")
    @patch("punt_vox.hooks._speak_via_voxd")
    def test_resets_nudge_cadence(
        self, _mock_run: MagicMock, mock_cs: MagicMock
    ) -> None:
        """SessionEnd resets the nudge cadence so it never leaks to the next session."""
        config = _make_config(notify="y", speak="y", vibe_nudge_turns=3)
        config_dir = Path("/fake/.punt-labs/vox")
        handle_session_end(config, config_dir)
        mock_cs.assert_called_once_with(config_dir)
        mock_cs.return_value.write_field.assert_called_once_with(
            "vibe_nudge_turns", "0"
        )

    @patch("punt_vox.hooks.ConfigStore")
    @patch("punt_vox.hooks._speak_via_voxd")
    def test_no_write_when_cadence_at_zero(
        self, _mock_run: MagicMock, mock_cs: MagicMock
    ) -> None:
        """Does not write config if the cadence counter is already zero."""
        config = _make_config(notify="y", speak="y", vibe_nudge_turns=0)
        handle_session_end(config, Path("/fake/.punt-labs/vox"))
        mock_cs.return_value.write_field.assert_not_called()

    @patch("punt_vox.hooks._speak_via_voxd")
    def test_reset_failure_warns_and_returns(
        self, _mock_speak: MagicMock, caplog: pytest.LogCaptureFixture
    ) -> None:
        # An unwritable vox.local.md must not crash the SessionEnd hook.
        config = _make_config(notify="y", speak="y", vibe_nudge_turns=2)
        with (
            patch.object(
                ConfigStore, "write_field", side_effect=OSError("read-only fs")
            ),
            caplog.at_level(logging.WARNING, logger="punt_vox.hooks"),
        ):
            handle_session_end(config, Path("/fake/.punt-labs/vox"))  # must not raise
        assert any("session-end" in r.getMessage() for r in caplog.records)


# ---------------------------------------------------------------------------
# Quip pool size tests
# ---------------------------------------------------------------------------


class TestQuipPools:
    """Verify all quip pools have at least 10 entries (user requirement)."""

    def test_acknowledge_phrases_count(self) -> None:
        assert len(ACKNOWLEDGE_PHRASES) >= 10

    def test_subagent_start_phrases_count(self) -> None:
        assert len(SUBAGENT_START_PHRASES) >= 10

    def test_subagent_stop_phrases_count(self) -> None:
        assert len(SUBAGENT_STOP_PHRASES) >= 10

    def test_farewell_phrases_count(self) -> None:
        assert len(FAREWELL_PHRASES) >= 10

    def test_stop_phrases_count(self) -> None:
        assert len(STOP_PHRASES) >= 7

    def test_pre_compact_phrases_count(self) -> None:
        assert len(PRE_COMPACT_PHRASES) >= 7


# ---------------------------------------------------------------------------
# repo_name prefix tests
# ---------------------------------------------------------------------------


class TestRepoNamePrefix:
    """Verify repo_name is prepended to spoken text across all speech paths."""

    @patch("punt_vox.hooks._speak_via_voxd")
    def test_speak_phrase_prepends_repo_name(self, mock_speak: MagicMock) -> None:
        """_speak_phrase prepends 'repo. phrase' when repo_name is set."""
        config = _make_config(notify="c", speak="y", repo_name="vox")
        _speak_phrase(("On it.",), config, chime_signal="done")
        text = mock_speak.call_args[0][0]
        assert text.startswith("vox. ")
        assert text == "vox. On it."

    @patch("punt_vox.hooks._speak_via_voxd")
    def test_speak_phrase_no_prefix_when_repo_name_none(
        self, mock_speak: MagicMock
    ) -> None:
        """_speak_phrase omits prefix when repo_name is None."""
        config = _make_config(notify="c", speak="y", repo_name=None)
        _speak_phrase(("On it.",), config, chime_signal="done")
        text = mock_speak.call_args[0][0]
        assert text == "On it."

    @patch("punt_vox.hooks._chime_via_voxd")
    def test_speak_phrase_chime_mode_ignores_repo_name(
        self, mock_chime: MagicMock
    ) -> None:
        """Chime mode skips speech entirely — repo_name irrelevant."""
        config = _make_config(notify="c", speak="n", repo_name="vox")
        _speak_phrase(("On it.",), config, chime_signal="done")
        mock_chime.assert_called_once_with("done")

    def test_pick_notification_phrase_prepends_repo_name(self) -> None:
        """_pick_notification_phrase prepends repo_name to permission phrases."""
        text = _pick_notification_phrase("permission_prompt", "", repo_name="quarry")
        assert text.startswith("quarry. ")

    def test_pick_notification_phrase_no_prefix_when_none(self) -> None:
        """_pick_notification_phrase omits prefix when repo_name is None."""
        text = _pick_notification_phrase("idle_prompt", "", repo_name=None)
        assert not text.startswith("None")

    @patch("punt_vox.hooks._speak_via_voxd")
    def test_handle_notification_passes_repo_name(self, mock_speak: MagicMock) -> None:
        """handle_notification threads repo_name to the spoken phrase."""
        config = _make_config(speak="y", repo_name="biff")
        handle_notification(
            NotificationPayload(
                notification_type="permission_prompt",
                message="Needs your attention",
            ),
            config,
        )
        text = mock_speak.call_args[0][0]
        assert text.startswith("biff. ")

    @patch("punt_vox.hooks.ConfigStore")
    def test_handle_stop_prepends_repo_name_to_reason(
        self, _mock_cs: MagicMock
    ) -> None:
        """handle_stop includes repo_name in the block reason string."""
        config = _make_config(repo_name="vox")
        result = handle_stop(
            StopPayload(stop_hook_active=False, cwd=None),
            config,
            Path("/repo/.punt-labs/vox"),
        )
        assert result is not None
        reason = str(result["reason"])
        assert reason.startswith("vox. ")

    @patch("punt_vox.hooks.ConfigStore")
    def test_handle_stop_no_prefix_when_repo_name_none(
        self, _mock_cs: MagicMock
    ) -> None:
        """handle_stop reason is a plain phrase when repo_name is None."""
        config = _make_config(repo_name=None)
        result = handle_stop(
            StopPayload(stop_hook_active=False, cwd=None),
            config,
            Path("/repo/.punt-labs/vox"),
        )
        assert result is not None
        reason = str(result["reason"])
        assert any(reason == phrase for phrase in STOP_PHRASES)

    @patch("punt_vox.hooks._speak_via_voxd")
    def test_handle_pre_compact_prepends_repo_name(self, mock_speak: MagicMock) -> None:
        """handle_pre_compact speaks with repo_name prefix."""
        config = _make_config(notify="c", speak="y", repo_name="ethos")
        handle_pre_compact(config)
        text = mock_speak.call_args[0][0]
        assert text.startswith("ethos. ")

    @patch("punt_vox.hooks._speak_via_voxd")
    def test_handle_session_end_prepends_repo_name(self, mock_speak: MagicMock) -> None:
        """handle_session_end speaks with repo_name prefix."""
        config = _make_config(notify="y", speak="y", repo_name="lux")
        handle_session_end(config, Path("/fake/.punt-labs/vox"))
        text = mock_speak.call_args[0][0]
        assert text.startswith("lux. ")


# ---------------------------------------------------------------------------
# _read_hook_input (non-blocking stdin, DES-027)
# ---------------------------------------------------------------------------


class TestReadHookInput:
    """Verify _read_hook_input doesn't block on open pipes."""

    def test_empty_stdin_returns_empty(self) -> None:
        """EOF with no data returns {}."""
        r_fd, w_fd = os.pipe()
        os.close(w_fd)
        r = os.fdopen(r_fd, "r")
        try:
            with patch.object(sys, "stdin", r):
                result = _read_hook_input()
        finally:
            r.close()
        assert result == {}

    def test_valid_json_parsed(self) -> None:
        """Valid JSON on stdin is parsed and returned."""
        r_fd, w_fd = os.pipe()
        os.write(w_fd, b'{"tool_name": "Bash"}\n')
        os.close(w_fd)
        r = os.fdopen(r_fd, "r")
        try:
            with patch.object(sys, "stdin", r):
                result = _read_hook_input()
        finally:
            r.close()
        assert result == {"tool_name": "Bash"}

    def test_no_eof_does_not_hang(self) -> None:
        """Stdin with data but no EOF returns data without blocking.

        Regression test for the session resume hang (DES-027).
        """
        r_fd, w_fd = os.pipe()
        os.write(w_fd, b'{"event": "stop"}\n')
        # Do NOT close w_fd — simulates open pipe without EOF.
        r = os.fdopen(r_fd, "r")
        try:
            with patch.object(sys, "stdin", r):
                result = _read_hook_input()
        finally:
            r.close()
            os.close(w_fd)
        assert result == {"event": "stop"}

    def test_no_data_no_eof_returns_empty(self) -> None:
        """Open pipe with no data returns {} without blocking."""
        r_fd, w_fd = os.pipe()
        r = os.fdopen(r_fd, "r")
        try:
            with patch.object(sys, "stdin", r):
                result = _read_hook_input()
        finally:
            r.close()
            os.close(w_fd)
        assert result == {}

    def test_invalid_json_returns_empty(self) -> None:
        """Invalid JSON on stdin returns {} gracefully."""
        r_fd, w_fd = os.pipe()
        os.write(w_fd, b"not json\n")
        os.close(w_fd)
        r = os.fdopen(r_fd, "r")
        try:
            with patch.object(sys, "stdin", r):
                result = _read_hook_input()
        finally:
            r.close()
        assert result == {}

    def test_malformed_payload_warns_metadata_only(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Garbled stdin logs one WARNING with a byte count and no payload content."""
        payload = b'{"secret_token": "leak-me-abcdef"} trailing garbage'
        r_fd, w_fd = os.pipe()
        os.write(w_fd, payload)
        os.close(w_fd)
        r = os.fdopen(r_fd, "r")
        try:
            with (
                caplog.at_level(logging.WARNING, logger="punt_vox.hooks"),
                patch.object(sys, "stdin", r),
            ):
                assert _read_hook_input() == {}
        finally:
            r.close()

        warnings = [
            m for r in caplog.records if "malformed JSON" in (m := r.getMessage())
        ]
        assert len(warnings) == 1
        assert f"{len(payload)} bytes" in warnings[0]  # metadata only
        assert "leak-me" not in warnings[0]  # never the content

    def test_unexpected_oserror_logs_errno(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Any errno logs at WARNING — even ENFILE, outside the old allow-list."""
        r_fd, w_fd = os.pipe()
        os.write(w_fd, b'{"x": 1}')  # ensure select() reports readable
        r = os.fdopen(r_fd, "r")
        try:
            with (
                patch(
                    "punt_vox.hooks.os.read",
                    side_effect=OSError(errno.ENFILE, "too many open files"),
                ),
                caplog.at_level(logging.WARNING, logger="punt_vox.hooks"),
                patch.object(sys, "stdin", r),
            ):
                result = _read_hook_input()
        finally:
            r.close()
            os.close(w_fd)
        assert result == {}
        assert any(
            str(errno.ENFILE) in rec.getMessage() and "errno" in rec.getMessage()
            for rec in caplog.records
        )

    def test_oserror_without_errno_stays_quiet(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """An OSError with no errno (e.g. empty pipe) returns {} without warning."""
        r_fd, w_fd = os.pipe()
        os.write(w_fd, b'{"x": 1}')
        r = os.fdopen(r_fd, "r")
        try:
            with (
                patch("punt_vox.hooks.os.read", side_effect=OSError()),
                caplog.at_level(logging.WARNING, logger="punt_vox.hooks"),
                patch.object(sys, "stdin", r),
            ):
                result = _read_hook_input()
        finally:
            r.close()
            os.close(w_fd)
        assert result == {}
        assert not [rec for rec in caplog.records if rec.levelno >= logging.WARNING]

    def test_closed_empty_stdin_stays_quiet(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """EOF with no data returns {} quietly — the expected empty path."""
        r_fd, w_fd = os.pipe()
        os.close(w_fd)
        r = os.fdopen(r_fd, "r")
        try:
            with (
                caplog.at_level(logging.WARNING, logger="punt_vox.hooks"),
                patch.object(sys, "stdin", r),
            ):
                result = _read_hook_input()
        finally:
            r.close()
        assert result == {}
        assert not [rec for rec in caplog.records if rec.levelno >= logging.WARNING]


class TestConfigAbsentIsQuiet:
    """A hook in a non-vox repo stays silent, logging only a DEBUG diagnosis line."""

    def test_config_absent_logs_debug(self, caplog: pytest.LogCaptureFixture) -> None:
        """config_dir is None -> one DEBUG line, no INFO/WARNING noise."""
        r_fd, w_fd = os.pipe()
        os.write(w_fd, b'{"cwd": "/somewhere/without/vox"}')
        os.close(w_fd)
        r = os.fdopen(r_fd, "r")
        try:
            with (
                caplog.at_level(logging.DEBUG, logger="punt_vox.hooks"),
                patch("punt_vox.hooks.find_config_dir", return_value=None),
                patch.object(sys, "stdin", r),
            ):
                stop_cmd()
        finally:
            r.close()

        debugs = [r for r in caplog.records if r.levelno == logging.DEBUG]
        assert len(debugs) == 1
        assert "no vox config" in debugs[0].getMessage()
        assert not [r for r in caplog.records if r.levelno >= logging.INFO]


# ---------------------------------------------------------------------------
# _chime_via_voxd — spawn failure carries exception detail (vox-wqft)
# ---------------------------------------------------------------------------


class TestChimeSpawnFailure:
    """The detached-chime spawn warning must name the OSError detail."""

    def test_spawn_failure_logs_exception_detail(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A failed Popen logs the errno/strerror, not just a generic message."""
        boom = OSError(errno.ENOENT, "No such file or directory")
        with (
            patch("subprocess.Popen", side_effect=boom),
            caplog.at_level(logging.WARNING, logger="punt_vox.hooks"),
        ):
            _chime_via_voxd("done", wait=False)

        messages = [rec.getMessage() for rec in caplog.records]
        assert any("Could not spawn chime subprocess" in m for m in messages)
        assert any("No such file or directory" in m for m in messages)
        # The errno NUMBER must survive too -- %s on the full OSError yields
        # "[Errno 2] ...", locking the contract against a strerror-only log.
        assert any("Errno 2" in m for m in messages)


# ---------------------------------------------------------------------------
# _repo_name_from_cwd / _with_repo_name — git-root override
# ---------------------------------------------------------------------------


class TestRepoNameFromCwd:
    def test_none_cwd_returns_none(self) -> None:
        assert _repo_name_from_cwd(None) is None

    def test_returns_repo_name_from_git_root(self, tmp_path: Path) -> None:
        repo = tmp_path / "my-repo"
        repo.mkdir()
        (repo / ".git").mkdir()
        assert _repo_name_from_cwd(repo) == "my-repo"

    def test_walks_up_to_git_root(self, tmp_path: Path) -> None:
        repo = tmp_path / "my-repo"
        subdir = repo / "src" / "pkg"
        subdir.mkdir(parents=True)
        (repo / ".git").mkdir()
        assert _repo_name_from_cwd(subdir) == "my-repo"

    def test_no_git_root_returns_none(self, tmp_path: Path) -> None:
        isolated = tmp_path / "no-git"
        isolated.mkdir()
        # May find a real .git above tmp_path on the host; only assert
        # None when truly isolated.
        result = _repo_name_from_cwd(isolated)
        if result is not None:
            assert not Path(result).is_relative_to(isolated)


class TestWithRepoName:
    def test_overrides_when_names_differ(self, tmp_path: Path) -> None:
        repo = tmp_path / "vox"
        repo.mkdir()
        (repo / ".git").mkdir()
        config = _make_config(repo_name="punt-labs")
        updated = _with_repo_name(config, repo)
        assert updated.repo_name == "vox"

    def test_no_change_when_names_match(self, tmp_path: Path) -> None:
        repo = tmp_path / "vox"
        repo.mkdir()
        (repo / ".git").mkdir()
        config = _make_config(repo_name="vox")
        updated = _with_repo_name(config, repo)
        assert updated is config  # same object — no replacement

    def test_no_change_when_cwd_none(self) -> None:
        config = _make_config(repo_name="punt-labs")
        updated = _with_repo_name(config, None)
        assert updated is config

    def test_inherited_config_gets_child_repo_name(self, tmp_path: Path) -> None:
        """When config_dir resolves to parent workspace but cwd is a child repo."""
        # Simulate: workspace/child-repo/ has .git but no .punt-labs/vox/
        # Config was inherited from workspace/.punt-labs/vox/ yielding "punt-labs"
        child = tmp_path / "child-repo"
        child.mkdir()
        (child / ".git").mkdir()
        config = _make_config(repo_name="punt-labs")
        updated = _with_repo_name(config, child)
        assert updated.repo_name == "child-repo"


# ---------------------------------------------------------------------------
# Command handlers resolve repo identity from the payload cwd
# ---------------------------------------------------------------------------


def _make_repo(root: Path, *, notify: str = "c") -> Path:
    """Create a configured repo under *root* and return its path."""
    config_dir = root / ".punt-labs" / "vox"
    config_dir.mkdir(parents=True)
    (config_dir / "vox.md").write_text(f'---\nnotify: "{notify}"\n---\n')
    # find_repo_root() stops at the first ancestor holding .git, and pytest's
    # tmp_path lives under the punt-labs checkout whenever TMPDIR points into
    # it -- so without a boundary here the walk escapes the fixture and the
    # spoken repo name is the enclosing checkout's. Same reason test_hook_gate
    # git-inits its fixture; existence is all find_repo_root checks.
    (root / ".git").mkdir()
    return root


class TestCommandsResolveFromCwd:
    """The *_cmd handlers derive the spoken repo name from payload.cwd."""

    @patch("punt_vox.hooks._speak_via_voxd")
    def test_stop_cmd_speaks_repo_from_cwd(
        self, mock_speak: MagicMock, tmp_path: Path
    ) -> None:
        repo = _make_repo(tmp_path / "vox", notify="y")
        payload = {"cwd": str(repo), "stop_hook_active": False}
        with (
            patch("punt_vox.hooks._read_hook_input", return_value=payload),
            patch("punt_vox.hooks._emit") as mock_emit,
            patch("punt_vox.hooks.find_config_dir", wraps=find_config_dir) as mock_find,
        ):
            stop_cmd()
        mock_find.assert_called_once_with(Path(str(repo)))
        result = mock_emit.call_args[0][0]
        # The block reason names the repo resolved from cwd: "vox. <phrase>".
        assert str(result["reason"]).startswith("vox. ")

    @patch("punt_vox.hooks._speak_via_voxd")
    def test_notification_cmd_speaks_repo_from_cwd(
        self, mock_speak: MagicMock, tmp_path: Path
    ) -> None:
        repo = _make_repo(tmp_path / "vox", notify="y")
        payload = {
            "cwd": str(repo),
            "notification_type": "permission_prompt",
            "message": "Needs your attention",
        }
        with (
            patch("punt_vox.hooks._read_hook_input", return_value=payload),
            patch("punt_vox.hooks.find_config_dir", wraps=find_config_dir) as mock_find,
        ):
            notification_cmd()
        mock_find.assert_called_once_with(Path(str(repo)))
        text = mock_speak.call_args[0][0]
        assert text.startswith("vox. ")

    @patch("punt_vox.hooks._speak_via_voxd")
    @patch("punt_vox.hooks._chime_via_voxd")
    def test_notification_cmd_silent_when_no_config(
        self, mock_chime: MagicMock, mock_speak: MagicMock, no_config_dir: Path
    ) -> None:
        # cwd points at a directory with no .punt-labs/vox config — silent.
        # no_config_dir has no ambient config above it, so find_config_dir
        # cannot leak into the real repo config (fails under TMPDIR=.tmp).
        bare = no_config_dir / "punt-labs"
        bare.mkdir()
        payload = {
            "cwd": str(bare),
            "notification_type": "permission_prompt",
            "message": "Needs your attention",
        }
        with patch("punt_vox.hooks._read_hook_input", return_value=payload):
            notification_cmd()
        mock_speak.assert_not_called()
        mock_chime.assert_not_called()

    def test_stop_cmd_leaves_agent_vibe_tags_untouched(self, tmp_path: Path) -> None:
        # The stop hook blocks for a summary but writes nothing: the vibe tags
        # the agent set during the session survive unchanged.
        repo = _make_repo(tmp_path / "vox", notify="y")
        config_dir = repo / ".punt-labs" / "vox"
        (config_dir / "vox.local.md").write_text('---\nvibe_tags: "[warm]"\n---\n')
        payload = {"cwd": str(repo), "stop_hook_active": False}
        with (
            patch("punt_vox.hooks._read_hook_input", return_value=payload),
            patch("punt_vox.hooks._emit") as mock_emit,
        ):
            stop_cmd()
        assert mock_emit.call_args[0][0]["decision"] == "block"
        assert ConfigStore(config_dir).read_field("vibe_tags") == "[warm]"

    @patch("punt_vox.hooks._speak_via_voxd")
    def test_stop_cmd_inherited_config_uses_child_repo_name(
        self, mock_speak: MagicMock, tmp_path: Path
    ) -> None:
        """Config lives in parent workspace but cwd is a child repo.

        The bug: config_dir resolves to ``workspace/.punt-labs/vox/``
        so ``_derive_repo_name`` returns "punt-labs" (the workspace dir).
        With ``_with_repo_name``, the git root of cwd overrides the name.
        """
        workspace = tmp_path / "punt-labs"
        # Config lives only in the workspace
        _make_repo(workspace, notify="y")
        # Child repo has .git but no .punt-labs/vox/
        child = workspace / "biff"
        child.mkdir()
        (child / ".git").mkdir()
        payload = {"cwd": str(child), "stop_hook_active": False}
        with (
            patch("punt_vox.hooks._read_hook_input", return_value=payload),
            patch("punt_vox.hooks._emit") as mock_emit,
        ):
            stop_cmd()
        result = mock_emit.call_args[0][0]
        # Must say "biff" (child repo), not "punt-labs" (workspace)
        assert str(result["reason"]).startswith("biff. ")

    @patch("punt_vox.hooks._speak_via_voxd")
    def test_notification_cmd_inherited_config_uses_child_repo_name(
        self, mock_speak: MagicMock, tmp_path: Path
    ) -> None:
        """Notification with inherited config speaks child repo name."""
        workspace = tmp_path / "punt-labs"
        _make_repo(workspace, notify="y")
        child = workspace / "vox"
        child.mkdir()
        (child / ".git").mkdir()
        payload = {
            "cwd": str(child),
            "notification_type": "permission_prompt",
            "message": "Needs your attention",
        }
        with patch("punt_vox.hooks._read_hook_input", return_value=payload):
            notification_cmd()
        text = mock_speak.call_args[0][0]
        assert text.startswith("vox. ")

    def test_stop_cmd_silent_when_cwd_missing(self, no_config_dir: Path) -> None:
        # No cwd on the payload → no new pwd-fallback suppression added;
        # resolution falls through to find_config_dir(None). In an isolated
        # dir with no config above it, the hook stays silent (no ambient
        # repo config leaks in under TMPDIR=.tmp).
        isolated = no_config_dir / "isolated"
        isolated.mkdir()
        payload = {"stop_hook_active": False}
        with (
            patch("punt_vox.hooks._read_hook_input", return_value=payload),
            patch("punt_vox.dirs.Path.cwd", return_value=isolated),
            patch("punt_vox.hooks._emit") as mock_emit,
        ):
            stop_cmd()
        mock_emit.assert_not_called()
