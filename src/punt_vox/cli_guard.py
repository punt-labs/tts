"""``CliGuard`` -- the one place a ``voxd`` fault becomes a clean CLI exit.

Every ``vox`` verb that crosses to the daemon can fail the same handful of ways,
and every one must report it identically: an ``{"error": ...}`` object under
``--json`` (the envelope the ``mic`` tools answer with) or ``Error: ...`` on
stderr, and a non-zero exit either way. Holding that policy in one object keeps
each verb to its own work and keeps a second CLI surface from inventing a second
way to fail.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, NoReturn, Self, final

import typer
from websockets.exceptions import WebSocketException

from punt_vox.client_errors import VoxdConnectionError, VoxdProtocolError

if TYPE_CHECKING:
    from collections.abc import Callable

    from punt_vox.output_formatter import OutputFormatter

__all__ = ["GATEWAY_ERRORS", "CliGuard"]

# A client error, a raw WebSocket failure (stale-token handshake / mid-request
# close, matching the MCP tools), or a bad name (ValueError) fails cleanly.
GATEWAY_ERRORS = (
    VoxdConnectionError,
    VoxdProtocolError,
    WebSocketException,
    OSError,
    ValueError,
)


@final
class CliGuard:
    """Turn a daemon fault into a reported, non-zero CLI exit."""

    __slots__ = ("_formatter",)
    _formatter: OutputFormatter

    def __new__(cls, formatter: OutputFormatter) -> Self:
        self = super().__new__(cls)
        self._formatter = formatter
        return self

    def run[T](self, op: Callable[[], T]) -> T:
        """Return the result of daemon call *op*, or exit cleanly on a fault."""
        try:
            return op()
        except GATEWAY_ERRORS as exc:
            self.fail(str(exc))

    def fail(self, message: str) -> NoReturn:
        """Report *message* and exit non-zero.

        Routed through the formatter so ``--json`` answers with the same
        ``{"error": ...}`` envelope the ``mic`` tools return, rather than a blank
        stdout beside plain-text stderr a JSON consumer cannot parse.
        """
        self._formatter.error(message, f"Error: {message}")
        raise typer.Exit(code=1)
