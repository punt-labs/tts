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


def _write_settings(tmp_path: Path, data: object) -> Path:
    settings = tmp_path / ".claude" / "settings.json"
    settings.parent.mkdir(parents=True)
    settings.write_text(json.dumps(data), encoding="utf-8")
    return settings


def test_register_raises_when_permissions_is_not_an_object(tmp_path: Path) -> None:
    # A present-but-malformed nested value is a boundary error, symmetric with
    # the root check: never silently discarded, never overwritten.
    settings = _write_settings(tmp_path, {"permissions": "nope"})
    with pytest.raises(ValueError, match="permissions must be a JSON object"):
        _reg(tmp_path).register()
    assert json.loads(settings.read_text(encoding="utf-8")) == {"permissions": "nope"}


def test_register_raises_when_allow_is_not_a_list(tmp_path: Path) -> None:
    settings = _write_settings(tmp_path, {"permissions": {"allow": "nope"}})
    with pytest.raises(ValueError, match=r"permissions\.allow must be a JSON array"):
        _reg(tmp_path).register()
    assert json.loads(settings.read_text(encoding="utf-8")) == {
        "permissions": {"allow": "nope"}
    }


def test_deregister_raises_when_permissions_is_not_an_object(tmp_path: Path) -> None:
    _write_settings(tmp_path, {"permissions": 5})
    with pytest.raises(ValueError, match="permissions must be a JSON object"):
        _reg(tmp_path).deregister()


def test_register_raises_when_permissions_is_null(tmp_path: Path) -> None:
    # A present-but-null permissions is malformed, not absent: a JSON `null` is a
    # value the user wrote, so treating it as "key absent" and overwriting it
    # would silently discard it. Symmetric with the non-dict case.
    settings = _write_settings(tmp_path, {"permissions": None})
    with pytest.raises(ValueError, match="permissions must be a JSON object"):
        _reg(tmp_path).register()
    assert json.loads(settings.read_text(encoding="utf-8")) == {"permissions": None}


def test_register_raises_when_allow_is_null(tmp_path: Path) -> None:
    # A present-but-null allow is malformed, not absent -- never silently created
    # over the user's explicit null.
    settings = _write_settings(tmp_path, {"permissions": {"allow": None}})
    with pytest.raises(ValueError, match=r"permissions\.allow must be a JSON array"):
        _reg(tmp_path).register()
    assert json.loads(settings.read_text(encoding="utf-8")) == {
        "permissions": {"allow": None}
    }


def test_deregister_raises_when_permissions_is_null(tmp_path: Path) -> None:
    _write_settings(tmp_path, {"permissions": None})
    with pytest.raises(ValueError, match="permissions must be a JSON object"):
        _reg(tmp_path).deregister()


def test_register_creates_absent_permissions_key(tmp_path: Path) -> None:
    # An ABSENT permissions key is safe to add (create-when-absent), leaving
    # unrelated keys untouched.
    settings = _write_settings(tmp_path, {"model": "opus"})
    assert _reg(tmp_path).register() is True
    data = json.loads(settings.read_text(encoding="utf-8"))
    assert data["model"] == "opus"
    assert data["permissions"]["allow"] == [_ENTRY]


def test_register_creates_absent_allow_key(tmp_path: Path) -> None:
    # permissions present as an object but with no allow list: the list is
    # created, the object is not overwritten.
    settings = _write_settings(tmp_path, {"permissions": {"deny": ["X"]}})
    assert _reg(tmp_path).register() is True
    data = json.loads(settings.read_text(encoding="utf-8"))
    assert data["permissions"]["deny"] == ["X"]
    assert data["permissions"]["allow"] == [_ENTRY]
