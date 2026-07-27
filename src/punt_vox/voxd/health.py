"""Daemon health reporting for voxd."""
# pyright: reportPrivateUsage=false
# Internal module within the voxd package -- cross-module private access is expected.

from __future__ import annotations

import os
import time
from collections.abc import Callable
from typing import Self

from punt_vox.paths import installed_version
from punt_vox.voxd.playback import PlaybackQueue


class DaemonHealth:
    """Own health-check state and payload construction for voxd."""

    __slots__ = (
        "_daemon_version",
        "_get_client_count",
        "_playback",
        "_port",
        "_start_time",
    )

    _daemon_version: str
    _get_client_count: Callable[[], int]
    _playback: PlaybackQueue
    _port: int
    _start_time: float

    def __new__(
        cls,
        playback: PlaybackQueue,
        get_client_count: Callable[[], int],
        port: int,
    ) -> Self:
        self = super().__new__(cls)
        self._playback = playback
        self._get_client_count = get_client_count
        self._start_time = time.monotonic()
        self._port = port
        self._daemon_version = installed_version()
        return self

    # -- Properties ----------------------------------------------------------

    @property
    def daemon_version(self) -> str:
        """Return the cached daemon version string."""
        return self._daemon_version

    @property
    def port(self) -> int:
        """Return the daemon listen port."""
        return self._port

    @property
    def start_time(self) -> float:
        """Return the monotonic timestamp when the daemon started."""
        return self._start_time

    # -- Public methods ------------------------------------------------------

    def minimal_payload(self) -> dict[str, object]:
        """Return the public health payload safe for unauthenticated callers.

        Carries only liveness verdicts and service metrics. The TTS ``provider``
        is dropped (D1): naming the backend to an unauthenticated probe
        fingerprints it, and it is an out-of-jail host fact with no relative
        form. Everything else is added only by :meth:`full_payload`, behind the
        auth token.
        """
        uptime = time.monotonic() - self._start_time
        return {
            "status": "ok",
            "uptime_seconds": round(uptime, 1),
            "queued": self._playback.queue_size,
            "port": self._port,
            "active_sessions": self._get_client_count(),
        }

    def full_payload(self) -> dict[str, object]:
        """Return the full diagnostic health payload for authenticated callers.

        Used only by the token-gated WebSocket health handler. ``provider`` is
        authenticated-only (D1) -- ``doctor`` reads it, the ``/health`` probe
        never sees it. The old host-fact diagnostics ``audio_env`` and
        ``player_binary`` are dropped (D2): out of jail, no relative form.
        ``last_playback`` is relativized by :meth:`PlaybackResult.to_health_dict`,
        so no absolute prefix crosses even here. ``pid`` (used by ``vox daemon
        restart``) and ``daemon_version`` (used by ``vox doctor``) are neither a
        host map nor forbidden over the token.
        """
        from punt_vox.providers import auto_detect_provider

        payload = self.minimal_payload()
        payload["provider"] = auto_detect_provider()
        if result := self._playback.last_result:
            payload["last_playback"] = result.to_health_dict()
        else:
            payload["last_playback"] = None
        payload["pid"] = os.getpid()
        payload["daemon_version"] = self._daemon_version
        return payload

    def set_daemon_version(self, val: str) -> None:
        """Override the cached daemon version. For test use."""
        self._daemon_version = val
