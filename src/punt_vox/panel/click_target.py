"""``ClickTarget`` -- what a radio click's index names, per settings snapshot.

Every radio the panel renders sends back an index, never a name: the display
knows the position the user picked and nothing about what sits there. Turning
that index into a voice, a provider, or a model needs the same settings the
scene was rendered from, because the option lists are derived from them --
the roster for voices, the current provider for models.

The settings must be one immutable snapshot, not read field by field: the
holder mutates them from several threads, so a roster read for the item list
and a provider read for the model list could otherwise come from two different
moments and resolve an index against a state that never existed.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Self, final

from punt_vox.models import MODEL_TABLE
from punt_vox.panel.model_control import ModelControl
from punt_vox.panel.provider_control import ProviderControl
from punt_vox.panel.voice_control import VoiceControl
from punt_vox.server_switches import PROVIDER_NAMES

if TYPE_CHECKING:
    from punt_vox.panel.state import PanelState

__all__ = ["ClickTarget"]


@final
class ClickTarget:
    """One settings snapshot, answering what each control's index names."""

    _state: PanelState
    __slots__ = ("_state",)

    def __new__(cls, state: PanelState) -> Self:
        self = super().__new__(cls)
        self._state = state
        return self

    def voice(self, index: int) -> str:
        """Return the voice at *index* in the snapshot's roster."""
        control = VoiceControl(roster=self._state.roster, current=self._state.voice)
        return control.voice_for_index(index)

    def provider(self, index: int) -> str:
        """Return the provider at *index* in the closed provider list."""
        control = ProviderControl(
            providers=PROVIDER_NAMES, current=self._state.provider
        )
        return control.provider_for_index(index)

    def model(self, index: int) -> str:
        """Return the model at *index* in the current provider's model list.

        A snapshot with no provider offers no models, and so does a
        modelless one -- the same empty list
        :meth:`~punt_vox.panel.panel_scene.PanelScene.render_request` draws
        the inert ``(no models)`` sentinel from. Both surfaces ask
        ``MODEL_TABLE`` the same question about the same optional provider,
        so a state that publishes no clickable model can never resolve one.
        """
        control = ModelControl(
            models=MODEL_TABLE.available(self._state.provider),
            current=self._state.model,
        )
        return control.model_for_index(index)
