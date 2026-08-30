"""Machine adjudication of the barge-in state-integrity criteria from a trace.

The four criteria, ruled on from the event-trace JSONL alone:

1. an ``interruption`` (or ``agent_response_correction``) event landed
   between the ``search_code`` ``client_tool_call`` and the agent's
   post-tool response;
2. the session survived (no WS close, agent turns after the barge-in);
3. the answer to the recall probe references the tool's reported
   findings rather than being confused or absent;
4. ``write_note`` still round-trips cleanly after the barge-in.

PASS requires all four. No ``search_code`` call or no interruption event
at all means the scenario was never reached: INCONCLUSIVE, not FAIL.

Ordering comes from trace position (``seq``), never from the ``ms``
stamps: the trace is append-only, while adjacent wall-clock stamps can
collide at their 0.1ms resolution. ``ms`` appears only in evidence text.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Self, final

# Events that evidence a barge-in on the server side.
_INTERRUPT_TYPES: frozenset[str] = frozenset(
    {"interruption", "agent_response_correction"}
)

# Vocabulary distinctive of the search_code RESULT (spike_tools fake
# matches), deliberately excluding "playback"/"queue", which appear in
# the user's own request -- an echo of the question must not pass. No
# bare "match" either: "I didn't find any matches" must not count, so
# only the count-bearing forms the tool actually returns qualify.
_RESULT_MARKERS: tuple[str, ...] = (
    "3 matches",
    "three matches",
    "daemon",
    "dispatch",
    "registry",
    "voxd",
    "provider",
)

# A negated answer is not grounded in the result even when it happens
# to name one of the markers ("I did not find the daemon dispatch").
_NEGATION_MARKERS: tuple[str, ...] = (
    "did not find",
    "didn't find",
    "found nothing",
    "no matches",
    "was not able to find",
    "wasn't able to find",
)

_END_OF_TRACE = 1 << 62  # sentinel seq: "after every recorded event"


class Verdict(StrEnum):
    """Outcome of the adjudication."""

    PASSED = "PASS"
    FAILED = "FAIL"
    INCONCLUSIVE = "INCONCLUSIVE"


@dataclass(frozen=True, slots=True)
class TraceEvent:
    """One parsed line of the EventTrace JSONL, with its trace position."""

    seq: int
    ms: float
    direction: str
    event_type: str
    body: Mapping[str, object]  # wire boundary: shape varies per event type

    @classmethod
    def from_line(cls, seq: int, line: str) -> Self:
        data = dict(json.loads(line))
        return cls(
            seq=seq,
            ms=float(str(data.pop("ms"))),
            direction=str(data.pop("dir")),
            event_type=str(data.pop("type")),
            body=data,
        )

    def stamp(self) -> str:
        """Human-readable position for evidence strings."""
        return f"#{self.seq}@{self.ms}ms"


@dataclass(frozen=True, slots=True)
class CriterionResult:
    """One criterion's ruling plus the trace evidence it rests on."""

    name: str
    passed: bool
    evidence: str

    def line(self) -> str:
        mark = "x" if self.passed else " "
        return f"  [{mark}] {self.name}: {self.evidence}"


@dataclass(frozen=True, slots=True)
class BargeInVerdict:
    """The overall ruling with per-criterion evidence."""

    verdict: Verdict
    criteria: tuple[CriterionResult, ...]
    answer_text: str

    @classmethod
    def inconclusive(cls, reason: str) -> Self:
        scenario = CriterionResult(name="scenario", passed=False, evidence=reason)
        return cls(verdict=Verdict.INCONCLUSIVE, criteria=(scenario,), answer_text="")

    def summary(self) -> str:
        lines = [f"barge-in state integrity: {self.verdict}"]
        lines.extend(criterion.line() for criterion in self.criteria)
        return "\n".join(lines)

    def as_dict(self) -> dict[str, object]:
        return {
            "verdict": str(self.verdict),
            "criteria": [
                {"name": c.name, "passed": c.passed, "evidence": c.evidence}
                for c in self.criteria
            ],
            "answer_text": self.answer_text,
        }


