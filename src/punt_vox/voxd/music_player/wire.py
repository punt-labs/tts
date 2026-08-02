"""The music player's pub-sub wire vocabulary: the topics and the anchor key.

Shared by both legs -- :class:`AlbumTable` publishes and :class:`LuxSubscription`
subscribes -- so a rename can never drift one out of step with the other.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Final

__all__ = ["ANCHOR_KEY", "MusicTopic"]


class MusicTopic(StrEnum):
    """The lux pub-sub topics the scene publishes and voxd subscribes to.

    ``PLAY`` is the album table's row-selection publish -- one topic for every
    album, its target carried in the payload's ``anchor`` rather than the topic.
    The rest are the transport row. The receive leg subscribes to each once.
    """

    PLAY = "music.play"
    STOP = "music.stop"
    PREV = "music.prev"
    PAUSE = "music.pause"
    RESUME = "music.resume"
    NEXT = "music.next"


ANCHOR_KEY: Final = "anchor"
"""The ``music.play`` payload key holding the clicked row's ``key_column`` cell."""
