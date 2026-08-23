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
    with patch("punt_vox.commands.call.CallCli._lock_dir", return_value=tmp_path):
        result = runner.invoke(build_call_app(), ["stop"])
    assert result.exit_code == 0
    control = CallControl(tmp_path / "call.control")
    request = control.consume()
    assert request is not None
    assert request.kind == "stop"


def test_transfer_writes_a_transfer_request_with_session_id(tmp_path: Path) -> None:
    with patch("punt_vox.commands.call.CallCli._lock_dir", return_value=tmp_path):
        result = runner.invoke(build_call_app(), ["transfer", "--session", "session-b"])
    assert result.exit_code == 0
    control = CallControl(tmp_path / "call.control")
    request = control.consume()
    assert request is not None
    assert request.kind == "transfer"
    assert request.target_session_id == "session-b"


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
    from punt_vox.voxd.conversation_mode.call_lock import CallLock

    assert CallLock(tmp_path / "call.lock").read() is None  # released after hangup


class _FakeLiveMicSource:
    """Stands in for :class:`MicAudioSource`: no hardware, real turn shape."""

    def __init__(self, calibration: list[AudioChunk], live: list[AudioChunk]) -> None:
        self._calibration = calibration
        self._live = live

    async def capture_seconds(self, _duration_s: float) -> list[AudioChunk]:
        return self._calibration

    async def chunks(self) -> AsyncIterator[AudioChunk]:
        for chunk in self._live:
            yield chunk


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
        patch.object(call_module, "MicAudioSource", return_value=mic_source),
        patch.object(call_module, "ElevenLabsSTTProvider", return_value=stt),
        patch(
            "punt_vox.voxd.conversation_mode.claude_session_attach.asyncio.create_subprocess_exec",
            return_value=_fake_process("It returns the sum."),
        ),
        patch("punt_vox.commands.call.CallCli._lock_dir", return_value=tmp_path),
    ):
        await call_module.CallCli()._run(None, "session-a")

    assert "It returns the sum." in spoken
    from punt_vox.voxd.conversation_mode.call_lock import CallLock

    assert CallLock(tmp_path / "call.lock").read() is None  # released after hangup


if __name__ == "__main__":
    pytest.main([__file__])
