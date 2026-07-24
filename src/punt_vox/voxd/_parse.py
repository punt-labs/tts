"""Low-level wire-frame helpers for voxd handlers.

Inbound: extract optional typed values from a message dict. Outbound: send a
reply frame in a way that survives a client that has already disconnected --
grouped here as the one place low-level WebSocket-frame marshalling lives, so
every handler shares one parse path and one disconnect-safe send path.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from starlette.websockets import WebSocketDisconnect

if TYPE_CHECKING:
    from starlette.websockets import WebSocket

__all__ = [
    "parse_optional_float",
    "parse_optional_int",
    "parse_optional_str",
    "safe_send",
]

logger = logging.getLogger(__name__)


def parse_optional_float(msg: dict[str, object], key: str) -> float | None:
    """Extract an optional float field from a message dict."""
    raw = msg.get(key)
    if raw is None:
        return None
    return float(str(raw))


def parse_optional_int(msg: dict[str, object], key: str) -> int | None:
    """Extract an optional int field from a message dict."""
    raw = msg.get(key)
    if raw is None:
        return None
    return int(str(raw))


def parse_optional_str(msg: dict[str, object], key: str) -> str | None:
    """Extract an optional string field; reject a non-string wire value.

    An absent field or JSON null is absence (None). A present non-string --
    a number, bool, list -- is a malformed frame, not a stringifiable value:
    reject it at the boundary rather than coerce (``null`` -> ``"None"``) and
    let a bogus ref/album/part/prompt reach a handler.
    """
    raw = msg.get(key)
    if raw is None:
        return None
    if not isinstance(raw, str):
        detail = f"{key} must be a string, got {type(raw).__name__}"
        raise ValueError(detail)
    return raw or None


async def safe_send(websocket: WebSocket, payload: dict[str, object]) -> bool:
    """Send *payload* as JSON; return True if delivered, False if the peer had gone.

    A client that closes before or during a reply must end the request quietly,
    not surface as a traceback through the router's broad ``except``. Both drop
    paths are debug-logged with the frame's type/id so an operator grepping the
    log can tell WHICH request lost its reply -- a normal disconnect is not an
    error, so it stays at debug, but it carries correlation context.
    ``WebSocketDisconnect`` is the expected closed-client signal; a
    ``RuntimeError`` from a send on an already-closed socket also carries the
    underlying cause. The bool return lets a caller skip work once the peer is
    gone.
    """
    try:
        await websocket.send_json(payload)
    except WebSocketDisconnect:
        logger.debug("dropped %s reply: client gone", _frame_context(payload))
        return False
    except RuntimeError as exc:
        logger.debug(
            "dropped %s reply: client closed? %s", _frame_context(payload), exc
        )
        return False
    return True


def _frame_context(payload: dict[str, object]) -> str:
    """Return a short 'type id=... ref=...' tag identifying a wire frame for logs."""
    kind = payload.get("type") or payload.get("op") or "frame"
    ids = " ".join(
        f"{key}={payload[key]!r}" for key in ("id", "ref", "name") if payload.get(key)
    )
    return f"{kind} {ids}".rstrip()
