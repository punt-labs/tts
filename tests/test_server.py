"""Tests for server-level helpers and mic API tools."""
# pyright: reportPrivateUsage=false

from __future__ import annotations

import json
import typing
from pathlib import Path
from typing import Any, NoReturn, cast, final
from unittest.mock import MagicMock

import pytest
from _program_fakes import FakeProgramGateway

from punt_vox.client import SynthesizeResult
from punt_vox.client_errors import VoxdConnectionError
from punt_vox.config import ConfigStore
from punt_vox.music_phrases import (
    GENERATING_NO_STYLE,
    GENERATING_WITH_STYLE,
    REPLAY_RADIO,
    REPLAY_WITH_NAME,
    SKIP,
    STOPPED,
)
from punt_vox.resolve import (
    apply_vibe,
    split_leading_expressive_tags,
    strip_expressive_tags,
)
from punt_vox.server import (
    SessionConfig,
    notify,
    speak,
    status,
    unmute,
    vibe,
    who,
)
from punt_vox.server_music_tool import MusicSubcommand
from punt_vox.types import VoiceNotFoundError
from punt_vox.types_programs import ProgramName, ProgramStatus, Reason
from punt_vox.types_programs.control import CommandOutcome, ProgramSummary
from punt_vox.voices import voice_not_found_message
from punt_vox.voxd.programs import Part, Program, ProgramState
from punt_vox.voxd.programs.playback_policy import Advance, AdvanceResult


def music(
    mode: str,
    style: str | None = None,
    name: str | None = None,
    base_prompt: str | None = None,
    variations: list[str] | None = None,
) -> str:
    """Drive the wired ``music`` tool's on/off path (the old ``music`` verb)."""
    import punt_vox.server as srv

    return srv._music_tool.dispatch(
        cast("MusicSubcommand", mode),
        style=style,
        name=name,
        base_prompt=base_prompt,
        variations=variations,
    )


def music_play(
    style: str | None = None,
    vibe: str | None = None,
    name: str | None = None,
    album_id: str | None = None,
) -> str:
    """Drive the wired ``music`` tool's ``play`` subcommand."""
    import punt_vox.server as srv

    return srv._music_tool.dispatch(
        "play", style=style, vibe=vibe, name=name, album_id=album_id
    )


def music_list() -> str:
    """Drive the wired ``music`` tool's ``list`` subcommand."""
    import punt_vox.server as srv

    return srv._music_tool.dispatch("list")


def music_next() -> str:
    """Drive the wired ``music`` tool's ``next`` subcommand."""
    import punt_vox.server as srv

    return srv._music_tool.dispatch("next")


class _StatusPolicy:
    """Anti-repeat policy stand-in for building a playing Program in tests."""

    def next_part(self, pool: tuple[Part, ...], playing: Part | None) -> AdvanceResult:
        for part in pool:
            if part != playing:
                return Advance(part)
        return Advance(pool[0])


_DAEMON_DOWN = "voxd unreachable (hermetic test default)"


@final
class _UnreachableClient:
    """A ``VoxClientSync`` stand-in whose every RPC reports that voxd is down.

    Models "the daemon is not running" so a synthesis or voice test that forgets
    to install its own mock fails loudly with ``VoxdConnectionError`` -- instead
    of silently reaching, or mutating, the real daemon (the vox-73m5 class).
    """

    __slots__ = ()

    def synthesize(self, *_args: object, **_kwargs: object) -> NoReturn:
        raise VoxdConnectionError(_DAEMON_DOWN)

    def record(self, *_args: object, **_kwargs: object) -> NoReturn:
        raise VoxdConnectionError(_DAEMON_DOWN)

    def voices(self, *_args: object, **_kwargs: object) -> NoReturn:
        raise VoxdConnectionError(_DAEMON_DOWN)


def _unreachable_client() -> _UnreachableClient:
    """Return a synthesis client whose every RPC models an unreachable daemon."""
    return _UnreachableClient()


