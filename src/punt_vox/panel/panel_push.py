"""``PanelPush`` -- how the panel's held scene reaches luxd, and with what intent.

Two verbs, because a push carries an intent as well as a tree. :meth:`install`
shows the scene, which raises its frame, and is the answer to the ``Vox`` menu
click -- the user asked to see the panel, so bringing it forward is the point.
:meth:`refresh` writes the changed fields onto the installed scene and touches
frame, focus, and tab state not at all, which is what the confirm push behind a
click and every control-change re-push want: the panel is already on screen, and
where the user put it is where it should stay.

Talking to luxd is a different job from holding the session's settings, so it
lives here rather than on :class:`~punt_vox.panel.service.VoxPanelService` --
the panel's counterpart to the music player's ``LuxScenePublisher``.

The held :class:`LiveScene` needs no lock: every push runs on the panel leg's
event loop, unlike the settings state, which two ``asyncio.to_thread`` workers
can reach at once.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Self, final

from punt_lux import HubUnavailableError

from punt_vox.lux_common import LiveScene

if TYPE_CHECKING:
    from punt_lux import LuxClient, RenderRequest

    from punt_vox.lux_common import ScenePush

__all__ = ["PanelPush"]

logger = logging.getLogger(__name__)


@final
class PanelPush:
    """Send the panel's scene to luxd, installing or refreshing as asked."""

    __slots__ = ("_live",)
    _live: LiveScene

    def __new__(cls) -> Self:
        self = super().__new__(cls)
        self._live = LiveScene()
        return self

    async def install(self, client: LuxClient, request: RenderRequest) -> None:
        """Show ``request`` outright, frame raise and all (a menu click)."""
        await self._complete(client, self._live.install(request))

    async def refresh(self, client: LuxClient, request: RenderRequest) -> None:
        """Carry ``request`` onto the installed scene by the cheapest correct push.

        Nothing at all when the render is unchanged, a field patch when values
        moved, a full install only when the element roster or the frame shell
        changed and no patch could express it.
        """
        await self._complete(client, self._live.plan(request))

    async def _complete(self, client: LuxClient, push: ScenePush) -> None:
        """Complete ``push``, logging (never raising) a refusal.

        A refusal disarms the live scene: luxd kept whatever it had, so what we
        believe is installed is now a guess and the next push must install afresh
        rather than patch a tree that was never accepted. An absent luxd disarms
        for the same reason and propagates, because the caller's outage guard owns
        the throttled reporting of a display that is simply not there.
        """
        try:
            refusal = await push.apply(client)
        except HubUnavailableError:
            self._live.disarm()
            raise
        if refusal is not None:
            self._live.disarm()
            logger.error("vox-panel: luxd rejected the scene: %s", refusal.reason)
