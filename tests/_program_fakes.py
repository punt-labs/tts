"""An in-memory ``ProgramGateway`` fake shared by the server and CLI surface tests.

The surfaces are thin adapters over :class:`~punt_vox.program_gateway.ProgramGateway`,
so their tests inject this fake instead of a live daemon: it records every call,
returns a caller-controlled :class:`ProgramStatus`, and lets a test flip the
applied/rejected result or mutate the status between calls (to prove the surface
re-reads authoritatively rather than caching a stale shadow). It is a structural
stand-in (no inheritance), matching the Protocol by shape.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Self, final

from punt_vox.types_programs.control import (
    CommandOutcome,
    ProgramSummary,
    SelectionRequest,
    StartRequest,
)
from punt_vox.types_programs.status import ProgramStatus


@final
@dataclass(slots=True)
class GatewayCall:
    """One recorded call against the fake: the verb and its salient argument."""

    verb: str
    request: StartRequest | None = None
    selection: SelectionRequest | None = None


@final
class FakeProgramGateway:
    """A stateful, filesystem-free ``ProgramGateway`` for surface tests."""

    __slots__ = ("_applied", "_catalog", "_reason", "_status", "calls")
    _status: ProgramStatus
    _catalog: tuple[ProgramSummary, ...]
    _applied: bool
    _reason: str | None
    calls: list[GatewayCall]

    def __new__(
        cls,
        status: ProgramStatus | None = None,
        catalog: tuple[ProgramSummary, ...] = (),
        *,
        applied: bool = True,
        reason: str | None = None,
    ) -> Self:
        self = super().__new__(cls)
        self._status = status if status is not None else ProgramStatus.idle()
        self._catalog = catalog
        self._applied = applied
        self._reason = reason
        self.calls = []
        return self

    def set_status(self, status: ProgramStatus) -> None:
        """Replace the status a subsequent :meth:`status` call returns."""
        self._status = status

    def _outcome(self, message: str) -> CommandOutcome:
        """Model the daemon's F7 reply shape.

        An applied command reports no reason of its own -- the surface authors
        the success line (a personalised ``generating`` message, ``Music off.``,
        etc.); a rejected command carries the reason it was refused. Mirroring
        that here keeps the surface tests honest about which line comes from
        where (findings F4/F7). A test may pin the daemon's ``reason`` to assert
        it reaches the caller instead of a canned line.
        """
        if self._applied:
            return CommandOutcome.ok("")
        return CommandOutcome.rejected(self._reason or message)

    def status(self) -> ProgramStatus:
        self.calls.append(GatewayCall("status"))
        return self._status

    def start(self, request: StartRequest) -> CommandOutcome:
        self.calls.append(GatewayCall("start", request=request))
        return self._outcome("on")

    def stop(self) -> CommandOutcome:
        self.calls.append(GatewayCall("stop"))
        return self._outcome("off")

    def advance(self) -> CommandOutcome:
        self.calls.append(GatewayCall("advance"))
        return self._outcome("advanced")

    def prev(self) -> CommandOutcome:
        self.calls.append(GatewayCall("prev"))
        return self._outcome("stepped back")

    def pause(self) -> CommandOutcome:
        self.calls.append(GatewayCall("pause"))
        return self._outcome("paused")

    def resume(self) -> CommandOutcome:
        self.calls.append(GatewayCall("resume"))
        return self._outcome("resumed")

    def select(self, request: SelectionRequest) -> CommandOutcome:
        self.calls.append(GatewayCall("select", selection=request))
        return self._outcome("playing selection")

    def catalog(self) -> tuple[ProgramSummary, ...]:
        self.calls.append(GatewayCall("catalog"))
        return self._catalog

    def verbs(self) -> list[str]:
        """Return the recorded call verbs in order (a test-readability helper)."""
        return [call.verb for call in self.calls]
