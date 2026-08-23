"""Drives one live call: real microphone, ElevenLabs, the mic-echo gate, FR-2's timeout.

Extracted out of :mod:`punt_vox.commands.call` because the live path's state
(the mic source, the session, the inactivity clock) and the behavior that
reads and mutates it (the gate/hold-and-drain wrapper around every
utterance, the transition observer that resets the inactivity clock, the
drive loop itself) belong together on one object rather than as a pile of
local variables captured by nested closures inside one long function.
"""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING, Self, final

from punt_vox.providers.convert import estimate_speech_duration_s
from punt_vox.providers.elevenlabs_stt import ElevenLabsSTTProvider
from punt_vox.voxd.conversation_mode.call_session import CallSession
from punt_vox.voxd.conversation_mode.mic_audio_source import MicAudioSource
from punt_vox.voxd.conversation_mode.mode import Mode
from punt_vox.voxd.conversation_mode.turn_detector import TurnDetector

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from punt_vox.voxd.conversation_mode.call_control import CallControl
    from punt_vox.voxd.conversation_mode.call_session import SpeakFn
    from punt_vox.voxd.conversation_mode.session_attach import SessionAttach

__all__ = ["LiveCallDriver"]

# A control-request handler with the same shape as ``CallCli._apply_control``:
# consume one pending stop/transfer request against *session*, speaking
# through *speak* on a declined transfer; return whether the drive loop
# should stop. Shared with the scripted path (see call.py), so this driver
# takes it as a collaborator rather than duplicating transfer-resolution
# logic here.
type ApplyControlFn = Callable[["CallControl", CallSession, "SpeakFn"], Awaitable[bool]]

# FR-1's "a few seconds of 'say something'" calibration step: how long to
# sample ambient microphone audio before the call opens for real speech, so
# TurnDetector's noise floor reflects the room the human is actually
# calling from.
_CALIBRATION_S = 2.0

# Padding added on top of the estimated speech duration before the mic gate
# reopens. There is no true playback-completion signal available here (see
# :meth:`LiveCallDriver._speak_and_gate`'s docstring) -- generous because
# the estimate is a words-per-minute average, not a measurement, and a
# reopened-too-early gate is exactly the bug this exists to bound.
_MIC_GATE_SAFETY_MARGIN_S = 0.75

# FR-2's bounded-inactivity timeout: how long the call may sit in listening
# with no completed turn before it ends itself. No live-tunable config
# surface in this slice (the PRD's stated preference for barge-in timing
# elsewhere); a documented fixed default instead.
_INACTIVITY_TIMEOUT_S = 120.0


@final
class LiveCallDriver:
    """Owns one live call's mic source, session, and inactivity clock.

    ``create`` is the public entry point (async, since calibrating the
    detector against real ambient audio requires capturing some first);
    ``__new__`` stays synchronous per this codebase's constructor
    convention and takes the already-calibrated detector as a parameter.
    """

    __slots__ = (
        "_apply_control",
        "_control",
        "_last_activity_at",
        "_mic_source",
        "_session",
        "_speak",
    )
    _mic_source: MicAudioSource
    _session: CallSession
    _speak: SpeakFn
    _control: CallControl
    _apply_control: ApplyControlFn
    _last_activity_at: float

    def __new__(
        cls,
        *,
        session_attach: SessionAttach,
        speak: SpeakFn,
        control: CallControl,
        apply_control: ApplyControlFn,
        detector: TurnDetector,
        mic_source: MicAudioSource,
    ) -> Self:
        self = super().__new__(cls)
        self._mic_source = mic_source
        self._speak = speak
        self._control = control
        self._apply_control = apply_control
        self._session = CallSession(
            turn_detector=detector,
            stt_provider=ElevenLabsSTTProvider(),
            session_attach=session_attach,
            speak=self._speak_and_gate,
        )
        self._last_activity_at = time.monotonic()
        self._session.actor.on_transition(self._mark_activity)
        return self

    @classmethod
    async def create(
        cls,
        *,
        session_attach: SessionAttach,
        speak: SpeakFn,
        control: CallControl,
        apply_control: ApplyControlFn,
    ) -> Self:
        """Build a driver with a real :class:`MicAudioSource`, calibrated first."""
        mic_source = MicAudioSource()
        detector = TurnDetector()
        detector.calibrate(await mic_source.capture_seconds(_CALIBRATION_S))
        return cls(
            session_attach=session_attach,
            speak=speak,
            control=control,
            apply_control=apply_control,
            detector=detector,
            mic_source=mic_source,
        )

    async def run(self) -> None:
        """Drive the call to completion: listen, detect turns, forward, speak."""
        await self._session.start()
        chunks = self._mic_source.chunks()
        try:
            async for chunk in chunks:
                if await self._apply_control(
                    self._control, self._session, self._speak_and_gate
                ):
                    break
                if self._is_inactive():
                    await self._session.timeout()
                    break
                await self._session.process_chunk(chunk)
        finally:
            await chunks.aclose()
        # timeout() already returned the call to idle; hangup() on an idle
        # call would raise (EndCall requires an active mode), so only call
        # it when the loop above didn't already end the call itself.
        if self._session.actor.mode is not Mode.IDLE:
            await self._session.hangup()

    def _is_inactive(self) -> bool:
        return (
            self._session.actor.mode is Mode.LISTENING
            and time.monotonic() - self._last_activity_at > _INACTIVITY_TIMEOUT_S
        )

    async def _speak_and_gate(self, text: str) -> None:
        """Speak *text*, gating mic capture at the source around it.

        There is no true playback-completion signal available from the
        daemon client this calls through (see :mod:`punt_vox.commands.call`'s
        module docstring's "Known limitation") -- the gate closes before
        ``speak`` is even awaited (covering the enqueue round trip) and
        stays closed for an *estimated* speech duration afterward, not the
        real one. try/finally: the gate must reopen and the queue must
        drain even if ``speak`` itself raises, or a failed utterance would
        leave the microphone permanently deaf for the rest of the call.
        """
        self._mic_source.set_listening(listening=False)
        try:
            await self._speak(text)
            hold_s = estimate_speech_duration_s(text) + _MIC_GATE_SAFETY_MARGIN_S
            await asyncio.sleep(hold_s)
        finally:
            self._mic_source.drain_pending()
            self._mic_source.set_listening(listening=True)

    def _mark_activity(self, _before: Mode, _after: Mode) -> None:
        self._last_activity_at = time.monotonic()
