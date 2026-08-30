"""``PanelPush`` -- how the panel's held scene reaches luxd, and with what intent.

Two verbs, because a push carries an intent as well as a tree. :meth:`install`
shows the scene and then EXPLICITLY raises its frame, and is the answer to the
``Vox`` menu click -- the user asked to see the panel, so bringing it forward is
the point. The explicit raise matters because ``scene.show`` only
raises/unminimizes a frame the scene is genuinely new to (DES-072 addendum): the
panel scene stays installed on this object's ``LiveScene`` after the first
click, so every later click would otherwise call ``show`` against a frame that
already holds the scene and get no raise at all. :meth:`refresh` writes the
changed fields onto the installed scene and touches frame, focus, and tab state
not at all, which is what the confirm push behind a click and every
control-change re-push want: the panel is already on screen, and where the user
put it is where it should stay.

Talking to luxd is a different job from holding the session's settings, so it
lives here rather than on :class:`~punt_vox.panel.service.VoxPanelService` --
the panel's counterpart to the music player's ``LuxScenePublisher``.

**The pushes are serialized, and must be.** Planning a push reads the last
render and claims the new one as installed; applying it is a separate await.
Between those two the event loop is free to run another push, and the panel's
leg hands every menu click and every control event to its own bare
``asyncio.create_task`` (:meth:`~punt_vox.panel.leg.VoxPanelLeg._start`) with no
ordering between them. One event loop is not one at a time.

Left unserialized, two pushes interleave badly: the second diffs against a tree
the first has claimed but not yet landed, and if the two reach luxd out of order
the screen ends up holding one render while this object believes another is
installed. Every later diff is then computed against the wrong base, so a field
that really changed is silently skipped -- and nothing heals it, because each
subsequent push looks locally consistent. A lock held across plan-and-apply is
what makes the claim honest: no push may observe ``_previous`` between another's
plan and its confirmation.

The music player needs no such lock: ``LuxScenePublisher`` drains its mailbox
from a single task and awaits each push to completion before taking the next, so
its pushes are serialized by construction rather than by a lock.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Self, final

from punt_vox.lux_common import FrameRaiser, LiveScene

if TYPE_CHECKING:
    from punt_lux import LuxClient, RenderRequest

    from punt_vox.lux_common import ScenePush

__all__ = ["PanelPush"]

logger = logging.getLogger(__name__)


@final
class PanelPush:
    """Send the panel's scene to luxd, installing or refreshing as asked."""

    __slots__ = ("_live", "_lock", "_raiser")
    _live: LiveScene
    # Held across plan-and-apply, never merely around the plan: the window the
    # lock exists to close is the await between claiming a render and landing it.
    _lock: asyncio.Lock
    _raiser: FrameRaiser

    def __new__(cls) -> Self:
        self = super().__new__(cls)
        self._live = LiveScene()
        self._lock = asyncio.Lock()
        self._raiser = FrameRaiser(lambda msg: logger.warning("vox-panel: %s", msg))
        return self

    async def install(self, client: LuxClient, request: RenderRequest) -> None:
        """Show ``request`` and then explicitly raise its frame (a menu click).

        ``show`` alone only raises/unminimizes a frame the scene is new to; the
        panel scene stays installed on this object's ``LiveScene`` past the
        first click, so ``show``'s raise stops firing right when a later click
        needs it most. The explicit :class:`~punt_vox.lux_common.FrameRaiser`
        call below is what actually brings a minimized or buried window forward
        (DES-072 addendum).
        """
        async with self._lock:
            landed = await self._complete(client, self._live.install(request))
        if landed:
            await self._raiser.raise_frame(client, request)

    async def refresh(self, client: LuxClient, request: RenderRequest) -> None:
        """Carry ``request`` onto the installed scene by the cheapest correct push.

        Nothing at all when the render is unchanged, a field patch when values
        moved, a full install only when the element roster or the frame shell
        changed and no patch could express it.
        """
        async with self._lock:
            await self._complete(client, self._live.plan(request))

    async def _complete(self, client: LuxClient, push: ScenePush) -> bool:
        """Complete ``push``, logging a refusal; return whether it landed.

        A refusal disarms the live scene: luxd kept whatever it had, so what we
        believe is installed is now a guess and the next push must install afresh
        rather than patch a tree that was never accepted. Any exception out of
        ``push.apply`` -- an absent luxd (``HubUnavailableError``, whose throttled
        reporting is the caller's outage guard's job) or anything unexpected --
        disarms for the identical reason: there is no guarantee what, if anything,
        actually landed. Caught as one clause, not one per exception type, because
        the action is the same regardless of which fault it was; each then
        propagates to let its own caller decide what the fault means.
        """
        try:
            refusal = await push.apply(client)
        except Exception:
            self._live.disarm()
            raise
        if refusal is not None:
            self._live.disarm()
            logger.error("vox-panel: luxd rejected the scene: %s", refusal.reason)
            return False
        return True
