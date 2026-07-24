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

from typing import TYPE_CHECKING, Self

from punt_vox.types_audio import FETCH_CHUNK_BYTES
from punt_vox.voxd._parse import parse_optional_str
from punt_vox.voxd.chunked_fetch import ChunkedTransfer
from punt_vox.voxd.programs.album_id import AlbumId
from punt_vox.voxd.types import MessageHandler
from punt_vox.voxd.wire_reply import WireReply

if TYPE_CHECKING:
    from pathlib import Path

    from starlette.websockets import WebSocket

    from punt_vox.voxd.programs.library import MusicLibrary
    from punt_vox.voxd.record_store import RecordStore

__all__ = ["FetchHandler"]


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
            album = parse_optional_str(msg, "album")
            path, label, kind = self._resolve(album, msg)
        except ValueError as exc:
            await reply.error(str(exc))
            return
        if not path.is_file():
            await reply.error(f"no {kind} named {label!r}")
            return
        await ChunkedTransfer(reply, FETCH_CHUNK_BYTES).stream(path, label)

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
