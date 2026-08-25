"""Suppression baseline: the live base-ref ratchet check and relax gate."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Self

from .gitio import GitRepo
from .outcome import Outcome
from .persist import BaselineFile, SuppressionBaselineError
from .relax import SuppressionRelax
from .report import SuppressionReport
from .rescan import BaseRescanner

__all__ = ["SuppressionBaseline", "SuppressionBaselineError"]


class SuppressionBaseline:
    """Compare live counts against a rescan of the base ref; gate relaxations.

    The comparison baseline is a fresh rescan of the base commit's real source
    tree, not a committed ``.suppression-baseline.json`` blob -- a blob can go
    stale (a suppression added to source without updating the blob in the same
    commit), and a stale blob undercounts the base side, turning existing
    suppressions into false "new suppression" regressions. Rescanning both
    sides through the same pipeline (:class:`~.rescan.BaseRescanner`) is what
    guarantees the two counts can never disagree about what counts as a
    suppression. The static in-tree file (:class:`~.persist.BaselineFile`)
    is a separate concern: it backs only the never-loosening ``update()``
    path and the audit trail.
    """

    _git: GitRepo
    _rescanner: BaseRescanner
    _relax: SuppressionRelax
    _file: BaselineFile

    def __new__(cls, root: Path | None = None) -> Self:
        self = super().__new__(cls)
        base = root if root is not None else Path.cwd()
        self._git = GitRepo(base)
        self._file = BaselineFile(base)
        self._rescanner = BaseRescanner(self._git, self._file.path.parent)
        self._relax = SuppressionRelax(base)
        return self

    @property
    def has_baseline(self) -> bool:
        """Return whether a baseline file exists on disk."""
        return self._file.exists

    def check(
        self,
        report: SuppressionReport,
        *,
        target: Path,
        base_ref: str | None,
        require_base: bool,
    ) -> Outcome:
        """Compare current counts against a live rescan of ``target`` at the base."""
        base = self._git.resolve_base(base_ref)
        if base is None:
            return self._no_base(require_base=require_base)
        base_report = self._rescanner.rescan(base, target)
        if base_report is None:
            return self._absent_base(target)
        return self._compare(report, base_report)

    def _no_base(self, *, require_base: bool) -> Outcome:
        """Decide the verdict when no comparison base can be resolved.

        Matches the OO and coupling ratchets' ``_no_base`` exactly: fail closed
        under ``--require-base``; a genuine first-adoption (no in-tree baseline)
        passes so the first baseline can be created; but an in-tree baseline
        present with an unresolvable base means a stale or unfetched
        ``origin/main`` -- fail loud rather than trust a hand-editable file.
        """
        if require_base:
            return Outcome.failed(
                "FAIL: base ref unresolvable and --require-base is set"
            )
        if not self.has_baseline:
            return Outcome.passed(
                "No base and no in-tree baseline -- first-adoption bootstrap pass"
            )
        return Outcome.failed(
            "FAIL: cannot resolve merge-base (origin/main unfetched or stale) "
            "with an in-tree baseline present; fetch origin/main or pass --base-ref"
        )

    def _absent_base(self, target: Path) -> Outcome:
        """Decide the verdict when ``target`` does not exist at the base ref.

        Matches the OO and coupling ratchets' ``_absent_base_baseline`` exactly
        (no ``require_base`` param): fail closed unconditionally when the
        ``origin/main`` tip is unresolvable with an in-tree baseline present, or
        when the tip already carries ``target`` (the branch forked before the
        scanned source tree existed -- rebase, don't bootstrap).
        """
        tip = self._git.resolve_ref("origin/main")
        if tip is None:
            if self.has_baseline:
                return Outcome.failed(
                    "FAIL: base has no scanned source tree and origin/main is "
                    "unresolvable with an in-tree baseline present; fetch origin/main"
                )
            return Outcome.passed(
                "No source tree at base and no origin/main -- "
                "first-adoption bootstrap pass"
            )
        if self._git.path_exists_at_ref(tip, self._rescanner.relpath(target)):
            return Outcome.failed(
                "FAIL: base commit predates the scanned source tree; "
                "rebase onto current main"
            )
        return Outcome.passed(
            "No source tree at base or origin/main tip -- first-adoption bootstrap pass"
        )

    def _compare(
        self, report: SuppressionReport, base_report: SuppressionReport
    ) -> Outcome:
        baseline_total = base_report.total
        raw_total = report.total
        waived = self._waived_total(report, base_report)
        current_total = raw_total - waived
        head = [
            f"\nBaseline total: {baseline_total}",
            f"Current total:  {raw_total}"
            + (f" ({waived} waived, see {self._relax.path.name})" if waived else ""),
        ]
        if current_total > baseline_total:
            return Outcome(
                1, tuple(head + self._regression(base_report, report, baseline_total))
            )
        if current_total < baseline_total:
            drop = baseline_total - current_total
            return Outcome.passed(
                *head, f"\nPASS: suppression count decreased by {drop}"
            )
        return Outcome.passed(*head, "\nPASS: suppression count unchanged")

    def _unwaived_rise(self, fpath: str, cur: int, base: int) -> int:
        """Return the part of ``fpath``'s rise a recorded ceiling does not cover.

        A ceiling caps the file's *total* count, not the size of any one
        rise -- ``allowed`` is never below the base count, so once the base
        rescan itself reflects a merged, relaxed change (``base >=
        ceiling``), the ceiling stops covering anything and a fresh,
        unrelated rise in the same file is fully unwaived again.
        """
        rise = cur - base
        if rise <= 0:
            return 0
        ceiling = self._relax.ceiling(fpath)
        if ceiling is None:
            return rise
        allowed = max(base, ceiling)
        return max(0, cur - allowed)

    def _waived_total(
        self, report: SuppressionReport, base_report: SuppressionReport
    ) -> int:
        """Return how much of the raw total rise a recorded ceiling covers."""
        current_by_file = report.by_file
        baseline_by_file = base_report.by_file
        total = 0
        for fpath in set(current_by_file) | set(baseline_by_file):
            cur = sum(current_by_file.get(fpath, {}).values())
            base = sum(baseline_by_file.get(fpath, {}).values())
            rise = cur - base
            if rise > 0:
                total += rise - self._unwaived_rise(fpath, cur, base)
        return total

    def relax(
        self,
        report: SuppressionReport,
        *,
        target: Path,
        base_ref: str | None,
        file: str,
        justify: str,
        allow_ci_write: bool,
    ) -> Outcome:
        """Record an audited ceiling for ``file`` at its current count.

        Mirrors the OO ratchet's ``--relax``/``--justify``: the single
        sanctioned, audited loosening, refused without a non-empty
        justification and refused when there is nothing to relax. Unlike
        ``check``, an unresolvable base or an absent base tree is always a
        hard failure here -- ``check``'s bootstrap-pass verdicts exist for a
        read-only comparison and must never be reused to make a write
        command report success while writing nothing.
        """
        blocked = self._guard(allow_ci_write=allow_ci_write)
        if blocked is not None:
            return blocked
        if not justify.strip():
            return Outcome.failed("FAIL: --relax requires a non-empty --justify")
        base = self._git.resolve_base(base_ref)
        if base is None:
            return Outcome.failed(
                f"FAIL: cannot resolve comparison base for --relax "
                f"(base_ref={base_ref!r}); nothing was relaxed"
            )
        base_report = self._rescanner.rescan(base, target)
        if base_report is None:
            return Outcome.failed(
                f"FAIL: {target} does not exist at base {base}; nothing was relaxed"
            )
        current = sum(report.by_file.get(file, {}).values())
        baseline = sum(base_report.by_file.get(file, {}).values())
        if current <= baseline:
            return Outcome.failed(f"FAIL: nothing to relax for {file} (no rise)")
        self._relax.add(file=file, ceiling=current, justify=justify)
        self._file.append_relax_audit(
            file=file,
            before=baseline,
            after=current,
            justify=justify,
            commit=self._git.short_head(),
        )
        return Outcome.passed(
            f"\nRelaxed {file}: ceiling set to {current} "
            f"(was {baseline}, reason: {justify})",
            f"  ledger: {self._relax.path}",
        )

    def update(self, report: SuppressionReport, *, allow_ci_write: bool) -> Outcome:
        """Write current counts to the baseline, never loosening.

        Refuses any net increase over the in-tree baseline total: an update that
        would raise the count writes nothing and fails, exactly like the OO and
        coupling writers refuse a per-metric regression. A decrease or unchanged
        total writes normally; genuine first-adoption (no in-tree baseline)
        bootstraps.
        """
        blocked = self._guard(allow_ci_write=allow_ci_write)
        if blocked is not None:
            return blocked
        refused = self._file.refuse_increase(report)
        if refused is not None:
            return refused
        self._file.save(report)
        self._file.append_update_audit(report)
        lines = [
            f"\nBaseline updated: {self._file.path}",
            f"  total: {report.total}",
        ]
        lines.extend(
            f"  {category}: {count}"
            for category, count in sorted(report.by_category.items())
        )
        return Outcome.passed(*lines)

    @staticmethod
    def _guard(*, allow_ci_write: bool) -> Outcome | None:
        if os.environ.get("GITHUB_ACTIONS") == "true" and not allow_ci_write:
            return Outcome.failed(
                "FAIL: refusing to write suppression baseline under GITHUB_ACTIONS "
                "without --allow-ci-write"
            )
        return None

    def _regression(
        self,
        base_report: SuppressionReport,
        report: SuppressionReport,
        baseline_total: int,
    ) -> list[str]:
        effective_total = report.total - self._waived_total(report, base_report)
        diff = effective_total - baseline_total
        lines = [
            f"\nFAIL: suppression count increased by {diff} (after waivers)",
            "\nFiles with new or increased, unwaived suppressions:",
        ]
        baseline_by_file = base_report.by_file
        current_by_file = report.by_file
        for fpath in sorted(set(current_by_file) | set(baseline_by_file)):
            cur = sum(current_by_file.get(fpath, {}).values())
            base = sum(baseline_by_file.get(fpath, {}).values())
            unwaived = self._unwaived_rise(fpath, cur, base)
            if unwaived <= 0:
                continue
            lines.append(f"  {fpath}: +{unwaived} unwaived ({base} -> {cur})")
        return lines
