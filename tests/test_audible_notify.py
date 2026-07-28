"""Tests for :class:`~punt_vox.audible_notify.AudibleNotify`.

The load-bearing property: after ``ensure_audible`` the repo has an audible
notify level. An absent field or the silent ``"n"`` is raised to ``"y"``; an
already-audible ``"y"`` or ``"c"`` is preserved, so a re-enable never downgrades
a user's ``continuous`` choice.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from punt_vox.audible_notify import AudibleNotify
from punt_vox.config import ConfigStore


def test_ensure_audible_sets_default_when_notify_absent(tmp_path: Path) -> None:
    AudibleNotify(tmp_path).ensure_audible()
    assert ConfigStore(tmp_path).read_field("notify") == "y"


def test_ensure_audible_raises_silent_n_to_audible_y(tmp_path: Path) -> None:
    ConfigStore(tmp_path).write_field("notify", "n")
    AudibleNotify(tmp_path).ensure_audible()
    assert ConfigStore(tmp_path).read_field("notify") == "y"


def test_ensure_audible_preserves_continuous(tmp_path: Path) -> None:
    ConfigStore(tmp_path).write_field("notify", "c")
    AudibleNotify(tmp_path).ensure_audible()
    # A user who chose continuous keeps it across a re-enable -- no downgrade.
    assert ConfigStore(tmp_path).read_field("notify") == "c"


def test_ensure_audible_preserves_normal(tmp_path: Path) -> None:
    ConfigStore(tmp_path).write_field("notify", "y")
    AudibleNotify(tmp_path).ensure_audible()
    assert ConfigStore(tmp_path).read_field("notify") == "y"


@pytest.mark.parametrize("start", ["n", None, "c", "y"])
def test_ensure_audible_is_idempotent(start: str | None, tmp_path: Path) -> None:
    store = ConfigStore(tmp_path)
    if start is not None:
        store.write_field("notify", start)
    audible = AudibleNotify(tmp_path)
    audible.ensure_audible()
    once = store.read_field("notify")
    audible.ensure_audible()
    assert store.read_field("notify") == once
    assert once in ("y", "c")
