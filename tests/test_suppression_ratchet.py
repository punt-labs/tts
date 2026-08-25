"""Behavior tests for the decomposed suppression counter and ratchet.

The decomposition is behavior-preserving: these lock the counting semantics
(code-line detection, category totals), the ratchet verdict (increase fails,
steady/decrease passes), and the CLI dispatch through tmp files.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Self

import pytest

from tools.suppression.baseline import SuppressionBaseline, SuppressionBaselineError
from tools.suppression.cli import main
from tools.suppression.gitio import GitError, GitRepo
from tools.suppression.outcome import Outcome
from tools.suppression.patterns import FileSuppressions
from tools.suppression.persist import BaselineFile
from tools.suppression.pyproject import PerFileIgnoresCounter, PyprojectError
from tools.suppression.relax import SuppressionRelax, SuppressionRelaxError
from tools.suppression.report import SuppressionReport
from tools.suppression.scanner import Scanner

# Every ratchet/CLI test drives a real git init/commit/checkout sequence --
# see the marker rationale on test_oo_ratchet.py and the measured timing in
# TESTING.md.
pytestmark = pytest.mark.slow

WITH_SUPPRESSIONS = (
    "from __future__ import annotations\n\n"
    "x = 1  # noqa: E501\n"
    "y = 2  # type: ignore[assignment]\n"
    "z = 3  # pylint: disable=invalid-name\n"
)

# A multiline docstring interior and a bare comment line -- both excluded from
# the code-line scan, so their `noqa` markers must not be counted.
DOCSTRING_AND_COMMENT = '"""\n# noqa\ndocstring body\n"""\n\n# noqa\nvalue = 1\n'


class TestFileSuppressions:
    """Count suppression comments on code lines only."""

    def test_counts_code_line_suppressions(self) -> None:
        fs = FileSuppressions("m.py", WITH_SUPPRESSIONS)
        assert fs.count("noqa") == 1
        assert fs.count("type_ignore") == 1
        assert fs.count("pylint_disable") == 1
        assert fs.total == 3

    def test_docstring_and_comment_lines_excluded(self) -> None:
        assert FileSuppressions("m.py", DOCSTRING_AND_COMMENT).total == 0


class TestScanner:
    """Aggregate per-file counts into a report."""

    def test_scans_directory(self, tmp_path: Path) -> None:
        (tmp_path / "pkg").mkdir()
        (tmp_path / "pkg" / "a.py").write_text(WITH_SUPPRESSIONS)
        report = Scanner(tmp_path / "pkg", tmp_path).report
        assert report.by_category["noqa"] == 1
        assert report.by_category["type_ignore"] == 1
        assert report.total >= 2


class TestSuppressionReportRebased:
    """``rebased`` moves by_file keys onto a new root without losing counts."""

    def test_merges_on_key_collision_instead_of_overwriting(self) -> None:
        # A source file's real path can collide with a literal (non-glob)
        # per-file-ignores pattern in pyproject.toml -- e.g. a materialized
        # "/tmp/xyz/pkg/a.py" rebases onto "pkg/a.py", which is ALSO the key
        # a per-file-ignores breakdown already uses verbatim. Both entries
        # must survive the rebase merged, not one silently clobbering
        # the other.
        fs = FileSuppressions("/tmp/xyz/pkg/a.py", "x = 1  # noqa\ny = 2  # noqa\n")
        report = SuppressionReport([fs], 3, {"pkg/a.py": 3})
        rebased = SuppressionReport.rebased(report, "/tmp/xyz/pkg", "pkg")
        assert rebased.by_file == {"pkg/a.py": {"noqa": 2, "per_file_ignores": 3}}


