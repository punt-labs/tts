"""Tests for the ChangeSignal pubsub fan-out and its fail-soft contract."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, final

from punt_vox.voxd.programs.change_listener import ChangeListener
from punt_vox.voxd.programs.change_signal import ChangeSignal

if TYPE_CHECKING:
    import pytest


@final
class _Counter:
    """A listener that counts notifications."""

    def __init__(self) -> None:
        self.count = 0

    def notify_changed(self) -> None:
        self.count += 1


@final
class _Boom:
    """A listener that always raises."""

    def notify_changed(self) -> None:
        raise RuntimeError("boom")


def test_change_listener_is_runtime_checkable() -> None:
    assert isinstance(_Counter(), ChangeListener)


def test_emit_notifies_every_subscriber() -> None:
    signal = ChangeSignal()
    one, two = _Counter(), _Counter()
    signal.subscribe(one)
    signal.subscribe(two)

    signal.emit()
    signal.emit()

    assert one.count == 2
    assert two.count == 2


def test_emit_with_no_subscribers_is_a_noop() -> None:
    ChangeSignal().emit()  # must not raise


def test_a_raising_listener_is_logged_and_does_not_break_the_fan_out(
    caplog: pytest.LogCaptureFixture,
) -> None:
    signal = ChangeSignal()
    survivor = _Counter()
    signal.subscribe(_Boom())
    signal.subscribe(survivor)

    with caplog.at_level(logging.ERROR):
        signal.emit()

    assert survivor.count == 1  # the raising listener did not abort the loop
    assert any("change listener raised" in r.getMessage() for r in caplog.records)
