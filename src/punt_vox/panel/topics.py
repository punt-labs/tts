"""``PanelTopic`` -- the pub-sub topics the panel's controls publish to.

Shared by both legs -- the scene declares one of these on each control's
``handlers``/``publish`` field, and :class:`~punt_vox.panel.leg.VoxPanelLeg`
subscribes to every one -- so a rename can never drift one out of step with
the other.
"""

from __future__ import annotations

from enum import StrEnum

__all__ = ["PanelTopic"]


class PanelTopic(StrEnum):
    """Topics the ``vox.panel`` scene publishes, and the leg subscribes to.

    ``NOTIFY``, ``MIC_MODE``, and ``VOICE`` mirror the config fields they
    write -- ``notify``/``speak``/``voice`` in vox's own config store, the
    same fields the ``mic:notify``/``mic:speak`` MCP tools and the
    ``vox voice``/``vox voices`` CLI commands already read and write.
    ``VOICE_PREVIEW`` has no config counterpart: it plays the held voice back
    without committing anything.
    """

    NOTIFY = "vox.notify"
    MIC_MODE = "vox.speak"
    VOICE = "vox.voice"
    VOICE_PREVIEW = "vox.voice.preview"