class GitFixture:
    """An isolated git repo for exercising the base-commit suppression ratchet."""

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

    def write_source(self, source: str) -> None:
        pkg = self._root / "pkg"
        pkg.mkdir(exist_ok=True)
        (pkg / "a.py").write_text(source)

    def report(self) -> SuppressionReport:
        return Scanner(self._root / "pkg", self._root).report

    def check(self, *, base_ref: str | None, require_base: bool) -> Outcome:
        """Run the ratchet check against ``pkg``, the fixture's scan target."""
        return self.baseline().check(
            self.report(),
            target=self._root / "pkg",
            base_ref=base_ref,
            require_base=require_base,
        )

    def update_baseline(self) -> None:
        SuppressionBaseline(self._root).update(self.report(), allow_ci_write=True)

    def write_baseline_text(self, text: str) -> None:
        (self._root / ".suppression-baseline.json").write_text(text)

    def commit(self, msg: str) -> str:
        subprocess.run(["git", "add", "-A"], cwd=self._root, check=True)
        subprocess.run(["git", "commit", "-q", "-m", msg], cwd=self._root, check=True)
        out = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=self._root,
            check=True,
            capture_output=True,
            text=True,
        )
        return out.stdout.strip()

    def baseline(self) -> SuppressionBaseline:
        return SuppressionBaseline(self._root)


@pytest.fixture
def gfx(tmp_path: Path) -> GitFixture:
    return GitFixture(tmp_path)


class TestBaselineRatchet:
    """Increases fail; steady and decreases pass against the base-commit total."""

    def test_increase_fails(self, gfx: GitFixture) -> None:
        gfx.write_source("x = 1  # noqa\n")
        base = gfx.commit("base")
        gfx.write_source("x = 1  # noqa\ny = 2  # noqa\n")  # now 2
        outcome = gfx.check(base_ref=base, require_base=True)
        assert outcome.exit_code == 1
        assert any("increased" in line for line in outcome.lines)

    def test_decrease_passes(self, gfx: GitFixture) -> None:
        gfx.write_source("x = 1  # noqa\ny = 2  # noqa\n")
        base = gfx.commit("base")
        gfx.write_source("x = 1  # noqa\n")  # now 1
        outcome = gfx.check(base_ref=base, require_base=True)
        assert outcome.exit_code == 0
        assert any("decreased" in line for line in outcome.lines)

    def test_steady_passes(self, gfx: GitFixture) -> None:
        gfx.write_source("x = 1  # noqa\n")
        base = gfx.commit("base")
        outcome = gfx.check(base_ref=base, require_base=True)
        assert outcome.exit_code == 0
        assert any("unchanged" in line for line in outcome.lines)

    def test_no_base_baseline_is_bootstrap_pass(self, gfx: GitFixture) -> None:
        gfx.write_source("x = 1  # noqa\n")  # no baseline committed
        base = gfx.commit("pre-adoption")
        outcome = gfx.check(base_ref=base, require_base=False)
        assert outcome.exit_code == 0

    def test_absent_base_unresolvable_tip_fails_closed(self, gfx: GitFixture) -> None:
        # Base commit predates the scanned pkg/ tree entirely; origin/main is
        # unresolvable and an in-tree baseline is present -> fail closed
        # UNCONDITIONALLY (no require_base), matching the OO and coupling
        # ratchets.
        (gfx.root / "README.md").write_text("placeholder\n")
        base = gfx.commit("base before pkg existed")  # pkg/ doesn't exist yet
        gfx.write_source("x = 1  # noqa\n")
        gfx.update_baseline()  # in-tree baseline now present
        gfx.commit("add pkg and in-tree baseline")
        outcome = gfx.check(base_ref=base, require_base=False)
        assert outcome.exit_code == 1
        assert any("origin/main" in line for line in outcome.lines)

    def test_stale_baseline_blob_does_not_false_positive(self, gfx: GitFixture) -> None:
        # A suppression comment can land in source without the committed
        # .suppression-baseline.json being refreshed in the same commit --
        # the blob then undercounts what really existed at that ref. A live
        # rescan of the base ref's real source must not report the
        # pre-existing suppression as new.
        gfx.write_source("x = 1\n")  # no suppression yet
        gfx.update_baseline()  # baseline total 0
        gfx.commit("pre-suppression baseline")
        gfx.write_source("x = 1  # noqa\n")  # suppression lands...
        # ...but the in-tree baseline is never refreshed to match -- it goes
        # stale at this commit, which becomes the comparison base.
        base = gfx.commit("add suppression without refreshing the baseline blob")
        outcome = gfx.check(base_ref=base, require_base=True)
        assert outcome.exit_code == 0
        assert any("unchanged" in line for line in outcome.lines)

    def test_regression_line_names_the_file_at_its_real_path(
        self, gfx: GitFixture
    ) -> None:
        # Pins the rebase in _rescan_at_ref: the base-side report is scanned
        # from a tmp materialization, but the regression line printed to the
        # user must name the file at its real repo-relative path, not the
        # tmp root -- proof the by_file keys were actually rewired, not just
        # that the totals happened to match.
        gfx.write_source("x = 1  # noqa\n")
        base = gfx.commit("base")
        gfx.write_source("x = 1  # noqa\ny = 2  # noqa\n")
        outcome = gfx.check(base_ref=base, require_base=True)
        assert outcome.exit_code == 1
        assert any("pkg/a.py: +1 unwaived (1 -> 2)" in line for line in outcome.lines)


