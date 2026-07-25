"""The shared base for the mutating program wire handlers (template method).

Every mutating ``program_*`` handler is the same thin adapter: parse the wire
message, hand the work to the :class:`ProgramService` (which POSTs one serialized
:class:`ControlSignal` -- the handler never touches the Program), and reply. Only
the parse-and-dispatch step differs per command, so it is the one abstract hook;
the request-id plumbing, the applied ack, and the boundary error reply live here
once (DRY, replacing the copy-pasted try/except of the old music handlers).

The boundary catches every *expected* domain failure and replies through
:class:`WireReply`, which splits it by fault-vs-error: a ``ValueError`` (a bad
request or a lost-race guard) is a rejected client request audited as ``error``,
while a ``LookupError`` (``store.open`` on a deleted album dir) or an ``OSError``
(``store.create``'s ``mkdir(exist_ok=False)`` mint-race guard, disk-full,
permissions) is a server-side operational failure audited as ``fault``. Letting
any escape would tear the socket down, leaving the client a generic "connection
closed" instead of the cause; replying through :class:`WireReply` also id-stamps
every frame and no-ops on a gone peer. Handlers hold no session and no owner --
``voxd`` is machine-universal.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, ClassVar, Self

from punt_vox.voxd.wire_reply import WireReply

if TYPE_CHECKING:
    from starlette.websockets import WebSocket

    from punt_vox.voxd.programs.service import ProgramService

__all__ = ["ProgramCommandHandler"]


class ProgramCommandHandler(ABC):
    """A mutating ``program_*`` handler: parse, dispatch to the service, ack."""

    __slots__ = ("_service",)
    _service: ProgramService
    _WIRE_TYPE: ClassVar[str]
    """The reply ``type`` (and inbound message type) this handler answers."""

    def __new__(cls, service: ProgramService) -> Self:
        self = super().__new__(cls)
        self._service = service
        return self

    async def __call__(self, msg: dict[str, object], websocket: WebSocket) -> None:
        """Parse and dispatch, replying with an ack, a rejection, or a fault.

        The boundary classifies the domain failure through
        :meth:`WireReply.reject_or_fault`: a ``ValueError`` is a rejected client
        request (``error``), a ``LookupError``/``OSError`` a server-side
        operational failure (``fault``). The ack and both failure frames are
        id-stamped and gone-peer-safe, matching the store handlers.
        """
        reply = WireReply(websocket, str(msg.get("id", "")))
        try:
            self._run(msg)
        except (ValueError, LookupError, OSError) as exc:
            await reply.reject_or_fault(exc)
            return
        await reply.send({"type": self._WIRE_TYPE})

    @abstractmethod
    def _run(self, msg: dict[str, object], /) -> None:
        """Parse ``msg`` and issue the one serialized command to the service."""

    @staticmethod
    def _opt_str(msg: dict[str, object], key: str) -> str | None:
        """Return a present string field, or ``None`` when absent or null.

        Absence (missing key or ``null``) is the documented "not supplied"
        contract. A present-but-wrong-typed value (e.g. a JSON number) is a
        malformed request, not an absent field: it raises so the boundary answers
        with a wire error rather than silently ignoring it and falling through to
        a different resolution path (a numeric ``album_id`` must not become a
        catch-all tag query).
        """
        value = msg.get(key)
        if value is None or isinstance(value, str):
            return value
        msg_text = f"field {key!r} must be a string, got {type(value).__name__}"
        raise ValueError(msg_text)
