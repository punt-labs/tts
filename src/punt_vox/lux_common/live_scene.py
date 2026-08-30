"""``LiveScene`` -- what one surface believes is on screen, and how to change it.

A surface pushes its scene many times a second's worth of state changes; almost
none of those pushes need to *install* anything. :meth:`plan` compares the render
just built against the one this scene last put out and answers with the cheapest
push that is still correct: nothing at all when the two renders are identical,
a field patch when only values moved, a full install when the element roster or
the frame shell changed and no patch could express it.

:meth:`install` is the other door, and it is a different intent rather than a
different mechanism: the user clicked the menu entry that opens this window, or a
hub handshake just told us nothing is installed. Both mean *put this in front of
me*, so both take a full :class:`InstallScene` push unconditionally -- but
``show`` on its own does NOT reliably raise the frame (DES-072 addendum): the
Hub only clears the frame's minimized state and grabs focus when the scene is
new to it, and this scene stays installed past the first push, so every later
``show`` finds the scene already there and raises nothing. The caller that owns
this object is the one that must explicitly raise the frame after the push
lands.

One field, ``_previous``, is the whole state machine. ``None`` means nothing is
installed on the current connection; :meth:`disarm` restores that after luxd goes
away, so the first push across the fresh connection installs rather than patching
a scene the new luxd has never seen.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Self, final

from punt_vox.lux_common.scene_patch import SceneTree
from punt_vox.lux_common.scene_push import InstallScene, NoPush, PatchScene

if TYPE_CHECKING:
    from punt_lux import RenderRequest

    from punt_vox.lux_common.scene_push import ScenePush

__all__ = ["LiveScene"]


@final
class LiveScene:
    """Hold the last render pushed, and plan the cheapest correct next push."""

    __slots__ = ("_previous",)
    # PY-TS-14: absence is the state itself -- ``None`` is "nothing installed on
    # this connection", the initial state and the one ``disarm`` returns to.
    _previous: RenderRequest | None

    def __new__(cls) -> Self:
        self = super().__new__(cls)
        self._previous = None
        return self

    @property
    def armed(self) -> bool:
        """Return whether a scene is believed installed on the current connection."""
        return self._previous is not None

    def plan(self, request: RenderRequest) -> ScenePush:
        """Return the cheapest push carrying ``request``, and hold it as installed."""
        push = self._push_for(request)
        self._previous = request
        return push

    def install(self, request: RenderRequest) -> ScenePush:
        """Return a full install of ``request`` -- the bring-this-window-to-me verb."""
        self._previous = request
        return InstallScene(request)

    def disarm(self) -> None:
        """Forget the installed scene, so the next push installs it afresh."""
        self._previous = None

    def _push_for(self, request: RenderRequest) -> ScenePush:
        """Choose between installing, patching, and doing nothing for ``request``."""
        previous = self._previous
        if previous is None or not self._same_shell(previous, request):
            return InstallScene(request)
        current, before = SceneTree.of(request), SceneTree.of(previous)
        if not current.patchable_against(before):
            return InstallScene(request)
        patches = current.patches_against(before)
        if not patches:
            return NoPush(request.scene_id)
        return PatchScene(request, patches)

    @staticmethod
    def _same_shell(previous: RenderRequest, request: RenderRequest) -> bool:
        """Return whether the two renders share a frame shell a patch cannot move.

        The scene id, title, layout, and frame spec are installed with the tree and
        have no patch seam of their own, so any difference in them is an install.
        """
        return (
            previous.scene_id == request.scene_id
            and previous.title == request.title
            and previous.layout == request.layout
            and previous.frame == request.frame
        )
