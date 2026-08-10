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
    from punt_vox.panel.ports import SettingsSource, VoiceRoster

__all__ = ["PanelState"]


@final
@dataclass(frozen=True, slots=True)
class PanelState:
    """The settings snapshot the panel shows: notify, speak, voice engine, roster."""

    notify: str
    speak: str
    voice: str | None
    roster: tuple[str, ...]
    provider: str | None = None
    model: str | None = None

    @classmethod
    def empty(cls) -> Self:
        """Return the safe default held before the first successful read."""
        return cls(notify="n", speak="y", voice=None, roster=())

    @classmethod
    def read(cls, client: VoiceRoster, store: SettingsSource) -> Self:
        """Read the config fields fresh from disk and the voice roster from voxd.

        The roster is fetched for the current session provider (or the daemon's
        default when unset) so a mid-session switch is seen after the next
        resync -- the panel does not display an elevenlabs roster while the
        session is configured to speak through espeak.
        """
        cfg = store.read()
        roster = tuple(client.voices(cfg.provider))
        return cls(
            notify=cfg.notify,
            speak=cfg.speak,
            voice=cfg.voice,
            roster=roster,
            provider=cfg.provider,
            model=cfg.model,
        )

    def with_notify(self, code: str) -> Self:
        """Return a copy with ``notify`` set to ``code``."""
        return replace(self, notify=code)

    def with_speak(self, code: str) -> Self:
        """Return a copy with ``speak`` set to ``code``."""
        return replace(self, speak=code)

    def with_voice(self, voice: str) -> Self:
        """Return a copy with ``voice`` set to ``voice``."""
        return replace(self, voice=voice)

    def with_provider(
        self, provider: str, roster: tuple[str, ...] | None = None
    ) -> Self:
        """Return a copy with ``provider`` set + stale model/voice cleared.

        Both model names and voice names are provider-scoped -- an
        ``eleven_v3`` model or a ``benno`` voice left in state after a
        switch to OpenAI/espeak would drive an invalid request the next
        time synthesis fires. On a genuine change, this method (a) swaps
        the roster if one was supplied, (b) clears the stored model, and
        (c) clears the stored voice when it is not in the new roster.

        A re-publish of the same provider is a no-op -- an echoed event
        neither swaps the roster nor drops the current model/voice.

        When ``roster`` is ``None`` the caller has not fetched the new
        roster yet (e.g. a pre-refresh unit test); the previous roster
        is kept and the voice is not re-validated against a stale list.
        """
        if provider == self.provider:
            return self
        next_roster = self.roster if roster is None else roster
        next_voice = (
            self.voice
            if roster is None or self.voice is None or self.voice in next_roster
            else None
        )
        return replace(
            self, provider=provider, model=None, roster=next_roster, voice=next_voice
        )

    def with_model(self, model: str) -> Self:
        """Return a copy with ``model`` set to ``model``."""
        return replace(self, model=model)

    def scene(self) -> PanelScene:
        """Return this state as the scene it projects onto."""
        return PanelScene(
            notify=self.notify,
            speak=self.speak,
            voice=self.voice,
            roster=self.roster,
            provider=self.provider,
            model=self.model,
        )