class TestGitRepoPathExistsAtRef:
    """``path_exists_at_ref`` must distinguish real absence from any git failure."""

    def test_absent_path_is_false(self, gfx: GitFixture) -> None:
        gfx.write_source("x = 1\n")
        base = gfx.commit("base")
        repo = GitRepo(gfx.root)
        assert repo.path_exists_at_ref(base, "does/not/exist.py") is False

    def test_present_path_is_true(self, gfx: GitFixture) -> None:
        gfx.write_source("x = 1\n")
        base = gfx.commit("base")
        repo = GitRepo(gfx.root)
        assert repo.path_exists_at_ref(base, "pkg/a.py") is True

    def test_empty_path_probes_the_root_tree(self, gfx: GitFixture) -> None:
        gfx.write_source("x = 1\n")
        base = gfx.commit("base")
        repo = GitRepo(gfx.root)
        assert repo.path_exists_at_ref(base, "") is True

    def test_malformed_ref_raises_giterror_not_false(self, gfx: GitFixture) -> None:
        # A well-formed-but-nonexistent sha and a truly malformed ref both
        # exit 128, but git's stderr distinguishes them: the former reads
        # "does not exist in"/"exists on disk, but not in" (indistinguishable
        # from real absence, and correctly treated as absent), while the
        # latter -- an unparseable ref -- reads "invalid object name" and
        # must raise rather than be swallowed as "not present".
        gfx.write_source("x = 1\n")
        gfx.commit("base")
        repo = GitRepo(gfx.root)
        with pytest.raises(GitError):
            repo.path_exists_at_ref("bad..ref**", "pkg/a.py")


class TestGitRepoArchivePaths:
    """``archive_paths`` fails closed on a bad ref or an unwritable destination."""

    def test_bad_ref_raises(self, gfx: GitFixture, tmp_path: Path) -> None:
        gfx.write_source("x = 1\n")
        gfx.commit("base")
        repo = GitRepo(gfx.root)
        dest = tmp_path / "out"
        dest.mkdir()
        with pytest.raises(GitError):
            repo.archive_paths("0" * 40, ["pkg/a.py"], dest)

    def test_missing_dest_raises(self, gfx: GitFixture, tmp_path: Path) -> None:
        gfx.write_source("x = 1\n")
        base = gfx.commit("base")
        repo = GitRepo(gfx.root)
        with pytest.raises(GitError):
            repo.archive_paths(base, ["pkg/a.py"], tmp_path / "does-not-exist")

    def test_empty_paths_is_a_no_op(self, gfx: GitFixture, tmp_path: Path) -> None:
        gfx.write_source("x = 1\n")
        base = gfx.commit("base")
        repo = GitRepo(gfx.root)
        dest = tmp_path / "out"
        dest.mkdir()
        repo.archive_paths(base, [], dest)
        assert list(dest.iterdir()) == []


