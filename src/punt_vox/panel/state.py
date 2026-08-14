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

        The roster is fetched for the current session provider -- state is
        the sole authority on which provider voxd runs, so an unset
        ``cfg.provider`` returns an empty roster rather than borrowing a
        different provider's voices. The panel does not display an
        elevenlabs roster while the session is configured to speak through
        espeak, and it does not display any roster at all until the caller
        has chosen a provider.
        """
        cfg = store.read()
        roster = tuple(client.voices(cfg.provider)) if cfg.provider else ()
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
        self,
        provider: str,
        *,
        roster: tuple[str, ...],
        model: str | None,
        voice: str | None,
    ) -> Self:
        """Return a copy with ``provider`` + cascaded ``model``/``voice``/``roster``.

        The cascade is computed by the caller (``VoxPanelService`` in
        production) so this method is a pure setter -- it stores what it
        is handed. A re-publish of the same provider is still a no-op.

        Model names and voice names are provider-scoped, so the caller
        must supply defaults valid for *provider* (typically the first
        model from ``MODEL_TABLE.available(provider)`` and the first
        voice from ``roster``). ``model=None`` means "no model on this
        provider" (Polly/say/espeak); ``voice=None`` means "roster was
        empty" (an edge the caller normally guards against).
        """
        if provider == self.provider:
            return self
        return replace(self, provider=provider, model=model, roster=roster, voice=voice)

    def with_model(self, model: str, *, voice: str | None) -> Self:
        """Return a copy with ``model`` set + cascaded ``voice`` from the caller.

        The cascade is computed by the caller (``VoxPanelService``) so
        this method is a pure setter. ``voice=None`` means the roster
        the caller fetched was empty.
        """
        return replace(self, model=model, voice=voice)

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
