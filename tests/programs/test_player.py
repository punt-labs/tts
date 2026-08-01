"""Tests for the Player seam protocols."""

from __future__ import annotations

from punt_vox.voxd.programs.player import Player, PlayHandle


def test_protocols_are_importable() -> None:
    # The seam is a pair of Protocols; importing exercises their definitions and
    # documents the loop's dependency contract (await_ready/play + pause/resume/stop
    # on the player, and ended() on the handle).
    assert Player is not None
    assert PlayHandle is not None
