"""``PanelState`` -- the panel's settings, read once and held between clicks.

Held by :class:`~punt_vox.panel.service.VoxPanelService` so a click's visible
answer never waits on a fresh read: the state is refreshed off the loop and
projected onto a scene only when someone is about to look at it.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Self, final

from punt_vox.panel.panel_scene import PanelScene

if TYPE_CHECKING:
    from punt_vox.client_sync import VoxClientSync
    from punt_vox.config import ConfigStore

__all__ = ["PanelState"]


@final
@dataclass(frozen=True, slots=True)
class PanelState:
    """A snapshot of the settings the panel shows: notify, speak, voice, roster."""

    notify: str
    speak: str
    voice: str | None
    roster: tuple[str, ...]

    @classmethod
    def empty(cls) -> Self:
        """Return the safe default held before the first successful read."""
        return cls(notify="n", speak="y", voice=None, roster=())

    @classmethod
    def read(cls, client: VoxClientSync, store: ConfigStore) -> Self:
        """Read the config fields fresh from disk and the voice roster from voxd."""
        cfg = store.read()
        roster = tuple(client.voices())
        return cls(notify=cfg.notify, speak=cfg.speak, voice=cfg.voice, roster=roster)

    def with_notify(self, code: str) -> Self:
        """Return a copy with ``notify`` set to ``code``."""
        return replace(self, notify=code)

    def with_speak(self, code: str) -> Self:
        """Return a copy with ``speak`` set to ``code``."""
        return replace(self, speak=code)

    def with_voice(self, voice: str) -> Self:
        """Return a copy with ``voice`` set to ``voice``."""
        return replace(self, voice=voice)

    def scene(self) -> PanelScene:
        """Return this state as the scene it projects onto."""
        return PanelScene(
            notify=self.notify, speak=self.speak, voice=self.voice, roster=self.roster
        )
