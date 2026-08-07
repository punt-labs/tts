"""``VoxPanelProgram`` -- the panel's life: claim the session, serve it, leave with it.

``punt_lux.applets.AppletProgram`` is typed against the concrete, ``@final``
``AppletLeg``, so it cannot host a leg that also subscribes to pub-sub topics
(see :mod:`punt_vox.panel.leg`). This mirrors ``AppletProgram``'s shape
exactly, built against ``punt_lux``'s own ``AppletClaim``/``SessionEnd``
protocols so a ``SessionClaim`` and a ``SessionWatch`` -- or their
``NoClaim``/``NoSession`` stand-ins -- plug in unchanged.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Self, final

if TYPE_CHECKING:
    from punt_lux.applets.claim import AppletClaim
    from punt_lux.applets.watch import SessionEnd

    from punt_vox.panel.leg import VoxPanelLeg

__all__ = ["VoxPanelProgram"]


@final
class VoxPanelProgram:
    """The panel's three moments: the claim it takes, its leg, and its end."""

    _claim: AppletClaim
    _leg: VoxPanelLeg
    _watch: SessionEnd
    __slots__ = ("_claim", "_leg", "_watch")

    def __new__(cls, claim: AppletClaim, leg: VoxPanelLeg, watch: SessionEnd) -> Self:
        self = super().__new__(cls)
        self._claim = claim
        self._leg = leg
        self._watch = watch
        return self

    async def run(self) -> None:
        """Serve the entry until the session ends, then stop serving.

        A refused claim returns before the leg has run: an applet that is not
        this session's must not so much as connect, because connecting under
        an identity another applet holds is what takes that applet's
        callbacks away.
        """
        if not self._claim.take():
            return
        leg = asyncio.create_task(self._leg.serve())
        try:
            await self._watch.until_session_ends()
        finally:
            leg.cancel()
            await asyncio.gather(leg, return_exceptions=True)
