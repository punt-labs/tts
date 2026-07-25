"""Wire handlers for the catalog-authoring music verbs: new, manifest, remove.

Each is a thin adapter over :class:`MusicLibrary`: parse the bare album id or
prompt, drive the one library call, and reply through :class:`WireReply` so every
rejection is id-stamped and audit-logged at WARNING. The album id rides the wire
as ``album`` and is a catalog key -- turned into an :class:`AlbumId` (which
validates its hex shape) and resolved through the catalog, never treated as a
path. ``music_manifest`` and ``music_remove`` reply synchronously; ``music_new``
sends a ``generating`` ack before the long generation so the client's response
timeout does not fire.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Self, final

from punt_vox.types_programs.prompts import PromptSet
from punt_vox.voxd._parse import parse_optional_str
from punt_vox.voxd.programs.album_id import AlbumId
from punt_vox.voxd.wire_reply import WireReply

if TYPE_CHECKING:
    from collections.abc import Callable

    from starlette.websockets import WebSocket

    from punt_vox.voxd.programs.library import MusicLibrary

__all__ = ["MusicManifestHandler", "MusicNewHandler", "MusicRemoveHandler"]

# The library failures a handler turns into a clean wire reply rather than a torn
# socket. They split by fault-vs-error: a bad request/precondition (ValueError)
# is a client rejection audited as ``error``; a deleted album dir (LookupError)
# or a filesystem fault (OSError) is a server-side operational failure audited as
# ``fault``. ``WireReply.reject_or_fault`` applies that split in one place so all
# three handlers classify identically.
_LIBRARY_FAILURES = (ValueError, LookupError, OSError)


@final
class MusicNewHandler:
    """Handle ``music_new``: author one track into a fresh catalog album."""

    __slots__ = ("_library",)
    _library: MusicLibrary

    def __new__(cls, library: MusicLibrary) -> Self:
        self = super().__new__(cls)
        self._library = library
        return self

    async def __call__(self, msg: dict[str, object], websocket: WebSocket) -> None:
        """Reject every bad pre-generation input pre-ack, else ack and reply.

        A blank/non-string ``base_prompt``, a non-string or duplicate name -- all
        input rejection runs through :meth:`MusicLibrary.reserve` *before* the
        ``generating`` ack, so a malformed request never gets an ack it only fails
        after. The reservation's context frees the held name on every exit path.

        The authored input rides the wire as ``base_prompt`` (the same key
        ``program_on`` uses); it is wrapped as a single-track :class:`PromptSet`
        so ``new`` shares the one authored-input object both surfaces build, then
        generated as ``prompt_for(0)`` -- the verbatim base, undecorated.
        """
        reply = WireReply(websocket, str(msg.get("id", "")))
        try:
            base = parse_optional_str(msg, "base_prompt")
            if not base or not base.strip():
                await reply.error("music new requires a prompt")
                return
            prompt = PromptSet.single(base).prompt_for(0)
            with self._library.reserve(prompt, parse_optional_str(msg, "name")) as res:
                if not await reply.send({"type": "generating"}):
                    return
                album_id = await self._library.produce(res)
        except _LIBRARY_FAILURES as exc:
            await reply.reject_or_fault(exc)
            return
        await reply.send({"type": "album", "album_id": album_id.value, "parts": 1})


@final
class MusicManifestHandler:
    """Handle ``music_manifest``: describe an album's parts for ``music get``."""

    __slots__ = ("_library",)
    _library: MusicLibrary

    def __new__(cls, library: MusicLibrary) -> Self:
        self = super().__new__(cls)
        self._library = library
        return self

    async def __call__(self, msg: dict[str, object], websocket: WebSocket) -> None:
        """Catalog-resolve the album id and reply with its on-disk name and parts."""
        reply = WireReply(websocket, str(msg.get("id", "")))
        try:
            album = parse_optional_str(msg, "album")
            if not album:
                await reply.error("music get requires an album")
                return
            contents = self._library.manifest(AlbumId(album))
        except _LIBRARY_FAILURES as exc:
            await reply.reject_or_fault(exc)
            return
        await reply.send(
            {
                "type": "manifest",
                "album": contents.name,
                "parts": [
                    {"part": part.name, "bytes": part.byte_count}
                    for part in contents.parts
                ],
            }
        )


@final
class MusicRemoveHandler:
    """Handle ``music_remove``: delete an idle album, refusing a playing one."""

    __slots__ = ("_blocked", "_library")
    _library: MusicLibrary
    _blocked: Callable[[], frozenset[str]]

    def __new__(
        cls, library: MusicLibrary, blocked: Callable[[], frozenset[str]]
    ) -> Self:
        self = super().__new__(cls)
        self._library = library
        self._blocked = blocked
        return self

    async def __call__(self, msg: dict[str, object], websocket: WebSocket) -> None:
        """Catalog-resolve the album id and remove it unless it backs the source."""
        reply = WireReply(websocket, str(msg.get("id", "")))
        try:
            album = parse_optional_str(msg, "album")
            if not album:
                await reply.error("music remove requires an album")
                return
            self._library.remove(AlbumId(album), blocked=self._blocked())
        except _LIBRARY_FAILURES as exc:
            await reply.reject_or_fault(exc)
            return
        await reply.send({"type": "removed", "album_id": album})
