"""The ``program_select`` wire handler -- replay a Selection by positional/tags.

Replaces the name-addressed play/loop handlers: a replay resolves either from
the bare positional -- *id-or-name*: a catalogued album id is a direct lookup,
anything else is the curated-name radio -- or by a :class:`TagQuery` over the
``style``/``vibe``/``name`` selectors, driving ``service.replay``. The daemon
animates the resulting Selection as a consume-only radio.
"""

from __future__ import annotations

from typing import final

from punt_vox.voxd.programs.album_id import AlbumId
from punt_vox.voxd.programs.album_tags import TagQuery
from punt_vox.voxd.programs.command_handler import ProgramCommandHandler

__all__ = ["SelectHandler"]


@final
class SelectHandler(ProgramCommandHandler):
    """Handle ``program_select``: replay by id (direct) or by a tag query."""

    __slots__ = ()
    _WIRE_TYPE = "program_select"

    def _run(self, msg: dict[str, object], /) -> None:
        """Route by resolution kind: the bare positional (id-or-name), else tags.

        The positional rides the wire as ``album_id`` -- distinct from the ``id``
        request-correlation field the envelope already uses -- and is resolved by
        :meth:`_replay_positional`. Absent it, the ``style``/``vibe``/``name``
        selectors build the tag query.
        """
        album_id = self._opt_str(msg, "album_id")
        if album_id is not None:
            self._replay_positional(album_id)
            return
        # Canonicalize the tags the same way the on-path mints them, so a replay
        # of " trance " matches an album minted as "trance" -- write and read agree.
        self._service.replay(
            TagQuery.normalized(
                style=self._opt_str(msg, "style"),
                vibe=self._opt_str(msg, "vibe"),
                name=self._opt_str(msg, "name"),
            )
        )

    def _replay_positional(self, ref: str) -> None:
        """Replay what the bare positional names: a saved album id, else a name radio.

        A well-formed id that names a catalogued album replays that one album
        directly, so a known-but-empty album still reports "no playable tracks
        yet". Any other ref -- a non-hex string, or a hex string naming no album
        -- resolves as the curated-name radio, so ``play focus-beats`` plays the
        saved-name pool instead of raising a hex-validation error (D-3).
        """
        album_id = AlbumId.try_from(ref)
        if album_id is not None and self._service.catalog.by_id(album_id) is not None:
            self._service.replay_album(album_id)
            return
        self._service.replay(TagQuery.normalized(style=None, vibe=None, name=ref))
