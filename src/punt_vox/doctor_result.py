"""Doctor sub-check result type and its collection.

:class:`CheckResult` is the value each sub-check returns; its four alternate
constructors (:meth:`~CheckResult.ok`, :meth:`~CheckResult.fail`,
:meth:`~CheckResult.warn`, :meth:`~CheckResult.of`) are the only way callers
build one. :class:`CheckResults` wraps the list a run collects and owns the
render into ``(JSON payload, display text)`` -- kept together so the class of
things being formatted owns the formatting.

Kept out of :mod:`punt_vox.doctor` so the schedule/orchestration module
(``DoctorCheck``) and each sub-check module (``doctor_mpv``, and further
extractions) share one small dependency for the result type without an import
edge back through the schedule.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from typing import Self, final

__all__ = [
    "FAIL",
    "OK",
    "OPTIONAL",
    "STATUS_KIND",
    "WARN",
    "CheckResult",
    "CheckResults",
]


# ---------------------------------------------------------------------------
# Display constants
# ---------------------------------------------------------------------------

OK = "✓"
FAIL = "✗"
OPTIONAL = "○"
WARN = "⚠"

STATUS_KIND: dict[str, str] = {
    OK: "pass",
    FAIL: "fail",
    OPTIONAL: "skip",
    WARN: "warn",
}


# ---------------------------------------------------------------------------
# CheckResult
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CheckResult:
    """Outcome of a single diagnostic check.

    Construct via one of the alternate constructors -- :meth:`ok`,
    :meth:`fail`, :meth:`warn` for the three common shapes, or :meth:`of` for
    the general (explicit symbol, optional ``required``) case. Every
    production call site goes through a classmethod so the four wire fields
    (``symbol``, ``passed``, ``status_kind``, ``required``) stay consistent
    per verdict without every caller re-deriving them.

    The past-tense field name ``passed`` predates the classmethods; the
    verb-form constructor names (``ok`` / ``fail`` / ``warn``) sidestep the
    attribute-vs-method collision that ``passed`` / ``failed`` / ``warned``
    would create on the same class.
    """

    name: str
    passed: bool
    message: str
    detail: str = ""
    required: bool = True
    symbol: str = OK
    status_kind: str = "pass"

    @classmethod
    def ok(cls, message: str) -> Self:
        """Return a passing (``✓``) check result."""
        return cls(
            name=message, passed=True, message=message, symbol=OK, status_kind="pass"
        )

    @classmethod
    def fail(cls, message: str) -> Self:
        """Return a failing (``✗``) check result."""
        return cls(
            name=message,
            passed=False,
            message=message,
            symbol=FAIL,
            status_kind="fail",
        )

    @classmethod
    def warn(cls, message: str) -> Self:
        """Return a warning (``⚠``) check result."""
        return cls(
            name=message,
            passed=False,
            message=message,
            symbol=WARN,
            status_kind="warn",
        )

    @classmethod
    def of(cls, symbol: str, message: str, *, required: bool = True) -> Self:
        """Return a check result with an explicit symbol.

        Covers the ``○``-optional rows and the pass-but-not-required cases
        (``uvx: present`` with ``required=False``); the three verb-named
        constructors above handle every hard-verdict row without threading a
        symbol.
        """
        return cls(
            name=message,
            passed=symbol == OK,
            message=message,
            symbol=symbol,
            status_kind=STATUS_KIND.get(symbol, "fail"),
            required=required,
        )


# ---------------------------------------------------------------------------
# CheckResults -- the collection knows how to render itself
# ---------------------------------------------------------------------------


@final
class CheckResults:
    """A doctor run's collected check results and their render.

    The collection owns the render into ``(JSON payload, display text)``: a
    free ``format_results`` function taking a list of ``CheckResult`` was the
    exact PY-OO-7 smell (a helper that takes the class is a method of the
    class) that motivated pulling this out. ``__len__`` and iteration are
    exposed so callers that want the raw entries still can.
    """

    __slots__ = ("_entries",)
    _entries: tuple[CheckResult, ...]

    def __new__(cls, entries: Iterable[CheckResult]) -> Self:
        self = super().__new__(cls)
        self._entries = tuple(entries)
        return self

    def __iter__(self) -> Iterator[CheckResult]:
        return iter(self._entries)

    def __len__(self) -> int:
        return len(self._entries)

    def format(self) -> tuple[dict[str, object], str]:
        """Return ``(JSON payload, display text)`` for a doctor run.

        The payload keys match what ``vox doctor --json`` has always emitted;
        the display text is the equal-signs banner plus one ``<symbol> <msg>``
        line per entry plus a summary line.
        """
        passed = 0
        failed = 0
        warned = 0
        lines: list[str] = []
        checks: list[dict[str, object]] = []

        for r in self._entries:
            lines.append(f"{r.symbol} {r.message}")
            checks.append(
                {
                    "status": r.symbol,
                    "status_kind": r.status_kind,
                    "message": r.message,
                    "required": r.required,
                    "passed": r.passed,
                }
            )
            if r.passed:
                passed += 1
            elif r.symbol == FAIL and r.required:
                failed += 1
            elif r.symbol == WARN:
                warned += 1

        summary = f"{passed} passed, {failed} failed"
        if warned > 0:
            summary += f", {warned} warning" + ("s" if warned > 1 else "")
        text_parts = ["=" * 40, *lines, "=" * 40, summary]

        payload: dict[str, object] = {
            "passed": passed,
            "failed": failed,
            "warned": warned,
            "checks": checks,
        }
        return payload, "\n".join(text_parts)
