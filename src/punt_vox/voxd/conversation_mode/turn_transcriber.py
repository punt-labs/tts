"""Turns one closed audio run into a transcript, per FR-19's confidence floor.

:class:`TurnTranscriber` wraps a single :class:`~.stt_provider.STTProvider`
call with the "never fabricate on ambiguous audio" policy FR-19 requires: an
empty result, a low-confidence result, and a transient provider fault (rate
limit, 5xx, network blip) are all exactly as unactionable as each other, and
all resolve to the same ``None`` -- "ask the human to repeat" -- rather than
three different recovery paths a caller would otherwise have to know about.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Self, final

from punt_vox.types_provider_errors import ProviderAuthError

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from punt_vox.voxd.conversation_mode.audio_chunk import AudioChunk
    from punt_vox.voxd.conversation_mode.stt_provider import (
        STTProvider,
        TranscriptEvent,
    )
    from punt_vox.voxd.conversation_mode.turn_timer import TurnTimer

__all__ = ["CONFIDENCE_FLOOR", "TurnTranscriber"]

logger = logging.getLogger(__name__)

# FR-19: a transcript below this confidence is a signal to ask the human to
# repeat, never to act on. Mirrors the floor the fake-provider tests use
# (tests/conversation_mode/test_stt_provider.py), stated once here as the
# production gate.
CONFIDENCE_FLOOR = 0.6


@final
class TurnTranscriber:
    """Recognize one closed run of audio, applying FR-19's confidence floor."""

    __slots__ = ("_provider", "_turn_timer")
    _provider: STTProvider
    _turn_timer: TurnTimer

    def __new__(cls, provider: STTProvider, turn_timer: TurnTimer) -> Self:
        self = super().__new__(cls)
        self._provider = provider
        self._turn_timer = turn_timer
        return self

    async def transcribe(
        self, chunks: AsyncIterator[AudioChunk]
    ) -> TranscriptEvent | None:
        """Return the final transcript event for *chunks*, or ``None`` if ambiguous.

        ``None`` covers three cases FR-19 treats identically: no events at
        all, a final event below :data:`CONFIDENCE_FLOOR`, and a transient
        provider failure -- a caller's response in every case is "ask the
        human to repeat", never to fabricate an answer from an uncertain or
        missing result.
        """
        final_event = None
        try:
            async for event in self._provider.transcribe(chunks):
                final_event = event
        except ProviderAuthError:
            # A revoked/expired STT credential is certain and permanent --
            # every subsequent turn would hit exactly the same rejection,
            # the same asymmetry :class:`~.session_attach.BareAuthMissingError`
            # already draws against a transient session-attach fault. Folding
            # this into the broad ``except Exception`` below would launder
            # it into "ask the human to repeat", which repeats identically
            # forever and never tells the human their STT credentials are
            # broken. Re-raised, not swallowed -- the caller (CallSession)
            # ends the call with an actionable message instead.
            self._turn_timer.mark("stt_response_received", detail="STT auth failed")
            raise
        except Exception:
            logger.exception("STT transcribe failed mid-turn")
            self._turn_timer.mark("stt_response_received", detail="transcribe failed")
            return None
        detail = (
            "no transcript"
            if final_event is None
            else f"confidence={final_event.confidence:.2f}"
        )
        self._turn_timer.mark("stt_response_received", detail=detail)
        if final_event is None or final_event.confidence < CONFIDENCE_FLOOR:
            return None
        return final_event
