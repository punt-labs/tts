"""The derived task prompt is deterministic, short, and explicitly bounded."""

from __future__ import annotations

import pytest

from transcript import (
    CANNED_TRANSCRIPT,
    MAX_TASK_CHARS,
    STOP_SENTENCE,
    TaskSeed,
    VoiceTurn,
)


class TestTaskSeed:
    """Boundedness of the prompt handed to the spawned session."""

    def test_derivation_is_deterministic(self) -> None:
        assert TaskSeed().derive() == TaskSeed().derive()

    def test_prompt_stays_under_the_cap(self) -> None:
        assert len(TaskSeed().derive()) <= MAX_TASK_CHARS

    def test_prompt_carries_the_stop_sentence_verbatim(self) -> None:
        assert STOP_SENTENCE in TaskSeed().derive()

    def test_prompt_folds_in_only_user_turns(self) -> None:
        prompt = TaskSeed().derive()
        agent_lines = [t.text for t in CANNED_TRANSCRIPT if t.speaker == "agent"]
        assert all(line not in prompt for line in agent_lines)
        assert "greeting" in prompt.lower()

    def test_empty_transcript_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="empty transcript"):
            TaskSeed(())

    def test_oversized_transcript_is_rejected_at_derive(self) -> None:
        huge = (VoiceTurn("user", "x" * (MAX_TASK_CHARS + 1)),)
        with pytest.raises(ValueError, match="exceeds"):
            TaskSeed(huge).derive()
