"""The ``mic`` enable/disable tool: one action-dispatched verb over enablement.

``tool-enable-disable.md`` § 2.14: the Claude Code surface writes the *same*
``.punt-labs/vox/enabled`` marker the ``vox enable`` CLI writes -- one source of
truth, two doors. The tool takes an ``action`` constrained to ``enable`` /
``disable`` (never an ``enabled: bool`` -- that is the retired ``y|n`` vocabulary
wearing a type). Enablement is repo file operations, not daemon-owned audio
state, so this tool acts directly on :class:`~punt_vox.enablement.RepoEnablement`
rather than routing through ``voxd``.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Literal, Self, final

from punt_vox.enablement import RepoEnablement

if TYPE_CHECKING:
    from collections.abc import Callable

__all__ = ["EnablementTool"]


@final
class EnablementTool:
    """Route a ``mic`` enable/disable action to the per-repo enablement machine.

    The enablement source is injected (defaulting to
    :meth:`RepoEnablement.for_cwd`) so a test can bind a temp repo; production
    discovers the repo from the daemon/session working directory.
    """

    __slots__ = ("_source",)

    _source: Callable[[], RepoEnablement]

    def __new__(
        cls, source: Callable[[], RepoEnablement] = RepoEnablement.for_cwd
    ) -> Self:
        self = super().__new__(cls)
        self._source = source
        return self

    def dispatch(self, action: Literal["enable", "disable"]) -> str:
        """Enable or disable vox in the repo the session runs from.

        Returns a JSON object with the action, the repo root, the resulting
        enabled state, and the marker path. A working directory that is not inside
        a git repository is reported as a clean ``error`` object, never an
        exception across the tool boundary.

        Args:
            action: ``"enable"`` writes the guide, marker, import, and settings;
                ``"disable"`` removes the import, marker, and settings, leaving the
                subtree dormant.
        """
        if action not in ("enable", "disable"):
            return self._error(f"invalid action '{action}'. Use enable or disable.")
        try:
            enablement = self._source()
        except ValueError as exc:
            return self._error(str(exc))
        try:
            self._apply(enablement, action)
        except OSError as exc:
            # A filesystem failure (permission-denied, ENOSPC, a racing removal)
            # is reported as a clean error object -- it must never cross the tool
            # boundary as an exception.
            return self._error(str(exc))
        return json.dumps(
            {
                "action": action,
                "repo": str(enablement.root),
                # Report the observed state, not the intent: a disable that could
                # not remove the marker must not claim the repo is off.
                "enabled": enablement.is_enabled(),
                "marker": str(enablement.marker_path),
            }
        )

    @staticmethod
    def _apply(
        enablement: RepoEnablement, action: Literal["enable", "disable"]
    ) -> None:
        """Run the requested enablement transition on *enablement*."""
        if action == "enable":
            enablement.enable()
        else:
            enablement.disable()

    @staticmethod
    def _error(message: str) -> str:
        """Return a JSON error object -- the tool never raises across its boundary."""
        return json.dumps({"error": message})