class TestRelax:
    """``relax`` records an audited, justified ceiling for one file's real rise."""

    def test_relax_waives_the_regression(self, gfx: GitFixture) -> None:
        gfx.write_source("x = 1  # noqa\n")
        base = gfx.commit("base")
        gfx.write_source("x = 1  # noqa\ny = 2  # noqa\n")
        relax_outcome = gfx.baseline().relax(
            gfx.report(),
            target=gfx.root / "pkg",
            base_ref=base,
            file=str(gfx.root / "pkg" / "a.py"),
            justify="reason",
            allow_ci_write=True,
        )
        assert relax_outcome.exit_code == 0
        check_outcome = gfx.check(base_ref=base, require_base=True)
        assert check_outcome.exit_code == 0
        assert any("waived" in line for line in check_outcome.lines)

    def test_relax_requires_justification(self, gfx: GitFixture) -> None:
        gfx.write_source("x = 1  # noqa\n")
        base = gfx.commit("base")
        gfx.write_source("x = 1  # noqa\ny = 2  # noqa\n")
        outcome = gfx.baseline().relax(
            gfx.report(),
            target=gfx.root / "pkg",
            base_ref=base,
            file=str(gfx.root / "pkg" / "a.py"),
            justify="   ",
            allow_ci_write=True,
        )
        assert outcome.exit_code == 1
        assert any("--justify" in line for line in outcome.lines)

    def test_relax_refuses_when_nothing_rose(self, gfx: GitFixture) -> None:
        gfx.write_source("x = 1  # noqa\n")
        base = gfx.commit("base")
        outcome = gfx.baseline().relax(
            gfx.report(),
            target=gfx.root / "pkg",
            base_ref=base,
            file=str(gfx.root / "pkg" / "a.py"),
            justify="reason",
            allow_ci_write=True,
        )
        assert outcome.exit_code == 1
        assert any("nothing to relax" in line for line in outcome.lines)

    def test_relax_never_covers_a_larger_future_rise(self, gfx: GitFixture) -> None:
        # A ceiling caps the file's TOTAL count, not the size of one rise --
        # growing the SAME file further beyond the ceiling must not ride the
        # earlier relaxation for free.
        gfx.write_source("x = 1  # noqa\n")
        base = gfx.commit("base")
        gfx.write_source("x = 1  # noqa\ny = 2  # noqa\n")
        gfx.baseline().relax(
            gfx.report(),
            target=gfx.root / "pkg",
            base_ref=base,
            file=str(gfx.root / "pkg" / "a.py"),
            justify="reason",
            allow_ci_write=True,
        )
        gfx.write_source("x = 1  # noqa\ny = 2  # noqa\nz = 3  # noqa\n")
        outcome = gfx.check(base_ref=base, require_base=True)
        assert outcome.exit_code == 1
        assert any("+1 unwaived" in line for line in outcome.lines)

    def test_relax_stops_covering_once_the_base_absorbs_it(
        self, gfx: GitFixture
    ) -> None:
        # Regression test: a ceiling must not become standing headroom once
        # the relaxed change merges and the comparison base itself already
        # carries the elevated count -- a LATER, unrelated rise in the same
        # file must be fully unwaived, not silently absorbed by the old
        # ceiling.
        gfx.write_source("x = 1  # noqa\n")
        pre_relax = gfx.commit("base")
        gfx.write_source("x = 1  # noqa\ny = 2  # noqa\n")
        gfx.baseline().relax(
            gfx.report(),
            target=gfx.root / "pkg",
            base_ref=pre_relax,
            file=str(gfx.root / "pkg" / "a.py"),
            justify="reason",
            allow_ci_write=True,
        )
        merged = gfx.commit("merge the relaxed change")
        gfx.write_source("x = 1  # noqa\ny = 2  # noqa\nz = 3  # noqa\n")
        outcome = gfx.check(base_ref=merged, require_base=True)
        assert outcome.exit_code == 1
        assert any("+1 unwaived" in line for line in outcome.lines)
        assert not any("waived" in line for line in outcome.lines[:2])

    def test_relax_blocked_in_ci(
        self, gfx: GitFixture, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("GITHUB_ACTIONS", "true")
        gfx.write_source("x = 1  # noqa\n")
        base = gfx.commit("base")
        gfx.write_source("x = 1  # noqa\ny = 2  # noqa\n")
        outcome = gfx.baseline().relax(
            gfx.report(),
            target=gfx.root / "pkg",
            base_ref=base,
            file=str(gfx.root / "pkg" / "a.py"),
            justify="reason",
            allow_ci_write=False,
        )
        assert outcome.exit_code == 1
        assert any("GITHUB_ACTIONS" in line for line in outcome.lines)

    def test_relax_fails_hard_on_unresolvable_base(self, gfx: GitFixture) -> None:
        # Unlike check(), relax() must never report success while writing
        # nothing -- check()'s bootstrap-pass verdicts are for a read-only
        # comparison and must not leak into a write command's exit code.
        gfx.write_source("x = 1  # noqa\n")
        gfx.commit("base")
        outcome = gfx.baseline().relax(
            gfx.report(),
            target=gfx.root / "pkg",
            base_ref="0" * 40,
            file=str(gfx.root / "pkg" / "a.py"),
            justify="reason",
            allow_ci_write=True,
        )
        assert outcome.exit_code == 1
        assert not (gfx.root / ".suppression-relax.json").exists()

    def test_relax_fails_hard_on_absent_base_tree(self, gfx: GitFixture) -> None:
        (gfx.root / "README.md").write_text("placeholder\n")
        base = gfx.commit("base before pkg existed")
        gfx.write_source("x = 1  # noqa\n")
        gfx.commit("add pkg")
        outcome = gfx.baseline().relax(
            gfx.report(),
            target=gfx.root / "pkg",
            base_ref=base,
            file=str(gfx.root / "pkg" / "a.py"),
            justify="reason",
            allow_ci_write=True,
        )
        assert outcome.exit_code == 1
        assert not (gfx.root / ".suppression-relax.json").exists()


class TestSuppressionRelaxLedger:
    """The relax ledger's own load/save round-trip and fail-closed parsing."""

    def test_corrupt_ledger_raises_typed_error(self, gfx: GitFixture) -> None:
        (gfx.root / ".suppression-relax.json").write_text("{ not valid json")
        with pytest.raises(SuppressionRelaxError):
            SuppressionRelax(gfx.root)

    def test_non_list_ledger_raises_typed_error(self, gfx: GitFixture) -> None:
        (gfx.root / ".suppression-relax.json").write_text('{"file": "a.py"}')
        with pytest.raises(SuppressionRelaxError):
            SuppressionRelax(gfx.root)

    def test_non_positive_ceiling_raises_typed_error(self, gfx: GitFixture) -> None:
        (gfx.root / ".suppression-relax.json").write_text(
            json.dumps(
                [{"file": "a.py", "ceiling": 0, "justify": "x", "added_at": "t"}]
            )
        )
        with pytest.raises(SuppressionRelaxError):
            SuppressionRelax(gfx.root)

    def test_bool_ceiling_is_rejected_not_coerced(self, gfx: GitFixture) -> None:
        # bool is an int subclass; a bool ceiling must raise, not coerce to
        # 1 -- the sibling in-tree baseline guards this exact class of input
        # (BaselineFile._as_int) and this ledger must match.
        (gfx.root / ".suppression-relax.json").write_text(
            json.dumps(
                [{"file": "a.py", "ceiling": True, "justify": "x", "added_at": "t"}]
            )
        )
        with pytest.raises(SuppressionRelaxError):
            SuppressionRelax(gfx.root)

    def test_float_ceiling_is_rejected_not_truncated(self, gfx: GitFixture) -> None:
        (gfx.root / ".suppression-relax.json").write_text(
            json.dumps(
                [{"file": "a.py", "ceiling": 2.9, "justify": "x", "added_at": "t"}]
            )
        )
        with pytest.raises(SuppressionRelaxError):
            SuppressionRelax(gfx.root)

    def test_absent_ledger_has_no_ceiling(self, gfx: GitFixture) -> None:
        assert SuppressionRelax(gfx.root).ceiling("a.py") is None

    def test_round_trips_through_disk(self, gfx: GitFixture) -> None:
        SuppressionRelax(gfx.root).add(file="a.py", ceiling=2, justify="reason")
        reloaded = SuppressionRelax(gfx.root)
        assert reloaded.ceiling("a.py") == 2
        assert reloaded.ceiling("missing.py") is None


class TestUpdateNeverLoosen:
    """update() writes decreases but refuses any net increase in the total."""

    def test_update_refuses_increase(self, gfx: GitFixture) -> None:
        # A rising total is refused and nothing is written, mirroring the OO and
        # coupling writers that refuse a per-metric regression.
        gfx.write_source("x = 1  # noqa\n")
        gfx.update_baseline()  # bootstrap total 1
        gfx.write_source("x = 1  # noqa\ny = 2  # noqa\n")  # now 2
        outcome = gfx.baseline().update(gfx.report(), allow_ci_write=True)
        assert outcome.exit_code == 1
        assert any("never loosens" in line for line in outcome.lines)
        data = json.loads((gfx.root / ".suppression-baseline.json").read_text())
        assert data["total"] == 1  # unchanged: the increase wrote nothing

    def test_update_writes_decrease(self, gfx: GitFixture) -> None:
        gfx.write_source("x = 1  # noqa\ny = 2  # noqa\n")
        gfx.update_baseline()  # bootstrap total 2
        gfx.write_source("x = 1  # noqa\n")  # now 1
        outcome = gfx.baseline().update(gfx.report(), allow_ci_write=True)
        assert outcome.exit_code == 0
        data = json.loads((gfx.root / ".suppression-baseline.json").read_text())
        assert data["total"] == 1

    def test_update_rewrites_equal_total(self, gfx: GitFixture) -> None:
        gfx.write_source("x = 1  # noqa\n")
        gfx.update_baseline()  # bootstrap total 1
        outcome = gfx.baseline().update(gfx.report(), allow_ci_write=True)
        assert outcome.exit_code == 0

    def test_first_adoption_bootstraps_any_total(self, gfx: GitFixture) -> None:
        # No in-tree baseline: the first update writes whatever the current total
        # is, matching the coupling/OO writers that bootstrap absent a baseline.
        gfx.write_source("x = 1  # noqa\ny = 2  # noqa\n")  # total 2, no baseline
        outcome = gfx.baseline().update(gfx.report(), allow_ci_write=True)
        assert outcome.exit_code == 0
        data = json.loads((gfx.root / ".suppression-baseline.json").read_text())
        assert data["total"] == 2


class TestSuppressionFailClosed:
    """Base-commit authority, require-base, and controlled errors."""

    def test_in_tree_edit_cannot_launder_rising_count(self, gfx: GitFixture) -> None:
        # A PR adds a suppression AND rewrites the in-tree baseline to match. The
        # check rescans the base commit's real source, so the rise is still
        # caught regardless of what the laundered in-tree blob says.
        gfx.write_source("x = 1  # noqa\n")
        base = gfx.commit("base")
        gfx.write_source("x = 1  # noqa\ny = 2  # noqa\n")  # now 2
        gfx.update_baseline()  # launder the in-tree baseline to total 2
        gfx.commit("add suppression and launder the in-tree baseline")
        outcome = gfx.check(base_ref=base, require_base=True)
        assert outcome.exit_code == 1
        assert any("increased" in line for line in outcome.lines)

    def test_require_base_unresolvable_fails_closed(self, gfx: GitFixture) -> None:
        gfx.write_source("x = 1  # noqa\n")
        gfx.commit("base")
        outcome = gfx.check(base_ref="0" * 40, require_base=True)
        assert outcome.exit_code == 1
        assert any("--require-base" in line for line in outcome.lines)

    def test_unresolvable_base_with_baseline_fails_closed(
        self, gfx: GitFixture
    ) -> None:
        # No base resolvable + in-tree baseline present + not require_base: match
        # the OO and coupling ratchets -- hard-fail rather than trust the
        # hand-editable in-tree file. Consistent across all three ratchets.
        gfx.write_source("x = 1  # noqa\n")
        gfx.update_baseline()
        gfx.commit("base")
        outcome = gfx.check(base_ref="0" * 40, require_base=False)
        assert outcome.exit_code == 1
        assert any("origin/main" in line for line in outcome.lines)

    def test_corrupt_baseline_raises_typed_error(self, gfx: GitFixture) -> None:
        # A corrupt in-tree baseline is parsed eagerly at construction and raises
        # the typed error rather than a JSONDecodeError traceback.
        gfx.write_baseline_text("{ not valid json")
        with pytest.raises(SuppressionBaselineError):
            SuppressionBaseline(gfx.root)

    def test_corrupt_baseline_is_controlled_nonzero_via_cli(
        self, gfx: GitFixture, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        gfx.write_source("x = 1  # noqa\n")
        gfx.write_baseline_text("{ not valid json")
        gfx.commit("corrupt baseline")
        monkeypatch.chdir(gfx.root)
        # The corrupt in-tree baseline raises at construction; the CLI catches the
        # typed error and returns a clean non-zero exit.
        assert main(["pkg", "--check"]) == 1

    def test_non_dict_in_tree_baseline_raises_typed_error(
        self, gfx: GitFixture
    ) -> None:
        gfx.write_baseline_text("[1, 2, 3]")
        with pytest.raises(SuppressionBaselineError):
            SuppressionBaseline(gfx.root)

    def test_as_int_coerces_nan_and_inf_to_zero(self) -> None:
        assert BaselineFile._as_int(float("nan")) == 0
        assert BaselineFile._as_int(float("inf")) == 0
        assert BaselineFile._as_int(float("-inf")) == 0
        assert BaselineFile._as_int(5) == 5
        assert BaselineFile._as_int("x") == 0

    def test_as_int_rejects_bool(self) -> None:
        # bool is an int subclass; a bool count is invalid data -> 0, never
        # coerced to 1 (which would INFLATE the baseline, a fail-open). This
        # coercion still guards ``update()``'s in-tree ``_refuse_increase`` read.
        assert BaselineFile._as_int(True) == 0
        assert BaselineFile._as_int(False) == 0

    def test_non_utf8_baseline_raises_typed_error(self, gfx: GitFixture) -> None:
        # A non-UTF8 baseline file raises UnicodeDecodeError on read_text; _load
        # must turn it into the typed error, not a traceback.
        (gfx.root / ".suppression-baseline.json").write_bytes(b"\xff\xfe\x00")
        with pytest.raises(SuppressionBaselineError):
            SuppressionBaseline(gfx.root)

    def test_non_utf8_source_is_controlled_nonzero_via_cli(
        self, gfx: GitFixture, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A non-UTF8 .py file raises UnicodeDecodeError on read_text; the CLI must
        # surface a controlled non-zero, not a traceback and not a silent skip.
        gfx.write_source("x = 1\n")
        (gfx.root / "pkg" / "bad.py").write_bytes(b"\xff\xfe# noqa\n")
        monkeypatch.chdir(gfx.root)
        assert main(["pkg", "--json"]) == 1

    def test_scanner_propagates_unreadable_file(self, gfx: GitFixture) -> None:
        # An unreadable path that matches *.py (here a directory named like a
        # module) must raise, not be silently skipped -- skipping would
        # undercount a file's suppressions and let a real rise pass (fail-open).
        gfx.write_source("x = 1\n")
        (gfx.root / "pkg" / "isdir.py").mkdir()
        with pytest.raises(OSError):
            Scanner(gfx.root / "pkg", gfx.root)

    def test_unreadable_file_is_controlled_nonzero_via_cli(
        self, gfx: GitFixture, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        gfx.write_source("x = 1\n")
        (gfx.root / "pkg" / "isdir.py").mkdir()
        monkeypatch.chdir(gfx.root)
        # The OSError surfaces as a clean non-zero through the CLI, not a
        # traceback -- and not a silent skip.
        assert main(["pkg", "--json"]) == 1


class TestCiWriteGuard:
    """update() refuses to run under GITHUB_ACTIONS without --allow-ci-write."""

    def test_update_blocked_in_ci(
        self, gfx: GitFixture, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("GITHUB_ACTIONS", "true")
        gfx.write_source("x = 1  # noqa\n")
        outcome = gfx.baseline().update(gfx.report(), allow_ci_write=False)
        assert outcome.exit_code == 1
        assert any("GITHUB_ACTIONS" in line for line in outcome.lines)

    def test_update_allowed_with_flag_in_ci(
        self, gfx: GitFixture, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("GITHUB_ACTIONS", "true")
        gfx.write_source("x = 1  # noqa\n")
        outcome = gfx.baseline().update(gfx.report(), allow_ci_write=True)
        assert outcome.exit_code == 0


class TestPyproject:
    """per_file_ignores counting fails closed on a broken pyproject.toml."""

    def test_absent_pyproject_is_zero(self, gfx: GitFixture) -> None:
        # No pyproject.toml legitimately contributes 0 -- not a failure.
        counter = PerFileIgnoresCounter(gfx.root / "pyproject.toml")
        assert counter.total == 0

    def test_invalid_toml_raises(self, gfx: GitFixture) -> None:
        (gfx.root / "pyproject.toml").write_text("this is [ not valid toml")
        with pytest.raises(PyprojectError):
            PerFileIgnoresCounter(gfx.root / "pyproject.toml")

    def test_invalid_pyproject_is_controlled_nonzero_via_cli(
        self, gfx: GitFixture, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # An existing-but-invalid pyproject.toml would undercount per_file_ignores;
        # the CLI turns it into a controlled non-zero, not a silent zero.
        gfx.write_source("x = 1\n")
        (gfx.root / "pyproject.toml").write_text("this is [ not valid toml")
        monkeypatch.chdir(gfx.root)
        assert main(["pkg", "--json"]) == 1

    def test_absent_pyproject_passes_via_cli(
        self, gfx: GitFixture, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        gfx.write_source("x = 1\n")
        monkeypatch.chdir(gfx.root)
        assert main(["pkg", "--json"]) == 0

    def test_no_ignores_section_is_zero(self, gfx: GitFixture) -> None:
        # A valid pyproject with no per-file-ignores section legitimately -> 0.
        (gfx.root / "pyproject.toml").write_text("[tool.ruff]\nline-length = 88\n")
        assert PerFileIgnoresCounter(gfx.root / "pyproject.toml").total == 0

    def test_valid_ignores_are_counted(self, gfx: GitFixture) -> None:
        (gfx.root / "pyproject.toml").write_text(
            "[tool.ruff.lint.per-file-ignores]\n"
            '"a.py" = ["E501", "F401"]\n'
            '"b.py" = ["N801"]\n'
        )
        assert PerFileIgnoresCounter(gfx.root / "pyproject.toml").total == 3

    def test_ignores_not_a_table_raises(self, gfx: GitFixture) -> None:
        # per-file-ignores EXISTS but is a string, not a table -> fail closed.
        (gfx.root / "pyproject.toml").write_text(
            '[tool.ruff.lint]\nper-file-ignores = "oops"\n'
        )
        with pytest.raises(PyprojectError):
            PerFileIgnoresCounter(gfx.root / "pyproject.toml")

    def test_ignores_codes_not_a_list_raises(self, gfx: GitFixture) -> None:
        # A per-file entry whose codes value isn't a list is corrupt -> fail closed.
        (gfx.root / "pyproject.toml").write_text(
            '[tool.ruff.lint.per-file-ignores]\n"a.py" = "E501"\n'
        )
        with pytest.raises(PyprojectError):
            PerFileIgnoresCounter(gfx.root / "pyproject.toml")

    def test_non_table_ancestor_is_zero(self, gfx: GitFixture) -> None:
        # A malformed non-table ancestor means no section to count -> 0, no crash.
        (gfx.root / "pyproject.toml").write_text('tool = "not a table"\n')
        assert PerFileIgnoresCounter(gfx.root / "pyproject.toml").total == 0


class TestRepoRootResolution:
    """The CLI anchors the baseline and pyproject to the repo root, not cwd."""

    def test_cli_resolves_repo_root_from_subdir(
        self, gfx: GitFixture, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # From a subdirectory, the CLI resolves the repo root via GitRepo and
        # reads the ROOT .suppression-baseline.json. With cwd anchoring it would
        # miss the baseline and wrongly bootstrap-pass; anchored, the
        # unresolvable-base + baseline-present case fails closed.
        gfx.write_source("x = 1  # noqa\n")
        gfx.update_baseline()  # writes repo-root .suppression-baseline.json
        gfx.commit("base with root baseline")
        monkeypatch.chdir(gfx.root / "pkg")
        assert main([".", "--check", "--base-ref", "0" * 40]) == 1


def test_cli_json_roundtrip(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "a.py").write_text(WITH_SUPPRESSIONS)
    monkeypatch.chdir(tmp_path)
    assert main(["pkg", "--json"]) == 0
