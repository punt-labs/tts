"""The three ways one planned scene push completes: install, patch, or nothing.

``show`` and ``update`` are not two spellings of one operation. ``show`` installs
the whole tree, which is what a user gesture -- clicking the menu entry that
opens the window -- asks for. ``update`` writes fields onto an installed tree
and touches frame, focus, and tab state not at all, which is what a refresh of a
window the user is already looking at asks for. Conflating them is what makes a
widget steal focus every time a number in it changes.

``show`` does NOT reliably raise or unminimize the frame on its own (DES-072
addendum): the Hub only clears a frame's minimized state and grabs focus when
the scene is genuinely new to that frame, and both of vox's scenes stay
installed across the session, so by the second install ``show`` alone raises
nothing. A caller that means "bring this window to me" -- a menu click -- makes
an explicit ``client.frame.raise_`` call alongside the install; see
:class:`~punt_vox.voxd.music_player.lux_scene_publisher.LuxScenePublisher` and
:class:`~punt_vox.panel.panel_push.PanelPush` for where that call lives.

Each push knows how to complete itself, so the caller has no three-way branch:
:class:`InstallScene` awaits ``show``; :class:`PatchScene` awaits ``update`` and,
when luxd refuses the batch, completes itself by installing the same request --
safe because a rejected batch mutates nothing, so nothing partial is left behind;
:class:`NoPush` awaits nothing at all, which is the common case on a change signal
that altered nothing the widget shows.

``apply`` returns luxd's refusal or ``None`` for success, and never raises on a
refusal. A :class:`HubUnavailableError` *does* propagate: an absent display is the
caller's business, because the caller owns the client it has to drop.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol, final

from punt_lux import OpError
from punt_lux.operations import UpdateRequest

if TYPE_CHECKING:
    from punt_lux import LuxClient, RenderRequest

    from punt_vox.lux_common.scene_patch import ScenePatchSet

__all__ = ["InstallScene", "NoPush", "PatchScene", "ScenePush"]

logger = logging.getLogger(__name__)


class ScenePush(Protocol):
    """One planned push, knowing how to put itself on the wire (PY-DP-11).

    Structural only, deliberately not ``runtime_checkable``: nothing isinstances
    a push, and the decorator could not check this shape honestly if anything
    did -- it verifies member *names*, not that ``apply`` is a coroutine or that
    ``summary`` is a property, which is the whole contract here.
    """

    async def apply(self, client: LuxClient) -> OpError | None:
        """Complete the push; return luxd's refusal, or ``None`` on success.

        ``None`` is the documented success contract, not a swallowed failure --
        a refusal is returned for the caller to log, and an absent luxd raises
        :class:`HubUnavailableError` straight through.
        """
        ...

    @property
    def summary(self) -> str:
        """Return the one-line description of what this push put on the wire."""
        ...


@final
@dataclass(frozen=True, slots=True)
class InstallScene:
    """Install (or replace) the whole tree -- ``show``.

    Raising the frame is NOT a reliable side effect of this push (DES-072
    addendum): the caller that means "bring this window to me" makes its own
    explicit ``client.frame.raise_`` call alongside it.
    """

    request: RenderRequest

    async def apply(self, client: LuxClient) -> OpError | None:
        """Install the whole scene, returning luxd's refusal if it declined."""
        result = await client.scene.show(self.request)
        return result if isinstance(result, OpError) else None

    @property
    def summary(self) -> str:
        """Return the install line: the scene installed and how big it was."""
        return (
            f"installed {self.request.scene_id} scene "
            f"({len(self.request.elements)} elements)"
        )


@final
@dataclass(frozen=True, slots=True)
class PatchScene:
    """Write the changed fields onto the installed tree, leaving the frame alone."""

    request: RenderRequest
    patches: ScenePatchSet

    async def apply(self, client: LuxClient) -> OpError | None:
        """Patch the installed scene, re-installing it if luxd refuses the batch.

        A refused batch mutates nothing luxd holds, so the fall back to a full
        install is always safe -- and it is also the recovery path for a luxd that
        restarted underneath us, where the first patch meets an unknown scene.
        """
        request = UpdateRequest.parse(self.patches.to_wire())
        result = await client.scene.update(self.request.scene_id, request)
        if not isinstance(result, OpError):
            return None
        logger.warning(
            "luxd refused the %s scene patch (%s); re-installing the whole scene",
            self.request.scene_id,
            result.reason,
        )
        return await InstallScene(self.request).apply(client)

    @property
    def summary(self) -> str:
        """Return the patch line: how many elements were written, and where."""
        return (
            f"patched {len(self.patches)} elements of the {self.request.scene_id} scene"
        )


@final
@dataclass(frozen=True, slots=True)
class NoPush:
    """The Null Object: the render is byte-identical, so nothing goes on the wire."""

    scene_id: str

    async def apply(self, client: LuxClient) -> OpError | None:
        """Do nothing and report success; the installed scene is already correct.

        The client goes untouched on purpose -- that is the whole point of the
        null push, and it is why a change signal that alters nothing the widget
        shows costs zero bytes on the wire.
        """
        _ = client
        return None

    @property
    def summary(self) -> str:
        """Return the quiet line: the scene was already what the render says."""
        return f"{self.scene_id} scene unchanged; nothing pushed"
