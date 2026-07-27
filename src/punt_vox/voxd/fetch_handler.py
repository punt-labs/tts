"""Fetch WebSocket handler: stream a store file to a remote client in chunks.

``get`` retrieves a store file -- a recording, or one part of a music album -- of
arbitrary total size in bounded per-frame memory. The reference is resolved and
containment-checked *once*, before any byte, then handed to :class:`ChunkedTransfer`
which streams ``fetch_begin`` -> ``chunk``* -> ``fetch_end`` (or an ``error``
terminal on a mid-stream fault). A recording is addressed by a bare ``ref``; a
music part by a catalog ``album`` id plus a bare ``part`` name, resolved inside the
catalog-resolved album directory. A hostile ref/part is refused before the first
byte and audit-logged; the old single-frame size ceiling is gone.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Self

from punt_vox.types_audio import FETCH_CHUNK_BYTES
from punt_vox.voxd._parse import parse_optional_str
from punt_vox.voxd.chunked_fetch import ChunkedTransfer
from punt_vox.voxd.path_status import PathStatus
from punt_vox.voxd.programs.album_id import AlbumId
from punt_vox.voxd.types import MessageHandler
from punt_vox.voxd.wire_fault import SafeFault
from punt_vox.voxd.wire_reply import WireReply

if TYPE_CHECKING:
    from pathlib import Path

    from starlette.websockets import WebSocket

    from punt_vox.voxd.programs.library import MusicLibrary
    from punt_vox.voxd.record_store import RecordStore

__all__ = ["FetchHandler"]

logger = logging.getLogger(__name__)


class FetchHandler(MessageHandler):
    """Handle 'fetch' messages: resolve a recording or album part, then stream it."""

    __slots__ = ("_music", "_store")

    _store: RecordStore
    _music: MusicLibrary

    def __new__(cls, *, store: RecordStore, music: MusicLibrary) -> Self:
        self = super().__new__(cls)
        self._store = store
        self._music = music
        return self

    async def __call__(self, msg: dict[str, object], websocket: WebSocket) -> None:
        """Resolve the reference once (containment-checked), then stream in chunks."""
        reply = WireReply(websocket, str(msg.get("id", "")))
        try:
            path, label = self._resolve_existing(msg)
        except ValueError as exc:
            await reply.error(str(exc))
            return
        except OSError as exc:
            # A stat fault (EACCES/EIO) on the already-contained path is a
            # server-side fault, not a missing recording: PathStatus.of lets
            # ENOENT read as absent but propagates any other OSError, so a
            # genuine access failure never masquerades as "no such recording".
            # logger.exception records the traceback; SafeFault.from_exception
            # then puts the cause on the audit line too (and relativizes an
            # in-jail store path), never leaving the failure silent.
            logger.exception("fetch op failed id=%r", reply.request_id)
            await reply.fault(SafeFault.from_exception(exc))
            return
        await ChunkedTransfer(reply, FETCH_CHUNK_BYTES).stream(path, label)

    def _resolve_existing(self, msg: dict[str, object]) -> tuple[Path, str]:
        """Return ``(path, echo_ref)`` for an existing regular file, or raise.

        Resolves the reference once (containment-checked), then classifies it via
        :class:`PathStatus` with ``follow_symlinks=False`` -- a symlink entry is
        non-regular and rejected (never served its target), an absent path is a
        client "no such recording/part", and any other stat ``OSError`` faults.
        """
        album = parse_optional_str(msg, "album")
        path, label, kind = self._resolve(album, msg)
        if not PathStatus.of(path, follow_symlinks=False).is_regular_file:
            raise ValueError(f"no {kind} named {label!r}")
        return path, label

    def _resolve(
        self, album: str | None, msg: dict[str, object]
    ) -> tuple[Path, str, str]:
        """Return the resolved ``(path, echo_ref, kind)``, or raise ``ValueError``.

        A music part (``album`` present) catalog-resolves the album id, then
        bare-name-validates the part inside it; a recording resolves its bare
        ``ref``. The album id is a catalog key, never a validated path (F2).
        """
        if album is not None:
            part = parse_optional_str(msg, "part")
            if not part:
                raise ValueError("fetch of an album requires a part")
            return self._music.resolve_part(AlbumId(album), part), part, "part"
        ref = parse_optional_str(msg, "ref")
        if not ref:
            raise ValueError("fetch requires a ref or an album and part")
        return self._store.resolve_ref(ref), ref, "recording"
