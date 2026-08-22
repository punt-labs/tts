"""Integration tests for the merge-base coupling ratchet against tmp git repos.

The load-bearing behavior is merge-base scoping: a coupling regression made in an
earlier commit of a multi-commit PR and not re-touched in the final commit must
still be scored. The old ``HEAD~1..HEAD`` scoping missed exactly that case. The
rest covers the regression-only verdict, scoped never-loosen update, fail-closed
git/baseline handling, the CI-write guard, and the trivial-pass path.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Self

import pytest

from tools.coupling.audit import CouplingAudit, CouplingAuditError
from tools.coupling.baseline import CouplingBaseline, CouplingBaselineError
from tools.coupling.cli import main
from tools.coupling.gitio import GitError, GitRepo
from tools.coupling.ratchet import CouplingRatchet
from tools.coupling.scorer import CouplingScorer
from tools.coupling.writer import CouplingWriter

# Every test drives a real git init/commit/checkout sequence plus a real
# tools/coupling subprocess invocation -- see the marker rationale on
# test_oo_ratchet.py and the measured timing in TESTING.md.
pytestmark = pytest.mark.slow

# A shared bulky header so git reliably detects a rename across the small metric
# change between LOW and HIGH (the body dominates the similarity score).
_BODY = (
    "from __future__ import annotations\n\n"
    "# shared body line one, present in both variants for rename detection\n"
    "# shared body line two, present in both variants for rename detection\n"
    "# shared body line three, present in both variants for rename detection\n\n"
)
# public_names == 1 (via __all__); every other metric is 0.
LOW = _BODY + '__all__ = ["x"]\n\nx = 1\n'
# public_names == 2 -> a regression against a LOW baseline.
HIGH = _BODY + '__all__ = ["x", "y"]\n\nx = 1\ny = 2\n'
# public_names == 3 -> a fresh regression against a HIGH baseline.
HIGHER = _BODY + '__all__ = ["x", "y", "z"]\n\nx = 1\ny = 2\nz = 3\n'
BROKEN = "def oops(:\n    pass\n"


class GitFixture:
    """A throwaway git repo for exercising the coupling ratchet end to end."""

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
        (self._root / ".oo-coupling-baseline.json").write_text(
            json.dumps(data, indent=2) + "\n"
        )

    def snapshot(self, subdir: str = "pkg") -> None:
        scorer = CouplingScorer(self._root / subdir, self._root)
        self.write_baseline(CouplingBaseline.metrics_by_file(scorer.results))

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

    def ratchet(self) -> CouplingRatchet:
        return CouplingRatchet(self._root, GitRepo(self._root))

    def writer(self) -> CouplingWriter:
        return CouplingWriter(self._root, GitRepo(self._root))

    def scorer(self, subdir: str = "pkg") -> CouplingScorer:
        return CouplingScorer(self._root / subdir, self._root)


@pytest.fixture
def fx(tmp_path: Path) -> GitFixture:
    return GitFixture(tmp_path)


class TestMergeBaseScoping:
    """The core fix: score regressions anywhere in the PR, not just the tip."""

    def test_regression_in_earlier_commit_is_caught(self, fx: GitFixture) -> None:
        # a.py regresses in commit 1; commit 2 touches only b.py. The merge-base
        # diff (fork..worktree) still includes a.py, so its regression fails.
        fx.write("pkg/a.py", LOW)
        fx.write("pkg/b.py", LOW)
        fx.snapshot()
        fork = fx.commit("fork")
        fx.checkout_new("feature")
        fx.write("pkg/a.py", HIGH)  # regression, in the EARLIER commit
        fx.commit("regress a")
        fx.write("pkg/b.py", LOW + "# touched, no metric change\n")
        fx.commit("touch b only")
        outcome = fx.ratchet().check(fx.scorer(), base_ref=fork, require_base=True)
        assert outcome.exit_code == 1
        assert any(
            "pkg/a.py" in line and "public_names" in line for line in outcome.lines
        )

    def test_per_commit_scope_would_have_missed_it(self, fx: GitFixture) -> None:
        # The same tree, scoped HEAD~1..HEAD (the old behavior), only sees b.py --
        # a.py's regression slips through. This documents the hole the fix closes.
        fx.write("pkg/a.py", LOW)
        fx.write("pkg/b.py", LOW)
        fx.snapshot()
        fx.commit("fork")
        fx.checkout_new("feature")
        fx.write("pkg/a.py", HIGH)
        fx.commit("regress a")
        fx.write("pkg/b.py", LOW + "# touched\n")
        fx.commit("touch b only")
        outcome = fx.ratchet().check(fx.scorer(), base_ref="HEAD~1", require_base=True)
        assert outcome.exit_code == 0  # old per-commit scope misses a.py


class TestBaseCompare:
    """Regression-only verdict against the in-tree baseline."""

    def test_steady_passes(self, fx: GitFixture) -> None:
        fx.write("pkg/a.py", LOW)
        fx.snapshot()
        base = fx.commit("base")
        fx.write("pkg/a.py", LOW + "# comment\n")  # no metric change
        fx.commit("touch")
        outcome = fx.ratchet().check(fx.scorer(), base_ref=base, require_base=True)
        assert outcome.exit_code == 0

    def test_regression_fails(self, fx: GitFixture) -> None:
        fx.write("pkg/a.py", LOW)
        fx.snapshot()
        base = fx.commit("base")
        fx.write("pkg/a.py", HIGH)
        fx.commit("regress")
        outcome = fx.ratchet().check(fx.scorer(), base_ref=base, require_base=True)
        assert outcome.exit_code == 1
        assert any("regression" in line for line in outcome.lines)

    def test_new_file_is_info_not_regression(self, fx: GitFixture) -> None:
        fx.write("pkg/a.py", LOW)
        fx.snapshot()
        base = fx.commit("base")
        fx.write("pkg/new.py", HIGH)  # new, not in baseline
        fx.commit("add new")
        outcome = fx.ratchet().check(fx.scorer(), base_ref=base, require_base=True)
        assert outcome.exit_code == 0

    def test_no_src_touched_is_trivial_pass(self, fx: GitFixture) -> None:
        fx.write("pkg/a.py", LOW)
        fx.snapshot()
        base = fx.commit("base")
        fx.write("notes.txt", "unrelated change\n")  # not a scored .py
        fx.commit("touch non-source")
        outcome = fx.ratchet().check(fx.scorer(), base_ref=base, require_base=True)
        assert outcome.exit_code == 0
        assert any("No scored Python files touched" in line for line in outcome.lines)


class TestBaseCommitAuthoritative:
    """The comparison baseline is read from the base commit, not the worktree."""

    def test_in_tree_baseline_edit_cannot_launder_regression(
        self, fx: GitFixture
    ) -> None:
        # A PR regresses a.py AND rewrites the in-tree baseline to match. The
        # check reads the baseline from the base commit blob, so the regression
        # is still caught despite the hand-edit.
        fx.write("pkg/a.py", LOW)
        fx.snapshot()
        base = fx.commit("base")  # base-commit baseline: a.py public_names=1
        fx.write("pkg/a.py", HIGH)  # regression
        fx.write_baseline(CouplingBaseline.metrics_by_file(fx.scorer().results))
        fx.commit("regress and launder the in-tree baseline")
        outcome = fx.ratchet().check(fx.scorer(), base_ref=base, require_base=True)
        assert outcome.exit_code == 1
        assert any("public_names" in line for line in outcome.lines)

    def test_empty_base_baseline_fails_closed(self, fx: GitFixture) -> None:
        # An empty {} baseline at the base (truncated write / bad merge) makes
        # every touched file look new -> would pass. Under require_base, fail
        # closed exactly like a missing baseline.
        fx.write("pkg/a.py", LOW)
        fx.write_baseline({})
        base = fx.commit("base with empty baseline")
        fx.write("pkg/a.py", HIGH)
        fx.commit("regress")
        outcome = fx.ratchet().check(fx.scorer(), base_ref=base, require_base=True)
        assert outcome.exit_code == 1
        assert any("empty" in line for line in outcome.lines)

    def test_empty_base_baseline_local_passes_clean(self, fx: GitFixture) -> None:
        # Empty {} base baseline without --require-base flows through
        # _check_against: a clean touched file is new/INFO -> no regression ->
        # pass, matching the OO ratchet (not a bare short-circuit pass).
        fx.write("pkg/a.py", LOW)
        fx.write_baseline({})
        base = fx.commit("base with empty baseline")
        fx.write("pkg/a.py", HIGH)  # changed, but new-vs-empty-base is INFO
        fx.commit("change")
        outcome = fx.ratchet().check(fx.scorer(), base_ref=base, require_base=False)
        assert outcome.exit_code == 0

    def test_empty_base_baseline_local_still_parses(self, fx: GitFixture) -> None:
        # Empty {} base baseline without --require-base still runs the touched-file
        # parse check: an unparseable touched file fails, matching the OO ratchet.
        fx.write("pkg/a.py", LOW)
        fx.write_baseline({})
        base = fx.commit("base with empty baseline")
        fx.write("pkg/a.py", BROKEN)  # unparseable, touched
        fx.commit("break")
        outcome = fx.ratchet().check(fx.scorer(), base_ref=base, require_base=False)
        assert outcome.exit_code == 1
        assert any("failed to parse" in line for line in outcome.lines)


class TestScopedUpdate:
    """Scoped update writes improvements but never loosens."""

    def test_update_refuses_regression(self, fx: GitFixture) -> None:
        fx.write("pkg/a.py", LOW)
        fx.snapshot()
        base = fx.commit("base")
        fx.write("pkg/a.py", HIGH)  # worse than the in-tree baseline
        outcome = fx.writer().update(
            fx.scorer(),
            base_ref=base,
            require_base=False,
            allow_ci_write=True,
            source=None,
        )
        assert outcome.exit_code == 1
        entry = CouplingBaseline(fx.root).get("pkg/a.py")
        assert entry is not None
        assert entry["public_names"] == 1.0  # unloosened

    def test_update_writes_improvement(self, fx: GitFixture) -> None:
        fx.write("pkg/a.py", HIGH)
        fx.snapshot()
        base = fx.commit("base")
        fx.write("pkg/a.py", LOW)  # improved (fewer public names)
        outcome = fx.writer().update(
            fx.scorer(),
            base_ref=base,
            require_base=False,
            allow_ci_write=True,
            source=None,
        )
        assert outcome.exit_code == 0
        entry = CouplingBaseline(fx.root).get("pkg/a.py")
        assert entry is not None
        assert entry["public_names"] == 1.0

    def test_update_unresolvable_base_with_baseline_fails_closed(
        self, fx: GitFixture
    ) -> None:
        fx.write("pkg/a.py", LOW)
        fx.snapshot()
        fx.commit("base")
        before = dict(CouplingBaseline(fx.root).entries)
        outcome = fx.writer().update(
            fx.scorer(),
            base_ref="0" * 40,
            require_base=False,
            allow_ci_write=True,
            source=None,
        )
        assert outcome.exit_code == 1
        assert any("cannot resolve base" in line for line in outcome.lines)
        assert CouplingBaseline(fx.root).entries == before  # no whole-tree sweep

    def test_update_bootstrap_no_baseline_whole_tree(self, fx: GitFixture) -> None:
        fx.write("pkg/a.py", LOW)
        fx.commit("pre-adoption, no baseline")
        outcome = fx.writer().update(
            fx.scorer(),
            base_ref="0" * 40,
            require_base=False,
            allow_ci_write=True,
            source=None,
        )
        assert outcome.exit_code == 0
        assert CouplingBaseline(fx.root).get("pkg/a.py") is not None


class TestRelaxWaiver:
    """A relaxed, locked coupling regression is waived by check."""

    def test_relax_then_check_waives(self, fx: GitFixture) -> None:
        fx.write("pkg/a.py", LOW)
        fx.snapshot()
        base = fx.commit("base")
        fx.write("pkg/a.py", HIGH)  # regresses public_names vs base
        relax = fx.writer().relax(
            fx.scorer(),
            "pkg/a.py",
            justify="accepted coupling debt",
            allow_ci_write=True,
            source="vox-x #1",
        )
        assert relax.exit_code == 0
        fx.commit("relax")
        outcome = fx.ratchet().check(fx.scorer(), base_ref=base, require_base=True)
        assert outcome.exit_code == 0

    def test_relax_requires_justification(self, fx: GitFixture) -> None:
        fx.write("pkg/a.py", LOW)
        fx.snapshot()
        fx.commit("base")
        outcome = fx.writer().relax(
            fx.scorer(), "pkg/a.py", justify="  ", allow_ci_write=True, source=None
        )
        assert outcome.exit_code == 1

    def test_relax_untracked_file_writes_no_relaxation(self, fx: GitFixture) -> None:
        # A file with no prior baseline entry has nothing to loosen: relax must
        # refuse, forge no "relaxed" audit line, and add no waivable pair.
        fx.write("pkg/a.py", LOW)  # no snapshot -> no baseline entry for a.py
        fx.commit("base")
        outcome = fx.writer().relax(
            fx.scorer(), "pkg/a.py", justify="x", allow_ci_write=True, source=None
        )
        assert outcome.exit_code == 1
        assert any(
            "--update" in line or "--rebaseline" in line for line in outcome.lines
        )
        assert CouplingBaseline(fx.root).get("pkg/a.py") is None  # not added
        audit_path = fx.root / ".oo-coupling-audit.jsonl"
        assert not audit_path.exists() or "relaxed" not in audit_path.read_text()

    def test_relax_no_loosening_refuses(self, fx: GitFixture) -> None:
        # A file whose metrics all meet/beat its baseline has nothing to loosen:
        # relax refuses, writes no relaxed audit line, leaves the baseline as is.
        fx.write("pkg/a.py", LOW)
        fx.snapshot()  # baseline == current, nothing worse
        fx.commit("base")
        before = dict(CouplingBaseline(fx.root).entries)
        outcome = fx.writer().relax(
            fx.scorer(), "pkg/a.py", justify="x", allow_ci_write=True, source=None
        )
        assert outcome.exit_code == 1
        assert any("nothing to relax" in line for line in outcome.lines)
        assert CouplingBaseline(fx.root).entries == before  # unchanged
        audit_path = fx.root / ".oo-coupling-audit.jsonl"
        assert not audit_path.exists() or "relaxed" not in audit_path.read_text()

    def test_historical_relaxation_does_not_waive_fresh_regression(
        self, fx: GitFixture
    ) -> None:
        # A relaxation committed at base must not bless a fresh regression that a
        # later --rebaseline merely re-locks to a worse value.
        fx.write("pkg/a.py", LOW)
        fx.snapshot()  # baseline public_names=1
        fx.write("pkg/a.py", HIGH)  # regresses vs baseline -> relax loosens it
        fx.writer().relax(
            fx.scorer(),
            "pkg/a.py",
            justify="historical",
            allow_ci_write=True,
            source=None,
        )
        base = fx.commit("adopt with historical relaxation")
        fx.write("pkg/a.py", HIGHER)  # fresh regression: public_names 3 vs base 2
        fx.writer().rebaseline(fx.scorer(), allow_ci_write=True, source=None)
        fx.commit("regress then rebaseline")
        outcome = fx.ratchet().check(fx.scorer(), base_ref=base, require_base=True)
        assert outcome.exit_code == 1
        assert any("regression" in line for line in outcome.lines)

    def test_relaxing_one_metric_does_not_waive_another(self, fx: GitFixture) -> None:
        # Relax M1 (public_names); a fresh regression of M2 (efferent_coupling) on
        # the same file, though locked in-tree, must NOT be waived.
        fx.write("pkg/a.py", HIGH)
        current = CouplingBaseline.metrics_by_file(fx.scorer().results)["pkg/a.py"]
        base_metrics = {
            **current,
            "public_names": current["public_names"] - 1,
            "efferent_coupling": current["efferent_coupling"] - 1,
        }
        # base commit carries a different file body so a.py lands in the diff;
        # its baseline blob is the hand-built stricter metrics.
        fx.write("pkg/a.py", LOW)
        fx.write_baseline({"pkg/a.py": base_metrics})
        base = fx.commit("base with both metrics stricter")
        # HEAD: restore HIGH, lock in-tree baseline to current; relax ONLY M1.
        fx.write("pkg/a.py", HIGH)
        fx.write_baseline({"pkg/a.py": current})
        relaxed = {
            "verdict": "relaxed",
            "deltas": {
                "pkg/a.py": {
                    "public_names": [
                        base_metrics["public_names"],
                        current["public_names"],
                    ]
                }
            },
        }
        fx.write(".oo-coupling-audit.jsonl", json.dumps(relaxed) + "\n")
        fx.commit("lock and relax public_names only")
        outcome = fx.ratchet().check(fx.scorer(), base_ref=base, require_base=True)
        assert outcome.exit_code == 1
        assert any("efferent_coupling" in line for line in outcome.lines)


class TestRenameCarry:
    """A renamed file inherits its predecessor's baseline entry."""

    def test_rename_regression_is_caught(self, fx: GitFixture) -> None:
        # The in-tree baseline still keys the metrics under old.py; new.py falls
        # back to old.py's entry via the diff's rename map, so a worsened rename
        # cannot launder its history as a brand-new file.
        fx.write("pkg/old.py", LOW)
        fx.snapshot()
        base = fx.commit("base")
        fx.move("pkg/old.py", "pkg/new.py")
        fx.write("pkg/new.py", HIGH)  # worsened during the rename
        fx.commit("rename and worsen")
        outcome = fx.ratchet().check(fx.scorer(), base_ref=base, require_base=True)
        assert outcome.exit_code == 1
        assert any("public_names" in line for line in outcome.lines)

    def test_update_refuses_worsened_rename_and_keeps_source(
        self, fx: GitFixture
    ) -> None:
        # update() carries old.py's baseline entry across the rename: a worsened
        # new.py is refused (not written) and the old rename-source entry is
        # preserved, so a second run still refuses -- no laundering across update.
        fx.write("pkg/old.py", LOW)
        fx.snapshot()
        base = fx.commit("base")
        fx.move("pkg/old.py", "pkg/new.py")
        fx.write("pkg/new.py", HIGH)  # regressed vs old.py's carried baseline
        first = fx.writer().update(
            fx.scorer(),
            base_ref=base,
            require_base=False,
            allow_ci_write=True,
            source=None,
        )
        assert first.exit_code == 1
        assert any("regressed" in line for line in first.lines)
        assert CouplingBaseline(fx.root).get("pkg/new.py") is None  # not written
        assert CouplingBaseline(fx.root).get("pkg/old.py") is not None  # carry kept
        second = fx.writer().update(
            fx.scorer(),
            base_ref=base,
            require_base=False,
            allow_ci_write=True,
            source=None,
        )
        assert second.exit_code == 1  # still refuses on a second pass
        assert CouplingBaseline(fx.root).get("pkg/new.py") is None


