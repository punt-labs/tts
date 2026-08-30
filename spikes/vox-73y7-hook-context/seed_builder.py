# /// script
# requires-python = ">=3.13"
# dependencies = []
# ///
"""The `/vox:talk` seed prototype: hand-picked context in a bounded payload.

Bead question (d): does a seed alone compensate for a thin rolling store?
This builder is DES-070's Layer 1 shape in miniature: from the same ledger
a rolling store would hold, it HAND-PICKS the context a primary session
would fold into a call-start snapshot -- current goal, active files, the
last few tool results, the freshest open failure -- and bounds the whole
payload at ~10KB, trimming oldest results first.

The seed then answers the same four "what was I just doing?" timepoints
through :class:`SeedReconstructor`, producing the identical answer shape
as the ledger-tail path so the grading is apples-to-apples.

Run:  uv run seed_builder.py --ledger <path> --cutoff <recv_seq>
"""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Self, final

from reconstructor import ContextAnswer, TailReconstructor, response_text
from stamp import HookLedger, HookRecord

# The seed budget the bead names: ~10KB.
SEED_BUDGET_BYTES = 10_240

# The seed curates deeper than a display tail: it may reach back over the
# whole visible ledger for its goal/failure, but keeps only this many tool
# results verbatim.
_MAX_RESULTS = 3


@final
@dataclass(frozen=True, slots=True)
class SeedPayload:
    """The bounded, hand-picked call-start snapshot."""

    current_goal: str
    active_files: tuple[str, ...]
    last_tool_results: tuple[str, ...]
    recent_failure: str

    def to_json(self) -> str:
        """Serialize; the budget check applies to this form."""
        return json.dumps(
            {
                "current_goal": self.current_goal,
                "active_files": list(self.active_files),
                "last_tool_results": list(self.last_tool_results),
                "recent_failure": self.recent_failure,
            },
            indent=2,
            sort_keys=True,
        )

    def byte_size(self) -> int:
        """UTF-8 size of the serialized seed."""
        return len(self.to_json().encode("utf-8"))

    def trimmed(self) -> Self:
        """Drop the OLDEST tool result -- the budget-trim step."""
        if not self.last_tool_results:
            msg = "seed cannot be trimmed further: no tool results left"
            raise ValueError(msg)
        return type(self)(
            current_goal=self.current_goal,
            active_files=self.active_files,
            last_tool_results=self.last_tool_results[1:],
            recent_failure=self.recent_failure,
        )


@final
class SeedBuilder:
    """Hand-picks a bounded seed from the ledger visible at a cutoff."""

    __slots__ = ("_visible",)

    _visible: tuple[HookRecord, ...]

    def __new__(cls, records: tuple[HookRecord, ...], cutoff_recv_seq: int) -> Self:
        self = super().__new__(cls)
        self._visible = tuple(r for r in records if r.recv_seq <= cutoff_recv_seq)
        return self

    def build(self) -> SeedPayload:
        """Curate, then trim oldest-first until the budget holds."""
        # The curation reuses the tail extractors over the WHOLE visible
        # ledger (n=len): the seed's advantage over a display tail is
        # reach, its constraint is the byte budget.
        wide = TailReconstructor(self._visible, cutoff_recv_seq=2**31, n=2**31)
        answer = wide.answer("seed-build")
        seed = SeedPayload(
            current_goal=answer.goal,
            active_files=answer.files_in_play,
            last_tool_results=self._verbatim_results(),
            recent_failure=answer.open_failure,
        )
        while seed.byte_size() > SEED_BUDGET_BYTES:
            seed = seed.trimmed()
        return seed

    def _verbatim_results(self) -> tuple[str, ...]:
        results = []
        for record in self._visible:
            if record.event != "PostToolUse":
                continue
            text = response_text(record)
            if text:
                name = record.payload.get("tool_name")
                tool = name if isinstance(name, str) else "?"
                results.append(f"{tool}: {text[-2000:]}")
        return tuple(results[-_MAX_RESULTS:])


@final
class SeedReconstructor:
    """Answers "what was I just doing?" from the seed ALONE."""

    __slots__ = ("_seed",)

    _seed: SeedPayload

    def __new__(cls, seed: SeedPayload) -> Self:
        self = super().__new__(cls)
        self._seed = seed
        return self

    def answer(self, timepoint: str) -> ContextAnswer:
        """The seed-only reconstruction, same shape as the tail path."""
        actions = tuple(
            result.split("\n")[0][:120] for result in self._seed.last_tool_results
        )
        last = (
            self._seed.last_tool_results[-1][-400:]
            if self._seed.last_tool_results
            else ""
        )
        return ContextAnswer(
            source="seed-only",
            timepoint=timepoint,
            goal=self._seed.current_goal,
            recent_actions=actions,
            last_result=" ".join(last.split()),
            open_failure=self._seed.recent_failure,
            files_in_play=self._seed.active_files,
        )


def main() -> None:
    """CLI entry: build one seed at a cutoff, print it and its answer."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ledger", type=Path, required=True)
    parser.add_argument("--cutoff", type=int, required=True)
    parser.add_argument("--timepoint", default="ad-hoc")
    args = parser.parse_args()
    records = HookLedger(args.ledger).records()
    seed = SeedBuilder(records, args.cutoff).build()
    print(f"seed bytes: {seed.byte_size()} (budget {SEED_BUDGET_BYTES})")
    print(SeedReconstructor(seed).answer(args.timepoint).render())


if __name__ == "__main__":
    main()
