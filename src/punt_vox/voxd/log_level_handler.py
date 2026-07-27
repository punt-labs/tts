"""Wire handler for ``set_log_level`` -- set the daemon's level, clamped to INFO.

``vox log`` routes here so an operator can raise the *running* daemon to
``debug`` (and back to ``info``) live, including a remote daemon over
``VOXD_HOST`` -- the local-config write it replaced could never reach a remote
level. The op clamps server-side to the INFO audit floor (:class:`AuditFloorLevel`),
so a token-holding client -- or a crafted frame asking for a stricter level --
cannot drop the daemon below the trail that records its own rejected requests.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Self, final

from punt_vox.voxd._parse import parse_present_str
from punt_vox.voxd.audit_log_level import AuditFloorLevel
from punt_vox.voxd.wire_reply import WireReply

if TYPE_CHECKING:
    from collections.abc import Callable

    from starlette.websockets import WebSocket

__all__ = ["LogLevelHandler"]

logger = logging.getLogger(__name__)


@final
class LogLevelHandler:
    """Handle ``set_log_level``: clamp the requested level to INFO, apply, reply.

    Holds the level-applier as an injected collaborator (the shared
    ``apply_log_level``) so the handler is testable without touching the process
    root logger, and replies through :class:`WireReply` -- an unknown level name
    is a rejected client request (WARNING ``rejected op``), never a torn socket.
    """

    __slots__ = ("_apply",)

    _apply: Callable[[int], None]

    def __new__(cls, apply: Callable[[int], None]) -> Self:
        self = super().__new__(cls)
        self._apply = apply
        return self

    async def __call__(self, msg: dict[str, object], websocket: WebSocket) -> None:
        """Clamp the requested level to the INFO floor, apply it live, and reply."""
        reply = WireReply(websocket, str(msg.get("id", "")))
        try:
            level = AuditFloorLevel.from_name(parse_present_str(msg, "level") or "")
        except ValueError as exc:
            await reply.error(str(exc))
            return
        self._apply(level.numeric)
        logger.info("log level set to %s (clamped to the INFO audit floor)", level.name)
        await reply.send({"type": "log_level", "level": level.name})
