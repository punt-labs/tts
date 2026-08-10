"""``PanelTopic`` -- the pub-sub topics the panel's controls publish to.

Shared by both legs -- the scene declares one of these on each control's
``handlers``/``publish`` field, and :class:`~punt_vox.panel.leg.VoxPanelLeg`
subscribes to every one -- so a rename can never drift one out of step with
the other.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Final

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
    PROVIDER = "vox.provider"
    MODEL = "vox.model"

    @property
    def field_name(self) -> str:
        """Return the config field this topic's control writes.

        Distinct from the wire value (``"vox.notify"``): user-facing text
        (a rejected-write notice, a log line) names the field
        (``"notify"``), never the wire topic. Only the field-writing topics
        answer here -- calling this on ``VOICE_PREVIEW`` is a programmer
        error, since it commits nothing.
        """
        return _FIELD_NAMES[self]


_FIELD_NAMES: Final[dict[PanelTopic, str]] = {
    PanelTopic.NOTIFY: "notify",
    PanelTopic.MIC_MODE: "speak",
    PanelTopic.VOICE: "voice",
    PanelTopic.PROVIDER: "provider",
    PanelTopic.MODEL: "model",
}
