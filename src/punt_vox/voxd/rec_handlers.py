"""Wire handlers for the recordings-store list and remove verbs.

Both are thin adapters over :class:`RecordStore`, replying through
:class:`WireReply` so a rejection is id-stamped and audit-logged. ``rec_list``
takes no input -- it enumerates the immediate in-root recordings, so there is no
hostile name to reject. ``rec_remove`` runs its bare ``ref`` through the store's
shared containment validator, so a hostile ref is refused (and audit-logged)
before any unlink, and a not-found recording is a clean error, never a silent
success.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Self, final

from punt_vox.voxd._parse import parse_optional_str
from punt_vox.voxd.wire_reply import WireReply

if TYPE_CHECKING:
    from starlette.websockets import WebSocket

    from punt_vox.voxd.record_store import RecordStore

__all__ = ["RecListHandler", "RecRemoveHandler"]


@final
class RecListHandler:
    """Handle ``rec_list``: reply with each in-root recording's name and bytes."""

    __slots__ = ("_store",)
    _store: RecordStore

    def __new__(cls, store: RecordStore) -> Self:
        self = super().__new__(cls)
        self._store = store
        return self

    async def __call__(self, msg: dict[str, object], websocket: WebSocket) -> None:
        """Enumerate the store and reply with the recordings list -- or error.

        Enumeration is best-effort per entry. A root that is missing or has been
        swapped for a file is simply an empty store -- ``entries`` returns no rows
        rather than raising -- so only a genuine I/O fault reading a real
        directory (its permissions changed mid-scan) raises ``OSError`` here; the
        guard turns that into an id-stamped error frame, not a router teardown.
        """
        reply = WireReply(websocket, str(msg.get("id", "")))
        try:
            entries = self._store.entries()
        except OSError as exc:
            await reply.error(str(exc))
            return
        await reply.send(
            {
                "type": "recordings",
                "entries": [
                    {"name": entry.name, "bytes": entry.byte_count} for entry in entries
                ],
            }
        )


@final
class RecRemoveHandler:
    """Handle ``rec_remove``: delete one in-root recording by its bare name."""

    __slots__ = ("_store",)
    _store: RecordStore

    def __new__(cls, store: RecordStore) -> Self:
        self = super().__new__(cls)
        self._store = store
        return self

    async def __call__(self, msg: dict[str, object], websocket: WebSocket) -> None:
        """Validate the ref, unlink the recording, and reply -- or error.

        ``remove`` raises ``ValueError`` for a hostile ref and ``OSError`` for a
        filesystem fault (absent recording, denied unlink, or a race). Catching
        ``OSError`` -- the parent of ``FileNotFoundError`` and ``PermissionError``
        -- keeps a fault an id-stamped error frame instead of a router teardown.
        """
        reply = WireReply(websocket, str(msg.get("id", "")))
        try:
            ref = parse_optional_str(msg, "ref")
            if not ref:
                await reply.error("rec remove requires a ref")
                return
            self._store.remove(ref)
        except (ValueError, OSError) as exc:
            await reply.error(str(exc))
            return
        await reply.send({"type": "removed", "name": ref})
