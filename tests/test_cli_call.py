"""Tests for the ``vox call`` CLI: start/stop/transfer, per ADR naming."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from typer.testing import CliRunner

from punt_vox.commands.call import ScriptedTurn, build_call_app
from punt_vox.voxd.conversation_mode.call_control import CallControl

runner = CliRunner()


def _script_file(tmp_path: Path, lines: list[dict[str, object]]) -> Path:
    import json

    path = tmp_path / "script.jsonl"
    path.write_text("\n".join(json.dumps(line) for line in lines))
    return path


def test_scripted_turn_reads_a_jsonl_script(tmp_path: Path) -> None:
    path = _script_file(
        tmp_path,
        [
            {"text": "hello", "confidence": 0.9},
            {"text": "goodbye", "confidence": 0.95},
        ],
    )
    turns = ScriptedTurn.read_script(path)
    assert [t.text for t in turns] == ["hello", "goodbye"]
    assert [t.confidence for t in turns] == [0.9, 0.95]


def test_scripted_turn_skips_blank_lines(tmp_path: Path) -> None:
    path = tmp_path / "script.jsonl"
    path.write_text('{"text": "a", "confidence": 1.0}\n\n\n')
    turns = ScriptedTurn.read_script(path)
    assert len(turns) == 1


def test_synthetic_chunks_are_speech_then_silence() -> None:
    turn = ScriptedTurn(text="hi", confidence=0.9)
    chunks = turn.synthetic_chunks()
    # First chunks are non-silent (speech); trailing chunks are silent.
    assert any(chunk.pcm != chunks[-1].pcm for chunk in chunks[:5])
    assert chunks[-1].pcm == chunks[-2].pcm  # the silence run is uniform


def test_stop_writes_a_stop_request(tmp_path: Path) -> None:
    with patch("punt_vox.commands.call._lock_dir", return_value=tmp_path):
        result = runner.invoke(build_call_app(), ["stop"])
    assert result.exit_code == 0
    control = CallControl(tmp_path / "call.control")
    request = control.consume()
    assert request is not None
    assert request.kind == "stop"


def test_transfer_writes_a_transfer_request_with_session_id(tmp_path: Path) -> None:
    with patch("punt_vox.commands.call._lock_dir", return_value=tmp_path):
        result = runner.invoke(build_call_app(), ["transfer", "--session", "session-b"])
    assert result.exit_code == 0
    control = CallControl(tmp_path / "call.control")
    request = control.consume()
    assert request is not None
    assert request.kind == "transfer"
    assert request.target_session_id == "session-b"


def test_start_requires_the_script_option() -> None:
    result = runner.invoke(build_call_app(), ["start"])
    assert result.exit_code != 0


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

    def _fake_frame(text: str) -> bytes:
        import json

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

    def _fake_process() -> AsyncMock:
        process = AsyncMock()
        process.stdin = AsyncMock()
        process.stdin.write = _discard
        process.stdin.close = MagicMock()
        process.stdout = _AsyncLines([_fake_frame("It returns the sum.")])
        process.communicate.return_value = (b"", b"")
        process.returncode = 0
        return process

    with (
        patch.object(call_module, "VoxClientSync", return_value=_FakeClient()),
        patch(
            "punt_vox.voxd.conversation_mode.claude_session_attach.asyncio.create_subprocess_exec",
            return_value=_fake_process(),
        ),
        patch("punt_vox.commands.call._lock_dir", return_value=tmp_path),
    ):
        await call_module._run_call(script, "session-a")

    assert "It returns the sum." in spoken
    from punt_vox.voxd.conversation_mode.call_lock import CallLock

    assert CallLock(tmp_path / "call.lock").read() is None  # released after hangup


if __name__ == "__main__":
    pytest.main([__file__])
