"""Shared types for the commands layer: ``CommandResult``, ``Ctx``, ``SwitchList``.

Every command function in :mod:`punt_vox.commands` takes a :class:`Ctx`
and returns a :class:`CommandResult`. The CLI wrapper in
:mod:`punt_vox.__main__` interprets the result (JSON vs human text, exit
codes); library callers inspect the fields directly.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from punt_vox.client_sync import VoxClientSync
    from punt_vox.config import ConfigStore


@dataclass(frozen=True, slots=True)
class CommandResult:
    """Outcome of a command invocation.

    Attributes:
        text: Human-readable output for the CLI's text mode and stderr on error.
        json_data: JSON-serializable payload for the CLI's ``--json`` mode.
            ``None`` means the CLI falls back to ``text``.
        error: ``True`` signals a user-facing failure (invalid input, missing
            resource, daemon unreachable). The CLI reports through stderr
            and exits with ``exit_code``. Programmer errors and violated
            invariants still raise per python.md Error Handling.
        exit_code: The process exit code the CLI adapter emits when ``error``
            is ``True``. Ignored on success. Convention: ``1`` for user-facing
            failures, matching the daemon-error and MCP-tool envelope shapes.
    """

    text: str
    json_data: dict[str, object] | None = None
    error: bool = False
    exit_code: int = 0


@dataclass(frozen=True, slots=True)
class Ctx:
    """Collaborators shared by every command function.

    Attributes:
        store: The per-repo config reader/writer. Commands read the current
            provider/voice/model from it and write updates back.
        client: The daemon-facing client. Used by commands that reach voxd
            (e.g. ``voice`` needs the provider's voice roster).
    """

    store: ConfigStore
    client: VoxClientSync


@dataclass(frozen=True, slots=True)
class SwitchList:
    """A switch tool's roster with the current selection marked.

    The three switch commands (``model``, ``provider``, ``voice``) all list a
    set of names and mark the one in effect. Holding the pair as a value type
    lets each command render both the human line and the JSON payload without
    duplicating the ``(current)`` sigil across the layer.
    """

    names: tuple[str, ...]
    current: str | None

    def render(self, empty_message: str = "") -> str:
        """Return the human-readable listing, or *empty_message* when empty."""
        if not self.names:
            return empty_message
        return "\n".join(
            f"{n} (current)" if n == self.current else n for n in self.names
        )

    def payload(self) -> dict[str, object]:
        """Return the ``--json`` payload the CLI emits for this listing."""
        return {"names": list(self.names), "current": self.current}
