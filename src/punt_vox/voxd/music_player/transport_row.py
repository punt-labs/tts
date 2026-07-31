"""``TransportRow`` -- the prev / play-pause / next / stop control bar of the scene.

The row is a columns group of four buttons whose glyph, tooltip, ``publish`` topic,
and ``disabled`` state are all pure functions of the :class:`PlayerView` (Z ``T6``
for the play/pause glyph, ``T5`` for the prev/next bounds, and the idle-inert rule).
It renders every button ``disabled`` when nothing plays -- playback starts from the
album-list Play buttons, which stay enabled in every mode and switch -- so the
transport is inert exactly when the player is idle.

The play/pause button is one button: while ``playing`` it shows ``⏸`` and publishes
``music.pause``; while ``paused`` it shows ``⏵`` and publishes ``music.resume`` -- the
single unambiguous transition for the mode it was rendered in. The glyphs are the
operator-verified media characters on the lux ImGui/macOS font stack (``U+23F5`` for
play, never the undersized ``U+25B6``).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final, final

from punt_vox.voxd.music_player.player_view import PlayerMode, PlayerView
from punt_vox.voxd.music_player.wire import MusicTopic

__all__ = ["TransportRow"]

_PREV_GLYPH: Final = "⏮"  # ⏮
_PAUSE_GLYPH: Final = "⏸"  # ⏸ (shown while playing -- press to pause)
_PLAY_GLYPH: Final = "⏵"  # ⏵ (shown while paused -- press to resume)
_NEXT_GLYPH: Final = "⏭"  # ⏭
_STOP_GLYPH: Final = "⏹"  # ⏹

_ROW_ID: Final = "music.transport"


@final
@dataclass(frozen=True, slots=True)
class _TransportButton:
    """One transport button: a glyph label, a tooltip, a topic, and a disabled flag."""

    element_id: str
    label: str
    tooltip: str
    topic: MusicTopic
    disabled: bool

    def to_dict(self) -> dict[str, object]:
        """Return the button's wire dict (matching the lux button element shape)."""
        return {
            "kind": "button",
            "id": self.element_id,
            "label": self.label,
            "tooltip": self.tooltip,
            "disabled": self.disabled,
            "publish": {"topic": self.topic.value},
        }


@final
@dataclass(frozen=True, slots=True)
class TransportRow:
    """Project the player view onto the four-button transport control row."""

    view: PlayerView

    def to_dict(self) -> dict[str, object]:
        """Return the transport row's wire dict: a columns group of four buttons."""
        buttons = (self._prev(), self._play_pause(), self._next(), self._stop())
        return {
            "kind": "group",
            "id": _ROW_ID,
            "layout": "columns",
            "children": [button.to_dict() for button in buttons],
        }

    def _prev(self) -> _TransportButton:
        """Return the prev button, disabled when idle or already at part 1 (T5)."""
        return _TransportButton(
            element_id="music.transport.prev",
            label=_PREV_GLYPH,
            tooltip="Previous",
            topic=MusicTopic.PREV,
            disabled=self._idle or self._at_first,
        )

    def _next(self) -> _TransportButton:
        """Return the next button, disabled when idle or already at part M (T5)."""
        return _TransportButton(
            element_id="music.transport.next",
            label=_NEXT_GLYPH,
            tooltip="Next",
            topic=MusicTopic.NEXT,
            disabled=self._idle or self._at_last,
        )

    def _stop(self) -> _TransportButton:
        """Return the stop button, disabled only when nothing plays."""
        return _TransportButton(
            element_id="music.transport.stop",
            label=_STOP_GLYPH,
            tooltip="Stop",
            topic=MusicTopic.STOP,
            disabled=self._idle,
        )

    def _play_pause(self) -> _TransportButton:
        """Return the one play/pause button, its glyph and topic derived from mode (T6).

        Playing shows ``⏸`` and publishes ``music.pause``; paused shows ``⏵`` and
        publishes ``music.resume``. When idle the button is inert (disabled) and
        wears the play glyph, ready to resume nothing.
        """
        paused = self.view.mode is PlayerMode.PAUSED
        glyph = _PLAY_GLYPH if paused or self._idle else _PAUSE_GLYPH
        topic = MusicTopic.RESUME if paused or self._idle else MusicTopic.PAUSE
        tooltip = "Play" if paused or self._idle else "Pause"
        return _TransportButton(
            element_id="music.transport.playpause",
            label=glyph,
            tooltip=tooltip,
            topic=topic,
            disabled=self._idle,
        )

    @property
    def _idle(self) -> bool:
        """Return whether the transport is inert (no album active)."""
        return not self.view.mode.is_active

    @property
    def _at_first(self) -> bool:
        """Return whether the cursor sits on the first part (prev is a no-op)."""
        cursor = self.view.now_playing
        return cursor is not None and cursor.index <= 1

    @property
    def _at_last(self) -> bool:
        """Return whether the cursor sits on the last part (next is a no-op)."""
        cursor = self.view.now_playing
        return cursor is not None and cursor.index >= cursor.of
