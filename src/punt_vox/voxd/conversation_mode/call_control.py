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
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Self, final

from punt_vox.atomic_file import AtomicFile

logger = logging.getLogger(__name__)

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
        """Return and clear the pending request, or ``None`` if there is none.

        Claim-and-remove is one atomic operation: ``os.replace`` renames the
        mailbox file to a ``.consuming`` sibling before anything reads it.
        A read-then-unlink sequence has a gap between the two steps in
        which a fresh ``/call stop`` written into that gap would be deleted
        unread -- the hang-up would silently do nothing. Renaming first
        means there is no such gap: any request written after the rename
        starts a new mailbox file untouched by this call.

        A corrupt or unparseable request (a racing partial write, a
        hand-edited file) is still cleared from the mailbox -- logged and
        reported as "no request" rather than raised, since a caller
        mid-call must not have its boundary handler end the whole call
        over a malformed control file.
        """
        consuming_path = self._path.with_name(self._path.name + ".consuming")
        try:
            self._path.replace(consuming_path)
        except FileNotFoundError:
            return None
        try:
            try:
                # UnicodeDecodeError (from AtomicFile.read's utf-8 decode, on
                # a hand-edited or partially-overwritten file with invalid
                # bytes) is a ValueError subclass, same family as
                # json.JSONDecodeError -- both mean "this mailbox entry is
                # not usable", not "the call ended". Read and parse share one
                # try so a decode failure hits the same discard path a parse
                # failure already does, instead of propagating to the
                # call-ending boundary handler this method exists to spare.
                raw = AtomicFile(consuming_path).read()
                if not raw:
                    return None
                payload = json.loads(raw)
                kind = payload["kind"]
                # Validated, not trusted: a malformed mailbox file (a
                # racing partial write, a hand-edited entry) with an
                # unrecognized "kind" must land on the same discard-and-log
                # path as every other unusable entry -- constructing a
                # ControlRequest with an invalid kind would fall through
                # both branches of call.py's _apply_control silently,
                # dropping a "/call stop" with no log, no error, nothing.
                if kind not in ("stop", "transfer"):
                    msg = f"unrecognized control kind {kind!r}"
                    raise ValueError(msg)
                target_session_id = payload.get("target_session_id")
                # Same discipline as "kind" above: a wrong-typed value here
                # (an int, say, from a hand-edited or partially-overwritten
                # file) would otherwise flow straight into
                # ClaudeSessionAttach's constructor and crash with an
                # uncaught TypeError from create_subprocess_exec, ending the
                # whole call over a malformed transfer request -- exactly
                # the outcome the "kind" validation above already exists to
                # prevent.
                if target_session_id is not None and not isinstance(
                    target_session_id, str
                ):
                    msg = f"wrong-typed target_session_id {target_session_id!r}"
                    raise TypeError(msg)
                return ControlRequest(kind=kind, target_session_id=target_session_id)
            except (ValueError, KeyError, TypeError) as exc:
                logger.warning(
                    "call.control request is unreadable, discarding: %s", exc
                )
                return None
        finally:
            consuming_path.unlink(missing_ok=True)

    def _write(self, request: ControlRequest) -> None:
        # AtomicFile.replace, not a bare write_text: consume() polls this
        # file between turns, and a truncate-then-write leaves a window in
        # which it could read a partial, unparseable file.
        payload = {"kind": request.kind, "target_session_id": request.target_session_id}
        AtomicFile(self._path).replace(json.dumps(payload))
