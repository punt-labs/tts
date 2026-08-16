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
from tools.suppression.patterns import FileSuppressions
from tools.suppression.pyproject import PerFileIgnoresCounter, PyprojectError
from tools.suppression.report import SuppressionReport
from tools.suppression.scanner import Scanner

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
        gfx.update_baseline()  # base-commit baseline total 1
        base = gfx.commit("base")
        gfx.write_source("x = 1  # noqa\ny = 2  # noqa\n")  # now 2
        outcome = gfx.baseline().check(gfx.report(), base_ref=base, require_base=True)
        assert outcome.exit_code == 1
        assert any("increased" in line for line in outcome.lines)

    def test_decrease_passes(self, gfx: GitFixture) -> None:
        gfx.write_source("x = 1  # noqa\ny = 2  # noqa\n")
        gfx.update_baseline()  # base total 2
        base = gfx.commit("base")
        gfx.write_source("x = 1  # noqa\n")  # now 1
        outcome = gfx.baseline().check(gfx.report(), base_ref=base, require_base=True)
        assert outcome.exit_code == 0
        assert any("decreased" in line for line in outcome.lines)

    def test_steady_passes(self, gfx: GitFixture) -> None:
        gfx.write_source("x = 1  # noqa\n")
        gfx.update_baseline()
        base = gfx.commit("base")
        outcome = gfx.baseline().check(gfx.report(), base_ref=base, require_base=True)
        assert outcome.exit_code == 0
        assert any("unchanged" in line for line in outcome.lines)

    def test_no_base_baseline_is_bootstrap_pass(self, gfx: GitFixture) -> None:
        gfx.write_source("x = 1  # noqa\n")  # no baseline committed
        base = gfx.commit("pre-adoption")
        outcome = gfx.baseline().check(gfx.report(), base_ref=base, require_base=False)
        assert outcome.exit_code == 0

    def test_absent_base_unresolvable_tip_fails_closed(self, gfx: GitFixture) -> None:
        # Base resolves but carries no baseline blob; origin/main is unresolvable
        # and an in-tree baseline is present -> fail closed UNCONDITIONALLY (no
        # require_base), matching the OO and coupling ratchets.
        gfx.write_source("x = 1  # noqa\n")
        base = gfx.commit("base without baseline")  # no baseline blob at base
        gfx.update_baseline()  # in-tree baseline now present
        gfx.commit("add in-tree baseline")
        outcome = gfx.baseline().check(gfx.report(), base_ref=base, require_base=False)
        assert outcome.exit_code == 1
        assert any("origin/main" in line for line in outcome.lines)


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
        # check reads the base-commit baseline, so the rise is still caught.
        gfx.write_source("x = 1  # noqa\n")
        gfx.update_baseline()  # base total 1
        base = gfx.commit("base")
        gfx.write_source("x = 1  # noqa\ny = 2  # noqa\n")  # now 2
        gfx.update_baseline()  # launder the in-tree baseline to total 2
        gfx.commit("add suppression and launder the in-tree baseline")
        outcome = gfx.baseline().check(gfx.report(), base_ref=base, require_base=True)
        assert outcome.exit_code == 1
        assert any("increased" in line for line in outcome.lines)

    def test_require_base_unresolvable_fails_closed(self, gfx: GitFixture) -> None:
        gfx.write_source("x = 1  # noqa\n")
        gfx.update_baseline()
        gfx.commit("base")
        outcome = gfx.baseline().check(
            gfx.report(), base_ref="0" * 40, require_base=True
        )
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
        outcome = gfx.baseline().check(
            gfx.report(), base_ref="0" * 40, require_base=False
        )
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

    def test_non_dict_base_baseline_raises_giterror(self, gfx: GitFixture) -> None:
        # A committed baseline that is valid JSON but not an object (a list) is a
        # controlled GitError, not an AttributeError on .get().
        gfx.write_source("x = 1  # noqa\n")
        gfx.write_baseline_text("[1, 2, 3]")
        head = gfx.commit("non-dict baseline blob")
        with pytest.raises(GitError):
            GitRepo(gfx.root).show_baseline(head)

    def test_non_dict_in_tree_baseline_raises_typed_error(
        self, gfx: GitFixture
    ) -> None:
        gfx.write_baseline_text("[1, 2, 3]")
        with pytest.raises(SuppressionBaselineError):
            SuppressionBaseline(gfx.root)

    def test_nested_non_dict_by_file_is_fail_closed(self, gfx: GitFixture) -> None:
        # A base baseline whose by_file has a non-dict value must not crash on a
        # rise. The malformed entry is dropped (counts as 0 baseline), so the
        # current suppression registers as an increase -- fail-closed, not a
        # traceback.
        gfx.write_source("x = 1  # noqa\n")  # current total 1
        gfx.write_baseline_text('{"total": 0, "by_file": {"pkg/a.py": "garbage"}}')
        base = gfx.commit("nested non-dict by_file, total 0")
        outcome = gfx.baseline().check(gfx.report(), base_ref=base, require_base=True)
        assert outcome.exit_code == 1
        assert any("increased" in line for line in outcome.lines)

    def test_as_int_coerces_nan_and_inf_to_zero(self) -> None:
        assert SuppressionBaseline._as_int(float("nan")) == 0
        assert SuppressionBaseline._as_int(float("inf")) == 0
        assert SuppressionBaseline._as_int(float("-inf")) == 0
        assert SuppressionBaseline._as_int(5) == 5
        assert SuppressionBaseline._as_int("x") == 0

    def test_as_int_rejects_bool(self) -> None:
        # bool is an int subclass; a bool count is invalid data -> 0, never
        # coerced to 1 (which would INFLATE the baseline, a fail-open).
        assert SuppressionBaseline._as_int(True) == 0
        assert SuppressionBaseline._as_int(False) == 0

    def test_bool_total_does_not_inflate_baseline(self, gfx: GitFixture) -> None:
        # A corrupt `total: true` must coerce to 0, not 1. Fail-closed: baseline
        # 0 < current 1 -> increase, rather than baseline 1 == current 1 -> pass.
        gfx.write_source("x = 1  # noqa\n")  # current total 1
        gfx.write_baseline_text('{"total": true}')
        base = gfx.commit("bool total")
        outcome = gfx.baseline().check(gfx.report(), base_ref=base, require_base=True)
        assert outcome.exit_code == 1
        assert any("increased" in line for line in outcome.lines)

    def test_nan_total_is_coerced_not_crash(self, gfx: GitFixture) -> None:
        # json.loads parses NaN; _as_int must coerce the base total to 0 rather
        # than raise ValueError. Fail-closed: baseline 0 < current 1 -> increase.
        gfx.write_source("x = 1  # noqa\n")  # current total 1
        gfx.write_baseline_text('{"total": NaN}')
        base = gfx.commit("NaN total")
        outcome = gfx.baseline().check(gfx.report(), base_ref=base, require_base=True)
        assert outcome.exit_code == 1
        assert any("increased" in line for line in outcome.lines)

    def test_nested_non_numeric_count_is_coerced(self, gfx: GitFixture) -> None:
        # A by_file entry is a dict but its count is a string; it must coerce to
        # int (non-numeric -> 0) so _regression's sum(...values()) does not crash.
        # Fail-closed: the baseline counts as 0, so the current count is a rise.
        gfx.write_source("x = 1  # noqa\n")  # current total 1
        gfx.write_baseline_text(
            '{"total": 0, "by_file": {"pkg/a.py": {"noqa": "garbage"}}}'
        )
        base = gfx.commit("non-numeric nested count, total 0")
        outcome = gfx.baseline().check(gfx.report(), base_ref=base, require_base=True)
        assert outcome.exit_code == 1
        assert any("increased" in line for line in outcome.lines)

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
