"""``LuxScenePublisher`` -- drain the scene mailbox and push through the LuxClient.

:meth:`submit` and :meth:`reinstall` run on the control-channel single-writer and
only hand the newest scene to a latest-wins :class:`SceneMailbox` -- neither ever
blocks. :meth:`run` is the publisher's own task: it drains the mailbox and awaits
the push, which is natively async on the ``LuxClient`` facade, so a slow luxd
cannot stall the event loop (and thus playback).

What goes on the wire is :class:`LiveScene`'s decision, not this module's. A
refresh whose render is byte-identical to the installed one costs nothing; one
whose values moved is a field patch that leaves the frame's stacking order alone;
one whose element roster or frame shell changed re-installs, because no patch can
express that. :meth:`reinstall` is the other intent entirely -- the Music menu was
clicked, or a hub handshake says nothing is installed -- and this publisher backs
it with an *explicit* ``client.frame.raise_`` call once the push lands, because
``scene.show`` only raises/unminimizes a frame the scene is genuinely new to
(DES-072 addendum): the scene stays installed on this ``LiveScene`` across every
menu click after the first, so by the second click ``show`` alone no longer
raises anything, and the frame is left wherever the window manager last put it.

A lux timeout / :class:`HubUnavailableError` is logged, dropped, and disarms the
live scene, so the fresh connection installs rather than patching a scene the new
luxd never saw. An engine-side ``OpError`` -- a scene luxd refused, almost always
a projection defect rather than an absent display -- is logged at error. No lux
failure is propagated back into audio control. The explicit raise is
best-effort in the same spirit: a refused or unreachable raise is logged and
swallowed, never allowed to look like a playback failure.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Self, final

from punt_lux import HubUnavailableError

from punt_vox.lux_common import FrameRaiser, LiveScene
from punt_vox.voxd.music_player.lux_trace import LuxTrace
from punt_vox.voxd.music_player.scene_mailbox import SceneMailbox

if TYPE_CHECKING:
    from collections.abc import Callable

    from punt_lux import LuxClient, RenderRequest

    from punt_vox.lux_common import ScenePush
    from punt_vox.voxd.music_player.scene_mailbox import SceneDelivery

__all__ = ["LuxScenePublisher"]

logger = logging.getLogger(__name__)
_trace = LuxTrace(logger)


@final
class LuxScenePublisher:
    """Own the scene mailbox and render each newest scene to luxd on its own task."""

    __slots__ = ("_client", "_connect", "_live", "_mailbox", "_raiser")
    _connect: Callable[[], LuxClient]
    _client: LuxClient | None  # None until first connect / after a drop
    _mailbox: SceneMailbox
    _live: LiveScene
    _raiser: FrameRaiser

    def __new__(cls, connect: Callable[[], LuxClient]) -> Self:
        self = super().__new__(cls)
        self._connect = connect
        self._client = None
        self._mailbox = SceneMailbox()
        self._live = LiveScene()
        self._raiser = FrameRaiser(lambda msg: _trace.warning("%s", msg))
        return self

    def submit(self, request: RenderRequest) -> None:
        """Hand the newest scene to the mailbox -- non-blocking, writer-safe."""
        self._mailbox.submit(request)

    def reinstall(self, request: RenderRequest) -> None:
        """Hand over a scene that must be installed, frame raise and all."""
        self._mailbox.reinstall(request)

    async def run(self) -> None:
        """Drain the mailbox forever, rendering each newest scene to luxd.

        The per-scene guard is the last line of defence: any unexpected error
        rendering one scene is logged and the loop continues, so a display fault
        never kills the publisher task (and playback is untouched regardless).
        """
        while True:
            delivery = await self._mailbox.get()
            try:
                await self._publish(delivery)
            except Exception:
                # Disarm like the outage and refusal paths inside _publish do:
                # an unexpected fault here leaves no guarantee about what
                # actually landed on luxd, so the next push must install
                # afresh rather than patch against a tree that may never have
                # been accepted.
                self._live.disarm()
                logger.exception(
                    "[lux] scene publisher: unexpected error rendering a scene"
                )

    async def _publish(self, delivery: SceneDelivery) -> None:
        """Connect if needed and complete the planned push, dropping any lux failure."""
        request = delivery.request
        try:
            client = self._ensure_client()
            push = self._plan(delivery)
            refusal = await push.apply(client)
        except HubUnavailableError:
            self._client = None  # force a reconnect on the next scene
            self._live.disarm()  # ... which must install, not patch
            _trace.warning("luxd unavailable; dropped %s scene push", request.scene_id)
            return
        if refusal is not None:
            # A refused scene is a projection defect, not an absent display: log
            # at error so it reads distinctly from the down-luxd warning above.
            # Disarm too -- luxd kept whatever it had, so what we believe is
            # installed is now a guess, and the next push must install afresh
            # rather than patch against a tree that was never accepted.
            self._live.disarm()
            _trace.error("rejected %s scene: %s", request.scene_id, refusal.reason)
            return
        _trace.info("%s", push.summary)
        if delivery.install:
            await self._raiser.raise_frame(client, request)

    def _plan(self, delivery: SceneDelivery) -> ScenePush:
        """Return the push this delivery asked for: a demanded install, or the plan."""
        if delivery.install:
            return self._live.install(delivery.request)
        return self._live.plan(delivery.request)

    def _ensure_client(self) -> LuxClient:
        """Return the connected client, building the facade on first use."""
        if self._client is None:
            self._client = self._connect()
        return self._client
