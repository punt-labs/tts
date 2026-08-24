"""Tests for the ``vox call`` CLI: start/stop/transfer, per ADR naming.

``ScriptedTurn``/``ScriptedSTTProvider`` unit tests live in
``tests/test_call_scripted.py``, mirroring their module
(``punt_vox.commands.call_scripted``). This file covers the CLI verbs and
the two ``start`` paths: scripted (dev/test) and live (default), the latter
driven by mocked ``MicAudioSource``/``ElevenLabsSTTProvider`` so it never
touches a real microphone or the ElevenLabs API.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Coroutine
from contextlib import AbstractContextManager
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from typer.testing import CliRunner

from punt_vox.commands.call import build_call_app
from punt_vox.commands.call_scripted import ScriptedTurn
from punt_vox.session_spec import SessionSpec
from punt_vox.types_synthesis import SynthesisSpec
from punt_vox.voxd.conversation_mode.audio_chunk import AudioChunk
from punt_vox.voxd.conversation_mode.call_control import CallControl
from punt_vox.voxd.conversation_mode.call_lock import CallLock

runner = CliRunner()


def _resolved_session_spec(
    provider: str = "elevenlabs",
) -> AbstractContextManager[MagicMock]:
    """Patch ``SessionSpec.for_repo`` so tests never touch the real ``vox.md``.

    ``call.py``'s ``_run`` resolves a :class:`SynthesisSpec` through
    ``SessionSpec.for_repo().fill()`` before the call state machine starts
    (the same fill-from-``vox.md`` path ``vox say``/``vox rec new`` use).
    Faking it here keeps these tests hermetic -- they must pass on a CI
    machine with no configured provider, not only on a workstation that
    happens to have this repo's own vox enabled.
    """
    fake_spec = MagicMock()
    fake_spec.fill.return_value = SynthesisSpec(provider=provider)
    return patch.object(SessionSpec, "for_repo", return_value=fake_spec)


def _script_file(tmp_path: Path, lines: list[dict[str, object]]) -> Path:
    path = tmp_path / "script.jsonl"
    path.write_text("\n".join(json.dumps(line) for line in lines))
    return path


def _fake_frame(text: str) -> bytes:
    payload = json.dumps(
        {"type": "assistant", "message": {"content": [{"text": text}]}}
    )
    return payload.encode() + b"\n"


class _AsyncLines:
    def __init__(self, lines: list[bytes]) -> None:
        self._lines = list(lines)

    def __aiter__(self) -> _AsyncLines:
        return self

    async def __anext__(self) -> bytes:
        if not self._lines:
            raise StopAsyncIteration
        return self._lines.pop(0)


def _discard(_written: bytes) -> None:
    """A stdin sink that discards whatever the session-attach writes."""


def _fake_process(reply_text: str) -> AsyncMock:
    process = AsyncMock()
    process.stdin = AsyncMock()
    process.stdin.write = _discard
    process.stdin.close = MagicMock()
    process.stdout = _AsyncLines([_fake_frame(reply_text)])
    process.communicate.return_value = (b"", b"")
    process.returncode = 0
    return process


def test_stop_writes_a_stop_request(tmp_path: Path) -> None:
    CallLock(tmp_path / "call.lock").acquire("conversation mode call active")
    with patch("punt_vox.commands.call.CallCli._lock_dir", return_value=tmp_path):
        result = runner.invoke(build_call_app(), ["stop"])
    assert result.exit_code == 0
    control = CallControl(tmp_path / "call.control")
    request = control.consume()
    assert request is not None
    assert request.kind == "stop"


def test_stop_refuses_when_no_call_is_active(tmp_path: Path) -> None:
    with patch("punt_vox.commands.call.CallCli._lock_dir", return_value=tmp_path):
        result = runner.invoke(build_call_app(), ["stop"])
    assert result.exit_code != 0
    assert CallControl(tmp_path / "call.control").consume() is None


def test_transfer_writes_a_transfer_request_with_session_id(tmp_path: Path) -> None:
    CallLock(tmp_path / "call.lock").acquire("conversation mode call active")
    with patch("punt_vox.commands.call.CallCli._lock_dir", return_value=tmp_path):
        result = runner.invoke(build_call_app(), ["transfer", "--session", "session-b"])
    assert result.exit_code == 0
    control = CallControl(tmp_path / "call.control")
    request = control.consume()
    assert request is not None
    assert request.kind == "transfer"
    assert request.target_session_id == "session-b"


def test_transfer_refuses_when_no_call_is_active(tmp_path: Path) -> None:
    with patch("punt_vox.commands.call.CallCli._lock_dir", return_value=tmp_path):
        result = runner.invoke(build_call_app(), ["transfer"])
    assert result.exit_code != 0
    assert CallControl(tmp_path / "call.control").consume() is None


def test_stop_refuses_against_a_stale_lock_with_a_dead_pid(tmp_path: Path) -> None:
    """Regression: a killed `vox call start` leaves a lock file behind with a
    now-dead pid. `vox call stop` against that stale lock must refuse the
    same as "no call is active" -- not succeed and write a stop request
    into a mailbox nobody will ever read, since the process it thinks it is
    stopping is long gone.
    """
    CallLock(tmp_path / "call.lock").acquire("stale call")
    with (
        patch("os.kill", side_effect=ProcessLookupError),
        patch("punt_vox.commands.call.CallCli._lock_dir", return_value=tmp_path),
    ):
        result = runner.invoke(build_call_app(), ["stop"])
    assert result.exit_code != 0
    assert "no call is active" in result.output
    assert CallControl(tmp_path / "call.control").consume() is None


def test_transfer_refuses_against_a_stale_lock_with_a_dead_pid(tmp_path: Path) -> None:
    """Same stale-lock refusal as the ``stop`` test above."""
    CallLock(tmp_path / "call.lock").acquire("stale call")
    with (
        patch("os.kill", side_effect=ProcessLookupError),
        patch("punt_vox.commands.call.CallCli._lock_dir", return_value=tmp_path),
    ):
        result = runner.invoke(build_call_app(), ["transfer", "--session", "session-b"])
    assert result.exit_code != 0
    assert "no call is active" in result.output
    assert CallControl(tmp_path / "call.control").consume() is None


def test_start_no_longer_requires_the_script_option() -> None:
    """``--script`` is the dev/test opt-in now, not a required flag."""
    result = runner.invoke(build_call_app(), ["start", "--help"])
    assert result.exit_code == 0
    assert "--script" in result.stdout
    assert "not required" not in result.stdout  # sanity: help text renders


def _close_coro(coro: Coroutine[object, object, object]) -> None:
    """A no-op ``asyncio.run`` stand-in: closes *coro* without running it.

    Used where a test only asserts on state set up before ``asyncio.run`` is
    called -- a bare ``patch("asyncio.run")`` leaves the coroutine
    ``start()`` built unclosed, which emits a spurious "coroutine was never
    awaited" RuntimeWarning.
    """
    coro.close()


def test_start_help_shows_the_trace_turns_flag() -> None:
    result = runner.invoke(build_call_app(), ["start", "--help"])
    assert result.exit_code == 0
    assert "--trace-turns" in result.stdout


def test_start_help_does_not_collide_with_the_global_verbose_flag() -> None:
    """Regression: a second, same-named --verbose scoped to `call start`
    silently did something completely different from the pre-existing
    global --verbose (raise the client log level) depending on flag
    position -- vox's own OutputFlags convention says position must never
    change meaning. `call start --help` must not offer a bare `-v`/
    `--verbose` at all; only the distinctly-named --trace-turns.
    """
    result = runner.invoke(build_call_app(), ["start", "--help"])
    assert result.exit_code == 0
    assert "--verbose" not in result.stdout
    assert "-v," not in result.stdout


def test_start_with_trace_turns_echoes_the_turn_timer_to_console() -> None:
    """Regression: --trace-turns must reach configure_turn_timer_logging as
    echo_to_console=True -- the flag that decides whether the turn-latency
    trace also prints live, never whether it reaches vox.log (that's
    unconditional).
    """
    from punt_vox.commands import call as call_module

    with (
        patch.object(call_module, "configure_turn_timer_logging") as mock_configure,
        patch("asyncio.run", side_effect=_close_coro),
    ):
        call_module.CallCli().start(trace_turns=True)
    mock_configure.assert_called_once_with(echo_to_console=True)


def test_start_without_trace_turns_does_not_echo_the_turn_timer_to_console() -> None:
    from punt_vox.commands import call as call_module

    with (
        patch.object(call_module, "configure_turn_timer_logging") as mock_configure,
        patch("asyncio.run", side_effect=_close_coro),
    ):
        call_module.CallCli().start()
    mock_configure.assert_called_once_with(echo_to_console=False)


async def test_run_call_passes_a_resolved_spec_with_a_provider_to_synthesize(
    tmp_path: Path,
) -> None:
    """Regression: a bare ``client.synthesize(text)`` sends no provider on the
    wire, and voxd's ``speech_handlers.py`` rejects with ``Unknown provider
    ''`` -- ``parse_required_str`` requires one and does not guess. Every
    ``speak()`` call inside ``_run`` must pass a :class:`SynthesisSpec` whose
    ``provider`` survived ``SessionSpec.for_repo().fill()`` resolution, not
    just call ``speak()`` at all.
    """
    from punt_vox.commands import call as call_module

    script = _script_file(tmp_path, [])  # empty script: only the "Listening." cue

    received: list[SynthesisSpec | None] = []

    class _FakeClient:
        def synthesize(
            self,
            text: str,
            spec: SynthesisSpec | None = None,
            *,
            timeout: float | None = None,
        ) -> None:
            del timeout
            del text
            received.append(spec)

    with (
        patch.object(call_module, "VoxClientSync", return_value=_FakeClient()),
        _resolved_session_spec("elevenlabs"),
        patch("punt_vox.commands.call.CallCli._lock_dir", return_value=tmp_path),
    ):
        await call_module.CallCli()._run(script, "session-a")

    assert received  # at least the "Listening." cue was spoken
    assert all(spec is not None and spec.provider == "elevenlabs" for spec in received)


async def test_run_call_speaks_the_reply_and_holds_the_lock_only_while_active(
    tmp_path: Path,
) -> None:
    """End-to-end through the CLI orchestration: real detector, fake speech I/O."""
    from punt_vox.commands import call as call_module

    script = _script_file(tmp_path, [{"text": "what does this do", "confidence": 0.95}])

    spoken: list[str] = []

    class _FakeClient:
        def synthesize(
            self,
            text: str,
            spec: SynthesisSpec | None = None,
            *,
            timeout: float | None = None,
        ) -> None:
            del timeout
            del spec
            spoken.append(text)

    with (
        patch.object(call_module, "VoxClientSync", return_value=_FakeClient()),
        _resolved_session_spec(),
        patch(
            "punt_vox.voxd.conversation_mode.claude_session_attach.asyncio.create_subprocess_exec",
            return_value=_fake_process("It returns the sum."),
        ),
        patch("punt_vox.commands.call.CallCli._lock_dir", return_value=tmp_path),
    ):
        await call_module.CallCli()._run(script, "session-a")

    assert "It returns the sum." in spoken

    assert CallLock(tmp_path / "call.lock").read() is None  # released after hangup


class _FakeLiveMicSource:
    """Stands in for :class:`MicAudioSource`: no hardware, real turn shape."""

    def __init__(self, calibration: list[AudioChunk], live: list[AudioChunk]) -> None:
        self._calibration = calibration
        self._live = live
        self.drain_calls = 0
        self.listening_calls: list[bool] = []

    async def capture_seconds(self, _duration_s: float) -> list[AudioChunk]:
        return self._calibration

    async def chunks(self) -> AsyncIterator[AudioChunk]:
        for chunk in self._live:
            yield chunk

    def drain_pending(self) -> int:
        self.drain_calls += 1
        return 0

    def set_listening(self, *, listening: bool) -> None:
        self.listening_calls.append(listening)


class _FakeLiveSTT:
    """Stands in for :class:`ElevenLabsSTTProvider`: canned transcript, no SDK."""

    def __init__(self, text: str, confidence: float) -> None:
        self._text = text
        self._confidence = confidence

    @property
    def name(self) -> str:
        return "fake-elevenlabs"

    async def transcribe(
        self, chunks: AsyncIterator[AudioChunk]
    ) -> AsyncIterator[object]:
        from punt_vox.voxd.conversation_mode.stt_provider import TranscriptEvent

        async for _ in chunks:
            pass
        yield TranscriptEvent(
            text=self._text, confidence=self._confidence, is_final=True
        )

    def check_health(self) -> list[object]:
        return []


async def test_run_call_live_path_captures_from_mic_and_transcribes(
    tmp_path: Path,
) -> None:
    """Default (no ``--script``) drives mic capture + ElevenLabs STT, both faked."""
    from punt_vox.commands import call as call_module

    calibration = ScriptedTurn.silence_chunks(10)
    live_chunks = ScriptedTurn(text="ignored", confidence=1.0).synthetic_chunks()
    mic_source = _FakeLiveMicSource(calibration, live_chunks)
    stt = _FakeLiveSTT("what does this do", confidence=0.95)

    spoken: list[str] = []

    class _FakeClient:
        def synthesize(
            self,
            text: str,
            spec: SynthesisSpec | None = None,
            *,
            timeout: float | None = None,
        ) -> None:
            del timeout
            del spec
            spoken.append(text)

    with (
        patch.object(call_module, "VoxClientSync", return_value=_FakeClient()),
        _resolved_session_spec(),
        patch(
            "punt_vox.commands.call_live_driver.MicAudioSource", return_value=mic_source
        ),
        patch(
            "punt_vox.commands.call_live_driver.ElevenLabsSTTProvider", return_value=stt
        ),
        patch(
            "punt_vox.voxd.conversation_mode.claude_session_attach.asyncio.create_subprocess_exec",
            return_value=_fake_process("It returns the sum."),
        ),
        patch("punt_vox.commands.call.CallCli._lock_dir", return_value=tmp_path),
    ):
        await call_module.CallCli()._run(None, "session-a")

    assert "It returns the sum." in spoken

    assert CallLock(tmp_path / "call.lock").read() is None  # released after hangup

    # Regression: draining must happen after every utterance, not only on
    # the speaking -> listening mode transition -- "Listening.", the
    # vox-36xc instant ack quip, the reply itself, and "Ready." (spoken
    # *after* that transition already fired) are four separate speak()
    # calls, and each must drain the mic's self-captured backlog on its own.
    assert mic_source.drain_calls == len(spoken) == 4

    # The mic-echo mitigation (mic_audio_source.py's set_listening, gated at
    # the PortAudio callback itself, not just drained after the fact): the
    # gate closes before every speak() call and reopens after, one
    # False/True pair per utterance -- four now that the vox-36xc ack quip
    # is a fourth gated utterance.
    assert mic_source.listening_calls == [False, True] * 4


async def test_run_call_live_path_times_out_after_inactivity(tmp_path: Path) -> None:
    """FR-2's bounded-inactivity end must actually be wired into the live loop."""
    from punt_vox.commands import call as call_module

    calibration = ScriptedTurn.silence_chunks(10)
    # Plain silence, never enough to close a turn -- nothing here should
    # ever speak a reply or reset the inactivity clock via a mode change
    # other than the call's own start.
    live_chunks = [AudioChunk(pcm=b"\x00\x00", duration_s=0.02) for _ in range(50)]
    mic_source = _FakeLiveMicSource(calibration, live_chunks)
    stt = _FakeLiveSTT("unused", confidence=0.95)

    spoken: list[str] = []

    class _FakeClient:
        def synthesize(
            self,
            text: str,
            spec: SynthesisSpec | None = None,
            *,
            timeout: float | None = None,
        ) -> None:
            del timeout
            del spec
            spoken.append(text)

    with (
        patch.object(call_module, "VoxClientSync", return_value=_FakeClient()),
        _resolved_session_spec(),
        patch(
            "punt_vox.commands.call_live_driver.MicAudioSource", return_value=mic_source
        ),
        patch(
            "punt_vox.commands.call_live_driver.ElevenLabsSTTProvider", return_value=stt
        ),
        patch("punt_vox.commands.call_live_driver._INACTIVITY_TIMEOUT_S", 0.0),
        patch("punt_vox.commands.call.CallCli._lock_dir", return_value=tmp_path),
    ):
        await call_module.CallCli()._run(None, "session-a")

    # The loop ended itself via the FR-2 timeout, not by exhausting the
    # chunk generator with a turn still pending -- "Listening." is the only
    # cue spoken; there is no reply and no "Ready.".
    assert spoken == ["Listening."]
    assert CallLock(tmp_path / "call.lock").read() is None  # released after timeout


