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
from collections.abc import AsyncIterator
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from typer.testing import CliRunner

from punt_vox.commands.call import build_call_app
from punt_vox.commands.call_scripted import ScriptedTurn
from punt_vox.voxd.conversation_mode.audio_chunk import AudioChunk
from punt_vox.voxd.conversation_mode.call_control import CallControl
from punt_vox.voxd.conversation_mode.call_lock import CallLock

runner = CliRunner()


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


def test_start_no_longer_requires_the_script_option() -> None:
    """``--script`` is the dev/test opt-in now, not a required flag."""
    result = runner.invoke(build_call_app(), ["start", "--help"])
    assert result.exit_code == 0
    assert "--script" in result.stdout
    assert "not required" not in result.stdout  # sanity: help text renders


async def test_run_call_speaks_the_reply_and_holds_the_lock_only_while_active(
    tmp_path: Path,
) -> None:
    """End-to-end through the CLI orchestration: real detector, fake speech I/O."""
    from punt_vox.commands import call as call_module

    script = _script_file(tmp_path, [{"text": "what does this do", "confidence": 0.95}])

    spoken: list[str] = []

    class _FakeClient:
        def synthesize(self, text: str) -> None:
            spoken.append(text)

    with (
        patch.object(call_module, "VoxClientSync", return_value=_FakeClient()),
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
        def synthesize(self, text: str) -> None:
            spoken.append(text)

    with (
        patch.object(call_module, "VoxClientSync", return_value=_FakeClient()),
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
    # the speaking -> listening mode transition -- "Listening.", the reply
    # itself, and "Ready." (spoken *after* that transition already fired)
    # are three separate speak() calls, and each must drain the mic's
    # self-captured backlog on its own.
    assert mic_source.drain_calls == len(spoken) == 3

    # The mic-echo mitigation (mic_audio_source.py's set_listening, gated at
    # the PortAudio callback itself, not just drained after the fact): the
    # gate closes before every speak() call and reopens after, one
    # False/True pair per utterance.
    assert mic_source.listening_calls == [False, True] * 3


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
        def synthesize(self, text: str) -> None:
            spoken.append(text)

    with (
        patch.object(call_module, "VoxClientSync", return_value=_FakeClient()),
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


if __name__ == "__main__":
    pytest.main([__file__])
