"""The one place every ``claude`` subprocess this package spawns builds its
environment.

Both :class:`~.claude_session_attach.ClaudeSessionAttach` (one subprocess per
human turn) and :class:`~.session_discovery.SessionDiscovery` (``claude
agents --json``, run once per call to find a session to attach to) spawn the
real ``claude`` binary and must not forward the parent's own
``ANTHROPIC_API_KEY`` -- see :func:`claude_subprocess_env`'s docstring for
why. Extracted so a third future ``claude``-spawn site inherits the fix
automatically instead of rediscovering it independently.
"""

from __future__ import annotations

import os

__all__ = ["claude_subprocess_env"]

# claude -p --resume (and, by the same mechanism, every other claude
# subcommand) is meant to use the human's own already-authenticated
# claude.ai session identity. A stale/mismatched ANTHROPIC_API_KEY in the
# parent environment -- common in any dev shell sourcing this org's .envrc
# -- takes precedence over that login instead, per claude's own printed
# warning ("claude.ai connectors are disabled because ANTHROPIC_API_KEY or
# another auth source is set and takes precedence over your claude.ai
# login"). `claude --help` names only this one env var in that family; no
# sibling (e.g. a CLAUDE_API_KEY) is documented.
_STRIPPED_ENV_VARS = ("ANTHROPIC_API_KEY",)


def claude_subprocess_env(*, extra: dict[str, str] | None = None) -> dict[str, str]:
    """Return the parent environment minus every var in :data:`_STRIPPED_ENV_VARS`.

    *extra* is merged in last, so a caller-specific marker (e.g.
    :class:`ClaudeSessionAttach`'s ``VOX_CALL_RELAY``) always wins over
    anything of the same name inherited from the parent -- not that any
    entry in *extra* is expected to collide with a stripped var today.
    """
    env = {k: v for k, v in os.environ.items() if k not in _STRIPPED_ENV_VARS}
    if extra:
        env.update(extra)
    return env
