"""``MusicSession`` -- the session surface the ``music`` tool reads.

A structural view (PY-TS-6) of the server's session config, declared here rather
than imported from the presentation layer so the dependency arrow keeps pointing
inward: the music verbs state what they need from a session, and the server's
concrete session satisfies it by having those members (PY-IC-9).
"""

from __future__ import annotations

from typing import Protocol

__all__ = ["MusicSession"]


class MusicSession(Protocol):
    """The mood register a ``music`` verb consults before starting a Program."""

    @property
    def vibe(self) -> str | None:
        """Return the session mood tag, or None when it is cleared."""

    def refresh_from_config(self) -> None:
        """Re-read the config files so the yielded mood is current."""
