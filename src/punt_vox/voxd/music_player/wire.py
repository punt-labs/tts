"""The music player's pub-sub wire vocabulary: the two topics and the play key.

Both legs of the receive path share these names -- the :class:`AlbumListScene`
Play/Stop buttons *publish* them, and :class:`LuxSubscription` *subscribes* to and
decodes them -- so they live in one module neither leg owns, and a rename can never
drift the publisher out of step with the subscriber.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Final

__all__ = ["ALBUM_ID_KEY", "MusicTopic"]


class MusicTopic(StrEnum):
    """The two lux pub-sub topics the scene publishes and voxd subscribes to."""

    PLAY = "music.play"
    STOP = "music.stop"


ALBUM_ID_KEY: Final = "album_id"
"""The key carrying the target album id in a ``music.play`` event payload."""
