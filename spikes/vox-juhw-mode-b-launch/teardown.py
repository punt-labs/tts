# /// script
# requires-python = ">=3.13"
# dependencies = []
# ///
"""Idempotent teardown for everything the harness creates on this host.

Kills every tmux session carrying the harness prefix and removes the
scratch root (projects + isolated config dirs, credentials copies
included). Every claim in the log is verified after the fact: a session
is reported killed only when tmux no longer knows it, and the scratch
root is reported removed only when it is gone from disk. A pass that
leaves anything behind says so and exits nonzero -- exit 0 with
credentials still on disk would be a false all-clear. Running twice is
the idempotence evidence: the second pass finds nothing and is clean.

Run:  uv run teardown.py [--scratch-root .tmp]
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Self, final

from launcher import SESSION_PREFIX


@final
@dataclass(frozen=True, slots=True)
class TeardownOutcome:
    """What one teardown pass did, and whether everything is really gone."""

    log: tuple[str, ...]
    clean: bool


@final
class Teardown:
    """Removes harness tmux sessions and the scratch root, then verifies."""

    __slots__ = ("_scratch_root",)

    _scratch_root: Path

    def __new__(cls, scratch_root: Path) -> Self:
        self = super().__new__(cls)
        self._scratch_root = scratch_root
        return self

    def run(self) -> TeardownOutcome:
        """Tear everything down; report verified actions and leftovers."""
        session_lines, sessions_clean = self._kill_sessions()
        scratch_line, scratch_clean = self._remove_scratch()
        return TeardownOutcome(
            log=(*session_lines, scratch_line),
            clean=sessions_clean and scratch_clean,
        )

    def _kill_sessions(self) -> tuple[list[str], bool]:
        listing = subprocess.run(
            ["tmux", "list-sessions", "-F", "#{session_name}"],
            check=False,
            capture_output=True,
            text=True,
        )
        if listing.returncode != 0:
            # No tmux server running -- nothing to kill.
            return [], True
        mine = [
            name
            for name in listing.stdout.splitlines()
            if name.startswith(SESSION_PREFIX)
        ]
        lines: list[str] = []
        clean = True
        for name in mine:
            subprocess.run(
                ["tmux", "kill-session", "-t", f"={name}"],
                check=False,
                capture_output=True,
            )
            if self._session_gone(name):
                lines.append(f"killed tmux session {name}")
            else:
                lines.append(f"FAILED to kill tmux session {name}")
                clean = False
        return lines, clean

    def _session_gone(self, name: str) -> bool:
        probe = subprocess.run(
            ["tmux", "has-session", "-t", f"={name}"],
            check=False,
            capture_output=True,
        )
        return probe.returncode != 0

    def _remove_scratch(self) -> tuple[str, bool]:
        if not self._scratch_root.exists():
            return f"scratch root already absent: {self._scratch_root}", True
        shutil.rmtree(self._scratch_root, ignore_errors=True)
        if self._scratch_root.exists():
            message = (
                f"FAILED to remove scratch root (leftovers on disk, may "
                f"include a credentials copy): {self._scratch_root}"
            )
            return message, False
        return f"removed scratch root: {self._scratch_root}", True


def main() -> None:
    """CLI entry: tear down, report, exit nonzero on leftovers."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--scratch-root",
        type=Path,
        default=Path(__file__).parent / ".tmp",
    )
    args = parser.parse_args()
    outcome = Teardown(args.scratch_root).run()
    for line in outcome.log:
        print(line)
    raise SystemExit(0 if outcome.clean else 1)


if __name__ == "__main__":
    main()
