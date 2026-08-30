"""Sender-side stamp: inject relay_seq + relay_start_ns into a hook payload.

Runs on the FORK's side of the relay, between Claude Code's hook stdin and
`mcp-proxy`: reads the hook payload JSON from stdin, adds two fields, and
writes the result to stdout for mcp-proxy to deliver.

Why it exists: DES-070 has voxd stamp a per-session sequence at RECEIPT,
and its open risk is whether sequence-gap detection catches drops. A
receiver-assigned sequence cannot: events lost while the store is down were
never received, so receiver sequences stay contiguous by construction. Loss
is only detectable against a counter that advances at the SENDER. This
wrapper is that counter -- a per-session, flock-guarded file the launcher
owns -- plus the wall-clock start stamp the latency measurement needs
(hook-command start vs store receipt, same host, same clock).

Deliberately boring Python: executed by the host's `python3` (not uv, not
the repo venv) inside the fork's hook command, so it sticks to the stdlib
and syntax any modern system interpreter accepts.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import re
import sys
import time
from pathlib import Path
from typing import Self, final


@final
class SessionCounter:
    """A per-session monotonic counter persisted as a flock-guarded file.

    Hook commands are separate short-lived processes, possibly concurrent
    (a Stop can fire while a PostToolUse relay is still running), so the
    counter must live outside the process and increments must be atomic.
    """

    __slots__ = ("_path",)

    _path: Path

    def __new__(cls, counter_dir: Path, session_id: str) -> Self:
        self = super().__new__(cls)
        safe = re.sub(r"[^A-Za-z0-9_-]", "_", session_id) or "unattributed"
        self._path = counter_dir / f"{safe}.seq"
        return self

    def next(self) -> int:
        """Atomically increment and return the counter."""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._path.open("a+", encoding="utf-8") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            handle.seek(0)
            raw = handle.read().strip()
            value = int(raw) + 1 if raw else 1
            handle.seek(0)
            handle.truncate()
            handle.write(str(value))
            handle.flush()
            return value


def main() -> None:
    """Stamp stdin's payload and write it to stdout."""
    start_ns = time.time_ns()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--counter-dir", type=Path, required=True)
    parser.add_argument(
        "--start-ns",
        type=int,
        default=None,
        help="hook-command start from the shell wrapper (date +%%s%%N); "
        "falls back to this interpreter's own start when absent",
    )
    args = parser.parse_args()
    payload = json.load(sys.stdin)
    if not isinstance(payload, dict):
        # A non-object payload cannot carry stamps; forward it untouched
        # rather than break the relay -- the store will record it as-is.
        json.dump(payload, sys.stdout)
        return
    session_raw = payload.get("session_id")
    session_id = session_raw if isinstance(session_raw, str) and session_raw else ""
    payload["relay_seq"] = SessionCounter(args.counter_dir, session_id).next()
    payload["relay_start_ns"] = args.start_ns if args.start_ns else start_ns
    json.dump(payload, sys.stdout)


if __name__ == "__main__":
    main()
