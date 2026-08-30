"""What kind of re-push, if any, one applied control event needs.

:meth:`~punt_vox.panel.panel_runner.PanelRunner._applied` answers with one of
these instead of a bare ``bool``: a genuine change and a failure both need a
push, but not the *same* push. A genuine change is real new state, so the
cheap diff against what vox last pushed is correct and complete. A failure
path runs only because the widget already applied the change optimistically
client-side before voxd's answer came back "no" -- the diff sees nothing to
correct, because the last-pushed render vox holds was never wrong. Only a
full reinstall reasserts the control's true field over whatever the widget
guessed.
"""

from __future__ import annotations

from enum import Enum, auto

__all__ = ["ControlPush"]


class ControlPush(Enum):
    """The re-push a control event needs, if any."""

    NONE = auto()
    """Nothing on the scene changed; no push is warranted."""

    REFRESH = auto()
    """Real new state landed; the cheap diff against the last push suffices."""

    CORRECT = auto()
    """A failure left the widget showing a value that never really landed;
    only a full reinstall snaps it back."""
