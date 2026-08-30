# /// script
# requires-python = ">=3.13"
# dependencies = []
# ///
"""Idempotent teardown for everything the harness creates on this host.

Kills every tmux session carrying the harness prefix, removes the scratch
root (projects + isolated config dirs, credentials copies included), and
exits 0 whether or not anything existed -- running it twice is the
idempotence evidence the mission asks for.

Run:  uv run teardown.py [--scratch-root .tmp]
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
from pathlib import Path
from typing import Self, final

from launcher import SESSION_PREFIX


@final
class Teardown:
    """Removes harness tmux sessions and the scratch root."""

    __slots__ = ("_scratch_root",)

    _scratch_root: Path

    def __new__(cls, scratch_root: Path) -> Self:
        self = super().__new__(cls)
        self._scratch_root = scratch_root
        return self

    def run(self) -> list[str]:
        """Tear everything down; return a log of the actions taken."""
        log = [f"killed tmux session {name}" for name in self._kill_sessions()]
        log.append(self._remove_scratch())
        return log

    def _kill_sessions(self) -> list[str]:
        listing = subprocess.run(
            ["tmux", "list-sessions", "-F", "#{session_name}"],
            check=False,
            capture_output=True,
            text=True,
        )
        if listing.returncode != 0:
            # No tmux server running -- nothing to kill.
            return []
        mine = [
            name
            for name in listing.stdout.splitlines()
            if name.startswith(SESSION_PREFIX)
        ]
        for name in mine:
            subprocess.run(
                ["tmux", "kill-session", "-t", f"={name}"],
                check=False,
                capture_output=True,
            )
        return mine

    def _remove_scratch(self) -> str:
        if not self._scratch_root.exists():
            return f"scratch root already absent: {self._scratch_root}"
        shutil.rmtree(self._scratch_root, ignore_errors=True)
        return f"removed scratch root: {self._scratch_root}"


def main() -> None:
    """CLI entry: tear down and report."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--scratch-root",
        type=Path,
        default=Path(__file__).parent / ".tmp",
    )
    args = parser.parse_args()
    for line in Teardown(args.scratch_root).run():
        print(line)


if __name__ == "__main__":
    main()
