"""Result types and display helpers for the vox doctor sub-checks.

Splitting these out of :mod:`punt_vox.doctor` lets the schedule/orchestration
module (``DoctorCheck``) stay focused on the *what runs*, and lets each
sub-check module (``doctor_mpv``, potentially more) construct results without
reaching back into the schedule module -- a smaller, cleaner import graph.

Everything here is stateless. Callers construct a :class:`CheckResult`
directly, or use the shortcut helpers :func:`pass_`, :func:`fail_`,
:func:`warn_`, :func:`result` for the common shapes.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

__all__ = [
    "FAIL",
    "OK",
    "OPTIONAL",
    "STATUS_KIND",
    "WARN",
    "CheckResult",
    "claude_desktop_config_path",
    "fail_",
    "format_results",
    "pass_",
    "result",
    "warn_",
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
# Result type
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CheckResult:
    """Outcome of a single diagnostic check."""

    name: str
    passed: bool
    message: str
    detail: str = ""
    required: bool = True
    symbol: str = OK
    status_kind: str = "pass"


# ---------------------------------------------------------------------------
# Result constructors
# ---------------------------------------------------------------------------


def pass_(message: str) -> CheckResult:
    """Return a passing check result."""
    return CheckResult(
        name=message, passed=True, message=message, symbol=OK, status_kind="pass"
    )


def fail_(message: str) -> CheckResult:
    """Return a failing check result."""
    return CheckResult(
        name=message, passed=False, message=message, symbol=FAIL, status_kind="fail"
    )


def warn_(message: str) -> CheckResult:
    """Return a warning check result."""
    return CheckResult(
        name=message, passed=False, message=message, symbol=WARN, status_kind="warn"
    )


def result(symbol: str, message: str, *, required: bool = True) -> CheckResult:
    """Return a check result with an explicit symbol."""
    return CheckResult(
        name=message,
        passed=symbol == OK,
        message=message,
        symbol=symbol,
        status_kind=STATUS_KIND.get(symbol, "fail"),
        required=required,
    )


# ---------------------------------------------------------------------------
# Display / config location
# ---------------------------------------------------------------------------


def claude_desktop_config_path() -> Path:
    """Return the Claude Desktop config file path.

    Kept out of the doctor schedule module so both the check and the CLI
    (which registers vox as a Claude Desktop MCP server) can reach one
    canonical location without importing the whole check runner.
    """
    return (
        Path.home()
        / "Library"
        / "Application Support"
        / "Claude"
        / "claude_desktop_config.json"
    )


def format_results(results: list[CheckResult]) -> tuple[dict[str, object], str]:
    """Format check results into a JSON payload and its display text.

    Returns a ``(payload, text)`` tuple matching the existing ``doctor``
    command output format.
    """
    passed = 0
    failed = 0
    warned = 0
    lines: list[str] = []
    checks: list[dict[str, object]] = []

    for r in results:
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
