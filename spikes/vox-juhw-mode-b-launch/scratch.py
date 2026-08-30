"""Throwaway scratch project and isolated Claude config dir for the fork.

Isolation is the mission's hard rule: a spawned session runs in a fresh
`git init` directory the spike creates, never a real checkout, and under a
fresh ``CLAUDE_CONFIG_DIR`` so none of the launching user's plugins, hooks,
MCP servers, or session state leak into it. Two pieces of real state are
seeded into the fresh config dir so the fork can run non-interactively:
the OAuth credentials file (copied verbatim, mode 0600) and a minimal
``.claude.json`` that marks onboarding complete and pre-accepts the trust
dialog for exactly the scratch project path.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Self, final


@final
class ScratchProject:
    """A fresh git-initialized project directory the fork works inside."""

    __slots__ = ("_root",)

    _root: Path

    def __new__(cls, root: Path) -> Self:
        self = super().__new__(cls)
        self._root = root
        return self

    @property
    def path(self) -> Path:
        """The project directory."""
        return self._root

    def create(self, settings_json: str) -> None:
        """Materialize the project: git init, seed README, deposit settings."""
        self._root.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            ["git", "init", "--quiet", str(self._root)],
            check=True,
            capture_output=True,
        )
        readme = self._root / "README.md"
        readme.write_text(
            "# Scratch project\n\nThrowaway Mode B launch target. "
            "Created and destroyed by the vox-juhw spike harness.\n",
            encoding="utf-8",
        )
        claude_dir = self._root / ".claude"
        claude_dir.mkdir(exist_ok=True)
        (claude_dir / "settings.json").write_text(settings_json, encoding="utf-8")

    def remove(self) -> None:
        """Delete the project tree; a second call is a no-op."""
        shutil.rmtree(self._root, ignore_errors=True)


@final
class IsolatedConfig:
    """A fresh ``CLAUDE_CONFIG_DIR`` so the fork sees no user-level state."""

    __slots__ = ("_root",)

    _root: Path

    def __new__(cls, root: Path) -> Self:
        self = super().__new__(cls)
        self._root = root
        return self

    @property
    def path(self) -> Path:
        """The config directory."""
        return self._root

    def env(self) -> dict[str, str]:
        """Environment entries the fork is launched with.

        Besides pointing at the fresh config dir, the launcher's own API
        credentials are blanked: a fork inheriting the launching process's
        ``ANTHROPIC_API_KEY`` would both bill the wrong account and trip an
        interactive "use this API key?" dialog before any work starts. The
        fork authenticates with the seeded credentials file only.
        """
        return {
            "CLAUDE_CONFIG_DIR": str(self._root),
            "ANTHROPIC_API_KEY": "",
            "ANTHROPIC_AUTH_TOKEN": "",
        }

    def create(self, project_path: Path, credentials_source: Path) -> None:
        """Materialize the config dir; seed credentials + minimal state."""
        self._root.mkdir(parents=True, exist_ok=True)
        self._seed_credentials(credentials_source)
        self._seed_state(project_path)

    def remove(self) -> None:
        """Delete the config tree (contains credentials); no-op if absent."""
        shutil.rmtree(self._root, ignore_errors=True)

    def _seed_credentials(self, source: Path) -> None:
        if not source.exists():
            # No file-based credentials on this host (e.g. keychain storage);
            # the fork will demand login. Recorded by the runner as a rough
            # edge rather than failing here -- the launch chain itself is
            # still exercisable up to the login prompt.
            return
        target = self._root / ".credentials.json"
        shutil.copyfile(source, target)
        target.chmod(0o600)

    def _seed_state(self, project_path: Path) -> None:
        state = {
            "hasCompletedOnboarding": True,
            "projects": {
                str(project_path): {
                    "hasTrustDialogAccepted": True,
                    "hasClaudeMdExternalIncludesApproved": False,
                    "hasClaudeMdExternalIncludesWarningShown": True,
                }
            },
        }
        (self._root / ".claude.json").write_text(
            json.dumps(state, indent=2, sort_keys=True), encoding="utf-8"
        )
