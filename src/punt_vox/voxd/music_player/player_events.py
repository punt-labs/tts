"""Inbound player events -- the receive leg's discriminated command union + codec.

A ``music.play`` or ``music.stop`` frame from an in-scene button decodes into one
:class:`PlayAlbum` or :class:`StopMusic`. Each knows how to :meth:`apply` itself to
the daemon -- ``PlayAlbum`` replays its album, ``StopMusic`` turns the source off --
so the subscription dispatches by sending the event a message, never an ``if``-ladder
on the topic (oo.md *Polymorphism Over Conditionals*, PY-OO-6). This mirrors the Z
model's total ``dispatch``: ``playEvent -> startRadio``, ``stopEvent -> radioOff``
(``docs/vox-music-player.tex`` invariant V).

:class:`PlayerEventCodec` is the boundary parser: it turns a raw ``(topic, payload)``
into a typed event, validating the album id's shape, and raises on anything it does
not recognise (PY-EH-8) so a malformed frame never reaches playback as a silent no-op.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, final

from punt_vox.voxd.music_player.wire import ALBUM_ID_KEY, MusicTopic
from punt_vox.voxd.programs.album_id import AlbumId

if TYPE_CHECKING:
    from collections.abc import Mapping

    from punt_vox.voxd.music_player.command_ports import PlayerCommands
    from punt_vox.voxd.music_player.presenter_ports import FailurePresenter

__all__ = [
    "Next",
    "Pause",
    "PlayAlbum",
    "PlayerEvent",
    "PlayerEventCodec",
    "Prev",
    "Resume",
    "StopMusic",
]


@final
@dataclass(frozen=True, slots=True)
class PlayAlbum:
    """Play one saved album -- ``music.play`` projected onto ``replay_album``."""

    album: AlbumId

    def apply(self, service: PlayerCommands) -> None:
        """Replay this album, displacing whatever was playing (Z model StartRadio)."""
        service.replay_album(self.album)

    def surface_failure(self, presenter: FailurePresenter) -> None:
        """Ask the scene to warn that this album could not play (double dispatch).

        A play the user clicked can fail in :meth:`apply` -- the album vanished from
        the crate, or has no ready tracks -- without changing daemon state, so nothing
        else re-pushes the scene. The event names its own failure here rather than the
        subscription branching on the topic (PY-OO-6).
        """
        presenter.present_play_failure(self.album)


@final
@dataclass(frozen=True, slots=True)
class StopMusic:
    """Stop the active source -- the projection of ``music.stop`` onto ``off``."""

    def apply(self, service: PlayerCommands) -> None:
        """Turn the active source off, returning to idle (Z model RadioOff)."""
        service.off()

    def surface_failure(self, presenter: FailurePresenter) -> None:
        """Ask the scene to warn that the stop could not apply (double dispatch)."""
        presenter.present_stop_failure()


@final
@dataclass(frozen=True, slots=True)
class Prev:
    """Transport prev -- ``music.prev`` projected onto the deterministic step-back."""

    def apply(self, service: PlayerCommands) -> None:
        """Step the replay cursor back one part (Z ``Prev``)."""
        service.prev()

    def surface_failure(self, presenter: FailurePresenter) -> None:
        """Re-push the scene after a refused prev (names no album; double dispatch)."""
        presenter.present_transport_failure()


@final
@dataclass(frozen=True, slots=True)
class Next:
    """Transport next -- ``music.next`` projected onto the deterministic step."""

    def apply(self, service: PlayerCommands) -> None:
        """Step the replay cursor forward one part (Z ``Next``)."""
        service.advance()

    def surface_failure(self, presenter: FailurePresenter) -> None:
        """Re-push the scene after a refused next (names no album; double dispatch)."""
        presenter.present_transport_failure()


@final
@dataclass(frozen=True, slots=True)
class Pause:
    """Transport pause -- ``music.pause`` projected onto the suspend transition."""

    def apply(self, service: PlayerCommands) -> None:
        """Suspend the active source in place (Z ``Pause``)."""
        service.pause()

    def surface_failure(self, presenter: FailurePresenter) -> None:
        """Re-push the scene after a refused pause (names no album; double dispatch)."""
        presenter.present_transport_failure()


@final
@dataclass(frozen=True, slots=True)
class Resume:
    """Transport resume -- ``music.resume`` projected onto the resume transition."""

    def apply(self, service: PlayerCommands) -> None:
        """Continue the suspended source (Z ``Resume``)."""
        service.resume()

    def surface_failure(self, presenter: FailurePresenter) -> None:
        """Re-push the scene after a refused resume (names no album; dispatch)."""
        presenter.present_transport_failure()


type PlayerEvent = PlayAlbum | StopMusic | Prev | Next | Pause | Resume


@final
class PlayerEventCodec:
    """Decode a lux pub-sub ``(topic, payload)`` into a typed :data:`PlayerEvent`."""

    __slots__ = ()

    def decode(self, topic: str, payload: Mapping[str, object]) -> PlayerEvent:
        """Return the event ``topic`` names, or raise on an unknown topic/payload.

        A ``music.play`` must carry a well-formed string album id; a ``music.stop``
        carries nothing. Any other topic, or a play missing its id, is a malformed
        frame the caller must not treat as a no-op -- so this raises rather than
        returning ``None`` (PY-EH-8), and the subscription boundary logs and drops it.
        """
        match topic:
            case MusicTopic.PLAY:
                return PlayAlbum(self._album_id(payload))
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

    def _album_id(self, payload: Mapping[str, object]) -> AlbumId:
        """Return the payload's album id, validating its hex shape at the boundary."""
        raw = payload.get(ALBUM_ID_KEY)
        if not isinstance(raw, str):
            msg = f"music.play payload missing a string {ALBUM_ID_KEY!r}: {payload!r}"
            raise ValueError(msg)
        return AlbumId(raw)
