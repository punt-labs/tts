"""``TrackCountCache`` -- the one place left that touches disk for a track count.

Each album's ready-Part count is a *disk read* (a stat, an open, a read, and a
JSON parse of its manifest -- see :meth:`~punt_vox.voxd.programs.catalog.Album.
ready_parts`), because the background fill grows the on-disk manifest long after
an album is minted. :class:`~punt_vox.voxd.music_player.player.MusicPlayer`
used to re-run that read, per album, inline inside every projection -- on the
control-channel single-writer for a state-change repaint, and on the lux
listener's event loop for a menu click or a hub handshake. Both are places a
blocking disk read must never run: the writer serializes every playback
mutation behind it, and the listener holds the session's lease keepalive.

This cache is the fix: :meth:`_refresh` is the one place that still does the
disk read. Every caller reaches it through :meth:`serialized_refresh`, which
dispatches it off the hot path via ``asyncio.to_thread`` AND serializes
overlapping callers behind a lock -- :meth:`~punt_vox.voxd.music_player.player.
MusicPlayer.install` (the lux listener's event loop) and
:meth:`~punt_vox.voxd.music_player.player.MusicPlayer._refresh_track_counts`
(the control-channel writer's event loop) can both reach this cache at once,
and without the lock each would independently read its own ``fresh`` dict and
unconditionally overwrite :attr:`_counts` -- whichever finishes LAST wins,
not whichever started most recently. Every render reads :meth:`get` instead --
an in-memory dict lookup, never disk.

:meth:`serialized_refresh` is bounded by :data:`_REFRESH_TIMEOUT_SECONDS`: a
genuinely stuck disk read (a hung mount, a sleeping disk) must never block its
caller forever. :meth:`install` runs inline in the lux listener's event loop,
so an unbounded wait there would freeze the whole connection's event
delivery -- every future menu click and hub handshake -- with no error or log
to explain why, and would also leave :class:`~punt_vox.voxd.music_player.
single_flight.SingleFlightRefresh` permanently wedged, since its guard only
clears when the scheduled work returns. Timing out closes both: the ``asyncio.
Lock`` below is released the moment ``asyncio.wait_for`` gives up (the
cancellation propagates through the ``async with`` block), so the next caller
is never blocked by one that timed out.

A timeout does not stop the underlying OS thread -- ``asyncio.to_thread``
dispatches to a thread pool, and cancelling the awaiting coroutine does not
cancel the thread itself, which keeps running :meth:`_refresh` to completion
(or forever) in the background. That orphaned thread can still finish and try
to write :attr:`_counts` after a newer, faster refresh has already landed a
better answer -- reintroducing the exact "last to FINISH wins" race the lock
exists to close, just narrowed to the timeout window. :attr:`_generation` and
:attr:`_written_generation` close that window: each :meth:`serialized_refresh`
call is stamped with a generation number, bumped only on the event loop before
dispatch (so it needs no lock of its own), and threaded explicitly into
:meth:`_refresh` as a plain parameter, captured at dispatch time -- not read
back off :attr:`_generation` when the worker thread finally runs, because two
overlapping dispatches share that one counter and a slow-to-start worker
could otherwise read a LATER call's bump instead of its own. :meth:`_refresh`
-- which may run on an orphaned thread well after its caller gave up --
commits its write under :attr:`_write_lock` only when the generation it was
handed is still newer than the last write that actually landed in
:attr:`_counts` -- not newer than every generation any caller has ever
attempted. Those are different: a dispatch can and does legitimately commit
even while a newer generation is still in flight elsewhere, because "in
flight" is not "landed." Gating on "newest attempted" instead would be
stricter and wrong -- it would drop a perfectly valid write whenever some
later-dispatched attempt never completes (its own timeout, say), wedging the
cache against a write that would otherwise have succeeded. A late write from
an abandoned refresh is thus a safe no-op only once a newer one has actually
landed, never merely attempted.

An album this cache has never seen reads as zero, matching a genuinely fresh
album before its first Part lands, so an unrefreshed row looks identical to a
real empty one rather than some other placeholder (PY-TS-14: the *absence* of a
cached count and a *genuine* zero count are meant to render the same way; there
is no third state a caller could distinguish them by, nor would one be useful
here). An album the store no longer holds is simply dropped from a refresh
(:meth:`_refresh` catches its ``LookupError`` and excludes it) -- the catalog
itself, not this cache, is what stops a deleted album from being rendered at
all, since deletion removes it from the catalog synchronously in the same call
that removes it from disk (:meth:`~punt_vox.voxd.programs.library.Library.
remove`), well before any render sees it again.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from typing import TYPE_CHECKING, Self, final

from punt_vox.voxd.music_player.album_display import AlbumDisplay

if TYPE_CHECKING:
    from punt_vox.voxd.programs.album_id import AlbumId
    from punt_vox.voxd.programs.catalog import Album

__all__ = ["TrackCountCache"]

logger = logging.getLogger(__name__)

# A disk read across a real catalog should never legitimately take this long;
# past this, the caller treats the worker thread as stuck rather than block on
# it indefinitely.
_REFRESH_TIMEOUT_SECONDS: float = 5.0


@final
class TrackCountCache:
    """The last-known ready-track count per album, refreshed off the hot path.

    ``@final``, with a leading underscore on every method but :meth:`get` and
    :meth:`serialized_refresh`, to signal internal-use-only -- Python enforces
    neither: there is no true private method, and a caller holding this
    instance can still call :meth:`_refresh` directly, generation argument and
    all, if it chooses to ignore the convention. That is not a gap this class
    tries to close structurally -- a caller determined to corrupt
    :attr:`_written_generation` could just as easily set :attr:`_generation`
    directly, whether or not :meth:`_refresh` takes a parameter. The
    generation is threaded through :meth:`_refresh` as a plain parameter, not
    read back off :attr:`_generation` at execution time, because that is what
    correctness actually requires here: it must be the value captured at
    THIS call's dispatch, not whatever :attr:`_generation` happens to hold by
    the time this call's worker thread gets around to running (see module
    docstring for the concurrent interleaving this closes). The convention
    (leading underscore, ``@final``) is what keeps :meth:`_refresh`
    internal-only in practice; it was never a real enforcement boundary.
    """

    __slots__ = (
        "_counts",
        "_generation",
        "_lock",
        "_write_lock",
        "_written_generation",
    )
    _counts: dict[AlbumId, int]
    # Guards :meth:`serialized_refresh` so overlapping callers run one at a
    # time instead of racing to overwrite :attr:`_counts` with whichever
    # thread happens to finish last. Released early by a timeout -- see
    # module docstring.
    _lock: asyncio.Lock
    # Bumped by ``+= 1`` in exactly two places -- :meth:`serialized_refresh`
    # (the real, async path, on the event loop) and :meth:`for_testing` (a
    # synchronous, loop-independent test fixture) -- never concurrently with
    # each other. Each bump captures the new value into a local variable in
    # the same statement and threads it explicitly into :meth:`_refresh` as a
    # parameter; :meth:`_refresh` itself never reads or writes this attribute.
    _generation: int
    # A plain ``threading.Lock``, not an ``asyncio.Lock``: the write it guards
    # runs inside :meth:`_refresh`, on a worker thread, and an orphaned thread
    # from a timed-out caller can still reach it well after that caller's
    # event-loop task has moved on.
    _write_lock: threading.Lock
    # The generation of the last write that actually landed in :attr:`_counts`.
    _written_generation: int

    def __new__(cls) -> Self:
        self = super().__new__(cls)
        self._counts = {}
        self._lock = asyncio.Lock()
        self._write_lock = threading.Lock()
        self._generation = 0
        self._written_generation = 0
        return self

    def get(self, album_id: AlbumId) -> int:
        """Return the last-known ready-track count for ``album_id``, or zero."""
        return self._counts.get(album_id, 0)

    async def serialized_refresh(self, albums: tuple[Album, ...]) -> None:
        """Refresh off the event loop via :meth:`_refresh`, one caller at a time.

        This is the entry point every caller must use -- :meth:`install` (the
        lux listener's event loop) and the single-flighted background repaint
        (the control-channel writer's event loop) can both reach this cache at
        once, a menu click landing while a Part-completion refresh is
        mid-flight. Without serialization, both would independently build a
        ``fresh`` dict from their own snapshot and unconditionally overwrite
        :attr:`_counts` -- whichever thread happens to finish LAST wins, not
        whichever started most recently. The lock is acquired here, on the
        event loop, before the thread dispatch -- never inside :meth:`_refresh`
        itself, which runs in a worker thread once dispatched and cannot await
        an ``asyncio.Lock`` there -- so a caller that arrives mid-refresh
        simply waits its turn instead of racing the one already running.

        Bounded by :data:`_REFRESH_TIMEOUT_SECONDS`: a stuck disk read gives up
        the wait rather than blocking this caller (and everyone serialized
        behind it) forever. A timeout is logged and swallowed here -- the
        caller gets whatever the cache already held, same as any other refresh
        fault -- while :attr:`_generation` protects against the abandoned
        worker thread landing a stale write later (see module docstring).

        Only OUR deadline is swallowed. A real disk-level timeout (``OSError``
        with ``errno.ETIMEDOUT``, e.g. a hung NFS mount) is also a
        ``TimeoutError`` in Python (a built-in ``OSError`` subclass), and would
        be caught by an unqualified ``except TimeoutError`` just the same --
        silently misreporting a genuine fault as "gave up waiting" and hiding
        it from :meth:`~punt_vox.voxd.music_player.player.MusicPlayer.
        _try_refresh_cache`. :meth:`asyncio.wait_for` only cancels its inner
        task when ITS OWN deadline elapses, so checking ``task.cancelled()``
        distinguishes the two: cancelled means the deadline fired and this is
        our timeout; not cancelled means the task ran to completion and raised
        that ``TimeoutError`` itself, which is re-raised unchanged. One
        acknowledged, razor-thin edge: CPython's ``Task.cancel()``/``__step``
        machinery has a same-event-loop-tick window where a genuine fault
        completing in the exact tick the deadline fires can be misreported as
        our own cancellation, discarding the real exception's diagnostic
        content in favor of the generic timeout warning below -- an inherent
        scheduling limitation, not a defect in this discrimination.
        """
        self._generation += 1
        generation = self._generation
        task = asyncio.ensure_future(self._locked_refresh(albums, generation))
        try:
            await asyncio.wait_for(task, timeout=_REFRESH_TIMEOUT_SECONDS)
        except TimeoutError:
            if not task.cancelled():
                raise  # a genuine fault from the read itself, not our deadline
            logger.warning(
                "music: track-count refresh for %d albums exceeded %.1fs; "
                "abandoning the wait (a late write, if any, will be ignored "
                "once a newer refresh has actually landed, not merely attempted)",
                len(albums),
                _REFRESH_TIMEOUT_SECONDS,
            )

    async def _locked_refresh(self, albums: tuple[Album, ...], generation: int) -> None:
        """Hold :attr:`_lock` across the dispatch, so overlapping callers queue."""
        async with self._lock:
            await asyncio.to_thread(self._refresh, albums, generation)

    def _refresh(self, albums: tuple[Album, ...], generation: int) -> None:
        """Re-read every album's live count from disk; the one blocking call here.

        Intended to run only via :meth:`serialized_refresh` -- a direct call
        runs inline, blocking whatever event loop called it, and races any
        concurrent :meth:`serialized_refresh` in flight. Only ``LookupError``
        (the store's documented "this album was deleted" contract) drops an
        album from the refreshed set; any other fault -- a transient
        ``OSError`` from a permission blip or a descriptor exhaustion --
        propagates rather than silently freezing that album's count at its
        last-known value.

        ``generation`` is captured by the caller at DISPATCH time (in
        :meth:`serialized_refresh`, synchronously, before this method's worker
        thread is even created) and handed to this method as a plain
        parameter -- it is never read back off :attr:`_generation` once the
        worker thread actually starts running. Two overlapping calls to
        :meth:`serialized_refresh` share that one counter, and it is bumped
        again on every new dispatch; if this method read :attr:`_generation`
        itself instead of being handed the value that was current at ITS OWN
        dispatch, a call whose worker thread is slow to start could read a
        LATER call's bump instead of its own, write stale data under a newer
        generation number, and cause a genuinely fresher, later-dispatched
        refresh to lose to it -- exactly the race this whole mechanism exists
        to prevent, reachable via two ordinary, fully-successful calls with no
        timeout involved at all.

        The write is gated on ``generation`` under :attr:`_write_lock`: this
        method may run on a worker thread orphaned by a caller that already
        timed out (see module docstring), so a late write here must never
        clobber a newer refresh's result. It commits only when ``generation``
        is still newer than :attr:`_written_generation` -- the last write
        that actually landed, not the newest one any caller has ever
        attempted. A dispatch can commit even while a newer generation is
        still in flight elsewhere; gating on "newest attempted" instead would
        be stricter and wrong, since it would drop a legitimate write
        whenever some later-dispatched attempt never completes (its own
        timeout, say).
        """
        fresh: dict[AlbumId, int] = {}
        for album in albums:
            try:
                fresh[album.id] = AlbumDisplay.read(album).tracks
            except LookupError:
                logger.debug(
                    "album %s is no longer on disk; dropping its cached count",
                    album.id,
                )
        with self._write_lock:
            if generation > self._written_generation:
                self._counts = fresh
                self._written_generation = generation

    @classmethod
    def for_testing(cls, albums: tuple[Album, ...]) -> Self:
        """Return a cache pre-populated with ``albums``' live counts; tests only.

        Synchronous by design -- production code always reaches the disk read
        through :meth:`serialized_refresh`, off the event loop. This exists so
        fixtures for :class:`~punt_vox.voxd.music_player.album_roster.
        AlbumRoster`, :class:`~punt_vox.voxd.music_player.album_table.
        AlbumTable`, and :class:`~punt_vox.voxd.music_player.scene.
        AlbumListScene` can populate a cache without going through the async
        ``serialized_refresh`` path, which needs a running event loop these
        fixtures don't have.

        Bumps :attr:`_generation` the same ``+= 1`` way :meth:`serialized_refresh`
        does, rather than setting it to a literal value -- a cache built here
        that a test later drives through a real :meth:`serialized_refresh`
        must still be able to write: that call bumps ``_generation`` to 2
        starting from 1, which stays ``> _written_generation``. Setting
        ``_generation`` to a literal ``1`` here would look identical for a
        fresh cache (``0 + 1 == 1``), but the ``+=`` is what keeps the "only
        these two places ever bump ``_generation``" comment on the attribute
        honest. Captures the bumped value into a local, exactly like
        :meth:`serialized_refresh` does, and threads it into :meth:`_refresh`
        explicitly -- this method is synchronous, loop-independent test
        fixture code, not a worker thread, so there is no execution-time delay
        between the bump and the call for a later bump to race against, but
        the shape stays identical to the real dispatch path on purpose.
        """
        self = cls()
        self._generation += 1
        generation = self._generation
        self._refresh(albums, generation)
        return self
