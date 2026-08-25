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

import typer

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
    from punt_vox.voxd.conversation_mode.stt_provider import STTProvider
    from punt_vox.voxd.conversation_mode.wait_cue import ChimeFn

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

# Ceiling on how long the mic-echo hold sleep may run. Neither ``/call
# stop`` nor FR-2's inactivity timeout is evaluated while this sleep is in
# progress -- an uncapped estimate on a very long reply (a multi-paragraph
# answer) would make the call unresponsive for as long as that reply takes
# to read aloud, which can run to minutes. Capping means a pathological
# reply degrades to "gate reopens slightly before the reply actually
# finishes" (the existing, already-documented estimation error) rather than
# "the call stops responding."
_MIC_GATE_MAX_HOLD_S = 20.0

# FR-2's bounded-inactivity timeout: how long the call may sit in listening
# with no completed turn before it ends itself. No live-tunable config
# surface (the PRD's stated preference for barge-in timing elsewhere); a
# documented fixed default instead.
_INACTIVITY_TIMEOUT_S = 120.0

# The control mailbox used to be polled once per audio chunk (every
# _CHUNK_S=20ms in mic_audio_source.py -- 50 renames plus 50
# raised-and-caught FileNotFoundErrors per second, in steady state, on the
# same event loop that has to keep up with audio capture). 250ms is still
# well inside "responsive to /call stop" while cutting that cost ~200x.
_CONTROL_POLL_INTERVAL_S = 0.25

