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


@runtime_checkable
class ScenePresenter(ChangeListener, FailurePresenter, Protocol):
    """The receive leg's full scene sink: a quiet re-push, or a failure warning.

    It unites the :class:`ChangeListener` re-push (a menu open, a reconnect) with the
    :class:`FailurePresenter` warnings, so the subscription holds one object that both
    repaints on demand and surfaces a click that could not be applied.
    """
