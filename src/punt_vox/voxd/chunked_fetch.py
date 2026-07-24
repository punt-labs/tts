"""Stream a resolved store file to the client as bounded, ordered chunks.

``ChunkedTransfer`` is the daemon half of the ``get`` transport modelled in
``docs/vox-chunked-transfer.tex``: a ``fetch_begin`` announcing the total byte
count, chunk count, and sha256; then strictly-increasing ``chunk`` frames, each at
most ``FETCH_CHUNK_BYTES`` raw bytes; then a ``fetch_end`` -- or, on a mid-stream
read fault, an ``error`` terminal instead. The file is read in slices twice (once
to declare the totals and hash, once to send), so memory stays bounded for a file
of any size and the declared byte count is a guard the client checks against.

Containment happens *before* a transfer is handed here: the caller resolves the
reference once and passes a ``Path``, so no byte is ever emitted for an
uncontained reference (the ``RejectBeforeBegin`` property).
"""

from __future__ import annotations

import base64
import hashlib
import logging
from typing import TYPE_CHECKING, Self, final

if TYPE_CHECKING:
    from pathlib import Path

    from punt_vox.voxd.wire_reply import WireReply

__all__ = ["ChunkedTransfer"]

logger = logging.getLogger(__name__)


@final
class ChunkedTransfer:
    """Read a file in ``chunk_bytes`` slices and stream it as begin/chunk*/end."""

    __slots__ = ("_chunk_bytes", "_reply")

    _reply: WireReply
    _chunk_bytes: int

    def __new__(cls, reply: WireReply, chunk_bytes: int) -> Self:
        self = super().__new__(cls)
        self._reply = reply
        self._chunk_bytes = chunk_bytes
        return self

    async def stream(self, path: Path, ref: str) -> None:
        """Announce the totals, send ordered chunks, and end -- or abort on a fault.

        A read fault while measuring is refused *before* ``fetch_begin`` (no
        totals announced, nothing sent); a fault mid-stream ends the exchange with
        an ``error`` terminal, so the client discards its partial and no complete
        file is ever claimed for an interrupted transfer.
        """
        try:
            total, chunks, digest = self._measure(path)
        except OSError as exc:
            await self._abort_before_begin(ref, exc)
            return

        if not await self._reply.send(
            {
                "type": "fetch_begin",
                "ref": ref,
                "bytes": total,
                "chunks": chunks,
                "sha256": digest,
            }
        ):
            return
        logger.info(
            "Fetch begin: id=%r ref=%r bytes=%d chunks=%d",
            self._reply.request_id,
            ref,
            total,
            chunks,
        )
        await self._send_chunks(path, ref, total)

    def _measure(self, path: Path) -> tuple[int, int, str]:
        """Return ``(total_bytes, chunk_count, sha256_hex)`` from one bounded read."""
        hasher = hashlib.sha256()
        total = 0
        with path.open("rb") as handle:
            while slice_ := handle.read(self._chunk_bytes):
                hasher.update(slice_)
                total += len(slice_)
        chunks = -(-total // self._chunk_bytes)  # ceil; 0 for an empty file
        return total, chunks, hasher.hexdigest()

    async def _send_chunks(self, path: Path, ref: str, total: int) -> None:
        """Send the data frames in order, then ``fetch_end`` -- or abort on a fault."""
        try:
            sent = await self._pump(path, total)
        except OSError as exc:
            await self._abort_mid_stream(ref, exc)
            return
        if sent is None:
            # The peer left mid-stream: a silent end, like a gone peer at begin.
            # Nothing to deliver and no fault to report -- do not claim the file
            # "changed".
            return
        if sent != total:
            await self._abort_mid_stream(
                ref, OSError(f"file changed mid-fetch: sent {sent} of {total} bytes")
            )
            return
        await self._reply.send({"type": "fetch_end", "ref": ref, "bytes": total})

    async def _pump(self, path: Path, total: int) -> int | None:
        """Send one ``chunk`` per slice up to ``total`` bytes; return the bytes sent.

        Bounded by the declared ``total``, not the live file size: a file grown
        after the measure pass transfers exactly its declared prefix, and a file
        that shrank yields an early EOF so ``sent < total`` aborts the stream.

        Returns ``None`` -- a terminal peer-gone state, not a byte count -- when a
        chunk send finds the peer already disconnected: reading and base64-encoding
        the rest of the file would deliver nothing, so the pump stops at once.
        """
        seq = 0
        sent = 0
        with path.open("rb") as handle:
            while sent < total:
                slice_ = handle.read(min(self._chunk_bytes, total - sent))
                if not slice_:
                    break  # EOF before the declared total -- the file shrank
                if not await self._reply.send(
                    {
                        "type": "chunk",
                        "seq": seq,
                        "data": base64.b64encode(slice_).decode("ascii"),
                    }
                ):
                    return None  # peer gone -- stop reading and encoding further
                seq += 1
                sent += len(slice_)
        return sent

    async def _abort_before_begin(self, ref: str, exc: OSError) -> None:
        """Refuse a transfer that faulted before any byte -- an error, no begin."""
        logger.warning(
            "Fetch measure failed for id=%r ref=%r: %s",
            self._reply.request_id,
            ref,
            exc,
        )
        await self._reply.send(
            {"type": "error", "message": f"cannot read {ref!r}: {exc}"}
        )

    async def _abort_mid_stream(self, ref: str, exc: OSError) -> None:
        """End a started stream with an error terminal so the client discards it."""
        logger.warning(
            "Fetch aborted mid-stream for id=%r ref=%r: %s",
            self._reply.request_id,
            ref,
            exc,
        )
        await self._reply.send(
            {"type": "error", "message": f"fetch of {ref!r} failed: {exc}"}
        )
