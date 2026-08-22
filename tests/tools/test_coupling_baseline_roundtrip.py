"""Guard the scorer/baseline round-trip against writer regressions.

A baseline whose values disagree with the current scorer causes false
REGRESSED verdicts on any touched file whose seed values are stale — the
exact class of bug that let 88 entries hold `efferent_coupling = 1.0` in
the tree until vox-orvz. The invariant is simple: score, save, load,
and the loaded numbers must equal a fresh score of the same tree.
"""

from __future__ import annotations

from pathlib import Path
from typing import Self

from tools.coupling.baseline import CouplingBaseline
from tools.coupling.scorer import CouplingScorer

_MOD_A = (
    "from __future__ import annotations\n\n"
    "from .b import B\n"
    "from .c import C\n\n"
    '__all__ = ["A"]\n\n\n'
    "class A:\n"
    "    _b: B\n"
    "    _c: C\n\n"
    "    def __new__(cls) -> 'A':\n"
    "        self = super().__new__(cls)\n"
    "        self._b = B()\n"
    "        self._c = C()\n"
    "        return self\n"
)
_MOD_B = (
    "from __future__ import annotations\n\n"
    "from .c import C\n\n"
    '__all__ = ["B"]\n\n\n'
    "class B:\n"
    "    _c: C\n\n"
    "    def __new__(cls) -> 'B':\n"
    "        self = super().__new__(cls)\n"
    "        self._c = C()\n"
    "        return self\n"
)
_MOD_C = (
    "from __future__ import annotations\n\n"
    '__all__ = ["C"]\n\n\n'
    "class C:\n"
    "    _n: int\n\n"
    "    def __new__(cls) -> 'C':\n"
    "        self = super().__new__(cls)\n"
    "        self._n = 0\n"
    "        return self\n"
)


class _Package:
    """A tiny synthetic package under ``tmp_path`` for round-tripping."""

    _root: Path
    _pkg: Path

    def __new__(cls, tmp: Path) -> Self:
        self = super().__new__(cls)
        self._root = tmp
        self._pkg = tmp / "sample_pkg"
        self._pkg.mkdir()
        (self._pkg / "__init__.py").write_text("", encoding="utf-8")
        (self._pkg / "a.py").write_text(_MOD_A, encoding="utf-8")
        (self._pkg / "b.py").write_text(_MOD_B, encoding="utf-8")
        (self._pkg / "c.py").write_text(_MOD_C, encoding="utf-8")
        return self

    @property
    def root(self) -> Path:
        """Return the repo-root equivalent for path normalization."""
        return self._root

    @property
    def target(self) -> Path:
        """Return the directory the scorer walks."""
        return self._pkg


class TestBaselineRoundTrip:
    """Score -> save -> reload -> re-score must yield equal per-file metrics."""

    def test_reloaded_entries_equal_fresh_score(self, tmp_path: Path) -> None:
        pkg = _Package(tmp_path)
        first = CouplingScorer(pkg.target, repo_root=pkg.root)
        baseline = CouplingBaseline(pkg.root)
        baseline.save(CouplingBaseline.metrics_by_file(first.results))

        reloaded = CouplingBaseline(pkg.root)
        second = CouplingScorer(pkg.target, repo_root=pkg.root)
        fresh = CouplingBaseline.metrics_by_file(second.results)

        assert reloaded.entries == fresh

    def test_every_scored_file_lands_in_baseline(self, tmp_path: Path) -> None:
        pkg = _Package(tmp_path)
        scorer = CouplingScorer(pkg.target, repo_root=pkg.root)
        baseline = CouplingBaseline(pkg.root)
        baseline.save(CouplingBaseline.metrics_by_file(scorer.results))

        assert set(baseline.entries) == scorer.files
