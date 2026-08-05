"""Bind one fill outcome to the Program it was generated for -- the apply-time guard.

A generation that has already settled and posted its outcome can be overtaken in
the single control queue by a :class:`SwitchProgram`: the consumer applies the
switch first (retargeting the channel to a different Program), then applies the
now-stale outcome to *that* Program -- so a switched-in pool gains a Part it never
generated, or a retune inherits the abandoned pool's failure. The single-flight
cancel/discard in :class:`Filler` covers only an *in-flight* generation; it cannot
touch one that finished and enqueued its outcome before the switch drained.

``FreshFillOutcome`` closes that gap. Each posted outcome is tagged with the
Program it was generated under, and the guard drops it at apply time if the
control writer has since retargeted to another Program. Because every switch
installs a *fresh* Program instance (and cancels the prior fill), identity is an
exact tag: the outcome applies iff the writer still animates the very Program the
generation ran for.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, final

if TYPE_CHECKING:
    from punt_vox.voxd.programs.control_signal import ControlSignal
    from punt_vox.voxd.programs.playback_source import PlaybackSource

__all__ = ["FreshFillOutcome"]

logger = logging.getLogger(__name__)


@final
@dataclass(frozen=True, slots=True)
class FreshFillOutcome:
    """Apply a fill outcome only while its origin Program is still the active one."""

    origin: PlaybackSource
    inner: ControlSignal

    @property
    def interrupts(self) -> bool:
        """Delegate to the wrapped outcome -- a fill outcome never interrupts."""
        return self.inner.interrupts

    def apply(self, source: PlaybackSource, /) -> bool:
        """Apply the wrapped outcome, unless the writer has switched sources.

        ``source`` is the writer's *current* source; ``origin`` is the Program
        the generation ran for. A mismatch means a switch overtook this outcome
        in the queue (to another Program or a replay Selection), so the outcome
        is an orphan of an abandoned pool and is dropped (``False``) rather than
        polluting the switched-in source.
        """
        if source is not self.origin:
            logger.info(
                "dropped a stale fill outcome: its source was switched away "
                "before the control writer could apply it"
            )
            return False
        return self.inner.apply(source)
