"""Nearest-rank percentile summary shared by the ledger analyzers."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import TYPE_CHECKING, Self, final

if TYPE_CHECKING:
    from collections.abc import Sequence


@final
@dataclass(frozen=True, slots=True)
class PercentileStats:
    """n / p50 / p95 / max of a sample, nearest-rank."""

    n: int
    p50: float
    p95: float
    max: float

    @classmethod
    def of(cls, values: Sequence[float]) -> Self:
        """Summarize ``values``; an empty sample is all zeros."""
        if not values:
            return cls(n=0, p50=0.0, p95=0.0, max=0.0)
        ordered = sorted(values)
        return cls(
            n=len(ordered),
            p50=cls._rank(ordered, 0.50),
            p95=cls._rank(ordered, 0.95),
            max=ordered[-1],
        )

    @staticmethod
    def _rank(ordered: Sequence[float], p: float) -> float:
        index = max(math.ceil(p * len(ordered)) - 1, 0)
        return ordered[index]

    def as_dict(self) -> dict[str, float | int]:
        """Machine-readable form for the JSON artifacts."""
        return {
            "n": self.n,
            "p50": round(self.p50, 1),
            "p95": round(self.p95, 1),
            "max": round(self.max, 1),
        }

    def row(self, label: str) -> str:
        """One aligned table row."""
        return (
            f"{label:<36} {self.n:>4} {self.p50:>10.1f} "
            f"{self.p95:>10.1f} {self.max:>10.1f}"
        )

    @staticmethod
    def header() -> str:
        """The table header matching :meth:`row`."""
        return f"{'metric':<36} {'n':>4} {'p50':>10} {'p95':>10} {'max':>10}"
