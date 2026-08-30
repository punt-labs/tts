"""Canned Mode B voice conversation and the bounded task prompt derived from it.

DES-071's `launch_session(agent, task, ...)` derives `task` from the voice
conversation up to the launch decision. The spike fixes that conversation as
a canned transcript so every run derives the same short, bounded prompt --
and so the boundedness itself (no shell, no wandering, explicit stop) is a
testable property rather than a hope.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Self, final

# The derived prompt must stay small: it is one voice conversation's intent,
# not a spec document, and a bounded prompt is the isolation contract's cap
# on how much work a spawned session can be asked to do.
MAX_TASK_CHARS = 1400

# The sentence that bounds the spawned session's work. Present verbatim in
# every derived prompt; tests assert on it.
STOP_SENTENCE = (
    "When both files exist, reply with the single word DONE and stop. "
    "Do not run shell commands, do not use the network, and do not read or "
    "write anything outside this directory."
)


@final
@dataclass(frozen=True, slots=True)
class VoiceTurn:
    """One turn of the voice conversation preceding the launch decision."""

    speaker: Literal["user", "agent"]
    text: str


CANNED_TRANSCRIPT: tuple[VoiceTurn, ...] = (
    VoiceTurn("user", "I want to start a tiny Python utility while I walk."),
    VoiceTurn("agent", "Sure -- what should it do?"),
    VoiceTurn(
        "user",
        "A greeting module: a greet function that takes a name and returns "
        "a hello string, plus a short readme describing it.",
    ),
    VoiceTurn("agent", "Want me to spin up a coding session for that now?"),
    VoiceTurn("user", "Yes, launch it."),
)


@final
class TaskSeed:
    """Derives the bounded initial prompt `launch_session` hands the fork."""

    __slots__ = ("_turns",)

    _turns: tuple[VoiceTurn, ...]

    def __new__(cls, turns: tuple[VoiceTurn, ...] = CANNED_TRANSCRIPT) -> Self:
        if not turns:
            msg = "cannot derive a task from an empty transcript"
            raise ValueError(msg)
        self = super().__new__(cls)
        self._turns = turns
        return self

    def derive(self) -> str:
        """Return the initial prompt: user intent summary + bounded task."""
        wants = " ".join(turn.text for turn in self._turns if turn.speaker == "user")
        prompt = (
            "You were launched from a voice conversation (Mode B). "
            f"The user said: {wants}\n"
            "Task: in the current directory create greeting.py defining "
            'greet(name: str) -> str returning f"Hello, {name}!", and a '
            "README.md (three sentences max) describing the module. "
            f"{STOP_SENTENCE}"
        )
        if len(prompt) > MAX_TASK_CHARS:
            msg = f"derived task exceeds {MAX_TASK_CHARS} chars: {len(prompt)}"
            raise ValueError(msg)
        return prompt