@pytest.fixture()
def _patch_config(  # pyright: ignore[reportUnusedFunction]
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Path:
    """Return a writable config directory and patch config module default.

    Returns tmp_path as the config directory. Callers write
    ``vox.md`` or ``vox.local.md`` inside tmp_path.
    """
    import punt_vox.config as cfg
    import punt_vox.dirs as dirs

    monkeypatch.setattr(cfg, "DEFAULT_CONFIG_DIR", tmp_path)
    monkeypatch.setattr(dirs, "DEFAULT_CONFIG_DIR", tmp_path)
    return tmp_path


@pytest.fixture(autouse=True)
def _fresh_session(monkeypatch: pytest.MonkeyPatch) -> None:  # pyright: ignore[reportUnusedFunction]
    """Reset server session config before every test.

    Also stubs _find_config_dir to return None so refresh_from_config
    is a no-op by default.  Tests that need refresh behavior override via
    the _refresh_config fixture.
    """
    import punt_vox.server as srv
    from punt_vox.vibe_command import MusicPreference

    monkeypatch.setattr(srv, "_session", SessionConfig())
    monkeypatch.setattr(srv, "_music_pref", MusicPreference())
    monkeypatch.setattr(srv, "_find_config_dir", lambda: None)


@pytest.fixture(autouse=True)
def _hermetic_daemon(monkeypatch: pytest.MonkeyPatch) -> None:  # pyright: ignore[reportUnusedFunction]
    """Neutralize both daemon-facing seams so no test reads or mutates the real voxd.

    ``status`` and the music tools drive ``_program_tools``; ``unmute`` / ``record``
    / ``who`` drive ``_voxd_client`` (``speak`` / ``notify`` / ``vibe`` touch neither
    seam -- they only read and write config). Left un-patched, both reach the
    live daemon -- a test asserting ``music_mode == "off"`` then fails whenever
    music is actually playing (the vox-73m5 config/state-pollution class). The
    defaults installed here are a clean *idle* ``FakeProgramGateway`` and an
    *unreachable* synthesis client, so every test starts hermetic. Tests that need
    a specific Program state override ``_program_tools`` via ``_install_fake``;
    synthesis tests override ``_voxd_client`` with their own mock.
    """
    import punt_vox.server as srv

    monkeypatch.setattr(srv, "_program_tools", FakeProgramGateway())
    monkeypatch.setattr(srv, "_voxd_client", _unreachable_client)


# ---------------------------------------------------------------------------
# apply_vibe tests
# ---------------------------------------------------------------------------


class TestApplyVibe:
    """Tests for apply_vibe text injection."""

    def test_prepends_tags(self, _patch_config: Path) -> None:
        local_md = _patch_config / "vox.local.md"
        local_md.write_text('---\nvibe_tags: "[excited]"\n---\n')
        result = apply_vibe("Hello world", expressive_tags=True)
        assert result == "[excited] Hello world"

    def test_multiple_tags(self, _patch_config: Path) -> None:
        (_patch_config / "vox.local.md").write_text(
            '---\nvibe_tags: "[frustrated] [sighs]"\n---\n'
        )
        result = apply_vibe("Hello world", expressive_tags=True)
        assert result == "[frustrated] [sighs] Hello world"

    def test_skips_prepend_when_text_starts_with_tag(self, _patch_config: Path) -> None:
        (_patch_config / "vox.local.md").write_text('---\nvibe_tags: "[calm]"\n---\n')
        result = apply_vibe("[calm] Already tagged", expressive_tags=True)
        assert result == "[calm] Already tagged"

    def test_skips_prepend_when_text_starts_with_different_tag(
        self, _patch_config: Path
    ) -> None:
        (_patch_config / "vox.local.md").write_text('---\nvibe_tags: "[calm]"\n---\n')
        result = apply_vibe("[excited] Different tag", expressive_tags=True)
        assert result == "[excited] Different tag"

    def test_skips_prepend_when_tag_contains_punctuation(
        self, _patch_config: Path
    ) -> None:
        (_patch_config / "vox.local.md").write_text('---\nvibe_tags: "[calm]"\n---\n')
        result = apply_vibe("[dramatic tone] Something important", expressive_tags=True)
        assert result == "[dramatic tone] Something important"

    def test_passthrough_when_no_tags(self, tmp_path: Path, monkeypatch: Any) -> None:
        import punt_vox.config as cfg

        missing = tmp_path / "missing_dir"
        monkeypatch.setattr(cfg, "DEFAULT_CONFIG_DIR", missing)
        assert apply_vibe("Hello world", expressive_tags=True) == "Hello world"

    def test_skips_tags_when_not_supported(self, _patch_config: Path) -> None:
        local_md = _patch_config / "vox.local.md"
        local_md.write_text('---\nvibe_tags: "[excited]"\n---\n')
        result = apply_vibe("Hello world", expressive_tags=False)
        assert result == "Hello world"

    def test_strips_leading_tags_when_not_supported(self, _patch_config: Path) -> None:
        result = apply_vibe("[serious] Hello world", expressive_tags=False)
        assert result == "Hello world"

    def test_strips_multiple_leading_tags_when_not_supported(
        self, _patch_config: Path
    ) -> None:
        result = apply_vibe("[serious] [calm] Hello world", expressive_tags=False)
        assert result == "Hello world"

    def test_preserves_text_when_only_tags(self, _patch_config: Path) -> None:
        result = apply_vibe("[serious]", expressive_tags=False)
        assert result == "[serious]"

    def test_override_tags_win_over_config(self, _patch_config: Path) -> None:
        (_patch_config / "vox.local.md").write_text('---\nvibe_tags: "[calm]"\n---\n')
        result = apply_vibe(
            "Hello world",
            expressive_tags=True,
            override_tags="[excited]",
        )
        assert result == "[excited] Hello world"

    def test_override_tags_empty_falls_through_to_config(
        self, _patch_config: Path
    ) -> None:
        (_patch_config / "vox.local.md").write_text('---\nvibe_tags: "[calm]"\n---\n')
        result = apply_vibe("Hello world", expressive_tags=True, override_tags="")
        assert result == "[calm] Hello world"


class TestStripExpressiveTags:
    """Tests for strip_expressive_tags — the underlying primitive."""

    def test_strips_single_leading_tag(self) -> None:
        assert strip_expressive_tags("[serious] Hello world") == "Hello world"

    def test_strips_multiple_leading_tags(self) -> None:
        assert strip_expressive_tags("[serious] [calm] Hello world") == "Hello world"

    def test_strips_tag_with_leading_whitespace(self) -> None:
        assert strip_expressive_tags("   [excited] Hello") == "Hello"

    def test_strips_tag_with_punctuation_inside(self) -> None:
        assert (
            strip_expressive_tags("[dramatic tone] Important message")
            == "Important message"
        )

    def test_passes_through_text_with_no_tags(self) -> None:
        assert strip_expressive_tags("Hello world") == "Hello world"

    def test_only_strips_leading_tags_not_embedded(self) -> None:
        assert strip_expressive_tags("Hello [serious] world") == "Hello [serious] world"

    def test_returns_original_when_stripping_would_empty_text(self) -> None:
        # Degenerate case: text was nothing but tags. Returning empty would
        # produce silence, which is worse than speaking the literal text.
        assert strip_expressive_tags("[serious]") == "[serious]"

    def test_returns_original_when_stripping_would_leave_only_whitespace(self) -> None:
        assert strip_expressive_tags("[serious]   ") == "[serious]   "

    def test_empty_string_passes_through(self) -> None:
        assert strip_expressive_tags("") == ""


class TestSplitLeadingExpressiveTags:
    """Tests for split_leading_expressive_tags — the (tags, body) splitter."""

    def test_splits_single_leading_tag(self) -> None:
        tags, body = split_leading_expressive_tags("[serious] Hello world")
        assert tags == "[serious]"
        # _LEADING_TAGS_RE consumes the whitespace after the bracket
        # via the trailing \s* in its pattern.
        assert body == "Hello world"

    def test_splits_multiple_leading_tags(self) -> None:
        tags, body = split_leading_expressive_tags("[serious] [calm] Hello world")
        assert tags == "[serious] [calm]"
        assert body == "Hello world"

    def test_splits_tag_with_leading_whitespace(self) -> None:
        tags, body = split_leading_expressive_tags("   [excited] Hello")
        assert tags == "[excited]"
        assert body == "Hello"

    def test_splits_tag_with_punctuation_inside(self) -> None:
        tags, body = split_leading_expressive_tags("[dramatic tone] Important message")
        assert tags == "[dramatic tone]"
        assert body == "Important message"

    def test_no_tags_returns_empty_tags_and_full_text(self) -> None:
        tags, body = split_leading_expressive_tags("Hello world")
        assert tags == ""
        assert body == "Hello world"

    def test_only_splits_leading_not_embedded(self) -> None:
        tags, body = split_leading_expressive_tags("Hello [serious] world")
        assert tags == ""
        assert body == "Hello [serious] world"

    def test_tags_only_input(self) -> None:
        tags, body = split_leading_expressive_tags("[serious]")
        assert tags == "[serious]"
        assert body == ""

    def test_empty_string(self) -> None:
        tags, body = split_leading_expressive_tags("")
        assert tags == ""
        assert body == ""


# ---------------------------------------------------------------------------
# Config write tests
# ---------------------------------------------------------------------------


class TestWriteConfigField:
    """Tests for write_field in-place YAML editing."""

    def test_creates_file_when_missing(self, _patch_config: Path) -> None:
        ConfigStore().write_field("vibe_tags", "[excited]")
        local_md = _patch_config / "vox.local.md"
        assert local_md.exists()
        text = local_md.read_text()
        assert 'vibe_tags: "[excited]"' in text
        assert text.startswith("---\n")
        assert text.rstrip().endswith("---")

    def test_updates_existing_field(self, _patch_config: Path) -> None:
        local_md = _patch_config / "vox.local.md"
        local_md.write_text('---\nvibe_tags: "[tired]"\n---\n')
        ConfigStore().write_field("vibe_tags", "[excited]")
        text = local_md.read_text()
        assert 'vibe_tags: "[excited]"' in text
        assert "[tired]" not in text

    def test_updates_unquoted_field(self, _patch_config: Path) -> None:
        local_md = _patch_config / "vox.local.md"
        local_md.write_text("---\nvibe_tags: [whispers]\n---\n")
        ConfigStore().write_field("vibe_tags", "[excited]")
        text = local_md.read_text()
        assert 'vibe_tags: "[excited]"' in text
        assert "[whispers]" not in text

    def test_inserts_new_field_before_closing_fence(self, _patch_config: Path) -> None:
        local_md = _patch_config / "vox.local.md"
        local_md.write_text('---\nvibe: "happy"\n---\n')
        ConfigStore().write_field("vibe_tags", "[excited]")
        text = local_md.read_text()
        assert 'vibe_tags: "[excited]"' in text
        assert 'vibe: "happy"' in text

    def test_preserves_other_fields(self, _patch_config: Path) -> None:
        local_md = _patch_config / "vox.local.md"
        local_md.write_text(
            '---\nvibe: "happy"\nvibe_tags: "[tired]"\nvibe_nudge_turns: "2"\n---\n'
        )
        ConfigStore().write_field("vibe_tags", "[excited]")
        text = local_md.read_text()
        assert 'vibe: "happy"' in text
        assert 'vibe_nudge_turns: "2"' in text
        assert 'vibe_tags: "[excited]"' in text

    def test_clears_field_with_empty_string(self, _patch_config: Path) -> None:
        local_md = _patch_config / "vox.local.md"
        local_md.write_text('---\nvibe_tags: "[tired]"\n---\n')
        ConfigStore().write_field("vibe_tags", "")
        text = local_md.read_text()
        assert 'vibe_tags: ""' in text

    def test_rejects_unknown_key(self, _patch_config: Path) -> None:
        vox_md = _patch_config / "vox.md"
        vox_md.write_text("---\n---\n")
        with pytest.raises(ValueError, match="Unknown config key"):
            ConfigStore().write_field("bad_key", "value")

    def test_creates_parent_directory(self, tmp_path: Path) -> None:
        nested = tmp_path / "deep" / "dir"
        ConfigStore(nested).write_field("notify", "y")
        vox_md = nested / "vox.md"
        assert vox_md.exists()
        assert 'notify: "y"' in vox_md.read_text()

    def test_malformed_file_warns_and_overwrites(
        self, _patch_config: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        vox_md = _patch_config / "vox.md"
        vox_md.write_text("no frontmatter at all\n")
        import logging

        with caplog.at_level(logging.WARNING, logger="punt_vox.config"):
            ConfigStore(_patch_config).write_field("notify", "y")
        assert 'notify: "y"' in vox_md.read_text()
        assert "Malformed config" in caplog.text


class TestWriteConfigFields:
    """Tests for write_fields batch helper."""

    def test_writes_multiple_fields(self, _patch_config: Path) -> None:
        vox_md = _patch_config / "vox.md"
        vox_md.write_text('---\nnotify: "y"\n---\n')
        updates = {
            "vibe": "happy",
            "vibe_tags": "[cheerful]",
            "vibe_mode": "manual",
        }
        ConfigStore().write_fields(updates)
        # notify is the only durable pref here -> vox.md
        vox_text = vox_md.read_text()
        assert 'notify: "y"' in vox_text
        assert "vibe_mode" not in vox_text
        # the whole vibe cluster is ephemeral -> vox.local.md
        local_text = (_patch_config / "vox.local.md").read_text()
        assert 'vibe: "happy"' in local_text
        assert 'vibe_tags: "[cheerful]"' in local_text
        assert 'vibe_mode: "manual"' in local_text

    def test_updates_existing_fields(self, _patch_config: Path) -> None:
        local_md = _patch_config / "vox.local.md"
        local_md.write_text(
            '---\nvibe_mode: "off"\nvibe: "old"\nvibe_tags: "[old]"\n---\n'
        )
        updates = {
            "vibe": "new",
            "vibe_tags": "[new]",
            "vibe_mode": "manual",
        }
        ConfigStore().write_fields(updates)
        local_text = local_md.read_text()
        assert 'vibe_mode: "manual"' in local_text
        assert "off" not in local_text
        assert 'vibe: "new"' in local_text
        assert 'vibe_tags: "[new]"' in local_text
        assert "old" not in local_text

    def test_creates_file_when_missing(self, _patch_config: Path) -> None:
        ConfigStore().write_fields({"vibe": "happy", "vibe_tags": "[cheerful]"})
        local_md = _patch_config / "vox.local.md"
        text = local_md.read_text()
        assert text.startswith("---\n")
        assert 'vibe: "happy"' in text
        assert 'vibe_tags: "[cheerful]"' in text
        assert text.rstrip().endswith("---")

    def test_rejects_invalid_key(self, _patch_config: Path) -> None:
        vox_md = _patch_config / "vox.md"
        vox_md.write_text("---\n---\n")
        with pytest.raises(ValueError, match="Unknown config key"):
            ConfigStore().write_fields({"vibe": "ok", "bad_key": "fail"})
        # vox.md should be unchanged — validation fails before any write
        assert vox_md.read_text() == "---\n---\n"

    def test_atomic_single_read_write(
        self, _patch_config: Path, monkeypatch: Any
    ) -> None:
        """Verify batch performs one read and one write per file."""
        local_md = _patch_config / "vox.local.md"
        local_md.write_text("---\n---\n")
        vox_md = _patch_config / "vox.md"
        vox_md.write_text("---\n---\n")
        read_count = 0
        write_count = 0
        orig_read = Path.read_text
        orig_write = Path.write_text

        def counting_read(self: Path, *args: Any, **kwargs: Any) -> str:
            nonlocal read_count
            if self == local_md:
                read_count += 1
            return orig_read(self, *args, **kwargs)

        def counting_write(self: Path, *args: Any, **kwargs: Any) -> int:
            nonlocal write_count
            if self == local_md:
                write_count += 1
            return orig_write(self, *args, **kwargs)

        monkeypatch.setattr(Path, "read_text", counting_read)
        monkeypatch.setattr(Path, "write_text", counting_write)
        # All ephemeral keys -> single file write to vox.local.md
        ConfigStore(_patch_config).write_fields({"vibe": "a", "vibe_tags": "[b]"})
        assert read_count == 1
        assert write_count == 1


# ---------------------------------------------------------------------------
# voice_not_found_message tests
# ---------------------------------------------------------------------------


class TestVoiceNotFoundMessage:
    """Tests for voice_not_found_message formatter."""

    def test_includes_voice_name(self) -> None:
        exc = VoiceNotFoundError("bob", ["matilda", "aria", "charlie"])
        msg = voice_not_found_message(exc)
        assert msg.startswith("bob ")

    def test_includes_suggestions(self) -> None:
        exc = VoiceNotFoundError("bob", ["matilda", "aria", "charlie"])
        msg = voice_not_found_message(exc)
        assert "How about " in msg

    def test_single_available_voice(self) -> None:
        exc = VoiceNotFoundError("bob", ["matilda"])
        msg = voice_not_found_message(exc)
        assert "matilda" in msg

    def test_empty_available_voices(self) -> None:
        exc = VoiceNotFoundError("bob", [])
        msg = voice_not_found_message(exc)
        assert "bob " in msg
        assert "How about ?" in msg


# ---------------------------------------------------------------------------
# unmute tool tests
# ---------------------------------------------------------------------------


class TestUnmute:
    """Tests for the unmute MCP tool."""

    def test_simple_text(self, monkeypatch: pytest.MonkeyPatch) -> None:
        mock_client = MagicMock()
        mock_client.synthesize.return_value = SynthesizeResult(request_id="req123")
        monkeypatch.setattr("punt_vox.server._voxd_client", lambda: mock_client)

        result = json.loads(unmute(text="Hello world"))

        assert isinstance(result, list)
        assert len(result) == 1  # pyright: ignore[reportUnknownArgumentType]
        assert result[0]["text"] == "Hello world"
        mock_client.synthesize.assert_called_once()

    def test_segments(self, monkeypatch: pytest.MonkeyPatch) -> None:
        mock_client = MagicMock()
        mock_client.synthesize.return_value = SynthesizeResult(request_id="req456")
        monkeypatch.setattr("punt_vox.server._voxd_client", lambda: mock_client)

        result = json.loads(
            unmute(
                segments=[
                    {"voice": "roger", "text": "Part one."},
                    {"text": "Part two."},
                ]
            )
        )

        assert isinstance(result, list)
        assert len(result) == 2  # pyright: ignore[reportUnknownArgumentType]
        assert mock_client.synthesize.call_count == 2

    def test_cached_flag_surfaces_in_result(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The client's cache-hit signal reaches the unmute MCP result."""
        mock_client = MagicMock()
        mock_client.synthesize.return_value = SynthesizeResult(
            request_id="rc", cached=True
        )
        monkeypatch.setattr("punt_vox.server._voxd_client", lambda: mock_client)

        result = json.loads(unmute(text="Hello world"))

        assert isinstance(result, list)
        assert result[0]["cached"] is True

    def test_cache_miss_flag_surfaces_in_result(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A cache miss reports cached=False in the unmute MCP result."""
        mock_client = MagicMock()
        mock_client.synthesize.return_value = SynthesizeResult(
            request_id="rm", cached=False
        )
        monkeypatch.setattr("punt_vox.server._voxd_client", lambda: mock_client)

        result = json.loads(unmute(text="Hello world"))

        assert result[0]["cached"] is False

    def test_no_input_returns_error(self) -> None:
        result = json.loads(unmute())
        assert "error" in result

    def test_vibe_tags_updates_session_state(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import punt_vox.server as srv

        mock_client = MagicMock()
        mock_client.synthesize.return_value = SynthesizeResult(request_id="req789")
        monkeypatch.setattr("punt_vox.server._voxd_client", lambda: mock_client)

        unmute(text="Done.", vibe_tags="[warm] [satisfied]")

        assert srv._session._vibe_tags == "[warm] [satisfied]"

    def test_voxd_connection_error_returns_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from punt_vox.client_errors import VoxdConnectionError

        mock_client = MagicMock()
        mock_client.synthesize.side_effect = VoxdConnectionError("not running")
        monkeypatch.setattr("punt_vox.server._voxd_client", lambda: mock_client)

        result = json.loads(unmute(text="Hello"))
        assert "error" in result
        assert "not running" in result["error"]

    def test_provider_persists_to_state(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import punt_vox.server as srv

        mock_client = MagicMock()
        mock_client.synthesize.return_value = SynthesizeResult(request_id="req_prov")
        monkeypatch.setattr("punt_vox.server._voxd_client", lambda: mock_client)

        unmute(text="Hello", provider="openai")
        assert srv._session.provider == "openai"

    def test_model_persists_to_state(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import punt_vox.server as srv

        mock_client = MagicMock()
        mock_client.synthesize.return_value = SynthesizeResult(request_id="req_mod")
        monkeypatch.setattr("punt_vox.server._voxd_client", lambda: mock_client)

        unmute(text="Hello", model="eleven_v3")
        assert srv._session.model == "eleven_v3"

    def test_config_only_update_no_text(self) -> None:
        """Provider/model/vibe_tags update without text returns config update."""
        import punt_vox.server as srv

        result = json.loads(unmute(provider="polly"))
        assert result["status"] == "config updated"
        assert result["provider"] == "polly"
        assert srv._session.provider == "polly"

    def test_voice_settings_validation(self) -> None:
        """Invalid voice settings raise ValueError."""
        with pytest.raises(ValueError, match="stability"):
            unmute(text="Hello", stability=1.5)

    def test_language_passed_to_synthesize(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Top-level language param is forwarded to voxd synthesize."""
        mock_client = MagicMock()
        mock_client.synthesize.return_value = SynthesizeResult(request_id="req_lang")
        monkeypatch.setattr("punt_vox.server._voxd_client", lambda: mock_client)

        unmute(text="Guten Tag", language="de")

        spec = mock_client.synthesize.call_args.args[1]
        assert spec.language == "de"

    def test_per_segment_language_overrides_default(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Per-segment language overrides the top-level default."""
        mock_client = MagicMock()
        mock_client.synthesize.return_value = SynthesizeResult(request_id="req_lang2")
        monkeypatch.setattr("punt_vox.server._voxd_client", lambda: mock_client)

        unmute(
            language="en",
            segments=[{"text": "Bonjour", "language": "fr"}],
        )

        spec = mock_client.synthesize.call_args.args[1]
        assert spec.language == "fr"


# The rec + music-catalog `mic` tools (rec_new/list/play/get/remove,
# music_new/get/remove) live on the _RecTools / _MusicCatalogTools humble
# objects and are covered in tests/test_server_rec_music.py.


# ---------------------------------------------------------------------------
# vibe tool tests
# ---------------------------------------------------------------------------


class TestVibeTool:
    """Tests for the vibe MCP tool."""

    @pytest.fixture(autouse=True)
    def _isolated(self, _patch_config: Path) -> Path:
        """Pin the vibe tool's config writes to an isolated tmp dir.

        The autouse ``hermetic_config`` fixture already redirects the default
        path, but ``vibe`` writes through ``_find_config_dir()`` -> the config
        default; requesting ``_patch_config`` states that dependency explicitly
        rather than leaning on ambient redirect state.
        """
        return _patch_config

    def test_set_mood(self) -> None:
        import punt_vox.server as srv

        result = json.loads(vibe(mood="excited"))
        assert result["vibe"]["vibe"] == "excited"
        assert srv._session._vibe == "excited"

    def test_set_tags(self) -> None:
        import punt_vox.server as srv

        result = json.loads(vibe(tags="[warm] [calm]"))
        assert result["vibe"]["vibe_tags"] == "[warm] [calm]"
        assert srv._session._vibe_tags == "[warm] [calm]"

    def test_set_mode(self) -> None:
        import punt_vox.server as srv

        result = json.loads(vibe(mode="manual"))
        assert result["vibe"]["vibe_mode"] == "manual"
        assert srv._session._vibe_mode == "manual"

    def test_invalid_mode(self) -> None:
        result = json.loads(vibe(mode="invalid"))
        assert "error" in result

    def test_no_args_returns_error(self) -> None:
        result = json.loads(vibe())
        assert "error" in result

    def test_combined_mood_and_tags(self) -> None:
        result = json.loads(vibe(mood="happy", tags="[cheerful]", mode="manual"))
        updates = result["vibe"]
        assert updates["vibe"] == "happy"
        assert updates["vibe_tags"] == "[cheerful]"
        assert updates["vibe_mode"] == "manual"

    def test_auto_clears_stale_mood(self) -> None:
        """/vibe auto is authoritative: it wipes a lingering mood and tags."""
        import punt_vox.server as srv

        vibe(mood="sad", tags="[melancholy]", mode="manual")
        updates = json.loads(vibe(tags="", mode="auto"))["vibe"]
        assert updates["vibe"] == ""
        assert updates["vibe_tags"] == ""
        assert updates["vibe_mode"] == "auto"
        assert srv._session._vibe is None
        assert srv._session._vibe_tags is None
        assert srv._session._vibe_mode == "auto"

    def test_off_clears_stale_mood(self) -> None:
        """/vibe off also resets mood and tags, not just the mode."""
        import punt_vox.server as srv

        vibe(mood="sad", tags="[melancholy]", mode="manual")
        updates = json.loads(vibe(tags="", mode="off"))["vibe"]
        assert updates["vibe"] == ""
        assert updates["vibe_mode"] == "off"
        assert srv._session._vibe is None
        assert srv._session._vibe_mode == "off"


class TestVibeToolMusicHint:
    """The vibe tool enriches its reply with a re-pool hint only while playing."""

    @pytest.fixture(autouse=True)
    def _isolated(self, _patch_config: Path) -> Path:
        return _patch_config

    def test_hint_present_when_program_playing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import punt_vox.server as srv

        srv._music_pref.started("flamenco")
        _install_fake(
            monkeypatch, FakeProgramGateway(status=ProgramStatus.radio(None, None))
        )

        result = json.loads(vibe(mood="relaxing", mode="manual"))

        assert "flamenco" in result["music_hint"]
        assert result["music"] == {"playing": True, "style": "flamenco"}

    def test_no_hint_when_music_off(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _install_fake(monkeypatch, FakeProgramGateway(status=ProgramStatus.idle()))
        result = json.loads(vibe(mood="relaxing", mode="manual"))
        assert "music_hint" not in result

    def test_vibe_never_switches_the_program(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import punt_vox.server as srv

        srv._music_pref.started("techno")
        fake = FakeProgramGateway(status=ProgramStatus.radio(None, None))
        _install_fake(monkeypatch, fake)

        vibe(mood="wired", mode="manual")

        assert fake.verbs() == ["status"]


# ---------------------------------------------------------------------------
# who tool tests
# ---------------------------------------------------------------------------


class TestWho:
    """Tests for the who MCP tool."""

    _VOICE_LIST: typing.ClassVar[list[str]] = [
        "aria",
        "callum",
        "charlie",
        "drew",
        "george",
        "jessica",
        "laura",
        "lily",
        "matilda",
        "river",
        "roger",
        "sarah",
    ]

    def test_returns_provider_and_voices(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import punt_vox.server as srv

        srv._session.provider = "elevenlabs"

        mock_client = MagicMock()
        mock_client.voices.return_value = self._VOICE_LIST
        monkeypatch.setattr("punt_vox.server._voxd_client", lambda: mock_client)

        result = json.loads(who())
        assert result["provider"] == "elevenlabs"
        assert isinstance(result["all"], list)
        assert len(result["all"]) == 12
        assert isinstance(result["featured"], list)

    def test_featured_includes_blurbs(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import punt_vox.server as srv

        srv._session.provider = "elevenlabs"

        mock_client = MagicMock()
        mock_client.voices.return_value = self._VOICE_LIST
        monkeypatch.setattr("punt_vox.server._voxd_client", lambda: mock_client)

        result = json.loads(who())
        for entry in result["featured"]:
            assert "name" in entry
            assert "blurb" in entry
            assert len(entry["blurb"]) > 0

    def test_featured_capped_at_six(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import punt_vox.server as srv

        srv._session.provider = "elevenlabs"

        mock_client = MagicMock()
        mock_client.voices.return_value = self._VOICE_LIST
        monkeypatch.setattr("punt_vox.server._voxd_client", lambda: mock_client)

        result = json.loads(who())
        assert len(result["featured"]) <= 6

    def test_current_voice_included(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import punt_vox.server as srv

        srv._session.voice = "aria"
        srv._session.provider = "elevenlabs"

        mock_client = MagicMock()
        mock_client.voices.return_value = self._VOICE_LIST
        monkeypatch.setattr("punt_vox.server._voxd_client", lambda: mock_client)

        result = json.loads(who())
        assert result["current"] == "aria"

    def test_no_current_voice(self, monkeypatch: pytest.MonkeyPatch) -> None:
        mock_client = MagicMock()
        mock_client.voices.return_value = self._VOICE_LIST
        monkeypatch.setattr("punt_vox.server._voxd_client", lambda: mock_client)

        result = json.loads(who())
        assert result["current"] is None

    def test_exposes_no_filter_argument(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import inspect

        # The tool advertises no filter -- an agent reading the schema must not
        # be offered a language argument the body would silently ignore.
        assert list(inspect.signature(who).parameters) == []

        mock_client = MagicMock()
        mock_client.voices.return_value = []
        monkeypatch.setattr("punt_vox.server._voxd_client", lambda: mock_client)
        assert "all" in json.loads(who())

    def test_provider_without_blurbs_returns_empty_featured(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import punt_vox.server as srv

        srv._session.provider = "say"

        mock_client = MagicMock()
        mock_client.voices.return_value = ["samantha", "alex"]
        monkeypatch.setattr("punt_vox.server._voxd_client", lambda: mock_client)

        result = json.loads(who())
        assert result["provider"] == "say"
        assert result["featured"] == []
        assert result["all"] == ["samantha", "alex"]

    def test_voxd_connection_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from punt_vox.client_errors import VoxdConnectionError

        mock_client = MagicMock()
        mock_client.voices.side_effect = VoxdConnectionError("not running")
        monkeypatch.setattr("punt_vox.server._voxd_client", lambda: mock_client)

        result = json.loads(who())
        assert "error" in result


# ---------------------------------------------------------------------------
# notify tool tests
# ---------------------------------------------------------------------------


class TestNotifyTool:
    """Tests for the notify MCP tool."""

    def test_set_mode_y(self) -> None:
        import punt_vox.server as srv

        result = json.loads(notify(mode="y"))
        assert result["notify"]["notify"] == "y"
        assert srv._session._notify == "y"

    def test_set_mode_n(self) -> None:
        import punt_vox.server as srv

        result = json.loads(notify(mode="n"))
        assert result["notify"]["notify"] == "n"
        assert srv._session._notify == "n"

    def test_speak_unset_c_inits_voice(self) -> None:
        """speak not yet set — mode=c should initialize it to y."""
        import punt_vox.server as srv

        result = json.loads(notify(mode="c"))
        assert result["notify"]["notify"] == "c"
        assert result["notify"]["speak"] == "y"
        assert srv._session._speak == "y"

    def test_speak_unset_y_inits_voice(self) -> None:
        """speak not yet set — mode=y should also initialize it."""
        result = json.loads(notify(mode="y"))
        assert result["notify"]["speak"] == "y"

    def test_speak_unset_first_init_inits_voice(self) -> None:
        """Fresh session — first init."""
        result = json.loads(notify(mode="y"))
        assert result["notify"]["speak"] == "y"

    def test_speak_set_preserved_by_c(self) -> None:
        """User explicitly muted — mode=c should not re-enable voice."""
        import punt_vox.server as srv

        # Simulate explicit mute.
        srv._session._speak = "n"
        srv._session._speak_explicit = True

        result = json.loads(notify(mode="c"))
        assert result["notify"]["notify"] == "c"
        assert "speak" not in result["notify"]
        assert srv._session._speak == "n"

    def test_speak_set_preserved_by_y(self) -> None:
        """User explicitly muted — mode=y should not re-enable voice."""
        import punt_vox.server as srv

        srv._session._speak = "n"
        srv._session._speak_explicit = True

        result = json.loads(notify(mode="y"))
        assert "speak" not in result["notify"]
        assert srv._session._speak == "n"

    def test_set_voice(self) -> None:
        import punt_vox.server as srv

        result = json.loads(notify(mode="c", voice="matilda"))
        assert result["notify"]["voice"] == "matilda"
        assert srv._session.voice == "matilda"

    def test_set_voice_strips_at_sigil(self) -> None:
        import punt_vox.server as srv

        result = json.loads(notify(mode="c", voice="@sarah"))
        assert result["notify"]["voice"] == "sarah"
        assert srv._session.voice == "sarah"

    def test_invalid_mode(self) -> None:
        result = json.loads(notify(mode="x"))
        assert "error" in result


# ---------------------------------------------------------------------------
# speak tool tests
# ---------------------------------------------------------------------------


class TestSpeakTool:
    """Tests for the speak MCP tool."""

    def test_set_speak_y(self) -> None:
        import punt_vox.server as srv

        result = json.loads(speak(mode="y"))
        assert result["speak"] == "y"
        assert srv._session._speak == "y"

    def test_set_speak_n(self) -> None:
        import punt_vox.server as srv

        result = json.loads(speak(mode="n"))
        assert result["speak"] == "n"
        assert srv._session._speak == "n"

    def test_set_voice(self) -> None:
        import punt_vox.server as srv

        result = json.loads(speak(mode="y", voice="matilda"))
        assert result["speak"] == "y"
        assert result["voice"] == "matilda"
        assert srv._session.voice == "matilda"

    def test_set_voice_strips_at_sigil(self) -> None:
        import punt_vox.server as srv

        result = json.loads(speak(mode="y", voice="@sarah"))
        assert result["voice"] == "sarah"
        assert srv._session.voice == "sarah"

    def test_lone_at_leaves_voice_unset(self) -> None:
        import punt_vox.server as srv

        srv._session.voice = "matilda"
        result = json.loads(speak(mode="y", voice="@"))
        assert "voice" not in result
        assert srv._session.voice == "matilda"

    def test_invalid_mode(self) -> None:
        result = json.loads(speak(mode="x"))
        assert "error" in result

    def test_marks_speak_explicit(self) -> None:
        import punt_vox.server as srv

        assert not srv._session._speak_explicit
        speak(mode="n")
        assert srv._session._speak_explicit


# ---------------------------------------------------------------------------
# status tool tests
# ---------------------------------------------------------------------------


class TestStatusTool:
    """Tests for the status MCP tool."""

    def test_returns_state_fields(self) -> None:
        import punt_vox.server as srv

        srv._session._notify = "c"
        srv._session._speak = "y"
        srv._session.voice = "sarah"
        srv._session.provider = "elevenlabs"
        srv._session._vibe_mode = "auto"
        srv._session._vibe_tags = "[excited]"

        result = json.loads(status())
        assert result["provider"] == "elevenlabs"
        assert result["voice"] == "sarah"
        assert result["notify"] == "c"
        assert result["speak"] == "y"
        assert result["vibe_mode"] == "auto"
        assert result["vibe_tags"] == "[excited]"
        # The status surfaces exactly the live vibe cluster plus the trace-sink
        # health block — no dead accumulator.
        assert {k for k in result if k.startswith("vibe")} == {
            "vibe_mode",
            "vibe",
            "vibe_tags",
            "vibe_trace",
        }

    def test_reports_vibe_trace_path_and_writable(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """status makes the trace-sink health client-observable: path + writable."""
        monkeypatch.setattr("punt_vox.vibe_trace.log_dir", lambda: tmp_path)

        health = json.loads(status())["vibe_trace"]

        assert health["path"] == str(tmp_path / "vibe-trace.log")
        assert health["writable"] is True

    def test_reports_log_level(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """status surfaces the effective (repo-override) log_level (D5)."""
        (tmp_path / "vox.local.md").write_text('---\nlog_level: "debug"\n---\n')

        def _repo(start: Path | None = None) -> Path:
            _ = start
            return tmp_path

        # A repo override raises just this repo's clients; resolve_log_level reads it.
        monkeypatch.setattr("punt_vox.config.find_config_dir", _repo)

        assert json.loads(status())["log_level"] == "debug"

    def test_reports_vibe_trace_unwritable(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """A read-only log dir surfaces writable=false through the status API."""
        locked = tmp_path / "locked"
        locked.mkdir()
        monkeypatch.setattr("punt_vox.vibe_trace.log_dir", lambda: locked)
        locked.chmod(0o500)
        try:
            health = json.loads(status())["vibe_trace"]
        finally:
            locked.chmod(0o700)

        assert health["writable"] is False

    def test_reflects_current_music_style(self) -> None:
        """status surfaces the current music style for a client to observe."""
        import punt_vox.server as srv

        srv._music_pref.started("flamenco")
        result = json.loads(status())
        assert result["style"] == "flamenco"

    def test_defaults_when_no_state_set(self) -> None:
        """A bare ``status`` reads the hermetic idle default, never the live daemon.

        ``music_mode`` is "off" because the autouse ``_hermetic_daemon`` fixture
        installs an idle ``FakeProgramGateway`` -- not because ``voxd`` happens to
        be unreachable. Without that fixture this test read the real daemon and
        failed whenever music was actually playing.
        """
        result = json.loads(status())
        assert result["voice"] is None
        assert result["provider"] is None
        assert result["notify"] == "n"
        assert result["speak"] == "n"
        assert result["music_mode"] == "off"

    def test_music_mode_derived_on_from_program_mode(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``music_mode`` is 'on' when the authoritative Program is playing."""
        program = Program(ProgramState.initial(), _StatusPolicy())
        program.turn_on()
        program.first_track_ok(Part("id001", 1))
        _install_fake(
            monkeypatch,
            FakeProgramGateway(status=program.to_status(ProgramName("amb"))),
        )
        assert json.loads(status())["music_mode"] == "on"

    def test_music_mode_derived_off_when_program_off(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``music_mode`` is 'off' when the daemon holds no active Program."""
        _install_fake(monkeypatch, FakeProgramGateway(status=ProgramStatus.idle()))
        assert json.loads(status())["music_mode"] == "off"

    def test_music_mode_reflects_external_change_no_shadow(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A Program mode change made elsewhere flips ``music_mode`` with no shadow.

        No music tool runs on this server, yet ``music_mode`` follows the daemon's
        authoritative ``program.mode`` -- no drift, because the label is derived
        instead of caching a session copy.
        """
        fake = FakeProgramGateway(status=ProgramStatus.idle())
        _install_fake(monkeypatch, fake)
        assert json.loads(status())["music_mode"] == "off"

        program = Program(ProgramState.initial(), _StatusPolicy())
        program.turn_on()
        program.first_track_ok(Part("id001", 1))
        fake.set_status(program.to_status(ProgramName("amb")))
        assert json.loads(status())["music_mode"] == "on"


class TestSuiteDoesNotTouchRealDaemon:
    """The autouse daemon redirect must be active and total for every test.

    Analogous to ``TestSuiteDoesNotTouchRealConfig`` (test_config_isolation): it
    fails loudly if the hermetic default ever regresses and a server-tool test can
    once again read or mutate the live ``voxd`` (the vox-73m5 class).
    """

    def test_program_seam_defaults_to_a_fake(self) -> None:
        """The status/music seam defaults to an in-memory fake, not a live client."""
        import punt_vox.server as srv

        assert isinstance(srv._program_tools, FakeProgramGateway)

    def test_status_reads_the_fake_not_the_daemon(self) -> None:
        """A bare ``status`` reads the idle default -- off -- through the fake seam."""
        import punt_vox.server as srv

        assert json.loads(status())["music_mode"] == "off"
        gateway = srv._program_tools
        assert isinstance(gateway, FakeProgramGateway)
        assert "status" in gateway.verbs()

    def test_synthesis_seam_is_unreachable_by_default(self) -> None:
        """The default synthesis client models 'voxd down', never the real daemon."""
        import punt_vox.server as srv

        with pytest.raises(VoxdConnectionError):
            srv._voxd_client().voices()


# ---------------------------------------------------------------------------
# SessionConfig tests
# ---------------------------------------------------------------------------


class TestSessionConfig:
    """Tests for SessionConfig defaults and session_id generation."""

    def test_session_id_is_uuid_hex(self) -> None:
        state = SessionConfig()
        assert len(state.session_id) == 32
        int(state.session_id, 16)  # valid hex

    def test_each_instance_gets_unique_id(self) -> None:
        a = SessionConfig()
        b = SessionConfig()
        assert a.session_id != b.session_id


# ---------------------------------------------------------------------------
# music / program tool tests (routed through the ProgramGateway seam)
# ---------------------------------------------------------------------------


def _install_fake(monkeypatch: pytest.MonkeyPatch, fake: FakeProgramGateway) -> None:
    """Point the server's module-level gateway at an in-memory fake."""
    import punt_vox.server as srv

    monkeypatch.setattr(srv, "_program_tools", fake)


def _music_trace_lines(logs: Path) -> list[str]:
    """Return the success ``[vibe-trace] music`` lines in the durable trace FILE.

    Reads the file the sink actually writes -- the hermetic ``hermetic_vibe_trace``
    log -- never ``caplog``: a ``[vibe-trace]`` line goes only to the file, not
    the logging module, so a caplog assertion would pass vacuously and stop
    guarding "a rejected outcome emits no success re-pool trace." The autouse
    ``_fresh_session`` rebuilds ``_music_pref`` after that redirect, so its sink
    already resolves to this file.
    """
    log = logs / "vibe-trace.log"
    if not log.exists():
        return []
    return [
        line
        for line in log.read_text(encoding="utf-8").splitlines()
        if "[vibe-trace] music" in line
    ]


class TestMusicTool:
    """The music tool routes on/off through the gateway with an F7 result."""

    def test_on_starts_via_gateway_and_reports_applied(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import punt_vox.server as srv

        srv._session._vibe = "focused"
        fake = FakeProgramGateway()
        _install_fake(monkeypatch, fake)

        result = json.loads(music(mode="on", style="techno"))

        assert result["applied"] is True
        # The panel line is a DJ phrase with the style interpolated -- and, per
        # vox-5aom, never the raw session mood.
        expected = {f"♪ {p.format(style='techno')}" for p in GENERATING_WITH_STYLE}
        assert result["message"] in expected
        assert "focused" not in result["message"]
        assert fake.verbs() == ["start"]
        request = fake.calls[0].request
        assert request is not None and request.style == "techno"

    def test_on_remembers_style_and_traces(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """music on records the style for the vibe hint and emits a [vibe-trace]."""
        import punt_vox.server as srv
        from punt_vox.vibe_command import MusicPreference

        _install_fake(monkeypatch, FakeProgramGateway())
        # Redirect the durable sink, then rebuild the session's pref so its
        # default() resolves to the temp log the test greps.
        monkeypatch.setattr("punt_vox.vibe_trace.log_dir", lambda: tmp_path)
        monkeypatch.setattr(srv, "_music_pref", MusicPreference())

        music(mode="on", style="jazz")

        assert srv._music_pref.style == "jazz"
        lines = (tmp_path / "vibe-trace.log").read_text(encoding="utf-8").splitlines()
        assert any("music on" in line and "style=jazz" in line for line in lines)

    def test_rejected_on_leaves_style_and_omits_trace(
        self, monkeypatch: pytest.MonkeyPatch, hermetic_vibe_trace: Path
    ) -> None:
        """A rejected start must not adopt the genre nor write a success trace."""
        import punt_vox.server as srv

        srv._music_pref.started("flamenco")
        _install_fake(monkeypatch, FakeProgramGateway(applied=False))

        music(mode="on", style="jazz")

        assert srv._music_pref.style == "flamenco"  # register untouched
        assert not _music_trace_lines(hermetic_vibe_trace)

    def test_applied_on_writes_music_trace(
        self, monkeypatch: pytest.MonkeyPatch, hermetic_vibe_trace: Path
    ) -> None:
        """Positive control: an applied start DOES write a success music trace.

        Proves the rejected-path ``omits_trace`` assertions are non-vacuous -- the
        server's pref writes to this same hermetic file, so a trace erroneously
        emitted on a rejected outcome would be seen and would fail those tests.
        """
        _install_fake(monkeypatch, FakeProgramGateway())  # applied=True default

        music(mode="on", style="jazz")

        assert any(
            "music on" in line and "style=jazz" in line
            for line in _music_trace_lines(hermetic_vibe_trace)
        )

    def test_daemon_error_on_leaves_style_and_omits_trace(
        self, monkeypatch: pytest.MonkeyPatch, hermetic_vibe_trace: Path
    ) -> None:
        """A start that raises leaves the register and writes no success trace."""
        import punt_vox.server as srv

        srv._music_pref.started("flamenco")
        fake = MagicMock()
        fake.start.side_effect = VoxdConnectionError("not running")
        monkeypatch.setattr(srv, "_program_tools", fake)

        result = json.loads(music(mode="on", style="jazz"))

        assert "error" in result
        assert srv._music_pref.style == "flamenco"  # register untouched
        assert not _music_trace_lines(hermetic_vibe_trace)

    @pytest.mark.parametrize("blank", ["", "   "])
    def test_blank_style_is_no_tag_and_no_style_pool(
        self, blank: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A blank style reaches the daemon as None and the panel as no-style.

        The pool pick and the forwarded tag must agree: a blank/whitespace style
        is canonicalized to None before both, so the daemon never receives an
        explicit "" while the panel shows the no-style phrase.
        """
        fake = FakeProgramGateway()
        _install_fake(monkeypatch, fake)

        result = json.loads(music(mode="on", style=blank))

        request = fake.calls[0].request
        assert request is not None and request.style is None
        assert result["message"] in {f"♪ {p}" for p in GENERATING_NO_STYLE}

    def test_on_forwards_agent_prompts(self, monkeypatch: pytest.MonkeyPatch) -> None:
        fake = FakeProgramGateway()
        _install_fake(monkeypatch, fake)
        variations = [f"var{i}" for i in range(12)]

        music(mode="on", base_prompt="deep techno", variations=variations)

        request = fake.calls[0].request
        assert request is not None
        assert request.prompts is not None
        assert request.prompts.base == "deep techno"
        assert request.prompts.variations == tuple(variations)

    def test_on_invalid_prompt_shape_reports_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _install_fake(monkeypatch, FakeProgramGateway())

        result = json.loads(music(mode="on", base_prompt="x", variations=["only one"]))

        assert "error" in result

    def test_off_stops_via_gateway(self, monkeypatch: pytest.MonkeyPatch) -> None:
        fake = FakeProgramGateway()
        _install_fake(monkeypatch, fake)

        result = json.loads(music(mode="off"))

        assert result["applied"] is True
        assert result["message"] in {f"♪ {p}" for p in STOPPED}
        assert fake.verbs() == ["stop"]

    def test_off_clears_the_style(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """music off clears the style so a later vibe change gets no stale hint."""
        import punt_vox.server as srv

        srv._music_pref.started("flamenco")
        _install_fake(monkeypatch, FakeProgramGateway())

        music(mode="off")

        assert srv._music_pref.style is None

    def test_rejected_off_keeps_style_and_omits_trace(
        self, monkeypatch: pytest.MonkeyPatch, hermetic_vibe_trace: Path
    ) -> None:
        """A rejected stop must not clear the style nor write a success trace."""
        import punt_vox.server as srv

        srv._music_pref.started("flamenco")
        _install_fake(monkeypatch, FakeProgramGateway(applied=False))

        music(mode="off")

        assert srv._music_pref.style == "flamenco"  # register untouched
        assert not _music_trace_lines(hermetic_vibe_trace)

    def test_invalid_mode(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _install_fake(monkeypatch, FakeProgramGateway())
        result = json.loads(music(mode="sideways"))
        assert "error" in result

    def test_rejected_start_surfaces_not_applied(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A lost-race start reaches the caller as applied=false."""
        _install_fake(monkeypatch, FakeProgramGateway(applied=False))

        result = json.loads(music(mode="on", style="techno"))

        assert result["applied"] is False

    def test_rejected_on_surfaces_reason_not_success_line(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A rejected 'on' shows the daemon's reason, not the generating line."""
        _install_fake(
            monkeypatch,
            FakeProgramGateway(applied=False, reason="already generating"),
        )

        result = json.loads(music(mode="on", style="techno"))

        assert result["applied"] is False
        assert "already generating" in result["message"]
        assert "generating a techno track" not in result["message"]

    def test_daemon_error_reports(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import punt_vox.server as srv

        fake = MagicMock()
        fake.start.side_effect = VoxdConnectionError("not running")
        monkeypatch.setattr(srv, "_program_tools", fake)

        result = json.loads(music(mode="on"))

        assert "error" in result


class TestMusicPlayTool:
    """music_play routes a Selection replay through the gateway."""

    def test_play_forwards_the_tag_query(self, monkeypatch: pytest.MonkeyPatch) -> None:
        fake = FakeProgramGateway()
        _install_fake(monkeypatch, fake)

        result = json.loads(music_play(style="trance", vibe="calm"))

        assert result["applied"] is True
        # No name -> the radio (crate) pool, never a named-replay line.
        assert result["message"] in {f"♪ {p}" for p in REPLAY_RADIO}
        assert fake.calls[0].verb == "select"
        assert fake.calls[0].selection is not None
        assert fake.calls[0].selection.style == "trance"

    def test_play_with_style_updates_the_style(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A styled replay makes that style the current genre for the hint."""
        import punt_vox.server as srv

        srv._music_pref.started("flamenco")
        _install_fake(monkeypatch, FakeProgramGateway())

        music_play(style="techno")

        assert srv._music_pref.style == "techno"

    def test_play_without_style_clears_the_style(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A by-name replay absent from the catalog clears the style (no genre)."""
        import punt_vox.server as srv

        srv._music_pref.started("flamenco")
        _install_fake(monkeypatch, FakeProgramGateway())

        music_play(name="deep cuts")

        assert srv._music_pref.style is None

    def test_play_by_id_adopts_the_albums_actual_style(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An id replay adopts the resolved album's genre, so the hint fires."""
        import punt_vox.server as srv

        srv._music_pref.stopped()
        catalog = (
            ProgramSummary(
                id="a3f1c9", style="trance", vibe="calm", format="music", ready=5
            ),
            ProgramSummary(
                id="7b2e04", style="lofi", vibe="focus", format="music", ready=1
            ),
        )
        _install_fake(monkeypatch, FakeProgramGateway(catalog=catalog))

        music_play(album_id="a3f1c9")

        assert srv._music_pref.style == "trance"

    def test_play_union_across_genres_clears_the_style(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A genre-spanning union replay has no single style, so the register clears."""
        import punt_vox.server as srv

        srv._music_pref.started("flamenco")
        catalog = (
            ProgramSummary(
                id="a3f1c9", style="trance", vibe="calm", format="music", ready=5
            ),
            ProgramSummary(
                id="7b2e04", style="lofi", vibe="calm", format="music", ready=1
            ),
        )
        _install_fake(monkeypatch, FakeProgramGateway(catalog=catalog))

        music_play()  # all-None replays every album, across genres

        assert srv._music_pref.style is None

    def test_rejected_play_leaves_style_and_omits_trace(
        self, monkeypatch: pytest.MonkeyPatch, hermetic_vibe_trace: Path
    ) -> None:
        """A rejected replay must not adopt the style nor write a success trace."""
        import punt_vox.server as srv

        srv._music_pref.started("flamenco")
        _install_fake(monkeypatch, FakeProgramGateway(applied=False))

        music_play(style="techno")

        assert srv._music_pref.style == "flamenco"  # register untouched
        assert not _music_trace_lines(hermetic_vibe_trace)

    def test_rejected_play_skips_the_catalog_lookup(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A rejected replay drops the style, so it spares the catalog round-trip."""
        fake = FakeProgramGateway(applied=False)
        _install_fake(monkeypatch, fake)

        result = json.loads(music_play(style="techno"))

        assert result["applied"] is False
        assert "catalog" not in fake.verbs()

    def test_applied_play_resolves_via_the_catalog(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An applied replay consults the catalog to name the genre now playing."""
        fake = FakeProgramGateway(
            catalog=(
                ProgramSummary(
                    id="a3f1c9", style="trance", vibe="calm", format="music", ready=5
                ),
            )
        )
        _install_fake(monkeypatch, fake)

        result = json.loads(music_play(album_id="a3f1c9"))

        assert result["applied"] is True
        assert "catalog" in fake.verbs()

    def test_play_by_name_uses_named_pool(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A named replay draws from the named pool with the name interpolated."""
        _install_fake(monkeypatch, FakeProgramGateway())

        result = json.loads(music_play(name="deep cuts"))

        expected = {f"♪ {p.format(name='deep cuts')}" for p in REPLAY_WITH_NAME}
        assert result["message"] in expected

    def test_blank_name_is_radio_not_named_pool(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A whitespace name reaches the daemon as None and the panel as radio.

        The canonicalized name is None before both the pool pick and the query,
        so a blank name never becomes an "" filter or a named-replay line.
        """
        fake = FakeProgramGateway()
        _install_fake(monkeypatch, fake)

        result = json.loads(music_play(name="  "))

        assert fake.calls[0].selection is not None
        assert fake.calls[0].selection.name is None
        assert result["message"] in {f"♪ {p}" for p in REPLAY_RADIO}

    def test_play_forwards_the_album_id(self, monkeypatch: pytest.MonkeyPatch) -> None:
        fake = FakeProgramGateway()
        _install_fake(monkeypatch, fake)

        json.loads(music_play(album_id="a3f1c9"))

        assert fake.calls[0].selection is not None
        assert fake.calls[0].selection.id == "a3f1c9"

    def test_play_daemon_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import punt_vox.server as srv

        fake = MagicMock()
        fake.select.side_effect = VoxdConnectionError("not running")
        monkeypatch.setattr(srv, "_program_tools", fake)

        result = json.loads(music_play(style="trance"))

        assert "error" in result

    def test_play_survives_a_catalog_fault_after_an_applied_select(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A catalog fault after an applied select still reports success.

        Naming the genre for the re-pool hint is a best-effort follow-up to a
        replay that has already applied; a daemon fault resolving it must not
        turn that success into a reported error. The style is left unknown (the
        hint is simply omitted), and no exception escapes.
        """
        import punt_vox.server as srv

        srv._music_pref.started("flamenco")
        fake = MagicMock()
        fake.select.return_value = CommandOutcome.ok("")
        fake.catalog.side_effect = VoxdConnectionError("not running")
        monkeypatch.setattr(srv, "_program_tools", fake)

        result = json.loads(music_play(name="deep cuts"))

        assert "error" not in result
        assert result["applied"] is True
        assert srv._music_pref.style is None


class TestMusicListTool:
    """music_list groups saved Programs, not loose files."""

    def test_list_groups_albums(self, monkeypatch: pytest.MonkeyPatch) -> None:
        catalog = (
            ProgramSummary(
                id="a3f1c9", style="trance", vibe="calm", format="music", ready=5
            ),
            ProgramSummary(
                id="7b2e04", style="lofi", vibe="focus", format="music", ready=1
            ),
        )
        _install_fake(monkeypatch, FakeProgramGateway(catalog=catalog))

        result = json.loads(music_list())

        assert [p["id"] for p in result["programs"]] == ["a3f1c9", "7b2e04"]
        assert "a3f1c9" in result["message"]

    def test_list_empty(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _install_fake(monkeypatch, FakeProgramGateway())
        result = json.loads(music_list())
        assert result["programs"] == []
        assert "No saved albums" in result["message"]

    def test_list_daemon_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import punt_vox.server as srv

        fake = MagicMock()
        fake.catalog.side_effect = VoxdConnectionError("not running")
        monkeypatch.setattr(srv, "_program_tools", fake)

        result = json.loads(music_list())

        assert "error" in result


class TestMusicNextTool:
    """music_next is the one ungated advance."""

    def test_next_advances(self, monkeypatch: pytest.MonkeyPatch) -> None:
        fake = FakeProgramGateway()
        _install_fake(monkeypatch, fake)

        result = json.loads(music_next())

        assert result["applied"] is True
        assert result["message"] in {f"♪ {p}" for p in SKIP}
        assert fake.verbs() == ["advance"]

    def test_next_rejected_surfaces_not_applied(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _install_fake(monkeypatch, FakeProgramGateway(applied=False))
        result = json.loads(music_next())
        assert result["applied"] is False

    def test_next_rejected_surfaces_reason_not_skip_line(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A next-after-off shows the reason, not a misleading skip line (F4/F7)."""
        _install_fake(
            monkeypatch,
            FakeProgramGateway(applied=False, reason="nothing is playing"),
        )

        result = json.loads(music_next())

        assert result["applied"] is False
        assert "nothing is playing" in result["message"]
        assert "Skipping" not in result["message"]

    def test_next_daemon_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import punt_vox.server as srv

        fake = MagicMock()
        fake.advance.side_effect = OSError("boom")
        monkeypatch.setattr(srv, "_program_tools", fake)

        result = json.loads(music_next())

        assert "error" in result


class TestStatusProgramSurface:
    """The status tool serves the daemon's authoritative Program status."""

    def test_status_includes_both_failure_surfaces(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Both the program-level error and per-Part failures reach the client."""
        program = Program(ProgramState.initial(), _StatusPolicy())
        program.turn_on()
        program.first_track_ok(Part("id001", 1))
        program.fill_bad_part(Part("id002", 2), Reason("bad_prompt: unsafe"))
        status_value = program.to_status(ProgramName("ambient_techno"))
        _install_fake(monkeypatch, FakeProgramGateway(status=status_value))

        block = json.loads(status())["program"]

        assert block["mode"] == "playing_filling"
        assert block["now_playing"] is not None
        assert block["failed_parts"][0]["index"] == 2

    def test_status_reads_fresh_no_server_cache(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A state change made via another path is reflected next call."""
        fake = FakeProgramGateway(status=ProgramStatus.idle())
        _install_fake(monkeypatch, fake)

        first = json.loads(status())["program"]
        assert first.get("name") is None

        # A different client turns music on; the server must not serve a shadow.
        program = Program(ProgramState.initial(), _StatusPolicy())
        program.turn_on()
        program.first_track_ok(Part("id001", 1))
        fake.set_status(program.to_status(ProgramName("ambient_techno")))

        second = json.loads(status())["program"]
        assert second["name"] == "ambient_techno"

    def test_status_degrades_when_daemon_unreachable(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import punt_vox.server as srv

        fake = MagicMock()
        fake.status.side_effect = VoxdConnectionError("not running")
        monkeypatch.setattr(srv, "_program_tools", fake)

        block = json.loads(status())["program"]

        assert "error" in block


# ---------------------------------------------------------------------------
# SessionConfig.refresh_from_config tests
# ---------------------------------------------------------------------------


@pytest.fixture()
def _refresh_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:  # pyright: ignore[reportUnusedFunction]
    """Patch _find_config_dir and DEFAULT_CONFIG_DIR for refresh tests."""
    import punt_vox.config as cfg
    import punt_vox.dirs as dirs
    import punt_vox.server as srv

    monkeypatch.setattr(cfg, "DEFAULT_CONFIG_DIR", tmp_path)
    monkeypatch.setattr(dirs, "DEFAULT_CONFIG_DIR", tmp_path)
    monkeypatch.setattr(srv, "_find_config_dir", lambda: tmp_path)
    return tmp_path


class TestRefreshFromConfig:
    """Tests for SessionConfig.refresh_from_config reading external changes."""

    def test_ephemeral_fields_always_updated(
        self, _refresh_config: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Vibe/tags written externally are picked up on refresh."""
        import punt_vox.server as srv

        srv._session._vibe = "old-mood"
        srv._session._vibe_tags = "[old]"

        (_refresh_config / "vox.local.md").write_text(
            '---\nvibe: "happy"\nvibe_tags: "[warm]"\n---\n'
        )

        srv._session.refresh_from_config()

        assert srv._session._vibe == "happy"
        assert srv._session._vibe_tags == "[warm]"

    def test_ephemeral_cleared_when_config_empty(
        self, _refresh_config: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When config has no vibe fields, in-memory vibe is cleared."""
        import punt_vox.server as srv

        srv._session._vibe = "stale-mood"
        srv._session._vibe_tags = "[stale]"

        # Config exists but has no vibe fields
        (_refresh_config / "vox.local.md").write_text("---\n---\n")
        (_refresh_config / "vox.md").write_text("---\n---\n")

        srv._session.refresh_from_config()

        assert srv._session._vibe is None
        assert srv._session._vibe_tags is None  # type: ignore[unreachable]

    def test_durable_fields_updated_from_config(
        self, _refresh_config: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """notify, speak take the durable value; vibe_mode the ephemeral one."""
        import punt_vox.server as srv

        srv._session._notify = "n"
        srv._session._speak = "n"
        srv._session._vibe_mode = "off"

        (_refresh_config / "vox.md").write_text('---\nnotify: "c"\nspeak: "y"\n---\n')
        (_refresh_config / "vox.local.md").write_text('---\nvibe_mode: "auto"\n---\n')

        srv._session.refresh_from_config()

        assert srv._session._notify == "c"
        assert srv._session._speak == "y"
        assert srv._session._vibe_mode == "auto"

    def test_voice_only_overwritten_when_config_has_value(
        self, _refresh_config: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """In-memory voice survives refresh when config has no voice."""
        import punt_vox.server as srv

        srv._session.voice = "matilda"

        # Config has no voice field
        (_refresh_config / "vox.md").write_text("---\n---\n")

        srv._session.refresh_from_config()

        assert srv._session.voice == "matilda"

    def test_voice_overwritten_when_config_has_value(
        self, _refresh_config: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Config voice overwrites in-memory voice."""
        import punt_vox.server as srv

        srv._session.voice = "matilda"

        (_refresh_config / "vox.md").write_text('---\nvoice: "roger"\n---\n')

        srv._session.refresh_from_config()

        assert srv._session.voice == "roger"

    def test_provider_survives_when_config_empty(
        self, _refresh_config: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """In-memory provider override survives when config has no provider."""
        import punt_vox.server as srv

        srv._session.provider = "openai"

        (_refresh_config / "vox.md").write_text("---\n---\n")

        srv._session.refresh_from_config()

        assert srv._session.provider == "openai"

    def test_model_survives_when_config_empty(
        self, _refresh_config: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """In-memory model override survives when config has no model."""
        import punt_vox.server as srv

        srv._session.model = "eleven_v3"

        (_refresh_config / "vox.md").write_text("---\n---\n")

        srv._session.refresh_from_config()

        assert srv._session.model == "eleven_v3"

    def test_no_config_dir_is_noop(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """When _find_config_dir returns None, refresh is a no-op."""
        import punt_vox.server as srv

        monkeypatch.setattr(srv, "_find_config_dir", lambda: None)
        srv._session._vibe = "should-survive"

        srv._session.refresh_from_config()

        assert srv._session._vibe == "should-survive"


class TestRefreshIntegrationWithTools:
    """Verify tool calls pick up external config writes via refresh."""

    def test_status_reflects_external_vibe_change(
        self, _refresh_config: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """CLI writes vibe to config; status tool reads the new value."""
        import punt_vox.server as srv

        srv._session._vibe = "sad"
        srv._session._vibe_tags = "[gloomy]"

        # Simulate CLI writing new vibe to config
        ConfigStore(_refresh_config).write_fields(
            {"vibe": "happy", "vibe_tags": "[cheerful]"}
        )

        result = json.loads(status())

        assert result["vibe"] == "happy"
        assert result["vibe_tags"] == "[cheerful]"

    def test_status_reflects_external_notify_change(
        self, _refresh_config: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """External notify write is reflected in status."""
        import punt_vox.server as srv

        srv._session._notify = "n"

        ConfigStore(_refresh_config).write_field("notify", "c")

        result = json.loads(status())

        assert result["notify"] == "c"

    def test_music_panel_never_carries_the_session_mood(
        self, _refresh_config: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The DJ panel line uses style/name only, never the raw mood (vox-5aom).

        The session vibe is display/record state; it neither surfaces in the
        panel line nor travels into a Program transition.
        """
        import punt_vox.server as srv

        srv._session._vibe = "old-mood"
        srv._session._vibe_tags = "[old]"

        # External write clears vibe (e.g. `vox vibe auto`).
        ConfigStore(_refresh_config).write_fields({"vibe": "", "vibe_tags": ""})

        fake = FakeProgramGateway()
        monkeypatch.setattr(srv, "_program_tools", fake)

        message = json.loads(music(mode="on"))["message"]

        # The mood never reaches the panel, cleared or not.
        assert "old-mood" not in message
        # And no vibe travelled into the Program start request.
        request = fake.calls[0].request
        assert request is not None and request.style is None

    def test_unmute_uses_refreshed_voice(
        self, _refresh_config: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """unmute picks up voice written to config externally."""
        import punt_vox.server as srv

        srv._session.voice = "matilda"

        ConfigStore(_refresh_config).write_field("voice", "roger")

        mock_client = MagicMock()
        mock_client.synthesize.return_value = SynthesizeResult(request_id="r1")
        monkeypatch.setattr("punt_vox.server._voxd_client", lambda: mock_client)

        unmute(text="Hello")

        spec = mock_client.synthesize.call_args.args[1]
        assert spec.voice == "roger"

    def test_unmute_preserves_inmemory_provider_when_config_empty(
        self, _refresh_config: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Provider set via MCP tool survives refresh when config is empty."""
        import punt_vox.server as srv

        srv._session.provider = "openai"

        # Config has no provider field
        (_refresh_config / "vox.md").write_text("---\n---\n")

        mock_client = MagicMock()
        mock_client.synthesize.return_value = SynthesizeResult(request_id="r2")
        monkeypatch.setattr("punt_vox.server._voxd_client", lambda: mock_client)

        unmute(text="Hello")

        spec = mock_client.synthesize.call_args.args[1]
        assert spec.provider == "openai"


class TestPerToolLogging:
    """The FastMCP subclass logs one ``mic:<tool>`` line per invocation."""

    @pytest.mark.asyncio
    async def test_call_tool_logs_named_line_and_preserves_schema(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A tool call logs ``mic:<name>`` and the tool schema is unchanged.

        Goes through ``call_tool`` -- the native FastMCP choke point -- so this
        would catch a decorator that FastMCP unwraps and bypasses.
        """
        import logging

        import punt_vox.server as srv

        tools = await srv.mcp.list_tools()
        # 6 session/synthesis tools (unmute, vibe, who, notify, speak, status) +
        # the single `music` tool + the single `rec` tool.
        assert len(tools) == 8  # schema intact, no tool lost to wrapping
        with caplog.at_level(logging.INFO, logger="punt_vox.server"):
            await srv.mcp.call_tool("status", {})
        named = [
            r.getMessage()
            for r in caplog.records
            if r.name == "punt_vox.server" and r.getMessage().startswith("mic:")
        ]
        assert named == ["mic:status"]

    @pytest.mark.asyncio
    async def test_call_tool_reapplies_log_level(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Every tool call re-applies the effective level, so vox log takes hold."""
        import punt_vox.server as srv

        calls: list[bool] = []
        monkeypatch.setattr(srv, "reapply_client_log_level", lambda: calls.append(True))
        await srv.mcp.call_tool("status", {})
        assert calls == [True]
