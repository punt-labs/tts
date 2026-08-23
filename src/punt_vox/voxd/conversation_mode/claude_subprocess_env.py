"""The one place every ``claude`` subprocess this package spawns builds its
environment.

Both :class:`~.claude_session_attach.ClaudeSessionAttach` (one subprocess per
human turn) and :class:`~.session_discovery.SessionDiscovery` (``claude
agents --json``, run once per call to find a session to attach to) spawn the
real ``claude`` binary and must not forward the parent's own
``ANTHROPIC_API_KEY`` -- see :class:`ClaudeSubprocessEnv`'s docstring for why.
Extracted so a third future ``claude``-spawn site inherits the fix
automatically instead of rediscovering it independently.
"""

from __future__ import annotations

import os
from typing import Self, final

__all__ = ["claude_subprocess_env"]


@final
class ClaudeSubprocessEnv:
    """Build a ``claude`` subprocess's environment, minus its auth traps.

    ``claude -p --resume`` (and, by the same mechanism, every other ``claude``
    subcommand) is meant to use the human's own already-authenticated
    claude.ai session identity. A stale/mismatched ``ANTHROPIC_API_KEY`` in
    the parent environment -- common in any dev shell sourcing this org's
    ``.envrc`` -- takes precedence over that login instead, per ``claude``'s
    own printed warning ("claude.ai connectors are disabled because
    ANTHROPIC_API_KEY or another auth source is set and takes precedence
    over your claude.ai login"). ``claude --help`` names only this one env
    var in that family; no sibling (e.g. a ``CLAUDE_API_KEY``) is documented.

    Callable, not a bare classmethod: every call site already spells this as
    a function call (``claude_subprocess_env(extra=...)``), and the module
    exports one ready-to-call instance so neither call site has to construct
    it themselves.
    """

    __slots__ = ()

    _STRIPPED_ENV_VARS = ("ANTHROPIC_API_KEY",)

    def __new__(cls) -> Self:
        return super().__new__(cls)

    def __call__(
        self, *, extra: dict[str, str] | None = None, keep_api_key: bool = False
    ) -> dict[str, str]:
        """Return the parent environment minus every stripped var.

        *extra* is merged in last, so a caller-specific marker (e.g.
        :class:`ClaudeSessionAttach`'s ``VOX_CALL_RELAY``) always wins over
        anything of the same name inherited from the parent -- not that any
        entry in *extra* is expected to collide with a stripped var today.

        *keep_api_key*, default ``False``, preserves every other call site's
        existing behavior -- ``ANTHROPIC_API_KEY`` stripped so a resumed
        session uses its own claude.ai OAuth login (see this class's own
        docstring). ``True`` is the exact opposite requirement: a ``claude
        --bare`` invocation has no OAuth support at all and *requires*
        ``ANTHROPIC_API_KEY`` explicitly (``claude --help``: "Anthropic auth
        is strictly ANTHROPIC_API_KEY or apiKeyHelper via --settings") --
        stripping it there does not fall back to OAuth, it fails every turn
        with "Not logged in".
        """
        stripped = () if keep_api_key else self._STRIPPED_ENV_VARS
        env = {k: v for k, v in os.environ.items() if k not in stripped}
        if extra:
            env.update(extra)
        return env


claude_subprocess_env: ClaudeSubprocessEnv = ClaudeSubprocessEnv()
