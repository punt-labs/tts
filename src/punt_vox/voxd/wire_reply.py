"""One request's reply channel: id-stamped sends and audit-logged rejections."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Self

from punt_vox.log_sanitize import SANITIZER
from punt_vox.voxd._parse import safe_send

if TYPE_CHECKING:
    from starlette.websockets import WebSocket

__all__ = ["WireReply"]

logger = logging.getLogger(__name__)

_LOG_FIELD_LIMIT = 120


class WireReply:
    """A client reply channel bound to one request's socket and id.

    Every store handler -- record, play, fetch -- replies through this one
    object. :meth:`send` stamps the request id and survives a client that has
    already disconnected. Two audit-logged failure paths share one wire frame
    but classify the cause differently: :meth:`error` records a rejected client
    request at WARNING (a hostile record name, or a play/fetch ref that escapes
    the store or names no recording), while :meth:`fault` records a server-side
    operational failure at ERROR (synthesis or a store write broke). Both are
    greppable in vox.log instead of silent, while a clean disconnect stays a
    quiet end-of-request.
    """

    __slots__ = ("_request_id", "_websocket")

    _websocket: WebSocket
    _request_id: str

    def __new__(cls, websocket: WebSocket, request_id: str) -> Self:
        self = super().__new__(cls)
        self._websocket = websocket
        self._request_id = request_id
        return self

    @property
    def request_id(self) -> str:
        """Return the wire request id this channel stamps onto every frame."""
        return self._request_id

    async def send(self, payload: dict[str, object]) -> bool:
        """Send *payload* stamped with this request's id; False if the peer had gone.

        The id is stamped *last* so a payload that happens to carry an ``id`` key
        can never override the wire request id -- the stamp always wins.
        """
        return await safe_send(self._websocket, {**payload, "id": self._request_id})

    async def error(self, message: str) -> bool:
        """Audit a rejected CLIENT request at WARNING (sanitized) and send its frame.

        A rejection means the daemon refused a malformed or hostile request --
        a hostile record name, a play/fetch ref that escapes the store or names
        no recording. For a server-side operational failure (synthesis or a
        store write broke) use :meth:`fault` instead, so the audit trail does
        not blame the client for a daemon-side error.

        *message* may embed an attacker-controlled name or ref, so it is
        sanitized before it reaches the log -- newlines and control characters
        are escaped and the length is capped -- which closes a log-injection
        vector into vox.log. The wire frame carries *message* verbatim. The
        rejection is logged even when the peer has gone, so the audit trail does
        not depend on the client still being there to receive the frame.
        """
        logger.warning(
            "rejected op id=%r: %s", self._request_id, self._sanitize(message)
        )
        return await self.send({"type": "error", "message": message})

    async def fault(self, message: str) -> bool:
        """Audit a server-side OPERATIONAL failure at ERROR and send its frame.

        A fault is a daemon-side error -- synthesis failed, a store write failed
        -- as opposed to :meth:`error`, which records a rejected client request.
        The client-facing frame is byte-for-byte identical to :meth:`error`'s;
        only the audit classification differs, so a debugger reading vox.log
        sees a server fault logged as "operation failed", never mislabeled
        "rejected op". The message is sanitized on the same log-injection path,
        and logged even when the peer has gone.
        """
        logger.error(
            "operation failed id=%r: %s", self._request_id, self._sanitize(message)
        )
        return await self.send({"type": "error", "message": message})

    async def reject_or_fault(self, exc: ValueError | LookupError | OSError) -> bool:
        """Route a domain failure to :meth:`error` or :meth:`fault` by its type.

        The one place the fault-vs-error taxonomy is decided for handlers whose
        boundary catches the same trio: a ``ValueError`` is a rejected client
        request (a bad field, a lost-race guard, a backing refusal) and audits as
        ``error``; a ``LookupError`` (a resolve that found nothing) or an
        ``OSError`` (a filesystem fault) is a server-side operational failure and
        audits as ``fault``. Centralising it here keeps every such handler
        classifying identically instead of drifting apart one boundary at a time.

        A handler whose not-found is a *client* rejection rather than an
        operational fault -- ``rec_remove``, where a ``FileNotFoundError`` means
        "names no recording", matching ``play``/``fetch`` -- must classify
        explicitly instead of using this helper.
        """
        if isinstance(exc, ValueError):
            return await self.error(str(exc))
        return await self.fault(str(exc))

    @staticmethod
    def _sanitize(message: str) -> str:
        """Return *message* escaped by the shared log sanitizer and length-capped.

        The shared :data:`SANITIZER` neutralizes the injection surface (every
        C0/C1/DEL control and Unicode line separator); the cap is applied
        *after* escaping so it bounds the actual logged field, not the pre-escape
        input a padded-out probe could inflate. The ellipsis is counted inside
        the limit, so the returned string is never longer than the limit.
        """
        ellipsis = "..."
        escaped = SANITIZER.escape(message)
        if len(escaped) > _LOG_FIELD_LIMIT:
            return f"{escaped[: _LOG_FIELD_LIMIT - len(ellipsis)]}{ellipsis}"
        return escaped
