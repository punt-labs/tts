"""Unit tests for the per-module LCOM computation in the coupling scorer."""

from __future__ import annotations

import ast
import textwrap

from tools.coupling.imports import ImportResolver
from tools.coupling.metrics import ModuleCouplingMetrics


def _score(source: str) -> dict[str, float | int | str]:
    tree = ast.parse(textwrap.dedent(source))
    resolver = ImportResolver("mod", frozenset({"mod"}), "pkg")
    return ModuleCouplingMetrics("mod.py", tree, resolver).compute()


def test_frozen_dataclass_public_fields_score_cohesive() -> None:
    """A frozen dataclass value object whose methods share public fields is cohesive.

    Regression for vox-yzme: LCOM previously counted only ``_``-prefixed
    ``self`` references, so a ``SelectionRequest``-shaped value object read
    as if every method touched disjoint state and scored the maximal 1.0.
    """
    metrics = _score(
        """
        from dataclasses import dataclass
        from typing import final

        @final
        @dataclass(frozen=True, slots=True)
        class Selection:
            style: str | None = None
            vibe: str | None = None
            name: str | None = None

            def is_empty(self) -> bool:
                return self.style is None and self.vibe is None and self.name is None

            def matches(self, other: "Selection") -> bool:
                return (
                    self.style == other.style
                    and self.vibe == other.vibe
                    and self.name == other.name
                )

            def with_style(self, style: str) -> "Selection":
                return Selection(style, self.vibe, self.name)
        """
    )
    assert metrics["max_lcom"] == 0.0


def test_private_field_cohesion_still_counted() -> None:
    """Underscore-prefixed fields keep counting as shared state."""
    metrics = _score(
        """
        class Box:
            def __new__(cls, value: int) -> "Box":
                self = super().__new__(cls)
                self._value = value
                return self

            def get(self) -> int:
                return self._value

            def doubled(self) -> int:
                return self._value * 2
        """
    )
    assert metrics["max_lcom"] == 0.0


def test_disjoint_methods_score_incohesive() -> None:
    """Two methods that touch no shared ``self`` attribute score LCOM 1.0."""
    metrics = _score(
        """
        class Split:
            a: int = 1
            b: int = 2

            def reads_a(self) -> int:
                return self.a

            def reads_b(self) -> int:
                return self.b
        """
    )
    assert metrics["max_lcom"] == 1.0
