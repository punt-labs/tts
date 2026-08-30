"""``CountStore`` -- the counts themselves, and the rule for replacing them.

Split out of :class:`~punt_vox.voxd.music_player.track_count_cache.
TrackCountCache`, which was doing two jobs: deciding *when* to read disk (a
dispatch, a lock, a deadline, an announcement) and deciding *whether* a read
that comes back is still worth keeping. This is the second job. It holds the
counts and the one rule that guards them; it knows nothing about threads
pools, event loops or timeouts.

That rule is generation ordering. A refresh whose caller stopped waiting for
it keeps running on its worker thread and can try to commit long after a
newer, faster refresh has already landed a better answer -- so a write is
accepted only when its generation is newer than the last write that actually
*landed*, tracked here as :attr:`_written`. Not the newest generation anyone
has ever *attempted*: those are different, and gating on attempts would be
stricter and wrong. A dispatch legitimately commits while a newer generation
is still in flight elsewhere, because "in flight" is not "landed" -- gating
on attempts would throw that write away whenever the newer attempt never
finished at all (its own timeout, say), wedging the store against a write
that would have succeeded.

The lock is a ``threading.Lock``, not an ``asyncio`` one, because the callers
that matter are worker threads rather than coroutines.
"""

from __future__ import annotations

import threading
from typing import TYPE_CHECKING, Self, final

if TYPE_CHECKING:
    from punt_vox.voxd.programs.album_id import AlbumId

__all__ = ["CountStore"]


@final
class CountStore:
    """Ready-track counts per album, replaced only by a newer generation."""

    _counts: dict[AlbumId, int]
    _lock: threading.Lock
    # The generation of the last write that actually landed -- never the
    # newest one attempted. See the module docstring for why that matters.
    _written: int
    __slots__ = ("_counts", "_lock", "_written")

    def __new__(cls) -> Self:
        self = super().__new__(cls)
        self._counts = {}
        self._lock = threading.Lock()
        self._written = 0
        return self

    def get(self, album_id: AlbumId) -> int:
        """Return the last-known count for ``album_id``, or zero.

        An album this store has never seen reads as zero, matching a
        genuinely fresh album before its first Part lands, so an unrefreshed
        row looks identical to a real empty one rather than carrying some
        other placeholder (PY-TS-14: the *absence* of a count and a
        *genuine* zero are meant to render the same way; there is no third
        state a caller could distinguish them by, nor would one be useful
        here).
        """
        return self._counts.get(album_id, 0)

    def commit(self, counts: dict[AlbumId, int], generation: int) -> bool:
        """Replace the counts if ``generation`` beats the last landed write.

        Answers whether the write was taken, so a caller that needs to tell
        the world its counts arrived can tell an accepted write from one
        that lost to a fresher answer and should stay silent.
        """
        with self._lock:
            if generation <= self._written:
                return False
            self._counts = counts
            self._written = generation
            return True