class TestCiWriteGuard:
    """Mutations refuse to run under GITHUB_ACTIONS without --allow-ci-write."""

    def test_update_blocked_in_ci(
        self, fx: GitFixture, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("GITHUB_ACTIONS", "true")
        fx.write("pkg/a.py", LOW)
        fx.snapshot()
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
        fx.write("pkg/a.py", LOW)
        fx.snapshot()
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
        fx.write("pkg/a.py", LOW)
        fx.snapshot()
        base = fx.commit("base")
        fx.write("pkg/a.py", BROKEN)  # now unparseable, still touched
        fx.commit("break")
        outcome = fx.ratchet().check(fx.scorer(), base_ref=base, require_base=True)
        assert outcome.exit_code == 1
        assert any("failed to parse" in line for line in outcome.lines)


class TestFailClosed:
    """A failed git command or corrupt baseline fails closed, not silent."""

    def test_diff_failure_raises(self, fx: GitFixture) -> None:
        fx.write("pkg/a.py", LOW)
        fx.commit("base")
        with pytest.raises(GitError):
            GitRepo(fx.root).diff("refs/does/not/exist")

    def test_empty_diff_is_not_a_failure(self, fx: GitFixture) -> None:
        fx.write("pkg/a.py", LOW)
        head = fx.commit("base")
        diff = GitRepo(fx.root).diff(head)  # HEAD vs work tree: no changes
        assert diff.touched == frozenset()

    def test_unresolvable_base_with_require_fails_closed(self, fx: GitFixture) -> None:
        # CI path: an unresolvable base under --require-base must never pass on a
        # stale or unfetched origin/main -- fail closed.
        fx.write("pkg/a.py", LOW)
        fx.snapshot()
        fx.commit("base")
        outcome = fx.ratchet().check(fx.scorer(), base_ref="0" * 40, require_base=True)
        assert outcome.exit_code == 1
        assert any("--require-base" in line for line in outcome.lines)

    def test_unresolvable_base_with_baseline_fails_closed(self, fx: GitFixture) -> None:
        # No base resolvable + in-tree baseline present + not require_base: match
        # the OO ratchet's _no_base -- hard-fail rather than trust the
        # hand-editable in-tree file. Consistent across all three ratchets.
        fx.write("pkg/a.py", LOW)
        fx.snapshot()
        fx.commit("base")
        fx.write("pkg/a.py", HIGH)  # a regression the soft path would have caught
        outcome = fx.ratchet().check(fx.scorer(), base_ref="0" * 40, require_base=False)
        assert outcome.exit_code == 1
        assert any("origin/main" in line for line in outcome.lines)

    def test_unresolvable_base_no_baseline_is_pass(self, fx: GitFixture) -> None:
        fx.write("pkg/a.py", LOW)  # no baseline committed
        fx.commit("pre-adoption")
        outcome = fx.ratchet().check(fx.scorer(), base_ref="0" * 40, require_base=False)
        assert outcome.exit_code == 0

    def test_absent_base_baseline_unresolvable_tip_fails_closed(
        self, fx: GitFixture
    ) -> None:
        # Base resolves but carries no baseline blob; origin/main is unresolvable
        # and an in-tree baseline is present -> first-adoption cannot be confirmed,
        # so fail closed UNCONDITIONALLY (no require_base), matching oo_ratchet.
        fx.write("pkg/a.py", LOW)
        base = fx.commit("base without baseline")  # no baseline blob at base
        fx.snapshot()  # in-tree baseline now present
        fx.commit("add in-tree baseline")
        outcome = fx.ratchet().check(fx.scorer(), base_ref=base, require_base=False)
        assert outcome.exit_code == 1
        assert any("origin/main" in line for line in outcome.lines)

    def test_corrupt_in_tree_baseline_is_controlled_nonzero(
        self, fx: GitFixture, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A corrupt .oo-coupling-baseline.json surfaces as a clean non-zero exit
        # through the CLI, not a JSONDecodeError traceback.
        fx.write("pkg/a.py", LOW)
        fx.write(".oo-coupling-baseline.json", "{ not valid json")
        fx.commit("corrupt baseline")
        monkeypatch.chdir(fx.root)
        assert main(["pkg", "--check"]) == 1

    def test_corrupt_baseline_raises_typed_error(self, fx: GitFixture) -> None:
        fx.write(".oo-coupling-baseline.json", "{ not valid json")
        with pytest.raises(CouplingBaselineError):
            CouplingBaseline(fx.root)

    def test_non_utf8_in_tree_baseline_raises_typed_error(self, fx: GitFixture) -> None:
        # A non-UTF8 baseline file raises UnicodeDecodeError on read_text; _load
        # must turn it into the typed error.
        (fx.root / ".oo-coupling-baseline.json").write_bytes(b"\xff\xfe\x00")
        with pytest.raises(CouplingBaselineError):
            CouplingBaseline(fx.root)

    def test_non_utf8_base_baseline_blob_raises_giterror(self, fx: GitFixture) -> None:
        # A committed non-UTF8 baseline blob makes git show's text decode fail;
        # show_baseline must fail closed with GitError, not a traceback.
        fx.write("pkg/a.py", LOW)
        (fx.root / ".oo-coupling-baseline.json").write_bytes(b"\xff\xfe\x00")
        head = fx.commit("non-utf8 baseline blob")
        with pytest.raises(GitError):
            GitRepo(fx.root).show_baseline(head)

    def test_non_dict_base_baseline_raises_giterror(self, fx: GitFixture) -> None:
        # A committed baseline that is valid JSON but not an object (a list)
        # is a controlled GitError, not an AttributeError on .get().
        fx.write("pkg/a.py", LOW)
        fx.write(".oo-coupling-baseline.json", "[1, 2, 3]")
        head = fx.commit("non-dict baseline blob")
        with pytest.raises(GitError):
            GitRepo(fx.root).show_baseline(head)

    def test_non_dict_in_tree_baseline_raises_typed_error(self, fx: GitFixture) -> None:
        fx.write(".oo-coupling-baseline.json", "[1, 2, 3]")
        with pytest.raises(CouplingBaselineError):
            CouplingBaseline(fx.root)

    def test_nested_non_dict_base_baseline_raises_giterror(
        self, fx: GitFixture
    ) -> None:
        # {"pkg/a.py": "garbage"} passes the top-level dict check but the value
        # is not a metric dict; without the nested guard `metric not in "garbage"`
        # is a substring test that skips every metric -> fail-OPEN. Reject it.
        fx.write("pkg/a.py", LOW)
        fx.write(".oo-coupling-baseline.json", '{"pkg/a.py": "garbage"}')
        head = fx.commit("nested non-dict baseline blob")
        with pytest.raises(GitError):
            GitRepo(fx.root).show_baseline(head)

    def test_nested_non_dict_in_tree_baseline_raises_typed_error(
        self, fx: GitFixture
    ) -> None:
        fx.write(".oo-coupling-baseline.json", '{"pkg/a.py": "garbage"}')
        with pytest.raises(CouplingBaselineError):
            CouplingBaseline(fx.root)

    def test_nested_non_dict_base_baseline_fails_closed_via_cli(
        self, fx: GitFixture, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # End-to-end: a nested-malformed base baseline must fail the gate, not
        # let a real regression pass. The in-tree baseline at HEAD is valid, so
        # only the base blob is malformed; the CLI catches the GitError as a
        # controlled non-zero.
        fx.write("pkg/a.py", LOW)
        fx.write(".oo-coupling-baseline.json", '{"pkg/a.py": "garbage"}')
        base = fx.commit("nested non-dict base baseline")
        fx.snapshot()  # overwrite with a valid in-tree baseline
        fx.write("pkg/a.py", HIGH)  # a regression that must not slip through
        fx.commit("valid in-tree baseline and regress")
        monkeypatch.chdir(fx.root)
        assert main(["pkg", "--check", "--base-ref", base, "--require-base"]) == 1

    def test_bool_metric_base_baseline_raises_giterror(self, fx: GitFixture) -> None:
        # A bool metric value (`true`) is an int subclass that would compare as
        # 0/1 -- fail-open. Reject it at the source with GitError.
        fx.write("pkg/a.py", LOW)
        fx.write(
            ".oo-coupling-baseline.json",
            '{"pkg/a.py": {"efferent_coupling": true}}',
        )
        head = fx.commit("bool metric baseline blob")
        with pytest.raises(GitError):
            GitRepo(fx.root).show_baseline(head)

    def test_string_metric_in_tree_baseline_raises_typed_error(
        self, fx: GitFixture
    ) -> None:
        # A string metric value would raise TypeError in the numeric comparison;
        # reject it at load with the typed error instead.
        fx.write(
            ".oo-coupling-baseline.json",
            '{"pkg/a.py": {"efferent_coupling": "5"}}',
        )
        with pytest.raises(CouplingBaselineError):
            CouplingBaseline(fx.root)

    def test_non_utf8_touched_file_fails(self, fx: GitFixture) -> None:
        # A touched .py file that cannot be decoded is scored as an error (like a
        # syntax error), so the ratchet fails on it -- fail-closed, not a crash.
        fx.write("pkg/a.py", LOW)
        fx.snapshot()
        base = fx.commit("base")
        (fx.root / "pkg" / "a.py").write_bytes(b"\xff\xfe# noqa\n")  # non-UTF8, touched
        fx.commit("break encoding")
        outcome = fx.ratchet().check(fx.scorer(), base_ref=base, require_base=True)
        assert outcome.exit_code == 1
        assert any("failed to parse" in line for line in outcome.lines)

    def test_render_log_raises_on_corrupt_audit(self, fx: GitFixture) -> None:
        fx.write(".oo-coupling-audit.jsonl", "not json\n")
        with pytest.raises(CouplingAuditError):
            CouplingAudit(fx.root).render_log()

    def test_corrupt_audit_log_is_controlled_nonzero_via_cli(
        self, fx: GitFixture, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The --log view over a corrupt audit log surfaces a controlled non-zero,
        # not a JSONDecodeError traceback.
        fx.write("pkg/a.py", LOW)
        fx.snapshot()
        fx.write(".oo-coupling-audit.jsonl", "<<<< merge conflict not json\n")
        fx.commit("corrupt audit")
        monkeypatch.chdir(fx.root)
        assert main(["pkg", "--log"]) == 1

    def test_git_degrades_to_none_without_a_repo(self) -> None:
        gr = GitRepo(Path("/nonexistent-coupling-ratchet-xyz"))
        assert gr.root is None
        assert gr.available is False
        assert gr.resolve_base(None) is None
