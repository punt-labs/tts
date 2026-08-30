"""The seam between a real :class:`VoxPanelService` and a real ``PanelRunner``.

vox-h777 round 11: ``PanelRunner._applied`` collapsed
:meth:`~punt_vox.panel.service.VoxPanelService.apply_event`'s three-way
:class:`~punt_vox.panel.control_push.ControlPush` answer back down to a bare
truthy check (``ControlPush.REFRESH if changed else ControlPush.NONE``).
Because ``ControlPush`` is a plain ``Enum`` with no ``__bool__``, every member
-- ``NONE`` and ``CORRECT`` included -- is truthy, so the ternary always
answered ``REFRESH`` regardless of what ``apply_event`` actually returned.

No existing test caught it because none crossed the seam: ``test_panel_runner``
drives ``PanelRunner`` against ``FakeService``, whose own ``apply_event`` (a
bare ``bool``) made the buggy ternary look correct; ``test_service`` drives
``VoxPanelService`` directly and correctly sees ``ControlPush.CORRECT``, but
never through a ``PanelRunner``. This module wires the two real classes
together and drives a genuine roster-fetch failure through ``changed()``, the
same path a panel control's own event handler would.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, cast, final

from punt_lux.operations import FrameRaise, Ok

from panel.doubles import PANEL_LOGGER
from punt_vox.client_errors import VoxdConnectionError
from punt_vox.config import VoxConfig
from punt_vox.panel.panel_guard import PanelGuard
from punt_vox.panel.panel_runner import PanelRunner
from punt_vox.panel.service import VoxPanelService
from punt_vox.panel.topics import PanelTopic
from punt_vox.server_switches import PROVIDER_NAMES

if TYPE_CHECKING:
    from punt_lux import LuxClient, RenderRequest, SceneShown
    from punt_lux.hub_client import CallbackHandler, ConnectHandler, EventHandler
    from punt_lux.operations import OpError, UpdateRequest

    from punt_vox.client import SynthesizeResult
    from punt_vox.panel.ports import HubListener
    from punt_vox.types_synthesis import SynthesisSpec


def _config(voice: str | None, provider: str | None) -> VoxConfig:
    return VoxConfig(
        notify="y",
        speak="y",
        vibe_mode="auto",
        voice=voice,
        provider=provider,
        model=None,
        vibe=None,
        vibe_tags=None,
    )


@final
class _Store:
    """A ``SettingsStore`` double: records every field a commit writes."""

    def __init__(self, cfg: VoxConfig) -> None:
        self._cfg = cfg
        self.written: dict[str, str] = {}

    def read(self) -> VoxConfig:
        return self._cfg

    def write_field(self, key: str, value: str) -> None:
        self.written[key] = value

    def write_fields(self, updates: dict[str, str]) -> None:
        self.written.update(updates)


@final
class _RosterFailsAfterPrefetch:
    """A ``PanelDaemonClient`` double: one clean roster read, then voxd drops.

    The prefetch needs a working roster so the panel starts with real state
    to snap back to; the provider commit's own roster fetch is the one that
    fails -- what a voxd that answers once and then drops looks like from a
    click mid-flight.
    """

    def __init__(self) -> None:
        self.call_count = 0

    def voices(self, provider: str | None = None) -> list[str]:
        self.call_count += 1
        if self.call_count > 1:
            msg = "voxd unreachable"
            raise VoxdConnectionError(msg)
        return ["benno", "aria"]

    def synthesize(
        self, text: str, spec: SynthesisSpec | None = None, *, once: int | None = None
    ) -> SynthesizeResult:
        raise NotImplementedError


@final
class _SceneAccessor:
    """Records a full install (``show``) apart from a diff patch (``update``).

    They are recorded apart because a diff-based re-push cannot express the
    correction this test exists to prove: a ``show`` unconditionally
    reasserts every field, a patch only touches what moved against the last
    successfully pushed tree.
    """

    def __init__(self, outer: _Rest) -> None:
        self._outer = outer

    async def show(self, request: RenderRequest) -> SceneShown | OpError:
        self._outer.rendered.append(request)
        return cast("SceneShown", Ok())

    async def update(
        self, scene_id: str, request: UpdateRequest | OpError
    ) -> SceneShown | OpError:
        self._outer.patched.append(request)
        return cast("SceneShown", Ok())


@final
class _CallbackAccessor:
    async def register(self, callback_id: str, label: str) -> Ok | OpError:
        raise NotImplementedError


@final
class _FrameAccessor:
    async def raise_(self, frame_id: str) -> FrameRaise | OpError:
        return FrameRaise(frame_id=frame_id, raised=True)


@final
class _Rest:
    """A ``PanelRestClient`` double recording installs apart from patches."""

    def __init__(self) -> None:
        self.rendered: list[RenderRequest] = []
        self.patched: list[UpdateRequest | OpError] = []
        self.scene = _SceneAccessor(self)
        self.callback = _CallbackAccessor()
        self.frame = _FrameAccessor()

    def listener(
        self,
        *,
        on_callback: CallbackHandler,
        on_event: EventHandler,
        on_connect: ConnectHandler | None = None,
    ) -> HubListener:
        raise NotImplementedError


def _selected(request: RenderRequest, element_id: str) -> object:
    """Return ``element_id``'s ``selected`` field from a full render, or raise."""
    for element in request.elements:
        if element.get("id") == element_id:
            return element["selected"]
    msg = f"no {element_id!r} element in the render"
    raise AssertionError(msg)


class TestRunnerServiceSeam:
    """A real ``VoxPanelService`` driven through a real ``PanelRunner``."""

    async def test_a_roster_fetch_failure_during_a_provider_commit_corrects(
        self,
    ) -> None:
        client = _RosterFailsAfterPrefetch()
        store = _Store(_config(voice="benno", provider="elevenlabs"))
        service = VoxPanelService(client, store)
        rest = _Rest()

        def rest_factory() -> LuxClient:
            return cast("LuxClient", rest)

        guard = PanelGuard(service, rest_factory, PANEL_LOGGER)
        runner = PanelRunner(service, rest_factory, guard, PANEL_LOGGER)

        # Establish the panel's first render -- the "last successfully pushed"
        # baseline a diff-based re-push would compare its next plan against.
        await runner.warmed()
        await service.push_scene(rest_factory())
        assert len(rest.rendered) == 1
        elevenlabs_index = PROVIDER_NAMES.index("elevenlabs")
        assert _selected(rest.rendered[0], "vox.panel.provider") == elevenlabs_index

        # The widget already shows "espeak" the instant the click fires; the
        # roster fetch behind this commit then fails, so ``_state`` never
        # moves off "elevenlabs" -- the round-10 regression this seam test
        # guards: a diff-based re-push always looked like the right answer
        # against a fake that couldn't tell REFRESH from CORRECT.
        espeak_index = PROVIDER_NAMES.index("espeak")
        await runner.changed(PanelTopic.PROVIDER.value, {"value": espeak_index})

        # A full reinstall, not a diff patch: the corrective render must
        # reassert the control's true (unchanged) field over the widget's
        # wrong guess, which a patch -- finding nothing changed to diff --
        # would skip entirely.
        assert len(rest.rendered) == 2
        assert rest.patched == []
        assert _selected(rest.rendered[1], "vox.panel.provider") == elevenlabs_index
        assert store.written == {}
