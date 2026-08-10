"""Tests for :mod:`punt_vox.commands._result` — the shared command types."""

from __future__ import annotations

import pytest

from punt_vox.commands import CommandResult, Ctx
from punt_vox.commands._result import SwitchList


class TestCommandResult:
    """The dataclass is frozen, defaults success, carries the four wire fields."""

    def test_defaults_are_success(self) -> None:
        result = CommandResult(text="ok")
        assert result.text == "ok"
        assert result.json_data is None
        assert result.error is False
        assert result.exit_code == 0

    def test_error_carries_exit_code(self) -> None:
        result = CommandResult(
            text="bad input",
            json_data={"error": "bad input"},
            error=True,
            exit_code=1,
        )
        assert result.error is True
        assert result.exit_code == 1
        assert result.json_data == {"error": "bad input"}

    def test_frozen(self) -> None:
        result = CommandResult(text="ok")
        with pytest.raises((AttributeError, TypeError)):
            result.text = "no"  # type: ignore[misc]


class TestCtxShape:
    """``Ctx`` is frozen -- library callers construct it once and reuse it."""

    def test_frozen(self) -> None:
        # Ctx carries live wire objects; here we inspect the dataclass discipline
        # (frozen, slotted), so stand-ins are enough.
        ctx = Ctx(store=object(), client=object())  # type: ignore[arg-type]
        with pytest.raises((AttributeError, TypeError)):
            ctx.store = object()  # type: ignore[misc,assignment]


class TestSwitchList:
    """``SwitchList`` marks the current entry and renders both text and JSON."""

    def test_render_marks_current(self) -> None:
        assert SwitchList(names=("a", "b", "c"), current="b").render() == (
            "a\nb (current)\nc"
        )

    def test_render_no_current(self) -> None:
        assert SwitchList(names=("a", "b"), current=None).render() == "a\nb"

    def test_render_empty_uses_message(self) -> None:
        assert SwitchList(names=(), current=None).render("no models") == "no models"

    def test_render_empty_default_message(self) -> None:
        assert SwitchList(names=(), current=None).render() == ""

    def test_payload_carries_names_and_current(self) -> None:
        payload = SwitchList(names=("a", "b"), current="a").payload()
        assert payload == {"names": ["a", "b"], "current": "a"}
