"""Integration tests for the merge-base ratchet against real tmp git repos.

Covers the load-bearing behaviors: merge-base base-compare (incl. the
concurrent-main-advance case), scoped update never loosening, the relax
waiver, rename carry, completeness enumeration, the CI-write guard, and the
bootstrap fallback.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Self

import pytest

from tools.oo_ratchet.baseline import Baseline
from tools.oo_ratchet.cli import main
from tools.oo_ratchet.gitio import GitError, GitRepo
from tools.oo_ratchet.ratchet import Ratchet
from tools.oo_ratchet.scorer import Scorer
from tools.oo_ratchet.writer import BaselineWriter

# Every test drives a real ``git init``/``commit``/``checkout`` sequence plus a
# real ``tools/oo_ratchet`` subprocess invocation per assertion -- genuinely
# slow relative to the mocked-boundary suite (measured: this file plus its
# three siblings, 153 tests, took 32s wall on a clean, uncontended run; see
# TESTING.md, which also records the ~8x slowdown observed under CPU
# contention from other agents sharing the dev box).
pytestmark = pytest.mark.slow

GOOD = '''from __future__ import annotations


class Widget:
    """A widget."""

    _n: int

    def __new__(cls, n: int) -> "Widget":
        self = super().__new__(cls)
        self._n = n
        return self

    def label(self) -> str:
        return "pos"
'''

WORSE = '''from __future__ import annotations


class Widget:
    """A widget."""

    _n: int

    def __new__(cls, n: int) -> "Widget":
        self = super().__new__(cls)
        self._n = n
        return self

    def label(self) -> str:
        if self._n > 0:
            if self._n > 10:
                return "big"
            return "pos"
        return "neg"
'''

BROKEN = "def oops(:\n    pass\n"

# Byte-different from GOOD but metric-identical: same line count, same
# complexity, only a string literal differs. Models a mechanical version bump
# (e.g. __version__ = "1.0.0" -> "1.0.1") that touches a scored file yet
# improves no metric. Guard the substring so a drift in GOOD fails loudly here
# instead of silently no-op'ing .replace() (GOOD_VARIANT == GOOD would stop
# exercising the metric-neutral case).
assert 'return "pos"' in GOOD, "GOOD fixture drifted; GOOD_VARIANT target missing"
GOOD_VARIANT = GOOD.replace('return "pos"', 'return "neg"')


class GitFixture:
    """A throwaway git repo for exercising the ratchet end to end."""

    _root: Path

    def __new__(cls, tmp: Path) -> Self:
        self = super().__new__(cls)
        subprocess.run(["git", "init", "-q", "-b", "main"], cwd=tmp, check=True)
        for key, val in (
            ("user.email", "t@example.com"),
            ("user.name", "Tester"),
            ("commit.gpgsign", "false"),
        ):
            subprocess.run(["git", "config", key, val], cwd=tmp, check=True)
        out = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=tmp,
            check=True,
            capture_output=True,
            text=True,
        )
        self._root = Path(out.stdout.strip())
        return self

    @property
    def root(self) -> Path:
        """Return the repository root."""
        return self._root

    def write(self, rel: str, content: str) -> None:
        path = self._root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)

    def write_baseline(self, data: dict[str, dict[str, float]]) -> None:
        (self._root / ".oo-baseline.json").write_text(json.dumps(data, indent=2) + "\n")

    def snapshot(self, subdir: str) -> None:
        scorer = Scorer(self._root / subdir, self._root)
        self.write_baseline(Baseline.metrics_by_file(scorer.results))

    def commit(self, msg: str) -> str:
        subprocess.run(["git", "add", "-A"], cwd=self._root, check=True)
        subprocess.run(["git", "commit", "-q", "-m", msg], cwd=self._root, check=True)
        return self._head()

    def move(self, src: str, dst: str) -> None:
        (self._root / dst).parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "mv", src, dst], cwd=self._root, check=True)

    def checkout_new(self, branch: str) -> None:
        subprocess.run(
            ["git", "checkout", "-q", "-b", branch], cwd=self._root, check=True
        )

    def checkout(self, branch: str) -> None:
        subprocess.run(["git", "checkout", "-q", branch], cwd=self._root, check=True)

    def set_origin_main(self, sha: str) -> None:
        subprocess.run(
            ["git", "update-ref", "refs/remotes/origin/main", sha],
            cwd=self._root,
            check=True,
        )

    def _head(self) -> str:
        out = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=self._root,
            check=True,
            capture_output=True,
            text=True,
        )
        return out.stdout.strip()

    def ratchet(self) -> Ratchet:
        return Ratchet(self._root, GitRepo(self._root))

    def writer(self) -> BaselineWriter:
        return BaselineWriter(self._root, GitRepo(self._root))

    def scorer(self, subdir: str = "sub") -> Scorer:
        return Scorer(self._root / subdir, self._root)


@pytest.fixture
def fx(tmp_path: Path) -> GitFixture:
    return GitFixture(tmp_path)


class TestBaseCompare:
    """Compare HEAD against the baseline committed at the base commit."""

    def test_improvement_passes(self, fx: GitFixture) -> None:
        fx.write("sub/w.py", WORSE)
        fx.snapshot("sub")
        base = fx.commit("base")
        fx.write("sub/w.py", GOOD)
        fx.snapshot("sub")
        fx.commit("improve")
        outcome = fx.ratchet().check(fx.scorer(), base_ref=base, require_base=True)
        assert outcome.exit_code == 0

    def test_regression_fails(self, fx: GitFixture) -> None:
        fx.write("sub/w.py", GOOD)
        fx.snapshot("sub")
        base = fx.commit("base")
        fx.write("sub/w.py", WORSE)
        fx.snapshot("sub")
        fx.commit("regress")
        outcome = fx.ratchet().check(fx.scorer(), base_ref=base, require_base=True)
        assert outcome.exit_code == 1
        assert any("regression" in line for line in outcome.lines)

    def test_merge_base_scopes_out_concurrent_main_change(self, fx: GitFixture) -> None:
        # gvr R1: a file regressed only on main is not in the PR's touched set.
        fx.write("sub/a.py", WORSE)
        fx.write("sub/b.py", GOOD)
        fx.snapshot("sub")
        fx.commit("fork")
        fx.checkout_new("feature")
        fx.write("sub/a.py", GOOD)  # PR improves a.py only
        fx.snapshot("sub")
        fx.commit("improve a")
        fx.checkout("main")
        fx.write("sub/b.py", WORSE)  # main regresses b.py concurrently
        fx.snapshot("sub")
        main_head = fx.commit("regress b on main")
        fx.set_origin_main(main_head)
        fx.checkout("feature")
        # Default base resolution uses merge-base(origin/main, HEAD) == fork.
        outcome = fx.ratchet().check(fx.scorer(), base_ref=None, require_base=True)
        assert outcome.exit_code == 0
        assert fx.root  # b.py's main-only regression did not fail the PR


class TestAllowNoImprovement:
    """--allow-no-improvement waives only the must-improve gate.

    Regressions, baseline lock-in, and completeness stay enforced. This is the
    gate mechanical version-bump PRs (release/* and post-release/*) need: they
    touch a scored file (__version__) but improve no metric.
    """

    def test_flag_passes_no_improvement_no_regression(self, fx: GitFixture) -> None:
        # A metric-neutral change (version bump) touches the file but neither
        # improves nor regresses: the flag lets it pass.
        fx.write("sub/w.py", GOOD)
        fx.snapshot("sub")
        base = fx.commit("base")
        fx.write("sub/w.py", GOOD_VARIANT)
        fx.snapshot("sub")  # in-tree baseline == current, locked
        fx.commit("metric-neutral change")
        outcome = fx.ratchet().check(
            fx.scorer(), base_ref=base, require_base=True, allow_no_improvement=True
        )
        assert outcome.exit_code == 0
        assert any("must-improve gate waived" in line for line in outcome.lines)

    def test_flag_still_fails_regression(self, fx: GitFixture) -> None:
        # The flag never launders a regression.
        fx.write("sub/w.py", GOOD)
        fx.snapshot("sub")
        base = fx.commit("base")
        fx.write("sub/w.py", WORSE)
        fx.snapshot("sub")
        fx.commit("regress")
        outcome = fx.ratchet().check(
            fx.scorer(), base_ref=base, require_base=True, allow_no_improvement=True
        )
        assert outcome.exit_code == 1
        assert any("regression" in line for line in outcome.lines)

    def test_default_still_fails_no_improvement(self, fx: GitFixture) -> None:
        # Without the flag, a metric-neutral change fails the must-improve gate
        # exactly as before -- the default is unchanged.
        fx.write("sub/w.py", GOOD)
        fx.snapshot("sub")
        base = fx.commit("base")
        fx.write("sub/w.py", GOOD_VARIANT)
        fx.snapshot("sub")
        fx.commit("metric-neutral change")
        outcome = fx.ratchet().check(fx.scorer(), base_ref=base, require_base=True)
        assert outcome.exit_code == 1
        assert any("no metric improved" in line for line in outcome.lines)

    def test_flag_still_enforces_lock_in(self, fx: GitFixture) -> None:
        # A stale in-tree baseline (out of date vs current) still fails under the
        # flag: lock-in is orthogonal to the must-improve gate.
        fx.write("sub/w.py", GOOD)
        fx.snapshot("sub")
        base = fx.commit("base")
        fx.write("sub/w.py", GOOD_VARIANT)
        current = Baseline.metrics_by_file(fx.scorer().results)["sub/w.py"]
        fx.write_baseline(
            {"sub/w.py": {**current, "module_size": current["module_size"] + 5}}
        )
        fx.commit("touch but leave baseline stale")
        outcome = fx.ratchet().check(
            fx.scorer(), base_ref=base, require_base=True, allow_no_improvement=True
        )
        assert outcome.exit_code == 1
        assert any("baseline out of date" in line for line in outcome.lines)

    def test_flag_still_requires_baseline_entry(self, fx: GitFixture) -> None:
        # A touched file absent from the in-tree baseline still fails under the
        # flag: completeness is orthogonal to the must-improve gate.
        fx.write("sub/w.py", GOOD)
        fx.snapshot("sub")
        base = fx.commit("base")
        fx.write("sub/w.py", GOOD_VARIANT)
        fx.write_baseline({})  # drop the file's in-tree entry
        fx.commit("touch but empty in-tree baseline")
        outcome = fx.ratchet().check(
            fx.scorer(), base_ref=base, require_base=True, allow_no_improvement=True
        )
        assert outcome.exit_code == 1
        assert any("not in baseline" in line for line in outcome.lines)


class TestEmptyBaseBaseline:
    """An empty ``{}`` base baseline is fail-closed under --require-base."""

    def test_empty_base_baseline_fails_closed(self, fx: GitFixture) -> None:
        # An empty {} baseline at the base (truncated write / bad merge) makes
        # every touched file look new -> would pass the must-improve comparison.
        # Under require_base, fail closed exactly like a missing baseline.
        fx.write("sub/w.py", GOOD)
        fx.write_baseline({})
        base = fx.commit("base with empty baseline")
        fx.write("sub/w.py", WORSE)
        fx.commit("regress")
        outcome = fx.ratchet().check(fx.scorer(), base_ref=base, require_base=True)
        assert outcome.exit_code == 1
        assert any("empty" in line for line in outcome.lines)

    def test_empty_base_baseline_local_still_parses(self, fx: GitFixture) -> None:
        # Empty {} base baseline without --require-base flows through the
        # comparison, which still runs the touched-file parse check: an
        # unparseable touched file fails loud, matching the coupling ratchet.
        fx.write("sub/w.py", GOOD)
        fx.write_baseline({})
        base = fx.commit("base with empty baseline")
        fx.write("sub/w.py", BROKEN)  # unparseable, touched
        fx.commit("break")
        outcome = fx.ratchet().check(fx.scorer(), base_ref=base, require_base=False)
        assert outcome.exit_code == 1
        assert any("failed to parse" in line for line in outcome.lines)


class TestScopedUpdate:
    """Scoped update writes improvements but never loosens."""

    def test_update_refuses_regression(self, fx: GitFixture) -> None:
        fx.write("sub/w.py", GOOD)
        fx.snapshot("sub")
        base = fx.commit("base")
        fx.write("sub/w.py", WORSE)  # regressed vs in-tree baseline
        outcome = fx.writer().update(
            fx.scorer(),
            base_ref=base,
            require_base=False,
            allow_ci_write=True,
            source=None,
        )
        assert outcome.exit_code == 1
        # baseline still holds the good (unloosened) value
        entry = Baseline(fx.root).get("sub/w.py")
        assert entry is not None
        assert entry["max_complexity"] == 1.0

    def test_update_writes_improvement(self, fx: GitFixture) -> None:
        fx.write("sub/w.py", WORSE)
        fx.snapshot("sub")
        base = fx.commit("base")
        fx.write("sub/w.py", GOOD)
        outcome = fx.writer().update(
            fx.scorer(),
            base_ref=base,
            require_base=False,
            allow_ci_write=True,
            source=None,
        )
        assert outcome.exit_code == 0
        entry = Baseline(fx.root).get("sub/w.py")
        assert entry is not None
        assert entry["max_complexity"] == 1.0

    def test_update_refuses_broken_touched_file_keeps_row(self, fx: GitFixture) -> None:
        # A touched file that is on disk but unparseable is not a deletion:
        # update must refuse and preserve its baseline row (Bugbot #2).
        fx.write("sub/w.py", GOOD)
        fx.snapshot("sub")
        base = fx.commit("base")
        fx.write("sub/w.py", BROKEN)  # still on disk, now unparseable
        outcome = fx.writer().update(
            fx.scorer(),
            base_ref=base,
            require_base=False,
            allow_ci_write=True,
            source=None,
        )
        assert outcome.exit_code == 1
        assert any("parse error" in line for line in outcome.lines)
        assert Baseline(fx.root).get("sub/w.py") is not None

    def test_reconcile_preserves_broken_file_baseline(self, fx: GitFixture) -> None:
        # Whole-tree reconcile must not prune a broken-on-disk file's baseline.
        fx.write("sub/w.py", GOOD)
        fx.write("sub/other.py", GOOD)
        fx.snapshot("sub")
        base = fx.commit("base")
        fx.set_origin_main(base)  # reconcile resolves a base for rename provenance
        fx.write("sub/w.py", BROKEN)
        outcome = fx.writer().reconcile(fx.scorer(), allow_ci_write=True, source=None)
        assert outcome.exit_code == 1
        assert Baseline(fx.root).get("sub/w.py") is not None

    def test_reconcile_unresolvable_base_with_baseline_fails_closed(
        self, fx: GitFixture
    ) -> None:
        # reconcile without a resolvable base (no origin/main) and a baseline
        # present must fail closed -- not run with empty rename provenance and
        # launder a regressed rename as new (Bugbot #1).
        fx.write("sub/w.py", GOOD)
        fx.snapshot("sub")
        fx.commit("base")  # no origin/main set -> merge-base unresolvable
        before = dict(Baseline(fx.root).entries)
        outcome = fx.writer().reconcile(fx.scorer(), allow_ci_write=True, source=None)
        assert outcome.exit_code == 1
        assert any("cannot resolve base" in line for line in outcome.lines)
        assert Baseline(fx.root).entries == before  # no write

    def test_update_unresolvable_base_with_baseline_fails_closed(
        self, fx: GitFixture
    ) -> None:
        # Parity with check: scoped update must not silently whole-tree-sweep on
        # an unresolvable base -- fail closed, baseline untouched (Bugbot #1).
        fx.write("sub/w.py", GOOD)
        fx.snapshot("sub")
        fx.commit("base")
        fx.write("sub/other.py", WORSE)  # a new file a whole-tree sweep would add
        before = dict(Baseline(fx.root).entries)
        outcome = fx.writer().update(
            fx.scorer(),
            base_ref="0" * 40,
            require_base=False,
            allow_ci_write=True,
            source=None,
        )
        assert outcome.exit_code == 1
        assert any("cannot resolve base" in line for line in outcome.lines)
        assert Baseline(fx.root).entries == before  # no whole-tree sweep

    def test_update_require_base_unresolvable_fails(self, fx: GitFixture) -> None:
        fx.write("sub/w.py", GOOD)
        fx.snapshot("sub")
        fx.commit("base")
        outcome = fx.writer().update(
            fx.scorer(),
            base_ref="0" * 40,
            require_base=True,
            allow_ci_write=True,
            source=None,
        )
        assert outcome.exit_code == 1
        assert any("--require-base" in line for line in outcome.lines)

    def test_update_bootstrap_no_baseline_whole_tree(self, fx: GitFixture) -> None:
        # First-adoption (no in-tree baseline) still bootstraps the whole tree.
        fx.write("sub/w.py", GOOD)
        fx.commit("pre-adoption, no baseline")
        outcome = fx.writer().update(
            fx.scorer(),
            base_ref="0" * 40,
            require_base=False,
            allow_ci_write=True,
            source=None,
        )
        assert outcome.exit_code == 0
        assert Baseline(fx.root).get("sub/w.py") is not None


class TestRelaxWaiver:
    """A relaxed, locked regression is waived by check."""

    def test_relax_then_check_waives(self, fx: GitFixture) -> None:
        fx.write("sub/w.py", GOOD)
        fx.snapshot("sub")
        base = fx.commit("base")
        fx.write("sub/w.py", WORSE)  # regresses vs base
        relax = fx.writer().relax(
            fx.scorer(),
            "sub/w.py",
            justify="accepted debt",
            allow_ci_write=True,
            source="vox-x #1",
        )
        assert relax.exit_code == 0
        fx.commit("relax")
        outcome = fx.ratchet().check(fx.scorer(), base_ref=base, require_base=True)
        assert outcome.exit_code == 0

    def test_relax_requires_justification(self, fx: GitFixture) -> None:
        fx.write("sub/w.py", GOOD)
        fx.snapshot("sub")
        fx.commit("base")
        outcome = fx.writer().relax(
            fx.scorer(), "sub/w.py", justify="  ", allow_ci_write=True, source=None
        )
        assert outcome.exit_code == 1

    def test_relax_untracked_file_writes_no_relaxation(self, fx: GitFixture) -> None:
        # A file with no prior baseline entry has nothing to loosen: relax must
        # refuse, forge no "relaxed" audit line, and add no waivable pair
        # (Bugbot #3).
        fx.write("sub/w.py", GOOD)  # no snapshot -> no baseline entry for w.py
        fx.commit("base")
        outcome = fx.writer().relax(
            fx.scorer(), "sub/w.py", justify="x", allow_ci_write=True, source=None
        )
        assert outcome.exit_code == 1
        hint = "--update"
        assert any(hint in line or "--reconcile" in line for line in outcome.lines)
        assert Baseline(fx.root).get("sub/w.py") is None  # not added
        audit_path = fx.root / ".oo-audit.jsonl"
        assert not audit_path.exists() or "relaxed" not in audit_path.read_text()

    def test_relax_no_loosening_refuses(self, fx: GitFixture) -> None:
        # A file whose metrics all meet/beat its baseline has nothing to loosen:
        # relax refuses, writes no relaxed audit line, leaves the baseline as is.
        fx.write("sub/w.py", GOOD)
        fx.snapshot("sub")  # baseline == current, nothing worse
        fx.commit("base")
        before = dict(Baseline(fx.root).entries)
        outcome = fx.writer().relax(
            fx.scorer(), "sub/w.py", justify="x", allow_ci_write=True, source=None
        )
        assert outcome.exit_code == 1
        assert any("nothing to relax" in line for line in outcome.lines)
        assert Baseline(fx.root).entries == before  # unchanged
        audit_path = fx.root / ".oo-audit.jsonl"
        assert not audit_path.exists() or "relaxed" not in audit_path.read_text()

    def test_historical_relaxation_does_not_waive_fresh_regression(
        self, fx: GitFixture
    ) -> None:
        # A relaxation committed at base must not bless a NEW regression that a
        # later --rebaseline merely re-locks to a worse value (S7 / gvr).
        fx.write("sub/w.py", GOOD)
        fx.snapshot("sub")
        fx.writer().relax(
            fx.scorer(),
            "sub/w.py",
            justify="historical",
            allow_ci_write=True,
            source=None,
        )
        base = fx.commit("adopt with historical relaxation")
        fx.write("sub/w.py", WORSE)  # fresh regression vs base
        fx.writer().rebaseline(fx.scorer(), allow_ci_write=True, source=None)
        fx.commit("regress then rebaseline")
        outcome = fx.ratchet().check(fx.scorer(), base_ref=base, require_base=True)
        assert outcome.exit_code == 1
        assert any("regression" in line for line in outcome.lines)

    def test_relax_records_only_loosened_metrics(self, fx: GitFixture) -> None:
        # --relax must record ONLY metrics that actually loosened, not every
        # metric on the file. Otherwise an untouched metric becomes waivable.
        fx.write("sub/w.py", GOOD)
        fx.snapshot("sub")
        fx.commit("base")
        current = Baseline.metrics_by_file(fx.scorer().results)["sub/w.py"]
        # In-tree baseline == current except max_complexity is stricter, so only
        # max_complexity loosens when we relax back to the current values.
        fx.write_baseline({"sub/w.py": {**current, "max_complexity": 0.0}})
        fx.writer().relax(
            fx.scorer(),
            "sub/w.py",
            justify="cc only",
            allow_ci_write=True,
            source=None,
        )
        entry = json.loads((fx.root / ".oo-audit.jsonl").read_text().splitlines()[-1])
        assert entry["verdict"] == "relaxed"
        assert set(entry["deltas"]["sub/w.py"]) == {"max_complexity"}

    def test_relaxing_one_metric_does_not_waive_another(self, fx: GitFixture) -> None:
        # Relax M1 (max_complexity); a fresh regression of M2 (module_size) on the
        # same file, though locked in-tree, must NOT be waived (gvr / Bugbot #1).
        fx.write("sub/w.py", WORSE)
        current = Baseline.metrics_by_file(fx.scorer().results)["sub/w.py"]
        base_metrics = {
            **current,
            "max_complexity": current["max_complexity"] - 1,
            "module_size": current["module_size"] - 1,
        }
        # base commit carries a different file body so w.py lands in the diff;
        # its baseline blob is the hand-built stricter metrics.
        fx.write("sub/w.py", GOOD)
        fx.write_baseline({"sub/w.py": base_metrics})
        base = fx.commit("base with both metrics stricter")
        # HEAD: restore WORSE, lock in-tree baseline to current; relax ONLY M1.
        fx.write("sub/w.py", WORSE)
        fx.write_baseline({"sub/w.py": current})
        relaxed = {
            "verdict": "relaxed",
            "deltas": {
                "sub/w.py": {
                    "max_complexity": [
                        base_metrics["max_complexity"],
                        current["max_complexity"],
                    ]
                }
            },
        }
        fx.write(".oo-audit.jsonl", json.dumps(relaxed) + "\n")
        fx.commit("lock and relax cc only")
        outcome = fx.ratchet().check(fx.scorer(), base_ref=base, require_base=True)
        assert outcome.exit_code == 1
        assert any("module_size" in line for line in outcome.lines)


class TestRenameCarry:
    """A renamed file inherits its predecessor's base entry (S8)."""

    def test_rename_regression_is_caught(self, fx: GitFixture) -> None:
        fx.write("sub/old.py", GOOD)
        fx.snapshot("sub")
        base = fx.commit("base")
        fx.move("sub/old.py", "sub/new.py")
        fx.write("sub/new.py", WORSE)  # worsened during rename
        # Lock the in-tree baseline to the current (worse) value so the only
        # possible failure is the vs-base regression carried from old.py.
        scorer = fx.scorer()
        fx.write_baseline(Baseline.metrics_by_file(scorer.results))
        fx.commit("rename and worsen")
        outcome = fx.ratchet().check(fx.scorer(), base_ref=base, require_base=True)
        assert outcome.exit_code == 1
        joined = "\n".join(outcome.lines)
        assert "regression" in joined
        assert "not in baseline" not in joined

    def test_update_refuses_renamed_regression(self, fx: GitFixture) -> None:
        # update must carry the old baseline entry across a rename so a
        # regressed metric cannot be laundered as a brand-new file (S8).
        fx.write("sub/old.py", GOOD)
        fx.snapshot("sub")
        base = fx.commit("base")
        fx.move("sub/old.py", "sub/new.py")
        fx.write("sub/new.py", WORSE)  # regressed vs old.py's baseline
        outcome = fx.writer().update(
            fx.scorer(),
            base_ref=base,
            require_base=False,
            allow_ci_write=True,
            source=None,
        )
        assert outcome.exit_code == 1
        assert any("regressed" in line for line in outcome.lines)
        # the worse value was NOT written under the new path
        entry = Baseline(fx.root).get("sub/new.py")
        assert entry is None or entry["max_complexity"] == 1.0

    def test_refused_rename_keeps_old_entry_and_still_refuses(
        self, fx: GitFixture
    ) -> None:
        # A refused rename must NOT drop the old baseline key -- otherwise a
        # second --update run has no carried base and launders the regression
        # (Copilot #1).
        fx.write("sub/old.py", GOOD)
        fx.snapshot("sub")
        base = fx.commit("base")
        fx.move("sub/old.py", "sub/new.py")
        fx.write("sub/new.py", WORSE)  # regressed vs old.py's carried baseline
        first = fx.writer().update(
            fx.scorer(),
            base_ref=base,
            require_base=False,
            allow_ci_write=True,
            source=None,
        )
        assert first.exit_code == 1
        # the old rename-source entry survives the refusal
        assert Baseline(fx.root).get("sub/old.py") is not None
        assert Baseline(fx.root).get("sub/new.py") is None
        # a second pass still refuses -- no laundering on run two
        second = fx.writer().update(
            fx.scorer(),
            base_ref=base,
            require_base=False,
            allow_ci_write=True,
            source=None,
        )
        assert second.exit_code == 1
        assert Baseline(fx.root).get("sub/new.py") is None

    def test_rename_to_unparseable_target_keeps_old_entry(self, fx: GitFixture) -> None:
        # rename old->new where new is unparseable must NOT drop old's row: the
        # drop is gated on the target being parse-clean AND written (Copilot #3).
        # Keep the target a near-copy of old so git still detects the rename.
        broken_similar = GOOD.replace('return "pos"', 'return "pos')  # unterminated
        fx.write("sub/old.py", GOOD)
        fx.snapshot("sub")
        base = fx.commit("base")
        fx.move("sub/old.py", "sub/new.py")
        fx.write("sub/new.py", broken_similar)  # rename target is unparseable
        outcome = fx.writer().update(
            fx.scorer(),
            base_ref=base,
            require_base=False,
            allow_ci_write=True,
            source=None,
        )
        assert outcome.exit_code == 1
        assert any("parse error" in line for line in outcome.lines)
        assert Baseline(fx.root).get("sub/old.py") is not None
        assert Baseline(fx.root).get("sub/new.py") is None

    def test_reconcile_refuses_regressed_rename_and_keeps_carry(
        self, fx: GitFixture
    ) -> None:
        # reconcile's prune must be rename-aware: a renamed+regressed module is
        # refused (new not added, old carry not pruned), and a later --update
        # still refuses -- no laundering across reconcile (Bugbot #1).
        fx.write("sub/old.py", GOOD)
        fx.snapshot("sub")
        fork = fx.commit("fork")
        fx.set_origin_main(fork)  # reconcile resolves base via merge-base
        fx.move("sub/old.py", "sub/new.py")
        fx.write("sub/new.py", WORSE)  # regressed rename
        fx.commit("rename and regress")
        reconciled = fx.writer().reconcile(
            fx.scorer(), allow_ci_write=True, source=None
        )
        assert reconciled.exit_code == 1
        assert Baseline(fx.root).get("sub/old.py") is not None  # carry preserved
        assert Baseline(fx.root).get("sub/new.py") is None  # regression not added
        # a subsequent scoped update still refuses -- carry survived reconcile
        second = fx.writer().update(
            fx.scorer(),
            base_ref=fork,
            require_base=False,
            allow_ci_write=True,
            source=None,
        )
        assert second.exit_code == 1
        assert Baseline(fx.root).get("sub/new.py") is None

    def test_reconcile_rename_to_unparseable_keeps_old_row(
        self, fx: GitFixture
    ) -> None:
        # reconcile must not prune a rename source whose target is present on
        # disk but unparseable -- the target counts as "present" even though it
        # is excluded from plan.current (Bugbot #2).
        broken_similar = GOOD.replace('return "pos"', 'return "pos')  # unterminated
        fx.write("sub/old.py", GOOD)
        fx.snapshot("sub")
        fork = fx.commit("fork")
        fx.set_origin_main(fork)
        fx.move("sub/old.py", "sub/new.py")
        fx.write("sub/new.py", broken_similar)  # rename target is unparseable
        fx.commit("rename to unparseable")
        outcome = fx.writer().reconcile(fx.scorer(), allow_ci_write=True, source=None)
        assert outcome.exit_code == 1  # fails on the parse error
        assert any("parse error" in line for line in outcome.lines)
        assert Baseline(fx.root).get("sub/old.py") is not None  # carry preserved


class TestCompleteness:
    """Whole-tree completeness enumerates from the scorer's own file set."""

    def test_missing_file_flagged_hidden_and_broken_excluded(
        self, fx: GitFixture
    ) -> None:
        fx.write("sub/good.py", GOOD)
        fx.write("sub/.hidden.py", WORSE)
        fx.write("sub/broken.py", BROKEN)
        fx.write_baseline({})
        outcome = fx.ratchet().audit_completeness(fx.scorer())
        assert outcome.exit_code == 1
        joined = "\n".join(outcome.lines)
        assert "sub/good.py" in joined
        assert ".hidden.py" not in joined
        assert "broken.py" not in joined

    def test_complete_baseline_passes(self, fx: GitFixture) -> None:
        fx.write("sub/good.py", GOOD)
        fx.snapshot("sub")
        outcome = fx.ratchet().audit_completeness(fx.scorer())
        assert outcome.exit_code == 0


class TestCiWriteGuard:
    """Mutations refuse to run under GITHUB_ACTIONS without --allow-ci-write."""

    def test_update_blocked_in_ci(
        self, fx: GitFixture, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("GITHUB_ACTIONS", "true")
        fx.write("sub/w.py", GOOD)
        fx.snapshot("sub")
        base = fx.commit("base")
        outcome = fx.writer().update(
            fx.scorer(),
            base_ref=base,
            require_base=False,
            allow_ci_write=False,
            source=None,
        )
        assert outcome.exit_code == 1
        assert any("GITHUB_ACTIONS" in line for line in outcome.lines)

    def test_update_allowed_with_flag_in_ci(
        self, fx: GitFixture, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("GITHUB_ACTIONS", "true")
        fx.write("sub/w.py", GOOD)
        fx.snapshot("sub")
        base = fx.commit("base")
        outcome = fx.writer().update(
            fx.scorer(),
            base_ref=base,
            require_base=False,
            allow_ci_write=True,
            source=None,
        )
        assert outcome.exit_code == 0


class TestTouchedParseError:
    """A touched file that fails to parse fails loud, not silently."""

    def test_broken_touched_file_fails(self, fx: GitFixture) -> None:
        fx.write("sub/w.py", GOOD)
        fx.snapshot("sub")
        base = fx.commit("base")
        fx.write("sub/w.py", BROKEN)  # now unparseable, still touched
        fx.commit("break")
        outcome = fx.ratchet().check(fx.scorer(), base_ref=base, require_base=True)
        assert outcome.exit_code == 1
        assert any("failed to parse" in line for line in outcome.lines)


class TestGitFailClosed:
    """A failed git command fails closed — never a silent empty diff."""

    def test_diff_failure_raises(self, fx: GitFixture) -> None:
        fx.write("sub/w.py", GOOD)
        fx.commit("base")
        with pytest.raises(GitError):
            GitRepo(fx.root).diff("refs/does/not/exist")

    def test_empty_diff_is_not_a_failure(self, fx: GitFixture) -> None:
        fx.write("sub/w.py", GOOD)
        head = fx.commit("base")
        diff = GitRepo(fx.root).diff(head)  # HEAD vs work tree: no changes
        assert diff.touched == frozenset()

    def test_corrupt_base_baseline_raises_giterror(self, fx: GitFixture) -> None:
        # A non-JSON baseline blob at the base is a controlled GitError, not a
        # json traceback that crashes the gate (Bugbot #2).
        fx.write("sub/w.py", GOOD)
        fx.write_baseline({"sub/w.py": {"module_size": 5.0}})
        fx.commit("valid base")
        fx.write(".oo-baseline.json", "{ this is not json")
        head = fx.commit("corrupt the baseline blob")
        with pytest.raises(GitError):
            GitRepo(fx.root).show_baseline(head)

    def test_non_utf8_base_baseline_blob_raises_giterror(self, fx: GitFixture) -> None:
        # A committed non-UTF8 baseline blob makes git show's text decode fail;
        # show_baseline must fail closed with GitError, not a traceback.
        fx.write("sub/w.py", GOOD)
        (fx.root / ".oo-baseline.json").write_bytes(b"\xff\xfe\x00")
        head = fx.commit("non-utf8 baseline blob")
        with pytest.raises(GitError):
            GitRepo(fx.root).show_baseline(head)

    def test_non_dict_base_baseline_raises_giterror(self, fx: GitFixture) -> None:
        # A committed baseline that is valid JSON but not an object (a list) is a
        # controlled GitError, not an AttributeError on .get().
        fx.write("sub/w.py", GOOD)
        fx.write(".oo-baseline.json", "[1, 2, 3]")
        head = fx.commit("non-dict baseline blob")
        with pytest.raises(GitError):
            GitRepo(fx.root).show_baseline(head)

    def test_nested_non_dict_base_baseline_raises_giterror(
        self, fx: GitFixture
    ) -> None:
        # {"sub/w.py": "garbage"} passes the top-level dict check but the value is
        # not a metric dict; without the nested guard `metric not in "garbage"` is
        # a substring test that skips every metric -> fail-OPEN. Reject it.
        fx.write("sub/w.py", GOOD)
        fx.write(".oo-baseline.json", '{"sub/w.py": "garbage"}')
        head = fx.commit("nested non-dict baseline blob")
        with pytest.raises(GitError):
            GitRepo(fx.root).show_baseline(head)

    def test_bool_metric_base_baseline_raises_giterror(self, fx: GitFixture) -> None:
        # A bool metric value (`true`) is an int subclass that would compare as
        # 0/1 -- fail-open. Reject it at the source with GitError.
        fx.write("sub/w.py", GOOD)
        fx.write(".oo-baseline.json", '{"sub/w.py": {"module_size": true}}')
        head = fx.commit("bool metric baseline blob")
        with pytest.raises(GitError):
            GitRepo(fx.root).show_baseline(head)

    def test_nested_non_dict_base_baseline_fails_closed_via_cli(
        self, fx: GitFixture, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # End-to-end: a nested-malformed base baseline must fail the gate, not let
        # a real regression pass. The in-tree baseline at HEAD is valid, so only
        # the base blob is malformed; the CLI catches the GitError as a controlled
        # non-zero.
        fx.write("sub/w.py", GOOD)
        fx.write(".oo-baseline.json", '{"sub/w.py": "garbage"}')
        base = fx.commit("nested non-dict base baseline")
        fx.snapshot("sub")  # overwrite with a valid in-tree baseline
        fx.write("sub/w.py", WORSE)  # a regression that must not slip through
        fx.commit("valid in-tree baseline and regress")
        monkeypatch.chdir(fx.root)
        assert main(["sub", "--check", "--base-ref", base, "--require-base"]) == 1

    def test_absent_baseline_blob_at_valid_ref_is_none(self, fx: GitFixture) -> None:
        # A path genuinely missing at a readable commit is trusted absence.
        fx.write("sub/w.py", GOOD)  # no baseline committed
        head = fx.commit("base without baseline")
        assert GitRepo(fx.root).show_baseline(head) is None
        assert GitRepo(fx.root).show_audit(head) is None

    def test_git_error_on_show_is_not_silent_absence(self, fx: GitFixture) -> None:
        # A non-absence git failure (invalid object name) fails closed, not None:
        # git show errors without the "does not exist in" path signature.
        fx.write("sub/w.py", GOOD)
        fx.commit("base")
        with pytest.raises(GitError):
            GitRepo(fx.root).show_baseline("nonexistent-ref-xyz")
        with pytest.raises(GitError):
            GitRepo(fx.root).show_audit("nonexistent-ref-xyz")

    def test_git_degrades_to_none_without_a_repo(self) -> None:
        # A nonexistent start dir -> _root is None -> git commands must degrade
        # to None, never run against the process's ambient CWD (a real repo).
        gr = GitRepo(Path("/nonexistent-oo-ratchet-xyz"))
        assert gr.root is None
        assert gr.available is False
        assert gr._git(["rev-parse", "HEAD"]) is None
        assert gr.show_baseline("HEAD") is None

    def test_corrupt_audit_line_is_controlled_nonzero(
        self, fx: GitFixture, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A malformed in-tree .oo-audit.jsonl must surface as a clean non-zero
        # exit through the CLI, not a JSONDecodeError traceback (Bugbot #2).
        fx.write("sub/w.py", WORSE)
        fx.snapshot("sub")
        base = fx.commit("base")
        fx.write("sub/w.py", GOOD)  # improves vs base -> check reads the audit
        fx.snapshot("sub")
        fx.write(".oo-audit.jsonl", "<<<<<<< merge conflict\n")
        fx.commit("corrupt audit")
        monkeypatch.chdir(fx.root)
        assert main(["sub", "--check", "--base-ref", base, "--require-base"]) == 1

    def test_corrupt_in_tree_baseline_is_controlled_nonzero(
        self, fx: GitFixture, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A corrupt in-tree .oo-baseline.json must surface as a clean non-zero
        # exit through the CLI, not a JSONDecodeError traceback (Bugbot #1).
        fx.write("sub/w.py", GOOD)
        fx.write(".oo-baseline.json", "{ not valid json")
        fx.commit("corrupt baseline")
        monkeypatch.chdir(fx.root)
        result = main(["sub", "--check"])
        assert result == 1


class TestBootstrap:
    """Base resolution and the O2 bootstrap / require-base rules."""

    def test_missing_base_baseline_is_bootstrap_pass(self, fx: GitFixture) -> None:
        # Genuine first-adoption: neither base nor origin/main tip has a baseline.
        fx.write("sub/w.py", GOOD)  # no .oo-baseline.json committed
        base = fx.commit("base without baseline")
        fx.write("sub/w.py", WORSE)
        fx.commit("change")
        outcome = fx.ratchet().check(fx.scorer(), base_ref=base, require_base=True)
        assert outcome.exit_code == 0

    def test_stale_branch_past_adoption_fails(self, fx: GitFixture) -> None:
        # Base forked before baseline adoption, but origin/main tip has one:
        # a stale branch that would launder a regression past the empty base.
        fx.write("sub/w.py", GOOD)
        fork = fx.commit("fork before adoption")
        fx.checkout_new("feature")
        fx.write("sub/w.py", WORSE)
        fx.commit("feature change")
        fx.checkout("main")
        fx.snapshot("sub")  # main adopts the baseline
        main_head = fx.commit("adopt baseline on main")
        fx.set_origin_main(main_head)
        fx.checkout("feature")
        outcome = fx.ratchet().check(fx.scorer(), base_ref=fork, require_base=True)
        assert outcome.exit_code == 1
        assert any("predates baseline adoption" in line for line in outcome.lines)

    def test_absent_base_baseline_unresolvable_tip_with_baseline_fails(
        self, fx: GitFixture
    ) -> None:
        # Base has no baseline blob and origin/main is unresolvable, but an
        # in-tree baseline exists: cannot confirm first-adoption -> fail closed
        # (Bugbot #3). An unfetched origin/main must not read as "no tip baseline".
        fx.write("sub/w.py", GOOD)
        base = fx.commit("base without baseline")
        fx.write("sub/w.py", WORSE)
        fx.snapshot("sub")  # in-tree baseline now present
        fx.commit("add in-tree baseline")
        outcome = fx.ratchet().check(fx.scorer(), base_ref=base, require_base=False)
        assert outcome.exit_code == 1
        assert any("origin/main" in line for line in outcome.lines)

    def test_unresolvable_base_with_require_fails(self, fx: GitFixture) -> None:
        fx.write("sub/w.py", GOOD)
        fx.snapshot("sub")
        fx.commit("base")
        outcome = fx.ratchet().check(fx.scorer(), base_ref="0" * 40, require_base=True)
        assert outcome.exit_code == 1

    def test_unresolvable_base_with_baseline_fails_loud(self, fx: GitFixture) -> None:
        # Fail-closed: a stale/unfetched base with a baseline present must not
        # silently pass a local check.
        fx.write("sub/w.py", GOOD)
        fx.snapshot("sub")
        fx.commit("base")
        outcome = fx.ratchet().check(fx.scorer(), base_ref="0" * 40, require_base=False)
        assert outcome.exit_code == 1
        assert any("origin/main" in line for line in outcome.lines)

    def test_unresolvable_base_no_baseline_is_bootstrap_pass(
        self, fx: GitFixture
    ) -> None:
        # Genuine first-adoption: no base AND no in-tree baseline.
        fx.write("sub/w.py", GOOD)
        fx.commit("pre-adoption")
        outcome = fx.ratchet().check(fx.scorer(), base_ref="0" * 40, require_base=False)
        assert outcome.exit_code == 0
