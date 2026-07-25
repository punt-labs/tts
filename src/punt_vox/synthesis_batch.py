"""Render a list of speech segments against a shared synthesis default.

The multi-voice ``unmute``/``record`` tools accept a ``segments`` list where each
segment may override ``voice``, ``language``, and ``vibe_tags`` on top of the
call-level :class:`~punt_vox.types_synthesis.SynthesisSpec`.  :class:`SegmentBatch`
owns that per-segment override logic and the daemon-error handling, so the tool
functions stay thin: build the defaults, hand off a synth/record ``handler``, and
serialize the result.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from dataclasses import replace
from typing import Self, final

from websockets.exceptions import WebSocketException

from punt_vox.client_errors import VoxdConnectionError, VoxdProtocolError
from punt_vox.types_synthesis import SynthesisSpec

logger = logging.getLogger(__name__)

SegmentHandler = Callable[[str, SynthesisSpec], dict[str, object]]


@final
class SegmentBatch:
    """Synthesize each non-empty segment against shared defaults."""

    __slots__ = ("_defaults", "_segments")

    _segments: list[dict[str, str]]
    _defaults: SynthesisSpec

    def __new__(cls, segments: list[dict[str, str]], defaults: SynthesisSpec) -> Self:
        self = super().__new__(cls)
        self._segments = segments
        self._defaults = defaults
        return self

    def render(self, *, handler: SegmentHandler, error_label: str) -> str:
        """Return a JSON list of per-segment results, or a JSON error string.

        Each non-empty segment is synthesized via *handler* against its
        per-segment spec; a daemon failure short-circuits to an error string,
        logged under *error_label*.
        """
        results: list[dict[str, object]] = []
        try:
            for seg in self._segments:
                seg_text = seg.get("text", "")
                if not seg_text:
                    continue
                results.append(handler(seg_text, self._spec_for(seg)))
        except VoxdConnectionError as exc:
            return self._error(str(exc), results)
        except (VoxdProtocolError, WebSocketException, OSError, ValueError) as exc:
            logger.exception("%s failed", error_label)
            return self._error(str(exc), results)
        return json.dumps(results)

    def _spec_for(self, seg: dict[str, str]) -> SynthesisSpec:
        """Return the per-segment spec: segment overrides win over the defaults."""
        defaults = self._defaults
        return replace(
            defaults,
            voice=seg.get("voice") or defaults.voice,
            language=seg.get("language") or defaults.language,
            vibe_tags=seg.get("vibe_tags") or defaults.vibe_tags,
        )

    @staticmethod
    def _error(message: str, results: list[dict[str, object]]) -> str:
        """Return the ``{"error": ...}`` envelope, carrying any landed results.

        A batch that fails partway still recorded the earlier segments; the
        landed ids ride beside the error so they stay addressable rather than
        orphaned and invisible. A total failure omits the empty ``results`` key.
        """
        envelope: dict[str, object] = {"error": message}
        if results:
            envelope["results"] = results
        return json.dumps(envelope)
