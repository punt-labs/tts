"""Wire handlers for the cache status and clear verbs.

Both route the daemon's *own* MP3 quip cache -- the directory the daemon reads
and writes during synthesis -- to a wire client, so ``vox cache status`` and
``vox cache clear`` operate on the daemon's cache rather than the caller's local
one (the vox-suvs bug: a remote daemon left the real cache untouched while the
client reported/cleared its own directory). Each handler holds its cache
operation as an injected collaborator and replies through :class:`WireReply`, so
a filesystem fault -- a denied unlink, a stat error mid-scan -- is an
audit-logged operational fault (ERROR "operation failed"), never a silent no-op
or a torn connection.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Self, final

from punt_vox.voxd.data_root_boundary import relativize_to_data_root
from punt_vox.voxd.wire_fault import SafeFault
from punt_vox.voxd.wire_reply import WireReply

if TYPE_CHECKING:
    from collections.abc import Callable

    from starlette.websockets import WebSocket

    from punt_vox.cache import CacheInfo

__all__ = ["CacheClearHandler", "CacheStatusHandler"]


@final
class CacheStatusHandler:
    """Handle ``cache_status``: reply with the daemon cache's entries/size/path."""

    __slots__ = ("_status",)
    _status: Callable[[], CacheInfo]

    def __new__(cls, status: Callable[[], CacheInfo]) -> Self:
        self = super().__new__(cls)
        self._status = status
        return self

    async def __call__(self, msg: dict[str, object], websocket: WebSocket) -> None:
        """Read the daemon cache's status and reply -- or fault on an I/O error.

        ``cache_status`` sizes every entry with a ``stat``; a fault reading the
        cache directory (a permission change mid-scan) is a server-side
        operational failure, so it routes through ``fault`` (ERROR), never a
        client rejection, and replies an id-stamped frame instead of escaping to
        a router teardown.
        """
        reply = WireReply(websocket, str(msg.get("id", "")))
        try:
            info = self._status()
        except OSError as exc:
            await reply.fault(SafeFault.from_exception(exc))
            return
        # The in-jail cache dir crosses relativized (`cache`), never a host
        # prefix; the bare name is the fail-closed fallback out of jail.
        rel = relativize_to_data_root(info.path)
        await reply.send(
            {
                "type": "cache_status",
                "entries": info.entries,
                "size_bytes": info.size_bytes,
                "path": str(rel.path) if rel is not None else info.path.name,
            }
        )


@final
class CacheClearHandler:
    """Handle ``cache_clear``: delete the daemon cache and reply with the count."""

    __slots__ = ("_clear",)
    _clear: Callable[[], int]

    def __new__(cls, clear: Callable[[], int]) -> Self:
        self = super().__new__(cls)
        self._clear = clear
        return self

    async def __call__(self, msg: dict[str, object], websocket: WebSocket) -> None:
        """Clear the daemon cache and reply with the delete count -- or fault.

        An ``OSError`` unlinking a cache entry (a denied delete, a device error)
        is a server-side operational failure routed through ``fault`` (ERROR), so
        a partial clear is audited rather than mislabeled a success.
        """
        reply = WireReply(websocket, str(msg.get("id", "")))
        try:
            cleared = self._clear()
        except OSError as exc:
            await reply.fault(SafeFault.from_exception(exc))
            return
        await reply.send({"type": "cache_cleared", "cleared": cleared})
