"""Play WebSocket handler: play a stored recording on the daemon host.

``vox play <id>`` routes here so audio comes out on the machine with speakers
(the daemon host), not on a remote client. The reference is a bare store name,
resolved and containment-checked exactly like a record name -- an absolute,
traversing, or separated ref is refused before any file is touched, and only a
file that exists inside the daemon-owned store is played. Playback runs through
the shared serialized :class:`PlaybackQueue`, so no audio is killed and the
flock ordering holds.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Self

from punt_vox.voxd._parse import parse_optional_str
from punt_vox.voxd.playback import PlaybackItem
from punt_vox.voxd.types import MessageHandler
from punt_vox.voxd.wire_fault import SafeFault
from punt_vox.voxd.wire_reply import WireReply

if TYPE_CHECKING:
    from pathlib import Path

    from starlette.websockets import WebSocket

    from punt_vox.voxd.playback import PlaybackQueue, PlaybackResult
    from punt_vox.voxd.record_store import RecordStore

__all__ = ["PlayHandler"]

logger = logging.getLogger(__name__)


class PlayHandler(MessageHandler):
    """Handle 'play' messages: play a store recording on the daemon host."""

    __slots__ = ("_playback", "_store")

    _playback: PlaybackQueue
    _store: RecordStore

    def __new__(cls, *, playback: PlaybackQueue, store: RecordStore) -> Self:
        self = super().__new__(cls)
        self._playback = playback
        self._store = store
        return self

    async def __call__(self, msg: dict[str, object], websocket: WebSocket) -> None:
        """Resolve a store reference, then hand it to the daemon-host playback."""
        reply = WireReply(websocket, str(msg.get("id", "")))
        try:
            ref = parse_optional_str(msg, "ref")
            if not ref:
                await reply.error("play requires a ref")
                return
            path = self._store.resolve_ref(ref)
        except ValueError as exc:
            await reply.error(str(exc))
            return
        if not path.is_file():
            await reply.error(f"no recording named {ref!r}")
            return
        await self._play(reply, path, ref)

    async def _play(self, reply: WireReply, path: Path, ref: str) -> None:
        """Enqueue *path* on the daemon, ack when queued, await the host outcome."""
        logger.info("Play: id=%r ref=%r", reply.request_id, ref)
        loop = asyncio.get_running_loop()
        outcome: asyncio.Future[PlaybackResult] = loop.create_future()
        await self._playback.enqueue(
            PlaybackItem(
                path=path,
                request_id=f"play:{ref}",
                notify=asyncio.Event(),
                outcome=outcome,
            )
        )
        # Ack that the job is queued, then wait for the real host-side outcome so
        # a failed playback (missing player, unplayable file, played-nothing)
        # reaches the client as an error instead of a silent success. If the ack
        # never lands the client is gone -- the recording still plays on the host
        # (already enqueued); just stop, don't await an outcome nobody waits on.
        if not await reply.send({"type": "playing"}):
            return
        result = await outcome
        if result.ok:
            await reply.send({"type": "done"})
            return
        # A host-side playback failure (missing player, unplayable file, played
        # nothing) is a server-side operational fault, not a client rejection, so
        # it routes through fault (the ERROR "operation failed" audit) rather than
        # a bare error frame the log would blame on the client. The wire carries
        # the rc/elapsed verdict with player-stderr paths relativized/stripped
        # (wire_failure_detail); the raw failure_detail -- absolute paths and all
        # -- stays in the host-local log.
        await reply.fault(
            SafeFault(
                f"playback failed: {result.wire_failure_detail()}",
                f"playback failed: {result.failure_detail()}",
            )
        )
