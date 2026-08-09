"""The saved-album verbs of the ``music`` tool: ``new``, ``get``, ``remove``.

These three touch the catalog on disk and nothing else -- no running Program, no
style register, no marquee -- so they share no state with the playback verbs
they used to sit beside. Held apart on that seam: a class whose methods disagree
about which data they operate on is two classes wearing one name (PL-CO-2), and
the split keeps each side's module under the size threshold as verbs are added.

The gateway arrives as a factory called per invocation, not a stored gateway, so
a caller that re-points the daemon connection between calls is honoured on the
next one rather than pinned to the connection that existed at construction.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Self, final

from punt_vox.music_faults import DAEMON_ERRORS, MusicFault
from punt_vox.types_programs.prompts import PromptSet

if TYPE_CHECKING:
    from collections.abc import Callable

    from punt_vox.catalog_gateway import CatalogGateway
    from punt_vox.music_args import MusicArgs

__all__ = ["CatalogVerbs"]


@final
class CatalogVerbs:
    """Author, export, and delete saved albums on behalf of the ``music`` tool."""

    __slots__ = ("_gateway_factory",)
    _gateway_factory: Callable[[], CatalogGateway]

    def __new__(cls, gateway_factory: Callable[[], CatalogGateway]) -> Self:
        self = super().__new__(cls)
        self._gateway_factory = gateway_factory
        return self

    def new(self, args: MusicArgs) -> str:
        """Author one verbatim-prompt track into a fresh catalog album."""
        if args.base_prompt is None:
            return MusicFault.rejecting("music new requires base_prompt")
        try:
            prompts = PromptSet.single(args.base_prompt)
            album_id = self._gateway_factory().new(prompts, args.canonical_title)
        except (ValueError, *DAEMON_ERRORS) as exc:
            return MusicFault.of(exc)
        return json.dumps({"album_id": album_id})

    def get(self, args: MusicArgs) -> str:
        """Export a saved album into *dest*; return the written locator."""
        if args.album_id is None or args.dest is None:
            return MusicFault.rejecting("music get requires album_id and dest")
        try:
            target = self._gateway_factory().get(args.album_id, args.dest)
        except (ValueError, *DAEMON_ERRORS) as exc:
            return MusicFault.of(exc)
        return json.dumps({"album_id": args.album_id, "path": target})

    def remove(self, args: MusicArgs) -> str:
        """Delete a saved album by id (a playing album is refused)."""
        if args.album_id is None:
            return MusicFault.rejecting("music remove requires album_id")
        try:
            self._gateway_factory().remove(args.album_id)
        except (ValueError, *DAEMON_ERRORS) as exc:
            return MusicFault.of(exc)
        return json.dumps({"removed": args.album_id})
