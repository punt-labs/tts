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

    # keep_api_key=True's subprocess (the --bare relay) is reachable by a
    # live, untrusted voice turn -- forwarding the whole parent environment
    # would hand it every other secret in the caller's shell. This is the
    # minimal set `claude -p --resume --bare` needs to run (verified
    # empirically: PATH+HOME+ANTHROPIC_API_KEY alone starts it and reaches
    # session-id validation, no SHELL/USER/TERM/LANG complaint).
    # ANTHROPIC_API_KEY is added separately below -- its presence is a
    # precondition owned upstream (BareAuthMissingError.check).
    _MINIMAL_RELAY_VARS = ("PATH", "HOME")

    def __new__(cls) -> Self:
        return super().__new__(cls)

    def __call__(
        self, *, extra: dict[str, str] | None = None, keep_api_key: bool = False
    ) -> dict[str, str]:
        """Return the subprocess's environment, minus every stripped var.

        *extra* is merged in last, so a caller-specific marker (e.g.
        :class:`ClaudeSessionAttach`'s ``VOX_CALL_RELAY``) always wins over
        anything of the same name inherited from the parent.

        *keep_api_key*, default ``False``, preserves every other call site's
        existing behavior -- the full parent environment minus
        ``ANTHROPIC_API_KEY`` (see this class's own docstring). ``True`` is
        the opposite requirement twice over: ``claude --bare`` has no OAuth
        support and *requires* the key explicitly, and its subprocess is the
        one reachable by untrusted voice input -- so it gets a MINIMAL
        environment (:data:`_MINIMAL_RELAY_VARS` plus the key), never the
        parent's full environment.
        """
        env = self._minimal_relay_env() if keep_api_key else self._full_env_minus_key()
        if extra:
            env.update(extra)
        return env

    def _full_env_minus_key(self) -> dict[str, str]:
        """Return the parent environment, minus :data:`_STRIPPED_ENV_VARS`."""
        return {k: v for k, v in os.environ.items() if k not in self._STRIPPED_ENV_VARS}

    def _minimal_relay_env(self) -> dict[str, str]:
        """Return only what ``claude -p --resume --bare`` needs to run."""
        env = {
            name: os.environ[name]
            for name in self._MINIMAL_RELAY_VARS
            if name in os.environ
        }
        api_key = os.environ.get("ANTHROPIC_API_KEY")
        if api_key is not None:
            env["ANTHROPIC_API_KEY"] = api_key
        return env


claude_subprocess_env: ClaudeSubprocessEnv = ClaudeSubprocessEnv()
