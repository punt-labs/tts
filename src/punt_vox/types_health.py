"""The daemon health snapshot a voxd client's ``health()`` call returns."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Self, cast, final


@final
@dataclass(frozen=True, slots=True)
class HealthStatus:
    """The voxd daemon's health snapshot, as ``health()`` returns it.

    Mirrors the daemon's health payload (``voxd/health.py``): a liveness
    summary (``status``, ``uptime_seconds``, ``queued``, ``active_sessions``)
    plus the running process's identity (``port``, ``pid``,
    ``daemon_version``) so a caller can confirm the daemon is up, on which
    port, and running which build. ``last_playback`` is the ``vox doctor``
    diagnostic the daemon reports, relativized so it carries no host path.

    Absent fields fall back to benign defaults rather than raising: a health
    read is best-effort observability, and every consumer already treats a
    missing field as "not reported".
    """

    status: str = "unknown"
    provider: str = "unknown"
    port: int = 0
    pid: int = 0
    daemon_version: str = ""
    uptime_seconds: float = 0.0
    queued: int = 0
    active_sessions: int = 0
    # The last playback result (file, rc, elapsed_s, stderr, ts) or None when
    # nothing has played -- a diagnostic sub-record kept as a typed mapping
    # because a caller only ever forwards it to a log, never branches on it.
    last_playback: Mapping[str, object] | None = None

    @classmethod
    def from_wire(cls, raw: Mapping[str, object]) -> Self:
        """Build a health snapshot from the daemon's ``health`` reply."""
        return cls(
            status=cls._as_str(raw.get("status"), "unknown"),
            provider=cls._as_str(raw.get("provider"), "unknown"),
            port=cls._as_int(raw.get("port")),
            pid=cls._as_int(raw.get("pid")),
            daemon_version=cls._as_str(raw.get("daemon_version"), ""),
            uptime_seconds=cls._as_float(raw.get("uptime_seconds")),
            queued=cls._as_int(raw.get("queued")),
            active_sessions=cls._as_int(raw.get("active_sessions")),
            last_playback=cls._as_map(raw.get("last_playback")),
        )

    @staticmethod
    def _as_str(value: object, default: str) -> str:
        """Return *value* when it is a string, else *default*."""
        return value if isinstance(value, str) else default

    @staticmethod
    def _as_int(value: object) -> int:
        """Return *value* as an int (rejecting bool), else 0."""
        return value if isinstance(value, int) and not isinstance(value, bool) else 0

    @staticmethod
    def _as_float(value: object) -> float:
        """Return *value* as a float (accepting ints), else 0.0."""
        if isinstance(value, bool):
            return 0.0
        return float(value) if isinstance(value, int | float) else 0.0

    @staticmethod
    def _as_map(value: object) -> Mapping[str, object] | None:
        """Return *value* as a mapping, or None when absent or wrong-typed."""
        if not isinstance(value, Mapping):
            return None
        return dict(cast("Mapping[str, object]", value))
