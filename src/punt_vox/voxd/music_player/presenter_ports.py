"""The scene-sink seams the receive leg drives: a quiet re-push, or a failure warning.

The player's *read* seam is :class:`~punt_vox.voxd.music_player.ports.PlayerService`
and its scene *sink* is
:class:`~punt_vox.voxd.music_player.ports.ScenePublisher`. This module adds the two
seams a *failed* click needs: :class:`FailurePresenter`, the sink an inbound event
drives when its ``apply`` raised, and :class:`ScenePresenter`, the union the
subscription holds so one object both repaints on demand and surfaces a click that
could not be applied. They live apart from ``ports`` so each module carries one
concern (PY-IC-6).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, runtime_checkable

from punt_vox.voxd.programs.change_listener import ChangeListener

if TYPE_CHECKING:
    from punt_vox.voxd.programs.album_id import AlbumId

__all__ = ["FailurePresenter", "ScenePresenter"]


@runtime_checkable
class FailurePresenter(Protocol):
    """The sink an inbound event drives when its apply could not run.

    The receive leg catches a Play/Stop whose ``apply`` raised and sends the event a
    message (double dispatch), so each event names its own failure rather than the
    subscription branching on the topic (oo.md *Polymorphism Over Conditionals*).
    """

    def present_play_failure(self, album: AlbumId) -> None:
        """Re-project the scene with a warning that ``album`` could not play."""
        ...

    def present_stop_failure(self) -> None:
        """Re-project the scene with a warning that the stop could not apply."""
        ...

    def present_resolve_failure(self, anchor: str) -> None:
        """Re-project the scene with a warning that ``anchor`` names no album.

        The click carried a well-formed name but the catalog no longer holds it
        -- a vanished album, or a stale row-cache click. Parallel to
        :meth:`present_play_failure`, which names an already-resolved album that
        refused to play; here nothing resolved, so the warning names the anchor
        text the user clicked, not an album id.
        """
        ...

    def present_transport_failure(self) -> None:
        """Re-project the scene after a transport control (prev/next/pause/resume).

        The transport commands mutate no album, so a refusal names no album to warn
        about; a plain re-push keeps the scene truthful to the settled daemon state
        rather than leaving a click looking silently ignored (client-observable).
        """
        ...


@runtime_checkable
class ScenePresenter(ChangeListener, FailurePresenter, Protocol):
    """The receive leg's full scene sink: repaint, install, or warn.

    It unites the :class:`ChangeListener` quiet repaint with the
    :class:`FailurePresenter` warnings, so the subscription holds one object that
    both refreshes on demand and surfaces a click that could not be applied --
    plus :meth:`install`, the verb for the two moments that mean "put this window
    in front of the user" rather than "the numbers in it changed".
    """

    async def install(self) -> None:
        """Project the scene and install it, raising its frame.

        The Music menu was clicked, or a hub handshake reports a connection with
        nothing on it. Both want the frame shown; every other trigger deliberately
        does not, because raising a window the user parked behind another one is
        the defect this verb exists to keep out of the refresh path. Async only to
        satisfy this Protocol -- the concrete presenter never awaits here, so a
        session lease is never held up by an inline disk read.
        """
        ...
