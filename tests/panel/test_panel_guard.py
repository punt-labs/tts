"""Tests for :mod:`punt_vox.panel.panel_guard`."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, cast, final

import pytest
from punt_lux import HubUnavailableError

from punt_vox.client_errors import VoxdConnectionError, VoxdProtocolError
from punt_vox.panel.panel_guard import PanelGuard

if TYPE_CHECKING:
    from punt_vox.panel.ports import PanelRestClient
    from punt_vox.panel.service import VoxPanelService

_REFUSAL = "unknown voice 'nope'"
_REST = cast("PanelRestClient", object())


def _raise(exc: Exception) -> None:
    """Raise *exc* from inside a guarded block.

    Through a call rather than a bare ``raise`` in the ``with`` body: a
    ``@contextmanager`` is typed as never suppressing, so raising inline makes
    every assertion after the block look unreachable to the type checker.
    """
    raise exc


@final
class _FakeService:
    """A ``VoxPanelService`` double recording the two calls a guard makes."""

    def __init__(self) -> None:
        self.pushed = 0
        self.rejections: list[str] = []

    def push_scene(self, client: object) -> None:
        self.pushed += 1

    def note_rejection(self, detail: str) -> None:
        self.rejections.append(detail)


def _guard_for(service: _FakeService) -> PanelGuard:
    return PanelGuard(
        cast("VoxPanelService", service),
        lambda: _REST,
        logging.getLogger("punt_vox.panel.leg"),
    )


class TestOutage:
    def test_luxd_being_away_is_swallowed_and_noted(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        caplog.set_level(logging.DEBUG, logger="punt_vox.panel.leg")
        guard = _guard_for(_FakeService())
        with guard.outage("luxd is down"):
            _raise(HubUnavailableError("down"))
        assert [r.levelno for r in caplog.records] == [logging.WARNING]

    def test_any_other_failure_is_left_to_the_caller(self) -> None:
        # The guard answers one failure. A bug wearing another exception must
        # reach the caller's own handler, not vanish into an outage report.
        guard = _guard_for(_FakeService())
        with pytest.raises(RuntimeError), guard.outage("luxd is down"):
            _raise(RuntimeError("boom"))

    def test_a_connect_closes_the_outage_so_the_next_one_reopens(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        caplog.set_level(logging.DEBUG, logger="punt_vox.panel.leg")
        guard = _guard_for(_FakeService())
        with guard.outage("luxd is down"):
            _raise(HubUnavailableError("down"))
        guard.connected()
        caplog.clear()
        with guard.outage("luxd is down"):
            _raise(HubUnavailableError("down"))
        assert [r.levelno for r in caplog.records] == [logging.WARNING]


class TestRejection:
    def test_a_refusal_is_noted_logged_and_pushed_back_into_the_scene(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        caplog.set_level(logging.DEBUG, logger="punt_vox.panel.leg")
        service = _FakeService()
        with _guard_for(service).rejection("a settings read"):
            _raise(VoxdProtocolError(_REFUSAL))
        assert service.rejections == [_REFUSAL]
        assert service.pushed == 1
        assert [r.levelno for r in caplog.records] == [logging.ERROR]
        assert caplog.records[0].exc_info is not None

    def test_voxd_merely_being_unreachable_is_not_a_refusal(self) -> None:
        # An unreachable voxd is a transient the callers already answer with a
        # staleness notice; only a refusal -- voxd reached and saying no --
        # belongs to this guard.
        service = _FakeService()
        with (
            pytest.raises(VoxdConnectionError),
            _guard_for(service).rejection("a settings read"),
        ):
            _raise(VoxdConnectionError("no socket"))
        assert service.rejections == []


class TestOffscreenRejection:
    def test_a_refusal_is_noted_without_opening_a_panel_nobody_asked_for(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        caplog.set_level(logging.DEBUG, logger="punt_vox.panel.leg")
        service = _FakeService()
        with _guard_for(service).offscreen_rejection("the read on connect"):
            _raise(VoxdProtocolError(_REFUSAL))
        assert service.rejections == [_REFUSAL]
        assert service.pushed == 0
        assert [r.levelno for r in caplog.records] == [logging.ERROR]


class TestRepush:
    def test_a_push_reaches_the_service(self) -> None:
        service = _FakeService()
        _guard_for(service).repush()
        assert service.pushed == 1

    def test_luxd_going_away_mid_push_is_swallowed(self) -> None:
        service = _FakeService()

        def _factory() -> PanelRestClient:
            raise HubUnavailableError("down")

        guard = PanelGuard(
            cast("VoxPanelService", service),
            _factory,
            logging.getLogger("punt_vox.panel.leg"),
        )
        guard.repush()  # must not raise
        assert service.pushed == 0
