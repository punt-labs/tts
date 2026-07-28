"""Tests for the additive ``.claude/settings.json`` registration (§ 2.8)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from punt_vox.settings_registration import SettingsRegistration

_ENTRY = "Bash(vox:*)"


def _reg(tmp_path: Path) -> SettingsRegistration:
    return SettingsRegistration(tmp_path / ".claude" / "settings.json")


def _allow(path: Path) -> list[str]:
    data = json.loads(path.read_text(encoding="utf-8"))
    return [str(entry) for entry in data["permissions"]["allow"]]


# ---------------------------------------------------------------------------
# register -- additive, order-preserving, idempotent
# ---------------------------------------------------------------------------


def test_register_creates_file_with_the_entry(tmp_path: Path) -> None:
    reg = _reg(tmp_path)
    assert reg.register() is True
    settings = tmp_path / ".claude" / "settings.json"
    assert _allow(settings) == [_ENTRY]


def test_second_register_is_a_no_op(tmp_path: Path) -> None:
    reg = _reg(tmp_path)
    assert reg.register() is True
    assert reg.register() is False
    assert _allow(tmp_path / ".claude" / "settings.json") == [_ENTRY]


def test_register_preserves_existing_entries_and_order(tmp_path: Path) -> None:
    settings = tmp_path / ".claude" / "settings.json"
    settings.parent.mkdir(parents=True)
    settings.write_text(
        json.dumps({"permissions": {"allow": ["Bash(git:*)", "Read"]}}),
        encoding="utf-8",
    )
    assert _reg(tmp_path).register() is True
    # Appended in order, existing entries untouched.
    assert _allow(settings) == ["Bash(git:*)", "Read", _ENTRY]


def test_register_preserves_unrelated_settings_keys(tmp_path: Path) -> None:
    settings = tmp_path / ".claude" / "settings.json"
    settings.parent.mkdir(parents=True)
    settings.write_text(json.dumps({"model": "opus", "hooks": {}}), encoding="utf-8")
    assert _reg(tmp_path).register() is True
    data = json.loads(settings.read_text(encoding="utf-8"))
    assert data["model"] == "opus"
    assert data["hooks"] == {}
    assert data["permissions"]["allow"] == [_ENTRY]


# ---------------------------------------------------------------------------
# deregister -- exact value-match removal
# ---------------------------------------------------------------------------


def test_deregister_removes_only_the_owned_entry(tmp_path: Path) -> None:
    settings = tmp_path / ".claude" / "settings.json"
    settings.parent.mkdir(parents=True)
    settings.write_text(
        json.dumps({"permissions": {"allow": ["Bash(git:*)", _ENTRY, "Read"]}}),
        encoding="utf-8",
    )
    assert _reg(tmp_path).deregister() is True
    assert _allow(settings) == ["Bash(git:*)", "Read"]


def test_deregister_absent_entry_is_a_no_op(tmp_path: Path) -> None:
    settings = tmp_path / ".claude" / "settings.json"
    settings.parent.mkdir(parents=True)
    settings.write_text(
        json.dumps({"permissions": {"allow": ["Read"]}}), encoding="utf-8"
    )
    assert _reg(tmp_path).deregister() is False
    assert _allow(settings) == ["Read"]


def test_deregister_missing_file_is_a_no_op(tmp_path: Path) -> None:
    assert _reg(tmp_path).deregister() is False


def test_register_then_deregister_round_trip(tmp_path: Path) -> None:
    reg = _reg(tmp_path)
    reg.register()
    assert reg.deregister() is True
    assert _allow(tmp_path / ".claude" / "settings.json") == []


# ---------------------------------------------------------------------------
# boundary -- malformed settings fails fast, never silently discarded
# ---------------------------------------------------------------------------


def test_malformed_json_raises_rather_than_clobbering(tmp_path: Path) -> None:
    settings = tmp_path / ".claude" / "settings.json"
    settings.parent.mkdir(parents=True)
    settings.write_text("{ not json", encoding="utf-8")
    with pytest.raises(ValueError):
        _reg(tmp_path).register()
    # The user's file is untouched -- not reset to {}.
    assert settings.read_text(encoding="utf-8") == "{ not json"


def test_non_object_root_raises(tmp_path: Path) -> None:
    settings = tmp_path / ".claude" / "settings.json"
    settings.parent.mkdir(parents=True)
    settings.write_text("[1, 2, 3]", encoding="utf-8")
    with pytest.raises(ValueError):
        _reg(tmp_path).register()
