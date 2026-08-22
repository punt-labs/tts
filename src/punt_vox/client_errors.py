"""Exception types raised by the voxd client and its transport."""

from __future__ import annotations


class VoxError(Exception):
    """Base class for every failure a voxd client raises.

    Catch this to handle any client failure in one place; catch a subclass
    to tell a connection failure (:class:`VoxdConnectionError`) from a
    protocol failure (:class:`VoxdProtocolError`).
    """


class VoxdConnectionError(VoxError):
    """Raised when the client cannot connect to voxd."""


class VoxdProtocolError(VoxError):
    """Raised when voxd returns an unexpected response."""


class VoxdRejectionError(VoxdProtocolError):
    """Raised when voxd answered with a typed ``{"type": "error"}`` frame.

    Distinct from a bare :class:`VoxdProtocolError` (malformed JSON, a
    missing key, an unexpected frame type): the daemon was reached and
    said no, carrying a caller-facing reason. A surface may render the
    reason verbatim; a bare ``VoxdProtocolError`` names a bug and
    propagates.
    """
