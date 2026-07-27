"""Tests for punt_vox.types_health -- the HealthStatus wire value object."""

from __future__ import annotations

from punt_vox.types_health import HealthStatus


def test_from_wire_reads_the_full_daemon_payload() -> None:
    """A full health payload maps field-for-field onto the typed snapshot."""
    status = HealthStatus.from_wire(
        {
            "type": "health",
            "status": "ok",
            "uptime_seconds": 42.5,
            "queued": 3,
            "port": 8421,
            "active_sessions": 2,
            "provider": "elevenlabs",
            "last_playback": {"rc": 0, "file": "recordings/a.mp3"},
            "pid": 4242,
            "daemon_version": "5.0.0",
        }
    )

    assert status.status == "ok"
    assert status.uptime_seconds == 42.5
    assert status.queued == 3
    assert status.port == 8421
    assert status.active_sessions == 2
    assert status.provider == "elevenlabs"
    assert status.last_playback == {"rc": 0, "file": "recordings/a.mp3"}
    assert status.pid == 4242
    assert status.daemon_version == "5.0.0"


def test_from_wire_defaults_absent_fields() -> None:
    """A minimal payload leaves the unreported fields at benign defaults.

    A health read is best-effort observability, so absence is not a parse
    failure -- an absent ``daemon_version`` reads as "" (which every consumer
    treats as "not reported"), and ``last_playback`` as None.
    """
    status = HealthStatus.from_wire({"status": "ok", "queued": 0})

    assert status.provider == "unknown"
    assert status.port == 0
    assert status.pid == 0
    assert status.daemon_version == ""
    assert status.uptime_seconds == 0.0
    assert status.last_playback is None


def test_from_wire_coerces_wrong_types_to_defaults() -> None:
    """Wrong-typed fields fall back to defaults rather than raising or leaking.

    ``bool`` is rejected as an int (``True`` is not a port); a non-string
    ``provider`` falls back to ``"unknown"``.
    """
    status = HealthStatus.from_wire(
        {"port": True, "provider": 7, "uptime_seconds": True}
    )

    assert status.port == 0
    assert status.provider == "unknown"
    assert status.uptime_seconds == 0.0


def test_from_wire_accepts_int_uptime() -> None:
    """An integer uptime is widened to float (JSON numbers may lack a point)."""
    status = HealthStatus.from_wire({"uptime_seconds": 5})

    assert status.uptime_seconds == 5.0
    assert isinstance(status.uptime_seconds, float)
