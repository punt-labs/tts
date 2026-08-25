"""In-tree baseline persistence: the never-loosening ``update`` write path.

Separate from the live base-ref comparison (:class:`~.baseline.SuppressionBaseline`):
this class owns only the static ``.suppression-baseline.json``/
``.suppression-audit.jsonl`` files -- reading them for ``update()``'s
never-loosen guard, writing them, and appending the audit trail (for both a
plain update and a ``--relax``).
"""

from __future__ import annotations

import datetime
import json
from pathlib import Path
from typing import ClassVar, Self

from .outcome import Outcome
from .report import SuppressionReport


class SuppressionBaselineError(Exception):
    """The in-tree ``.suppression-baseline.json`` could not be parsed.

    Raised instead of letting ``json.JSONDecodeError`` escape, so a corrupt or
    hand-broken baseline becomes a controlled non-zero outcome (the CLI catches
    it) rather than a traceback out of the gate.
    """


class BaselineFile:
    """The static in-tree baseline file: never-loosen updates and audit trail."""

    BASELINE_FILE: ClassVar[str] = ".suppression-baseline.json"
    AUDIT_FILE: ClassVar[str] = ".suppression-audit.jsonl"

    _baseline_path: Path
    _audit_path: Path
    _entries: dict[str, object]

    def __new__(cls, root: Path) -> Self:
        self = super().__new__(cls)
        self._baseline_path = root / cls.BASELINE_FILE
        self._audit_path = root / cls.AUDIT_FILE
        self._entries = self._load()  # eager: a corrupt in-tree file fails here
        return self

    @property
    def exists(self) -> bool:
        """Return whether a baseline file exists on disk."""
        return self._baseline_path.exists()

    @property
    def path(self) -> Path:
        """Return the baseline file's path."""
        return self._baseline_path

    def refuse_increase(self, report: SuppressionReport) -> Outcome | None:
        """Return a failure ``Outcome`` if ``report`` would raise the total.

        Genuine first-adoption (no in-tree baseline) bootstraps, mirroring the
        coupling/OO writers, which write new entries with no base to regress
        against. An existing baseline is never loosened: a rise refuses.
        """
        if not self.exists:
            return None
        baseline_total = self._as_int(self._entries.get("total", 0))
        if report.total > baseline_total:
            rise = report.total - baseline_total
            return Outcome.failed(
                f"\nBaseline total: {baseline_total}",
                f"Current total:  {report.total}",
                f"\nFAIL: refusing to raise the suppression baseline by {rise} "
                f"({baseline_total} -> {report.total}); update never loosens",
            )
        return None

    def save(self, report: SuppressionReport) -> None:
        """Overwrite the baseline file with ``report``'s counts."""
        data = {
            "total": report.total,
            "by_category": report.by_category,
            "by_file": report.by_file,
            "updated_at": self._now(),
        }
        self._baseline_path.write_text(json.dumps(data, indent=2) + "\n")

    def append_update_audit(self, report: SuppressionReport) -> None:
        """Append one ``update()`` entry to the audit trail."""
        entry = {
            "ts": self._now(),
            "total": report.total,
            "by_category": report.by_category,
        }
        self._append(entry)

    def append_relax_audit(
        self,
        *,
        file: str,
        before: int,
        after: int,
        justify: str,
        # None only when HEAD is unresolvable (no repo, or `rev-parse` fails) --
        # the entry is still worth recording, just without a commit to cite.
        commit: str | None,
    ) -> None:
        """Append one ``relax()`` entry to the audit trail."""
        entry = {
            "ts": self._now(),
            "verdict": "relaxed",
            "file": file,
            "before": before,
            "after": after,
            "reason": justify,
            "commit": commit,
        }
        self._append(entry)

    def _append(self, entry: dict[str, object]) -> None:
        with self._audit_path.open("a") as f:
            f.write(json.dumps(entry, separators=(",", ":")) + "\n")

    @staticmethod
    def _as_int(raw: object) -> int:
        # bool is a subclass of int; a bool count (`true`) is invalid data and
        # must be 0, not coerced to 1 -- coercing to 1 would INFLATE the baseline
        # (fail-open). Reject it before the numeric handling.
        if isinstance(raw, bool) or not isinstance(raw, (int, float)):
            return 0
        # json.loads parses NaN/Infinity, and int(nan)/int(inf) raise
        # ValueError/OverflowError. Coerce those to 0, consistent with the
        # non-numeric -> 0 contract, so a corrupt baseline never throws.
        try:
            return int(raw)
        except (ValueError, OverflowError):
            return 0

    def _load(self) -> dict[str, object]:
        if not self._baseline_path.exists():
            return {}
        try:
            raw = json.loads(self._baseline_path.read_text())
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            msg = f"unreadable suppression baseline file {self._baseline_path}: {exc}"
            raise SuppressionBaselineError(msg) from exc
        if not isinstance(raw, dict):
            msg = f"non-dict suppression baseline file {self._baseline_path}"
            raise SuppressionBaselineError(msg)
        return raw

    @staticmethod
    def _now() -> str:
        return datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
