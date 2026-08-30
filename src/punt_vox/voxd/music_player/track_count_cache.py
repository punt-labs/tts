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
:attr:`_written_generation` close that window. :meth:`serialized_refresh`
(and, for fixtures, :meth:`for_testing`) is the only code that ever bumps
:attr:`_generation`, always the same ``+= 1`` on the event loop, before
dispatch -- well before the corresponding worker thread can even start,
since the default thread-pool executor's queue is strictly FIFO, so a
thread's actual start order can never precede its own dispatch order.
:meth:`_refresh` never receives a generation from its caller: it *reads*
:attr:`_generation` itself, as the first thing it does once its worker
thread actually runs, and commits its write under :attr:`_write_lock` only
when that reading is still the newest generation ever attempted. A late
write from an abandoned refresh is thus a safe no-op whenever a newer one
has already landed, and no caller -- buggy or otherwise -- can hand
:meth:`_refresh` an arbitrary generation number to wedge the gate, because
the parameter does not exist.

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
    instance can still call :meth:`_refresh` directly if it chooses to ignore
    the convention. What IS structurally enforced, not just conventional, is
    the timeout/lock/generation-gate invariant the module docstring describes:
    :meth:`_refresh` reads its own generation rather than accepting one as an
    argument, so no caller -- however it reaches this class -- can hand it an
    arbitrary generation number and corrupt :attr:`_written_generation`.
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
    # (the real path) and :meth:`for_testing` (fixtures) -- both
    # synchronously, on the event loop, never concurrently with each other.
    # :meth:`_refresh` only ever *reads* this, once, as the first thing it
    # does when its worker thread actually starts; it never writes it, so no
    # caller can hand it an ungoverned generation number.
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
        task = asyncio.ensure_future(self._locked_refresh(albums))
        try:
            await asyncio.wait_for(task, timeout=_REFRESH_TIMEOUT_SECONDS)
        except TimeoutError:
            if not task.cancelled():
                raise  # a genuine fault from the read itself, not our deadline
            logger.warning(
                "music: track-count refresh for %d albums exceeded %.1fs; "
                "abandoning the wait (a late write, if any, will be ignored "
                "unless it is still the newest attempt)",
                len(albums),
                _REFRESH_TIMEOUT_SECONDS,
            )

    async def _locked_refresh(self, albums: tuple[Album, ...]) -> None:
        """Hold :attr:`_lock` across the dispatch, so overlapping callers queue."""
        async with self._lock:
            await asyncio.to_thread(self._refresh, albums)

    def _refresh(self, albums: tuple[Album, ...]) -> None:
        """Re-read every album's live count from disk; the one blocking call here.

        Intended to run only via :meth:`serialized_refresh` -- a direct call
        runs inline, blocking whatever event loop called it, and races any
        concurrent :meth:`serialized_refresh` in flight. Only ``LookupError``
        (the store's documented "this album was deleted" contract) drops an
        album from the refreshed set; any other fault -- a transient
        ``OSError`` from a permission blip or a descriptor exhaustion --
        propagates rather than silently freezing that album's count at its
        last-known value.

        Takes no generation argument: it reads :attr:`_generation` itself,
        once, as the very first thing it does once its worker thread actually
        starts running -- never handed one by a caller, so nothing outside
        this method can inject an arbitrary generation number to wedge the
        write-gate below. Reading it here, rather than being passed a value
        captured at dispatch time, is still safe: :meth:`serialized_refresh`
        is the only dispatcher, and the default thread-pool executor's queue
        is FIFO, so a worker thread can never start running before the
        dispatch that queued it -- the generation this method reads is
        always the one its own dispatch intended, never a later call's.

        The write is gated on that generation under :attr:`_write_lock`: this
        method may run on a worker thread orphaned by a caller that already
        timed out (see module docstring), so a late write here must never
        clobber a newer refresh's result. It commits only when the generation
        it read is still the newest one any caller has ever attempted.
        """
        generation = self._generation
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
        honest.
        """
        self = cls()
        self._generation += 1
        self._refresh(albums)
        return self
