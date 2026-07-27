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
from punt_vox.voxd.wire_fault import SafeFault
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
        """Enumerate the store and reply with the recordings list -- or fault.

        Enumeration is best-effort per entry. A root that is missing or has been
        swapped for a file is simply an empty store -- ``entries`` returns no rows
        rather than raising -- so only a genuine I/O fault reading a real directory
        (permissions changed mid-scan) raises ``OSError`` -- an operational fault,
        so it routes through ``fault`` (ERROR), not ``error``, and never tears down.
        """
        reply = WireReply(websocket, str(msg.get("id", "")))
        try:
            entries = self._store.entries()
        except OSError as exc:
            await reply.fault(SafeFault.from_exception(exc))
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
        """Validate the ref, unlink the recording, and reply -- or error/fault.

        A domain failure (``ValueError`` or ``OSError``) is classified by
        :meth:`_reply_failure` into a client rejection or a server-side fault;
        every path replies an id-stamped frame rather than escaping to a router
        teardown.
        """
        reply = WireReply(websocket, str(msg.get("id", "")))
        try:
            ref = parse_optional_str(msg, "ref")
            if not ref:
                await reply.error("rec remove requires a ref")
                return
            self._store.remove(ref)
        except (ValueError, OSError) as exc:
            await self._reply_failure(reply, exc)
            return
        await reply.send({"type": "removed", "removed": ref})

    @staticmethod
    async def _reply_failure(reply: WireReply, exc: ValueError | OSError) -> None:
        """Route a remove failure: client rejections verbatim, host faults relativized.

        A hostile or non-string ``ref`` (``ValueError``) and the store's own "no
        recording named X" (a ``FileNotFoundError`` with ``filename`` ``None``,
        echoing the client's own ref) are client rejections -- ``error`` (WARNING),
        sent verbatim, matching how ``play``/``fetch`` answer a ref that names no
        recording. A raw OS unlink race carries ``filename=<absolute store path>``;
        that, and any other ``OSError`` (a denied delete, a device error), is a
        server-side fault routed through :class:`SafeFault`, so no absolute prefix
        (host recon) ever crosses the wire while the raw detail stays in the log.
        """
        if isinstance(exc, ValueError) or (
            isinstance(exc, FileNotFoundError) and exc.filename is None
        ):
            await reply.error(str(exc))
        else:
            await reply.fault(SafeFault.from_exception(exc))