@final
class BargeInAdjudicator:
    """Rule on the four barge-in criteria over a parsed event trace."""

    _events: tuple[TraceEvent, ...]
    _markers: tuple[str, ...]

    def __new__(
        cls,
        events: Sequence[TraceEvent],
        *,
        markers: tuple[str, ...] = _RESULT_MARKERS,
    ) -> Self:
        self = super().__new__(cls)
        self._events = tuple(sorted(events, key=lambda e: e.seq))
        self._markers = markers
        return self

    @classmethod
    def from_jsonl(cls, path: Path) -> Self:
        lines = path.read_text(encoding="utf-8").splitlines()
        return cls(
            tuple(
                TraceEvent.from_line(seq, line)
                for seq, line in enumerate(lines)
                if line
            )
        )

    def adjudicate(self) -> BargeInVerdict:
        """Return the ruling; INCONCLUSIVE when the scenario never happened."""
        call = self._search_call()
        if call is None:
            return BargeInVerdict.inconclusive(
                "search_code was never invoked -- the scenario was not reached"
            )
        interrupts = self._interrupt_events()
        if not interrupts:
            return BargeInVerdict.inconclusive(
                "no interruption or agent_response_correction event in the "
                "trace -- VAD never triggered on the synthesized voice"
            )
        criteria = (
            self._interruption_mid_call(call, interrupts),
            self._session_survived(interrupts),
            self._answer_responsive(),
            self._note_after_barge_in(interrupts),
        )
        all_passed = all(c.passed for c in criteria)
        verdict = Verdict.PASSED if all_passed else Verdict.FAILED
        return BargeInVerdict(
            verdict=verdict,
            criteria=criteria,
            answer_text=self._probe_answer_text(),
        )

    # -- Criteria -----------------------------------------------------------

    def _interruption_mid_call(
        self, call: TraceEvent, interrupts: tuple[TraceEvent, ...]
    ) -> CriterionResult:
        name = "interruption mid tool call"
        post = self._post_tool_response(call)
        post_seq = post.seq if post is not None else _END_OF_TRACE
        post_stamp = post.stamp() if post is not None else "absent"
        inside = [e for e in interrupts if call.seq <= e.seq <= post_seq]
        if inside:
            hit = inside[0]
            evidence = (
                f"{hit.event_type} at {hit.stamp()}, inside "
                f"[tool_call {call.stamp()}, post-tool response {post_stamp}]"
            )
            return CriterionResult(name=name, passed=True, evidence=evidence)
        stamps = [f"{e.event_type}@{e.stamp()}" for e in interrupts]
        evidence = (
            f"interrupt events {stamps} all outside "
            f"[tool_call {call.stamp()}, post-tool response {post_stamp}]"
        )
        return CriterionResult(name=name, passed=False, evidence=evidence)

    def _session_survived(self, interrupts: tuple[TraceEvent, ...]) -> CriterionResult:
        name = "session survived"
        closed = [e for e in self._events if e.event_type == "ws_closed"]
        if closed:
            reason = closed[0].body.get("reason", "")
            evidence = f"WS closed at {closed[0].stamp()}: {reason}"
            return CriterionResult(name=name, passed=False, evidence=evidence)
        first = interrupts[0]
        later = [
            e
            for e in self._events
            if e.direction == "recv"
            and e.event_type == "agent_response"
            and e.seq > first.seq
        ]
        if not later:
            evidence = f"no agent_response after the barge-in at {first.stamp()}"
            return CriterionResult(name=name, passed=False, evidence=evidence)
        evidence = (
            f"no WS close; {len(later)} agent turns after the barge-in "
            f"at {first.stamp()}"
        )
        return CriterionResult(name=name, passed=True, evidence=evidence)

    def _answer_responsive(self) -> CriterionResult:
        name = "recall answer references tool findings"
        answer = self._probe_answer()
        if answer is None:
            return CriterionResult(
                name=name,
                passed=False,
                evidence="no agent_response after the recall probe",
            )
        text = str(answer.body.get("text", ""))
        lowered = text.lower()
        negation = next((n for n in _NEGATION_MARKERS if n in lowered), None)
        if negation is not None:
            return CriterionResult(
                name=name,
                passed=False,
                evidence=f"negated answer ({negation!r}): {text!r}",
            )
        hit = next((m for m in self._markers if m in lowered), None)
        if hit is None:
            return CriterionResult(
                name=name,
                passed=False,
                evidence=f"no result marker in the answer: {text!r}",
            )
        evidence = f"marker {hit!r} in the answer: {text!r}"
        return CriterionResult(name=name, passed=True, evidence=evidence)

    def _note_after_barge_in(
        self, interrupts: tuple[TraceEvent, ...]
    ) -> CriterionResult:
        name = "write_note works after barge-in"
        first = interrupts[0]
        result = next(
            (
                e
                for e in self._events
                if e.direction == "send"
                and e.event_type == "client_tool_result"
                and e.body.get("tool") == "write_note"
                and e.seq > first.seq
                and not e.body.get("is_error")
            ),
            None,
        )
        if result is None:
            evidence = (
                f"no clean write_note result after the barge-in at {first.stamp()}"
            )
            return CriterionResult(name=name, passed=False, evidence=evidence)
        evidence = (
            f"write_note result posted cleanly at {result.stamp()} "
            f"(after {first.stamp()})"
        )
        return CriterionResult(name=name, passed=True, evidence=evidence)

    # -- Trace queries -------------------------------------------------------

    def _search_call(self) -> TraceEvent | None:
        # None: absence is itself evidence -- adjudicate() rules INCONCLUSIVE.
        return next(
            (
                e
                for e in self._events
                if e.direction == "recv"
                and e.event_type == "client_tool_call"
                and e.body.get("tool") == "search_code"
            ),
            None,
        )

    def _interrupt_events(self) -> tuple[TraceEvent, ...]:
        return tuple(
            e
            for e in self._events
            if e.direction == "recv" and e.event_type in _INTERRUPT_TYPES
        )

    def _post_tool_response(self, call: TraceEvent) -> TraceEvent | None:
        """First agent_response after the call's result; None when absent."""
        call_id = call.body.get("tool_call_id")
        result = next(
            (
                e
                for e in self._events
                if e.direction == "send"
                and e.event_type == "client_tool_result"
                and e.body.get("tool_call_id") == call_id
            ),
            None,
        )
        if result is None:
            return None
        return next(
            (
                e
                for e in self._events
                if e.direction == "recv"
                and e.event_type == "agent_response"
                and e.seq > result.seq
            ),
            None,
        )

    def _probe_answer(self) -> TraceEvent | None:
        # None: no answer arrived -- _answer_responsive fails with evidence.
        probe = next(
            (
                e
                for e in self._events
                if e.event_type == "barge_in_step"
                and e.body.get("step") == "probe_recall"
            ),
            None,
        )
        if probe is None:
            return None
        return next(
            (
                e
                for e in self._events
                if e.direction == "recv"
                and e.event_type == "agent_response"
                and e.seq > probe.seq
            ),
            None,
        )

    def _probe_answer_text(self) -> str:
        answer = self._probe_answer()
        return str(answer.body.get("text", "")) if answer is not None else ""
