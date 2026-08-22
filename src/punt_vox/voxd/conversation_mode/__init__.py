"""Conversation Mode: a live voice call between one human and one agent session.

Domain seams: the call state machine
(:mod:`~punt_vox.voxd.conversation_mode.mode`,
:mod:`~punt_vox.voxd.conversation_mode.call_state`), the turn detector
(:mod:`~punt_vox.voxd.conversation_mode.turn_detector`), the
:class:`~punt_vox.voxd.conversation_mode.stt_provider.STTProvider` and
:class:`~punt_vox.voxd.conversation_mode.session_attach.SessionAttach`
protocols, and :class:`~punt_vox.voxd.conversation_mode.call_actor.CallActor`,
the single serialized dispatch point the Z specification
(``docs/conversation-mode-call-state.tex``) requires. Orchestration that
drives these into a live call lives in
:mod:`punt_vox.commands.call`, outside this package, so this package stays
importable without pulling in the CLI or provider SDKs.
"""

from __future__ import annotations

from punt_vox.voxd.conversation_mode.audio_chunk import AudioChunk
from punt_vox.voxd.conversation_mode.call_actor import CallActor
from punt_vox.voxd.conversation_mode.call_command import CallCommand
from punt_vox.voxd.conversation_mode.call_state import CallState, IllegalTransitionError
from punt_vox.voxd.conversation_mode.mode import Detector, Mode
from punt_vox.voxd.conversation_mode.reply import ReplyChunk
from punt_vox.voxd.conversation_mode.session_attach import (
    SessionAttach,
    SessionAttachError,
)
from punt_vox.voxd.conversation_mode.stt_provider import STTProvider, TranscriptEvent
from punt_vox.voxd.conversation_mode.turn import TranscribedTurn
from punt_vox.voxd.conversation_mode.turn_detector import TurnDetector
from punt_vox.voxd.conversation_mode.turn_signal import TurnSignal

# The eight concrete CallCommand implementations (start_call.py through
# reply_ends.py) are deliberately NOT re-exported here -- an orchestrator
# imports each from its own module, mirroring how
# voxd/programs/switch_signal.py etc. are consumed directly rather than
# through voxd.programs's package interface. Keeps this __init__ within the
# package-interface-width guideline as the family of commands grows.
__all__ = [
    "AudioChunk",
    "CallActor",
    "CallCommand",
    "CallState",
    "Detector",
    "IllegalTransitionError",
    "Mode",
    "ReplyChunk",
    "STTProvider",
    "SessionAttach",
    "SessionAttachError",
    "TranscribedTurn",
    "TranscriptEvent",
    "TurnDetector",
    "TurnSignal",
]
