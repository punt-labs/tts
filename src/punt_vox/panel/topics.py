"""``PanelTopic`` -- the pub-sub topics the panel's controls publish to.

Shared by both legs -- the scene declares one of these on each control's
``handlers``/``publish`` field, and :class:`~punt_vox.panel.leg.VoxPanelLeg`
subscribes to every one -- so a rename can never drift one out of step with
the other.

Two names hang off each topic besides its wire value: the human label a notice
shows, and the config field it commits. The label table is total and the field
table is not, so :attr:`PanelTopic.writes_field` exists to be asked before
:attr:`PanelTopic.field_name`. Without it a caller could ask a preview for the
field it writes and take a lookup failure on a path whose whole job was
reporting some other failure.
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
    def writes_field(self) -> bool:
        """Return whether this topic's control commits a config field.

        The one topic that does not is ``VOICE_PREVIEW``, which plays the
        held voice and commits nothing. Callers reaching for
        :attr:`field_name` on a failure path must ask this first: a failure
        while previewing has no field to revert, so recovering "the field
        that did not stick" is not an operation that exists there.
        """
        return self in _FIELD_NAMES

    @property
    def field_name(self) -> str:
        """Return the config field this topic's control writes.

        Distinct from the wire value (``"vox.notify"``): user-facing text
        (a rejected-write notice, a log line) names the field
        (``"notify"``), never the wire topic. Only the field-writing topics
        answer here -- asking ``VOICE_PREVIEW`` is a programmer error, since
        it commits nothing. Guard with :attr:`writes_field` wherever the
        topic is not statically known.
        """
        return _FIELD_NAMES[self]

    @property
    def label(self) -> str:
        """Return the human name for this topic, for text a user reads.

        Total where :attr:`field_name` is partial: every topic can appear
        in a notice, including ``VOICE_PREVIEW``, which has no config field
        to be named after. Reading a wire topic back to a user
        (``"that vox.model change was refused"``) leaks an identifier that
        means nothing outside this codebase.
        """
        return _LABELS[self]


_FIELD_NAMES: Final[dict[PanelTopic, str]] = {
    PanelTopic.NOTIFY: "notify",
    PanelTopic.MIC_MODE: "speak",
    PanelTopic.VOICE: "voice",
    PanelTopic.PROVIDER: "provider",
    PanelTopic.MODEL: "model",
}

_LABELS: Final[dict[PanelTopic, str]] = {
    PanelTopic.NOTIFY: "notification",
    PanelTopic.MIC_MODE: "mic mode",
    PanelTopic.VOICE: "voice",
    PanelTopic.VOICE_PREVIEW: "voice preview",
    PanelTopic.PROVIDER: "provider",
    PanelTopic.MODEL: "model",
}
