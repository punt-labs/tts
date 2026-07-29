"""``PublishButton`` -- a lux button whose click publishes a music pub-sub event.

The pinned ``ButtonElement`` carries no publish attribute yet, so voxd builds the
wire dict itself: a ``play-<id>`` or ``stop`` button whose ``publish`` names the topic
(and, for a play, the album-id payload) an in-scene click sends back over pub-sub. The
dict :meth:`to_dict` emits is exactly what :class:`LuxSubscription` decodes on the
other leg, so the two ends of the contract share one vocabulary (:mod:`wire`).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Self, final

from punt_vox.voxd.music_player.wire import ALBUM_ID_KEY, MusicTopic

__all__ = ["PublishButton"]


@final
@dataclass(frozen=True, slots=True)
class PublishButton:
    """A button that publishes ``topic`` (with an optional album payload) on click."""

    element_id: str
    label: str
    topic: MusicTopic
    # A play button carries the target album id in its payload; a stop button
    # publishes a bare topic, so absence is this field's documented state.
    album_id: str | None = None

    @classmethod
    def play(cls, album_id: str) -> Self:
        """Return the Play button for ``album_id`` (publishes ``music.play``)."""
        return cls(
            element_id=f"play-{album_id}",
            label="Play",
            topic=MusicTopic.PLAY,
            album_id=album_id,
        )

    @classmethod
    def stop(cls) -> Self:
        """Return the Stop button (publishes a bare ``music.stop``)."""
        return cls(element_id="stop", label="Stop", topic=MusicTopic.STOP)

    def to_dict(self) -> dict[str, object]:
        """Return the button's wire dict, carrying its ``publish`` attribute."""
        publish: dict[str, object] = {"topic": self.topic.value}
        if self.album_id is not None:
            publish["payload"] = {ALBUM_ID_KEY: self.album_id}
        return {
            "kind": "button",
            "id": self.element_id,
            "label": self.label,
            "publish": publish,
        }
