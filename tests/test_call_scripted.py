"""Tests for punt_vox.commands.call_scripted."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from pathlib import Path

from punt_vox.commands.call_scripted import ScriptedSTTProvider, ScriptedTurn
from punt_vox.voxd.conversation_mode.audio_chunk import AudioChunk


def _script_file(tmp_path: Path, lines: list[dict[str, object]]) -> Path:
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


def test_silence_chunks_are_all_silent() -> None:
    chunks = ScriptedTurn.silence_chunks(5)
    assert len(chunks) == 5
    assert all(c.pcm == chunks[0].pcm for c in chunks)


async def _empty_chunks() -> AsyncIterator[AudioChunk]:
    return
    yield  # pragma: no cover -- makes this an async generator with no items


class TestScriptedSTTProvider:
    def test_name(self) -> None:
        assert ScriptedSTTProvider([]).name == "scripted"

    async def test_replays_turns_in_order(self) -> None:
        turns = [
            ScriptedTurn(text="one", confidence=0.9),
            ScriptedTurn(text="two", confidence=0.95),
        ]
        provider = ScriptedSTTProvider(turns)
        (first,) = [event async for event in provider.transcribe(_empty_chunks())]
        (second,) = [event async for event in provider.transcribe(_empty_chunks())]
        assert first.text == "one"
        assert second.text == "two"

    def test_check_health_reports_no_checks(self) -> None:
        assert ScriptedSTTProvider([]).check_health() == []