class _ClientFailingOnErrorSummary:
    """Speaks normally except when asked to speak the outer boundary's
    "call ended unexpectedly" summary -- lets a test drive the outer
    ``except`` handler's own ``speak()`` call into failure without
    disturbing every earlier cue in the call.
    """

    def __init__(self) -> None:
        self.spoken: list[str] = []

    def synthesize(
        self,
        text: str,
        spec: SynthesisSpec | None = None,
        *,
        timeout: float | None = None,
    ) -> None:
        del spec, timeout
        self.spoken.append(text)
        if text.startswith("The call ended unexpectedly"):
            raise RuntimeError("daemon also unreachable")


async def test_outer_boundary_speaks_and_reraises_on_a_mid_call_crash(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """The one mechanism that turns a mid-call crash into audible feedback:
    ``commands/call.py``'s outer ``except Exception`` handler must speak a
    human-facing summary, log via ``logger.exception``, release the lock in
    ``finally``, and still re-raise so the process exits non-zero.
    """
    from punt_vox.commands import call as call_module

    script = _script_file(tmp_path, [{"text": "what does this do", "confidence": 0.95}])
    client = _ClientFailingOnErrorSummary()
    # A failing collaborator mid-call: the STT->claude relay subprocess
    # itself never spawns -- the same shape a real mic device-open failure
    # or STT auth failure takes, once it reaches this outer boundary.
    with (
        patch.object(call_module, "VoxClientSync", return_value=client),
        _resolved_session_spec(),
        patch(
            "punt_vox.voxd.conversation_mode.claude_session_attach."
            "asyncio.create_subprocess_exec",
            side_effect=OSError("mic device busy"),
        ),
        patch("punt_vox.commands.call.CallCli._lock_dir", return_value=tmp_path),
        caplog.at_level("ERROR", logger="punt_vox.commands.call"),
        pytest.raises(OSError, match="mic device busy"),
    ):
        await call_module.CallCli()._run(script, "session-a")

    assert any("call ended unexpectedly" in record.message for record in caplog.records)
    # The spoken summary is a fixed sentence -- never the raw exception text,
    # which for a SessionAttachError can embed decoded subprocess stderr
    # verbatim (a voice-disclosure hazard). The exception detail lives only
    # in the log line asserted above.
    assert (
        "The call ended unexpectedly. Check the terminal for details." in client.spoken
    )
    assert not any("mic device busy" in phrase for phrase in client.spoken)
    assert CallLock(tmp_path / "call.lock").read() is None  # released in finally


async def test_outer_boundary_survives_a_failing_speak_and_reraises_the_original(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A ``speak()`` failure inside the outer boundary's own fallback call
    must not replace the original exception -- the root cause, not a
    secondary daemon-RPC failure, is what the human's terminal and the
    process's exit code must show.
    """
    from punt_vox.commands import call as call_module

    script = _script_file(tmp_path, [{"text": "what does this do", "confidence": 0.95}])
    client = _ClientFailingOnErrorSummary()

    with (
        patch.object(call_module, "VoxClientSync", return_value=client),
        _resolved_session_spec(),
        patch(
            "punt_vox.voxd.conversation_mode.claude_session_attach."
            "asyncio.create_subprocess_exec",
            side_effect=OSError("mic device busy"),
        ),
        patch("punt_vox.commands.call.CallCli._lock_dir", return_value=tmp_path),
        caplog.at_level("ERROR", logger="punt_vox.commands.call"),
        # The original OSError, not the RuntimeError the fallback speak()
        # call raises, must be what actually propagates.
        pytest.raises(OSError, match="mic device busy"),
    ):
        await call_module.CallCli()._run(script, "session-a")

    assert any("call ended unexpectedly" in record.message for record in caplog.records)
    assert any(
        "also failed to speak the call-ended summary" in record.message
        for record in caplog.records
    )
    assert CallLock(tmp_path / "call.lock").read() is None  # still released


if __name__ == "__main__":
    pytest.main([__file__])
