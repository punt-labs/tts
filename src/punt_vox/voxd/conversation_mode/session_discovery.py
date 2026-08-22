"""Discover the human's active Claude Code session for a call to attach to.

Per ``docs/conversation-mode-session-attach-adr.md``: ``claude agents --json
--cwd <path>`` lists active sessions (interactive and background), filterable
by working directory, as a JSON array. FR-4 requires the call to use the
user's *active* session, not a fresh one -- but the ADR names silent
auto-pick across multiple candidates as unsafe, so this class always returns
every candidate for the caller to choose from, and the caller (the ``vox
call start`` orchestrator) is the one required to demand an explicit
confirmation whenever more than one exists.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Self, cast, final

__all__ = ["SessionCandidate", "SessionDiscovery", "SessionDiscoveryError"]


class SessionDiscoveryError(RuntimeError):
    """Raised when ``claude agents --json`` cannot be run or returns unusable output.

    A boundary error (PY-EH-1): the ``claude`` CLI is an external process
    whose exact output shape is not contractually documented beyond the ADR's
    own investigation, so a malformed or unreachable result fails loud here
    rather than the caller silently attaching to the wrong session.
    """


@final
@dataclass(frozen=True, slots=True)
class SessionCandidate:
    """One active Claude Code session, as reported by ``claude agents --json``."""

    session_id: str
    cwd: str


@final
class SessionDiscovery:
    """Runs ``claude agents --json --cwd <path>`` and parses its candidates."""

    __slots__ = ("_claude_bin",)
    _claude_bin: str

    def __new__(cls, *, claude_bin: str = "claude") -> Self:
        self = super().__new__(cls)
        self._claude_bin = claude_bin
        return self

    async def discover(self, cwd: Path) -> tuple[SessionCandidate, ...]:
        """Return every active session Claude Code reports for *cwd*.

        Never picks one for the caller -- the ADR found silent auto-pick
        across multiple candidates unsafe, so returning the full set and
        requiring the caller to choose is the contract, not an oversight.
        """
        process = await asyncio.create_subprocess_exec(
            self._claude_bin,
            "agents",
            "--json",
            "--cwd",
            str(cwd),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await process.communicate()
        if process.returncode != 0:
            msg = (
                f"{self._claude_bin} agents --json --cwd {cwd} exited "
                f"{process.returncode}: {stderr.decode(errors='replace').strip()}"
            )
            raise SessionDiscoveryError(msg)
        return self._parse(stdout.decode())

    @staticmethod
    def _parse(raw: str) -> tuple[SessionCandidate, ...]:
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            msg = f"claude agents --json returned invalid JSON: {exc}"
            raise SessionDiscoveryError(msg) from exc
        if not isinstance(payload, list):
            kind = type(payload).__name__
            msg = f"claude agents --json returned a {kind}, not a list"
            raise SessionDiscoveryError(msg)
        entries = cast("list[object]", payload)

        candidates: list[SessionCandidate] = []
        for entry in entries:
            if not isinstance(entry, dict):
                kind = type(entry).__name__
                msg = f"claude agents --json entry is a {kind}, not an object"
                raise SessionDiscoveryError(msg)
            fields = cast("dict[str, object]", entry)
            session_id = SessionDiscovery._first_str(
                fields, ("id", "sessionId", "session_id")
            )
            cwd_value = SessionDiscovery._first_str(
                fields, ("cwd", "workingDirectory", "working_directory")
            )
            if session_id is None or cwd_value is None:
                msg = (
                    "claude agents --json entry is missing an id or cwd field: "
                    f"{entry!r}"
                )
                raise SessionDiscoveryError(msg)
            candidates.append(SessionCandidate(session_id=session_id, cwd=cwd_value))
        return tuple(candidates)

    @staticmethod
    def _first_str(entry: dict[str, object], keys: tuple[str, ...]) -> str | None:
        """Return the first of *keys* present in *entry* as a string, or ``None``.

        ``claude agents --json``'s exact field naming is not contractually
        fixed (the ADR's own investigation notes this), so this tries the
        plausible aliases rather than committing to one and failing on a
        rename upstream.
        """
        for key in keys:
            value = entry.get(key)
            if isinstance(value, str):
                return value
        return None
