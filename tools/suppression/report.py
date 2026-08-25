"""Aggregate per-file suppression counts into a whole-tree report."""

from __future__ import annotations

import json
from typing import Self

from .patterns import CATEGORIES, PATTERN_NAMES, FileSuppressions


class SuppressionReport:
    """Aggregate suppression counts across files and render them."""

    _total: int
    _by_category: dict[str, int]
    _by_file: dict[str, dict[str, int]]

    def __new__(
        cls,
        file_results: list[FileSuppressions],
        per_file_ignores_count: int,
        per_file_ignores_breakdown: dict[str, int] | None = None,
    ) -> Self:
        self = super().__new__(cls)
        self._by_category = dict.fromkeys(CATEGORIES, 0)
        self._by_category["per_file_ignores"] = per_file_ignores_count
        self._by_file = {}
        for fs in file_results:
            for name in PATTERN_NAMES:
                self._by_category[name] += fs.count(name)
            if fs.total > 0:
                self._by_file[fs.path] = fs.to_dict()
        # Per-file-ignores entries are keyed by their literal pyproject.toml
        # pattern -- a repo-relative path on both the current and any
        # rescanned-at-ref side, so (unlike source-file keys) they never need
        # rebasing onto a different filesystem root. Folding them into
        # by_file is what lets a regression report name the actual
        # pyproject.toml entry responsible for a count rise, instead of
        # hiding it inside an aggregate category total with no location.
        for pattern, count in (per_file_ignores_breakdown or {}).items():
            if count <= 0:
                continue
            self._by_file.setdefault(pattern, {})["per_file_ignores"] = count
        self._total = sum(self._by_category.values())
        return self

    @property
    def total(self) -> int:
        """Return the total suppression count across all files and config."""
        return self._total

    @property
    def by_category(self) -> dict[str, int]:
        """Return the suppression count per category."""
        return dict(self._by_category)

    @property
    def by_file(self) -> dict[str, dict[str, int]]:
        """Return the non-zero suppression counts per file."""
        return dict(self._by_file)

    @classmethod
    def rebased(cls, source: Self, old_prefix: str, new_prefix: str) -> Self:
        """Return a copy of ``source`` with ``by_file`` keys moved onto a new root.

        A report scanned from a temporary materialization of a git ref and a
        report scanned from the real working tree describe the same source
        files under different filesystem roots -- comparing their ``by_file``
        dicts directly would treat every path as unrelated. Rewriting one
        side's keys onto the other's root is what makes a per-file diff
        meaningful. Only keys actually rooted under ``old_prefix`` are
        rewritten; a per-file-ignores key is a literal pyproject.toml
        pattern string (e.g. ``src/pkg/mod.py``), already repo-relative and
        identical on both sides, and must pass through unchanged -- blindly
        prepending ``new_prefix`` to it would garble an already-correct key.
        A rewritten source-file key can land on the same final string as an
        untouched per-file-ignores key (a file whose path is also a literal
        per-file-ignores pattern, e.g. ``__main__.py``) -- category counts
        are merged on collision, never overwritten, or the source-line
        counts for that file would silently vanish. ``total`` and
        ``by_category`` are unaffected by path spelling and carry over
        unchanged.
        """
        self = super().__new__(cls)
        self._total = source._total
        self._by_category = dict(source._by_category)
        by_file: dict[str, dict[str, int]] = {}
        for fpath, counts in source._by_file.items():
            new_key = (
                new_prefix + fpath.removeprefix(old_prefix)
                if fpath.startswith(old_prefix)
                else fpath
            )
            merged = by_file.setdefault(new_key, {})
            for category, count in counts.items():
                merged[category] = merged.get(category, 0) + count
        self._by_file = by_file
        return self

    def to_json(self) -> str:
        """Return the report as machine-readable JSON."""
        return json.dumps(
            {
                "total": self._total,
                "by_category": self._by_category,
                "by_file": self._by_file,
            },
            indent=2,
        )

    def render(self) -> list[str]:
        """Return the human-readable summary as report lines."""
        lines = [
            f"\nTotal suppressions: {self._total}",
            f"\n{'Category':<20} {'Count':>6}",
            "-" * 28,
        ]
        lines.extend(
            f"{category:<20} {count:>6}"
            for category, count in sorted(self._by_category.items())
        )
        return lines

    def render_threshold(self) -> list[str]:
        """Return the per-file breakdown as report lines."""
        lines = ["\n--- Per-file breakdown ---"]
        if not self._by_file:
            lines.append("  (no suppressions found)")
            return lines
        for fpath in sorted(self._by_file):
            counts = self._by_file[fpath]
            lines.append(f"\n  {fpath}  (total: {sum(counts.values())})")
            lines.extend(
                f"    {cat:<20} {count:>4}" for cat, count in sorted(counts.items())
            )
        return lines
