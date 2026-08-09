"""The fault contract every ``music`` verb answers a failure with.

Each verb reaches ``voxd`` over the same transport, so each can fail the same
four ways and must report that failure the same way: a JSON ``{"error": ...}``
envelope the caller reads, never a log line it cannot see. The dispatcher and
the verb groups it delegates to all need both halves of that contract -- the
faults to catch and the envelope to answer with -- so both live here once and
three modules cannot drift into reporting a broken daemon three different ways.

A verb's *success* payload is its own shape and stays with the verb; only the
failure shape is shared, because only the failure shape must be identical.
"""

from __future__ import annotations

import json
from typing import Self, final

from websockets.exceptions import WebSocketException

from punt_vox.client_errors import VoxdConnectionError, VoxdProtocolError

__all__ = ["DAEMON_ERRORS", "MusicFault"]

DAEMON_ERRORS = (VoxdConnectionError, VoxdProtocolError, WebSocketException, OSError)
"""The transport faults a verb catches: voxd unreachable, or answering badly."""


@final
class MusicFault:
    """A failed ``music`` call on its way back to the caller.

    Built from what went wrong and rendered at the return statement, so a verb
    states the reason once and the wire shape is decided in one place.
    """

    __slots__ = ("_message",)
    _message: str

    def __new__(cls, message: str) -> Self:
        self = super().__new__(cls)
        self._message = message
        return self

    @classmethod
    def of(cls, exc: BaseException) -> str:
        """Return the envelope reporting a caught *exc* to the caller."""
        return cls(str(exc)).render()

    @classmethod
    def rejecting(cls, message: str) -> str:
        """Return the envelope for a reject the verb detected itself.

        A missing ``album_id``, an unknown subcommand, or a daemon reject the
        verb re-renders with more context: the caller's mistake reaches them in
        the same shape as the daemon's, so a client parses one failure form.
        """
        return cls(message).render()

    def render(self) -> str:
        """Return the JSON text -- the one shape every music failure takes."""
        return json.dumps({"error": self._message})