# The wait cue is a short, fixed-length bundled asset, not synthesized
# speech -- there is no text to run through estimate_speech_duration_s, so a
# small fixed hold (long enough for the chime's own bundled clip to finish
# playing before the mic reopens) stands in for _MIC_GATE_SAFETY_MARGIN_S's
# role in _speak_and_gate below.
_CHIME_GATE_HOLD_S = 1.5


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
        "_chime",
        "_control",
        "_last_activity_at",
        "_last_control_check_at",
        "_mic_source",
        "_session",
        "_speak",
    )
    _mic_source: MicAudioSource
    _session: CallSession
    _speak: SpeakFn
    _chime: ChimeFn
    _control: CallControl
    _apply_control: ApplyControlFn
    _last_activity_at: float
    # See _CONTROL_POLL_INTERVAL_S: gates how often :meth:`run` actually
    # calls :attr:`_apply_control`, rather than doing it on every captured
    # chunk (every _CHUNK_S=20ms).
    _last_control_check_at: float

    def __new__(
        cls,
        *,
        session_attach: SessionAttach,
        speak: SpeakFn,
        chime: ChimeFn,
        control: CallControl,
        apply_control: ApplyControlFn,
        detector: TurnDetector,
        mic_source: MicAudioSource,
        stt_provider: STTProvider,
    ) -> Self:
        self = super().__new__(cls)
        self._mic_source = mic_source
        self._speak = speak
        self._chime = chime
        self._control = control
        self._apply_control = apply_control
        self._session = CallSession(
            turn_detector=detector,
            stt_provider=stt_provider,
            session_attach=session_attach,
            speak=self._speak_and_gate,
            chime=self._chime_and_gate,
        )
        self._last_activity_at = time.monotonic()
        # 0.0, not time.monotonic(): the first chunk of the call should
        # still see a due check (see _due_for_control_check), the same as
        # every _CONTROL_POLL_INTERVAL_S thereafter.
        self._last_control_check_at = 0.0
        self._session.actor.on_transition(self._mark_activity)
        return self

    @classmethod
    async def create(
        cls,
        *,
        session_attach: SessionAttach,
        speak: SpeakFn,
        chime: ChimeFn,
        control: CallControl,
        apply_control: ApplyControlFn,
    ) -> Self:
        """Build a driver with a real :class:`MicAudioSource`, calibrated first.

        Checks the STT provider's credentials before calibration even
        starts -- so a bad credential fails the call right here, rather
        than surviving 2s of mic calibration and the spoken "Listening."
        cue and only surfacing once the first turn's transcribe fails.
        ``ANTHROPIC_API_KEY`` itself is checked by :mod:`~.call`'s ``_run``,
        the one call site both this driver and the scripted driver share.
        """
        stt_provider = ElevenLabsSTTProvider()
        _require_healthy(stt_provider)
        mic_source = MicAudioSource()
        detector = TurnDetector()
        detector.calibrate(await mic_source.capture_seconds(_CALIBRATION_S))
        return cls(
            session_attach=session_attach,
            speak=speak,
            chime=chime,
            control=control,
            apply_control=apply_control,
            detector=detector,
            mic_source=mic_source,
            stt_provider=stt_provider,
        )

    async def run(self) -> None:
        """Drive the call to completion: listen, detect turns, forward, speak.

        Wraps the whole drive loop in ``try``/``finally`` so ``hangup()``
        runs on every exit path, not only a clean one -- an exception out of
        ``process_chunk`` (a provider fault, a subprocess crash) must not
        skip it, or :class:`~.call_actor.CallActor`'s mode is left stale
        (never transitioned back to idle) on the way out.
        """
        await self._session.start()
        chunks = self._mic_source.chunks()
        try:
            async for chunk in chunks:
                if self._due_for_control_check() and await self._apply_control(
                    self._control, self._session, self._speak_and_gate
                ):
                    break
                if self._is_inactive():
                    await self._session.timeout()
                    break
                await self._session.process_chunk(chunk)
                if self._session_ended_itself():
                    break
        finally:
            await chunks.aclose()
            # timeout() already returned the call to idle; hangup() on an
            # idle call would raise (EndCall requires an active mode), so
            # only call it when nothing above already ended the call itself
            # -- including an abnormal exit via an uncaught exception.
            if self._session.actor.mode is not Mode.IDLE:
                await self._session.hangup()

    def _due_for_control_check(self) -> bool:
        """Return whether enough time has passed to check the mailbox again.

        Without this gate, :meth:`run` called
        :attr:`_apply_control` -- one filesystem rename plus a
        raised-and-caught ``FileNotFoundError`` in the steady (no pending
        request) case -- on every captured chunk, 50 times a second on the
        same event loop that has to keep up with audio capture. Advances
        the clock as a side effect, matching the ``chunk-count`` gate this
        replaces: a caller checks once per due interval, not once per call
        to this method.
        """
        now = time.monotonic()
        if now - self._last_control_check_at < _CONTROL_POLL_INTERVAL_S:
            return False
        self._last_control_check_at = now
        return True

    def _session_ended_itself(self) -> bool:
        """Return whether :attr:`_session` already applied ``EndCall()`` on its own.

        A rejected STT credential (``ProviderAuthError``, in
        :meth:`~.call_session.CallSession._handle_turn_ended`) or a missing
        ``ANTHROPIC_API_KEY`` (``BareAuthMissingError``, in
        :class:`~.reply_recovery.ReplyRecovery`) both apply ``EndCall()``
        from inside :meth:`~.call_session.CallSession.process_chunk`, with
        no way for :meth:`run` to learn about it except polling mode after
        every chunk -- without this check, the loop keeps consuming mic
        chunks forever: mode is already ``Mode.IDLE``, so
        :meth:`_is_inactive`'s own ``Mode.LISTENING`` guard never fires and
        nothing else here notices the call already ended.
        """
        return self._session.actor.mode is Mode.IDLE

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
        real one, capped at :data:`_MIC_GATE_MAX_HOLD_S` -- neither
        ``/call stop`` nor FR-2's inactivity timeout is evaluated while this
        sleep runs, so an uncapped estimate on a very long reply would make
        the whole call unresponsive for as long as the estimate says the
        reply takes to read. try/finally: the gate must reopen and the queue
        must drain even if ``speak`` itself raises, or a failed utterance
        would leave the microphone permanently deaf for the rest of the
        call.
        """
        self._mic_source.set_listening(listening=False)
        try:
            await self._speak(text)
            hold_s = min(
                estimate_speech_duration_s(text) + _MIC_GATE_SAFETY_MARGIN_S,
                _MIC_GATE_MAX_HOLD_S,
            )
            await asyncio.sleep(hold_s)
        finally:
            self._mic_source.drain_pending()
            self._mic_source.set_listening(listening=True)

    async def _chime_and_gate(self) -> None:
        """Play the wait cue, gating mic capture the same way :meth:`_speak_and_gate`.

        See that method's own docstring for the shared "no true playback-
        completion signal" rationale.

        The wait cue fires while the call sits in ``waiting`` (see
        :meth:`~.call_session.CallSession.process_chunk`'s docstring: the
        turn detector stays active in that mode, same as ``listening``), so
        the chime is exactly as much a mic-echo risk as any spoken cue --
        without this gate the microphone would pick its own chime back up
        and could fabricate a "turn" from it. A short fixed hold
        (:data:`_CHIME_GATE_HOLD_S`) stands in for the estimated-speech-
        duration hold :meth:`_speak_and_gate` uses -- the chime is a fixed
        bundled clip with no text to estimate a duration from.
        """
        self._mic_source.set_listening(listening=False)
        try:
            await self._chime()
            await asyncio.sleep(_CHIME_GATE_HOLD_S)
        finally:
            self._mic_source.drain_pending()
            self._mic_source.set_listening(listening=True)

    def _mark_activity(self, _before: Mode, _after: Mode) -> None:
        self._last_activity_at = time.monotonic()


def _require_healthy(stt_provider: STTProvider) -> None:
    """Raise ``typer.BadParameter`` if *stt_provider* fails its own health check.

    Mirrors :func:`~.call_spec.resolve_call_spec`'s and
    :meth:`~.claude_session_attach.ClaudeSessionAttach._require_bare_auth`'s
    fail-fast-before-the-call-starts pattern for the third and last
    credential a call needs -- module-level rather than a method because it
    takes an arbitrary :class:`STTProvider`, not :class:`LiveCallDriver`'s
    own state.
    """
    failures = [
        check.message for check in stt_provider.check_health() if not check.passed
    ]
    if failures:
        raise typer.BadParameter("; ".join(failures))
