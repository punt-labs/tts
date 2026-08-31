# /// script
# requires-python = ">=3.13"
# dependencies = []
# ///
"""Offline "what was I just doing?" reconstruction from a ledger tail.

The verdict core: given ONLY the last N ledger events at a sampled
timepoint, produce a deterministic, auditable answer to "what was I just
doing?" -- current goal, recent actions, last tool result, open failure,
files in play. No LLM anywhere: the answer is a template over extracted
payload fields, so a grader can trace every line back to a ledger record,
and a graded FAIL indicts the payloads, not a model's imagination.

The same answer shape is produced by the seed-only path (``seed_builder``),
so the two conditions are graded apples-to-apples against the pane-capture
ground truth.

Run:  uv run reconstructor.py --ledger <path> --cutoff <recv_seq> [--n 15]
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from pathlib import Path
from typing import Self, final

from stamp import HookLedger, HookRecord

# How many trailing events "the last N raw turns" means here. DES-070
# leaves N open; 15 comfortably spans a few tool cycles plus the prompt.
DEFAULT_TAIL_N = 15

# Markers that flag a tool response as a failure in flight. Tuned to the
# seeded task's stdlib unittest output plus generic Python failure shapes.
FAILURE_MARKERS: tuple[str, ...] = (
    "FAILED (",
    "Traceback (most recent call last)",
    "AssertionError",
    "SyntaxError",
    "exit code 1",
)

# Markers that flag a test run as green again (unittest's trailing OK).
SUCCESS_MARKERS: tuple[str, ...] = ("\nOK", "\r\nOK")

_GOAL_CHARS = 400
_RESULT_CHARS = 400
_ACTION_CHARS = 120
_MAX_ACTIONS = 5
_MAX_FILES = 8


def response_text(record: HookRecord) -> str:
    """The tool response of a PostToolUse record flattened to text.

    Claude Code wraps Bash output as ``{"stdout": ..., "stderr": ...}``;
    a ``json.dumps`` of that escapes the newlines the failure/success
    markers key on (``\\nOK``), so string LEAVES are collected instead
    and joined with real newlines.
    """
    raw = record.payload.get("tool_response")
    if raw is None:
        return ""
    if isinstance(raw, str):
        return raw
    leaves: list[str] = []
    _collect_strings(raw, leaves)
    return "\n".join(leaves)


def _collect_strings(value: object, into: list[str]) -> None:
    if isinstance(value, str):
        if value:
            into.append(value)
    elif isinstance(value, dict):
        for item in value.values():
            _collect_strings(item, into)
    elif isinstance(value, list):
        for item in value:
            _collect_strings(item, into)


def has_failure(text: str) -> bool:
    """True when the text carries any failure marker."""
    return any(marker in text for marker in FAILURE_MARKERS)


def has_success(text: str) -> bool:
    """True when the text carries a green-suite marker."""
    return any(marker in text for marker in SUCCESS_MARKERS)


def _clip(text: str, limit: int) -> str:
    cleaned = " ".join(text.split())
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[: limit - 3] + "..."


def _tool_subject(payload: dict[str, object]) -> str:
    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict):
        return ""
    for field in ("file_path", "command", "pattern", "path"):
        value = tool_input.get(field)
        if isinstance(value, str) and value:
            return _clip(value, _ACTION_CHARS)
    return ""


@final
@dataclass(frozen=True, slots=True)
class ContextAnswer:
    """One reconstruction, quotable verbatim in the REPORT for grading."""

    source: str
    timepoint: str
    goal: str
    recent_actions: tuple[str, ...]
    last_result: str
    open_failure: str
    files_in_play: tuple[str, ...]
    agent_report: str

    def render(self) -> str:
        """The graded text block."""
        actions = "\n".join(f"  - {a}" for a in self.recent_actions) or "  (none)"
        files = ", ".join(self.files_in_play) or "(none)"
        return (
            f"[{self.source} @ {self.timepoint}] What was I just doing?\n"
            f"goal: {self.goal or '(unknown)'}\n"
            f"recent actions:\n{actions}\n"
            f"last result: {self.last_result or '(none)'}\n"
            f"open failure: {self.open_failure or 'none'}\n"
            f"files in play: {files}\n"
            f"agent last said: {self.agent_report or '(nothing yet)'}"
        )


@final
class TailReconstructor:
    """Builds a ContextAnswer from the last N events at a cutoff."""

    __slots__ = ("_tail",)

    _tail: tuple[HookRecord, ...]

    def __new__(
        cls,
        records: tuple[HookRecord, ...],
        cutoff_index: int,
        n: int = DEFAULT_TAIL_N,
    ) -> Self:
        # The cutoff is a FILE-ORDER index (1-based count of records
        # visible at the sampled moment), never a recv_seq: the store's
        # receiver-assigned sequence restarts at 1 when the store
        # restarts, so recv_seq values collide across restarts and a
        # seq-based cut silently admits post-restart records into an
        # earlier timepoint. Observed live in the capture run; the same
        # trap applies to DES-070's receiver-side stamping.
        self = super().__new__(cls)
        self._tail = tuple(records[:cutoff_index][-n:])
        return self

    @property
    def tail(self) -> tuple[HookRecord, ...]:
        """The events the reconstruction is allowed to see."""
        return self._tail

    def answer(self, timepoint: str) -> ContextAnswer:
        """Reconstruct from the tail alone."""
        return ContextAnswer(
            source="ledger-tail",
            timepoint=timepoint,
            goal=self._goal(),
            recent_actions=self._recent_actions(),
            last_result=self._last_result(),
            open_failure=self._open_failure(),
            files_in_play=self._files_in_play(),
            agent_report=self._agent_report(),
        )

    def _goal(self) -> str:
        for record in reversed(self._tail):
            if record.event != "UserPromptSubmit":
                continue
            prompt = record.payload.get("prompt")
            if isinstance(prompt, str) and prompt:
                return _clip(prompt, _GOAL_CHARS)
        return ""

    def _tool_uses(self) -> tuple[HookRecord, ...]:
        return tuple(r for r in self._tail if r.event == "PostToolUse")

    def _recent_actions(self) -> tuple[str, ...]:
        actions = []
        for record in self._tool_uses()[-_MAX_ACTIONS:]:
            name = record.payload.get("tool_name")
            tool = name if isinstance(name, str) else "?"
            subject = _tool_subject(record.payload)
            actions.append(f"{tool}: {subject}" if subject else tool)
        return tuple(actions)

    def _last_result(self) -> str:
        for record in reversed(self._tool_uses()):
            text = response_text(record)
            if text:
                # The tail of a long response carries the verdict lines
                # (test summaries, error trails); the head is preamble.
                return _clip(text[-1200:], _RESULT_CHARS)
        return ""

    def _open_failure(self) -> str:
        # Newest-first: a failure is open only if no later response went
        # green. The first hit decides.
        for record in reversed(self._tool_uses()):
            text = response_text(record)
            if has_success(text):
                return ""
            if has_failure(text):
                return _clip(text[-800:], _RESULT_CHARS)
        return ""

    def _agent_report(self) -> str:
        # Stop payloads carry the assistant's final message for the turn;
        # the newest one is the agent's own account of what it just did.
        for record in reversed(self._tail):
            if record.event != "Stop":
                continue
            raw = record.payload.get("last_assistant_message")
            if isinstance(raw, str) and raw:
                return _clip(raw, _RESULT_CHARS)
        return ""

    def _files_in_play(self) -> tuple[str, ...]:
        seen: dict[str, None] = {}
        for record in self._tool_uses():
            tool_input = record.payload.get("tool_input")
            if not isinstance(tool_input, dict):
                continue
            path = tool_input.get("file_path")
            if isinstance(path, str) and path:
                seen[path] = None
        return tuple(list(seen)[-_MAX_FILES:])


def main() -> None:
    """CLI entry: reconstruct at one cutoff and print the answer."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ledger", type=Path, required=True)
    parser.add_argument(
        "--cutoff", type=int, required=True, help="file-order record count"
    )
    parser.add_argument("--n", type=int, default=DEFAULT_TAIL_N)
    parser.add_argument("--timepoint", default="ad-hoc")
    args = parser.parse_args()
    records = HookLedger(args.ledger).records()
    reconstructor = TailReconstructor(records, args.cutoff, args.n)
    print(reconstructor.answer(args.timepoint).render())


if __name__ == "__main__":
    main()
