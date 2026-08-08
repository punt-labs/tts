"""Builders for the subjects the panel tests drive: the leg, runner, and entry.

Exposed as fixtures (not module imports) so mypy names this ``conftest`` once --
the repo's tests reach shared builders through fixtures. The doubles they are
wired to are ordinary types, and live in ``doubles``.

Both builders hold the one cast the doubles need: the leg and the runner are
typed against the concrete :class:`~punt_vox.panel.service.VoxPanelService`,
so the structural stand-in is asserted here rather than at every call site.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

import pytest

from panel.doubles import PANEL_LOGGER
from punt_vox.panel.leg import VoxPanelLeg
from punt_vox.panel.menu_entry import PanelMenuEntry
from punt_vox.panel.panel_guard import PanelGuard
from punt_vox.panel.panel_runner import PanelRunner

if TYPE_CHECKING:
    from collections.abc import Callable

    from punt_lux.domain.hub.client_identity import ClientIdentity

    from panel.doubles import FakeService
    from punt_vox.panel.ports import PanelRestClient
    from punt_vox.panel.service import VoxPanelService

# The leg is built with an identity it only ever hands to the REST factory,
# and every test supplies its own factory instead.
_IDENTITY = cast("ClientIdentity", object())


@pytest.fixture
def build_leg() -> Callable[..., VoxPanelLeg]:
    """Return a factory for a leg serving *service* over *rest_factory*."""

    def _build(
        service: FakeService,
        rest_factory: Callable[[], PanelRestClient],
        *,
        topics: tuple[str, ...] = (),
    ) -> VoxPanelLeg:
        return VoxPanelLeg(
            _IDENTITY,
            cast("VoxPanelService", service),
            topics=topics,
            rest_factory=rest_factory,
        )

    return _build


@pytest.fixture
def build_runner() -> Callable[..., PanelRunner]:
    """Return a factory for a runner and the guard it answers failures with.

    The real guard, not a double: the leg composes exactly this pair, and the
    guard's own behaviour is covered in ``test_panel_guard``.
    """

    def _build(
        service: FakeService, rest_factory: Callable[[], PanelRestClient]
    ) -> PanelRunner:
        held = cast("VoxPanelService", service)
        guard = PanelGuard(held, rest_factory, PANEL_LOGGER)
        return PanelRunner(held, rest_factory, guard, PANEL_LOGGER)

    return _build


@pytest.fixture
def build_entry() -> Callable[..., PanelMenuEntry]:
    """Return a factory for a menu entry and the guard it answers failures with."""

    def _build(
        service: FakeService, rest_factory: Callable[[], PanelRestClient]
    ) -> PanelMenuEntry:
        held = cast("VoxPanelService", service)
        guard = PanelGuard(held, rest_factory, PANEL_LOGGER)
        return PanelMenuEntry(held, rest_factory, guard, PANEL_LOGGER)

    return _build
