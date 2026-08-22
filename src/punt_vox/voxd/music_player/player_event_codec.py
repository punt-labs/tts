"""``PlayerEventCodec`` -- the receive leg's boundary parser for inbound frames.

Turns a raw lux pub-sub ``(topic, payload)`` into one typed :data:`PlayerEvent`.
A ``music.play`` (an album-table row selection) names its target by the clicked
row's ``key_column`` cell -- the album's displayed name -- which lux delivers as
``payload['anchor']``; the codec resolves that name back to an :class:`AlbumId`
against the fresh catalog, since voxd owns the name-to-id mapping
(:meth:`AlbumNames.resolve`). It raises on anything it does not recognise
(PY-EH-8) so a malformed or transitional frame never reaches playback as a silent
no-op; the subscription boundary logs and drops what it raises.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Self, final

from punt_vox.voxd.music_player.album_names import AlbumNames
from punt_vox.voxd.music_player.player_events import (
    Next,
    Pause,
    PlayAlbum,
    Prev,
    Resume,
    StopMusic,
)
from punt_vox.voxd.music_player.wire import ANCHOR_KEY, MusicTopic

if TYPE_CHECKING:
    from collections.abc import Mapping

    from punt_vox.voxd.music_player.player_events import PlayerEvent
    from punt_vox.voxd.programs.album_id import AlbumId
    from punt_vox.voxd.programs.catalog import Album

__all__ = ["AnchorUnresolvedError", "PlayerEventCodec"]


@final
class AnchorUnresolvedError(ValueError):
    """A well-formed ``music.play`` anchor named no catalogued album.

    Distinct from a malformed frame (empty/missing anchor, plain ``ValueError``):
    the click carried a real name, but the catalog no longer holds it -- a
    vanished album, a stale row-cache click. The receive boundary surfaces this
    as a transient warning rather than the silent drop malformed frames get.
    """

    __slots__ = ("_anchor",)
    _anchor: str

    def __new__(  # pyright: ignore[reportInconsistentConstructor]
        cls, anchor: str
    ) -> Self:
        # Pass ``anchor`` through so BaseException.__init__ stores it in
        # ``.args`` (structured data for pickling and reflection); the descriptive
        # message is composed in :meth:`__str__` from ``self._anchor``.
        self = super().__new__(cls, anchor)
        self._anchor = anchor
        return self

    @property
    def anchor(self) -> str:
        """Return the unresolved anchor string the click carried."""
        return self._anchor

    def __str__(self) -> str:
        return f"music.play anchor {self._anchor!r} names no catalogued album"


@final
class PlayerEventCodec:
    """Decode a lux pub-sub ``(topic, payload)`` into a typed :data:`PlayerEvent`."""

    __slots__ = ()

    def decode(
        self, topic: str, payload: Mapping[str, object], albums: tuple[Album, ...]
    ) -> PlayerEvent:
        """Return the event ``topic`` names, or raise on an unknown topic/payload.

        A ``music.play`` resolves its ``payload['anchor']`` name to an album id
        against ``albums``; ``music.stop`` and the transport topics carry nothing.
        An unknown topic, an empty ``music.play`` payload (the transitional state
        before the lux publish-payload passthrough lands, ``lux-r4pp``: no ``anchor``
        yet, so the click is inert), or an anchor naming no catalogued album raises
        (PY-EH-8), and the subscription boundary logs and drops it.
        """
        match topic:
            case MusicTopic.PLAY:
                return PlayAlbum(self._resolve(payload, albums))
            case MusicTopic.STOP:
                return StopMusic()
            case MusicTopic.PREV:
                return Prev()
            case MusicTopic.NEXT:
                return Next()
            case MusicTopic.PAUSE:
                return Pause()
            case MusicTopic.RESUME:
                return Resume()
            case _:
                msg = f"unknown music topic: {topic!r}"
                raise ValueError(msg)

    def _resolve(
        self, payload: Mapping[str, object], albums: tuple[Album, ...]
    ) -> AlbumId:
        """Return the album id the click's anchor names, resolved via the catalog.

        Reads ``payload['anchor']`` (the selected row's name cell), tolerating and
        ignoring any sibling keys lux carries alongside it (``row_ids``, and shapes
        still settling in ``lux-r4pp``). An absent anchor is the empty-payload
        transitional state -- inert -- and raises plain ``ValueError`` so the
        boundary drops it silently. A well-formed anchor that names no catalogued
        album is a different failure -- the album vanished, or the row cache is
        stale -- and raises :class:`AnchorUnresolvedError` so the boundary can
        surface a transient warning instead of a silent drop.
        """
        anchor = payload.get(ANCHOR_KEY)
        if not isinstance(anchor, str) or not anchor:
            msg = f"music.play payload carries no {ANCHOR_KEY!r} anchor: {payload!r}"
            raise ValueError(msg)
        try:
            return AlbumNames(albums).resolve(anchor).id
        except ValueError as exc:
            raise AnchorUnresolvedError(anchor) from exc
