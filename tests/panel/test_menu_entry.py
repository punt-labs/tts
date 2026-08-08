"""Tests for :mod:`punt_vox.panel.menu_entry`.

Four ways the registration ends -- it lands, luxd refuses it, luxd is away, or
the call is a bug -- and one answer the leg reads: whether there is now an
entry to click. The three failures must never escape, because this runs as
``on_connect``, on the far side of the hub client's own blanket handler.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from punt_lux import OpError

from panel.doubles import PANEL_LOGGER, FakeRest, FakeService, panel_records

if TYPE_CHECKING:
    from collections.abc import Callable

    import pytest

    from punt_vox.panel.menu_entry import PanelMenuEntry


class TestRegistered:
    async def test_a_landed_entry_is_reported_up(
        self, build_entry: Callable[..., PanelMenuEntry]
    ) -> None:
        rest = FakeRest()
        entry = build_entry(FakeService(), lambda: rest)
        assert await entry.registered() is True
        assert rest.registered == [("vox-panel", "Vox")]

    async def test_a_refusal_is_reported_down_and_logged(
        self,
        caplog: pytest.LogCaptureFixture,
        build_entry: Callable[..., PanelMenuEntry],
    ) -> None:
        caplog.set_level(logging.DEBUG, logger=PANEL_LOGGER.name)
        rest = FakeRest(register_result=OpError(code="rejected", reason="taken"))
        entry = build_entry(FakeService(), lambda: rest)
        assert await entry.registered() is False
        assert [r.levelno for r in panel_records(caplog)] == [logging.ERROR]

    async def test_hub_unavailable_is_reported_down_as_an_outage(
        self,
        caplog: pytest.LogCaptureFixture,
        build_entry: Callable[..., PanelMenuEntry],
    ) -> None:
        # luxd can drop between the handshake completing and this call
        # resolving -- the same transient every other hub call routes through
        # the outage report, not a bug worth a traceback.
        caplog.set_level(logging.DEBUG, logger=PANEL_LOGGER.name)
        entry = build_entry(FakeService(), lambda: FakeRest(fail_at="register"))
        assert await entry.registered() is False
        assert [r.levelno for r in panel_records(caplog)] == [logging.WARNING]

    async def test_an_unexpected_error_is_reported_down_and_logged(
        self,
        caplog: pytest.LogCaptureFixture,
        build_entry: Callable[..., PanelMenuEntry],
    ) -> None:
        caplog.set_level(logging.DEBUG, logger=PANEL_LOGGER.name)
        rest = FakeRest(fail_at="register", error=RuntimeError("boom"))
        entry = build_entry(FakeService(), lambda: rest)
        assert await entry.registered() is False
        records = panel_records(caplog)
        assert [r.levelno for r in records] == [logging.ERROR]
        assert records[0].exc_info is not None
