"""Cross-process control signals for a running ``vox call start`` loop.

``vox call start`` runs the call's loop in the foreground of the process
that started it (FR-2's explicit-hangup and bounded-timeout paths handle
ending it from *within* that process). ``vox call stop`` and ``vox call
transfer`` run as separate, short-lived invocations -- this file is how
they reach the running loop: one request written, read and cleared by the
loop the next time it checks between turns.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Self, final

__all__ = ["CallControl", "ControlRequest"]

type ControlKind = Literal["stop", "transfer"]


@final
@dataclass(frozen=True, slots=True)
class ControlRequest:
    """One pending cross-process request: hang up, or re-attach to a session."""

    kind: ControlKind
    target_session_id: str | None = None


@final
class CallControl:
    """A one-slot mailbox for :class:`ControlRequest`, backed by a JSON file."""

    __slots__ = ("_path",)
    _path: Path

    def __new__(cls, path: Path) -> Self:
        self = super().__new__(cls)
        self._path = path
        return self

    def request_stop(self) -> None:
        """Ask the running call to hang up."""
        self._write(ControlRequest(kind="stop"))

    def request_transfer(self, target_session_id: str | None) -> None:
        """Ask the running call to re-attach to *target_session_id*, or re-discover."""
        self._write(
            ControlRequest(kind="transfer", target_session_id=target_session_id)
        )

    def consume(self) -> ControlRequest | None:
        """Return and clear the pending request, or ``None`` if there is none."""
        try:
            raw = self._path.read_text()
        except FileNotFoundError:
            return None
        self._path.unlink(missing_ok=True)
        payload = json.loads(raw)
        return ControlRequest(
            kind=payload["kind"], target_session_id=payload.get("target_session_id")
        )

    def _write(self, request: ControlRequest) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"kind": request.kind, "target_session_id": request.target_session_id}
        self._path.write_text(json.dumps(payload))
