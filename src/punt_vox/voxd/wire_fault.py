"""Split a server-side fault into a prefix-free wire message and a raw log line.

``WireReply.fault`` once sent the exception text verbatim on the wire, leaking an
absolute prefix -- the home directory and the username inside it -- to any client
that could reach the socket. :class:`SafeFault` rebuilds the client-facing message
from the exception's own fields: an in-jail ``OSError`` becomes ``"recordings/foo.mp3:
permission denied"`` via :func:`relativize_to_data_root`, and everything else --
another exception type, an out-of-jail or absent ``filename``, a fault with no
exception at all -- becomes the generic ``"operation failed"``. The raw detail,
absolute paths and all, is kept for the host-local ``vox.log`` alone.
"""

from __future__ import annotations

from typing import Self, final

from punt_vox.voxd.data_root_boundary import relativize_to_data_root

__all__ = ["SafeFault"]

_GENERIC = "operation failed"


@final
class SafeFault:
    """A fault's two faces: a prefix-free wire message and a raw log detail.

    Built through :meth:`from_exception` for a caught exception, or :meth:`opaque`
    for a fault with no exception to mine (a nonzero player exit code, a playback
    failure carrying device stderr). The wire message never carries an absolute
    prefix; the log detail carries the full raw text for the operator.
    """

    __slots__ = ("_detail", "_wire")

    _wire: str
    _detail: str

    def __new__(cls, wire: str, detail: str) -> Self:
        self = super().__new__(cls)
        self._wire = wire
        self._detail = detail
        return self

    @classmethod
    def from_exception(cls, exc: BaseException) -> Self:
        """Build the safe fault from *exc*'s own fields.

        An ``OSError`` whose ``filename`` resolves in-jail crosses as
        ``"<relative path>: <reason>"`` -- the client learns the logical location
        and the cause, never the prefix. Any other exception type, or an
        out-of-jail / absent ``filename`` / absent ``strerror``, crosses as the
        generic ``"operation failed"``. The raw ``str(exc)`` -- the absolute path
        included -- is retained as the log detail.
        """
        # Boundary catch (PY-EH-6): building a fault runs while an error is
        # ALREADY being handled, so a raise here would fault the fault handler and
        # tear down the socket. Relativizing a hostile exc.filename has its own
        # fail-closed guards, but this backstop makes the fault path provably
        # un-teardownable regardless of any future edge -- any failure yields the
        # generic verdict. Defense in depth, not a substitute for the guards.
        try:
            wire = cls._wire_for(exc)
        except Exception:  # noqa: BLE001 -- PY-EH-6 fault-building boundary
            wire = _GENERIC
        return cls(wire, str(exc))

    @classmethod
    def opaque(cls, detail: str) -> Self:
        """Build a generic-wire fault whose *detail* is logged but never sent.

        For a fault with no exception to relativize, the wire gets the generic
        ``"operation failed"`` and *detail* goes to the host-local log alone.
        """
        return cls(_GENERIC, detail)

    @property
    def wire_message(self) -> str:
        """Return the prefix-free message safe to send to the client."""
        return self._wire

    @property
    def log_detail(self) -> str:
        """Return the raw fault detail for the host-local ``vox.log`` only."""
        return self._detail

    @staticmethod
    def _wire_for(exc: BaseException) -> str:
        """Return the relativized message for an in-jail ``OSError``, else generic."""
        if not isinstance(exc, OSError) or exc.strerror is None:
            return _GENERIC
        rel = relativize_to_data_root(exc.filename)
        if rel is None:
            return _GENERIC
        return f"{rel.path}: {exc.strerror.lower()}"
