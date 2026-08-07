"""``VoxPanelService`` -- the ``Vox`` menu entry a session owns.

Reads vox's current settings, applies a control change to the same config
store and daemon RPCs the CLI and MCP tool already use, and pushes the
confirmed scene. Satisfies :class:`punt_lux.applets.AppletService`
structurally (``callback_id``, ``label``, ``prefetch``, ``acknowledge``,
``service``) plus the extra methods :class:`~punt_vox.panel.leg.VoxPanelLeg`
calls when a subscribed control-change event arrives.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Self, final

from punt_lux import OpError

from punt_vox.panel.radio_control import MIC_MODE_SPEC, NOTIFY_SPEC
from punt_vox.panel.state import PanelState
from punt_vox.panel.topics import PanelTopic
from punt_vox.panel.voice_control import VoiceControl
from punt_vox.types_synthesis import SynthesisSpec

if TYPE_CHECKING:
    from collections.abc import Mapping

    from punt_lux.applets import ClickLatency

    from punt_vox.panel.panel_scene import PanelScene
    from punt_vox.panel.ports import PanelDaemonClient, PanelRestClient, SettingsStore

logger = logging.getLogger(__name__)

__all__ = ["VoxPanelService"]

_CALLBACK_ID = "vox-panel"
_LABEL = "Vox"
_PREVIEW_TEXT = "This is my voice."


@final
class VoxPanelService:
    """A session's Vox settings entry: read settings, apply a change, show the scene."""

    _client: PanelDaemonClient
    _store: SettingsStore
    _state: PanelState
    __slots__ = ("_client", "_state", "_store")

    def __new__(cls, client: PanelDaemonClient, store: SettingsStore) -> Self:
        self = super().__new__(cls)
        self._client = client
        self._store = store
        self._state = PanelState.empty()
        return self

    @property
    def callback_id(self) -> str:
        """The id the ``Vox`` menu entry's clicks carry back to this session."""
        return _CALLBACK_ID

    @property
    def label(self) -> str:
        """The entry the display shows under this session's submenu."""
        return _LABEL

    def prefetch(self) -> None:
        """Read settings once before any click, so the first click has some to show."""
        self._refresh()

    def acknowledge(self, client: PanelRestClient, latency: ClickLatency) -> None:
        """Push the held scene now -- the visible half of the click."""
        with latency.answering():
            self.push_scene(client)

    def service(self, client: PanelRestClient, latency: ClickLatency) -> None:
        """Re-read settings fresh and push the confirmed scene."""
        with latency.stage("refreshed"):
            self._refresh()
        self.push_scene(client)

    def scene(self) -> PanelScene:
        """Return the currently-held settings as a scene."""
        return self._state.scene()

    def push_scene(self, client: PanelRestClient) -> None:
        """Push the currently-held scene, logging (never raising) a refusal."""
        result = client.render(self._state.scene().render_request())
        if isinstance(result, OpError):
            logger.error("vox-panel: luxd rejected the scene: %s", result.reason)

    def apply_event(self, topic: str, payload: Mapping[str, object]) -> bool:
        """Apply one control-topic event; return whether the scene needs a re-push."""
        if topic == PanelTopic.NOTIFY:
            code = NOTIFY_SPEC.code_for_index(self._index(payload))
            self._store.write_field("notify", code)
            self._state = self._state.with_notify(code)
        elif topic == PanelTopic.MIC_MODE:
            code = MIC_MODE_SPEC.code_for_index(self._index(payload))
            self._store.write_field("speak", code)
            self._state = self._state.with_speak(code)
        elif topic == PanelTopic.VOICE:
            voice = self._voice_control().voice_for_index(self._index(payload))
            self._store.write_field("voice", voice)
            self._state = self._state.with_voice(voice)
        elif topic == PanelTopic.VOICE_PREVIEW:
            self._preview()
            return False
        else:
            logger.warning("vox-panel: no handler for topic %r", topic)
            return False
        return True

    def _voice_control(self) -> VoiceControl:
        return VoiceControl(roster=self._state.roster, current=self._state.voice)

    def _preview(self) -> None:
        """Play the held voice back, without touching any config field."""
        voice = self._state.voice
        if voice is None:
            logger.info("vox-panel: no voice selected yet; preview skipped")
            return
        try:
            self._client.synthesize(_PREVIEW_TEXT, SynthesisSpec(voice=voice))
        except Exception:
            logger.exception("vox-panel: voice preview failed")

    def _refresh(self) -> None:
        """Re-read settings from disk and voxd, keeping the held state on failure."""
        try:
            self._state = PanelState.read(self._client, self._store)
        except Exception:
            logger.exception(
                "vox-panel: could not read fresh settings; keeping the held ones"
            )

    @staticmethod
    def _index(payload: Mapping[str, object]) -> int:
        """Return the payload's int ``value``, or raise if it is missing/wrong-typed."""
        value = payload.get("value")
        if not isinstance(value, int) or isinstance(value, bool):
            msg = f"expected an int 'value' in the control payload, got {value!r}"
            raise TypeError(msg)
        return value
