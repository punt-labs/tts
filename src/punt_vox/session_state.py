"""The structural :class:`SessionState` protocol read by :class:`SessionSpec`.

Extracted into its own module so both :class:`~punt_vox.config.VoxConfig`
(the on-disk snapshot hooks and the CLI read) and
:class:`~punt_vox.server.SessionConfig` (the in-memory MCP session that a
tool may have mutated) satisfy it without inheritance, and so callers who
only need the type contract can import it without pulling in
:mod:`~punt_vox.models` and the rest of the ``session_spec`` machinery.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

__all__ = ["SessionState"]


@runtime_checkable
class SessionState(Protocol):
    """The state fields a :class:`~punt_vox.session_spec.SessionSpec` reads.

    Structural so :class:`~punt_vox.config.VoxConfig` (dataclass attributes)
    and :class:`~punt_vox.server.SessionConfig` (properties) both satisfy it
    without an inheritance relationship neither of them wants.
    """

    @property
    def provider(self) -> str | None:
        """Return the state's provider name, or ``None`` when unset."""

    @property
    def voice(self) -> str | None:
        """Return the state's voice name, or ``None`` when unset."""

    @property
    def model(self) -> str | None:
        """Return the state's model name, or ``None`` / ``""`` when unset."""

    @property
    def vibe_tags(self) -> str | None:
        """Return the state's ElevenLabs expressive tags, or ``None`` when unset."""
