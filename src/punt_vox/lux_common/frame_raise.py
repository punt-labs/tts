"""``FrameRaiser`` -- the explicit ``raise_frame`` call an install makes after showing.

``client.scene.show`` does NOT reliably raise or unminimize the frame it targets
(DES-072 addendum): the Hub's ``upsert_scene_in_frame`` only clears a frame's
minimized state and grabs focus when the scene is *new* to it, and both
``vox.music`` and ``vox.panel`` stay permanently installed on their
``LiveScene`` once the first connection lands -- so by the time a real menu
click reinstalls one, the scene is never new and ``show``'s raise never runs.

This is the call that actually surfaces a minimized or buried window, shared by
:class:`~punt_vox.voxd.music_player.lux_scene_publisher.LuxScenePublisher` and
:class:`~punt_vox.panel.panel_push.PanelPush` so the resolve-and-raise-and-log
sequence is written once rather than duplicated per caller. It never raises: a
failed raise leaves the scene content pushed but the window wherever it was --
a lesser miss than losing the whole click -- and is reported through the
caller's own ``warn`` callback instead, so each caller's log line keeps its own
prefix and level convention (``[lux] ...`` for the music player, ``vox-panel:
...`` for the panel).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Self, final

from punt_lux import HubUnavailableError, OpError

from punt_vox.lux_common.frame_id import FrameId

if TYPE_CHECKING:
    from collections.abc import Callable

    from punt_lux import LuxClient, RenderRequest

__all__ = ["FrameRaiser"]


@final
class FrameRaiser:
    """Resolve a request's frame id and raise it; report failures via ``warn``."""

    __slots__ = ("_warn",)
    _warn: Callable[[str], None]

    def __new__(cls, warn: Callable[[str], None]) -> Self:
        self = super().__new__(cls)
        self._warn = warn
        return self

    async def raise_frame(self, client: LuxClient, request: RenderRequest) -> None:
        """Raise the frame ``request`` was just pushed onto.

        Never raises on failure -- a refusal, an absent luxd, or a frame the
        display does not hold are each reported through ``warn`` instead.
        """
        frame_id = str(FrameId(request))
        try:
            result = await client.frame.raise_(frame_id)
        except HubUnavailableError:
            self._warn(f"luxd unavailable; could not raise the {frame_id} frame")
            return
        if isinstance(result, OpError):
            self._warn(f"luxd refused to raise the {frame_id} frame: {result.reason}")
            return
        if not result.raised:
            self._warn(f"luxd holds no {frame_id} frame to raise")
