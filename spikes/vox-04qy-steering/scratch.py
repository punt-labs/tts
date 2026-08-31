"""Throwaway scratch project and isolated Claude config dir for the fork.

Copied from the frozen vox-73y7 spike's ``scratch.py`` (itself the juhw
isolation contract): the spawned session runs in a fresh ``git init``
directory the spike creates, never a real checkout, under a fresh
``CLAUDE_CONFIG_DIR`` with the OAuth credentials file copied in (mode
0600) and a minimal ``.claude.json`` pre-accepting the trust dialog for
exactly the scratch project path. ``SeededFile`` is defined here (the
73y7 copy imported it from its task module): Arm 2 seeds plain note
files, not a buggy package. ``IsolatedConfig`` still deposits the relay
assets — rendered relay script, stamper copy, counter dir — in the
config dir so the fork's own file tools never trip over harness
plumbing.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Self, final

_SPIKE_DIR = Path(__file__).parent


@final
@dataclass(frozen=True, slots=True)
class SeededFile:
    """One file materialized into the scratch project before the fork."""

    relative_path: str
    content: str


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

    def create(self, settings_json: str, seeded: tuple[SeededFile, ...]) -> None:
        """Materialize the project: git init, seed the task, deposit settings."""
        self._root.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            ["git", "init", "--quiet", str(self._root)],
            check=True,
            capture_output=True,
        )
        for entry in seeded:
            target = self._root / entry.relative_path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(entry.content, encoding="utf-8")
        claude_dir = self._root / ".claude"
        claude_dir.mkdir(exist_ok=True)
        (claude_dir / "settings.json").write_text(settings_json, encoding="utf-8")

    def remove(self) -> None:
        """Delete the project tree; a second call is a no-op."""
        shutil.rmtree(self._root, ignore_errors=True)


@final
class IsolatedConfig:
    """A fresh ``CLAUDE_CONFIG_DIR`` so the fork sees no user-level state."""

    __slots__ = ("_credentials_seeded", "_root")

    _credentials_seeded: bool
    _root: Path

    def __new__(cls, root: Path) -> Self:
        self = super().__new__(cls)
        self._root = root
        self._credentials_seeded = False
        return self

    @property
    def path(self) -> Path:
        """The config directory."""
        return self._root

    @property
    def credentials_seeded(self) -> bool:
        """True once a credentials file was actually copied in."""
        return self._credentials_seeded

    @property
    def relay_script(self) -> Path:
        """Where the rendered relay script lives."""
        return self._root / "relay" / "relay.sh"

    @property
    def stamper_script(self) -> Path:
        """Where the deposited sender-side stamper lives."""
        return self._root / "relay" / "relay_stamp.py"

    @property
    def counter_dir(self) -> Path:
        """Where the per-session relay counters live."""
        return self._root / "relay" / "counters"

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

    def deposit_relay(self, relay_script_body: str) -> None:
        """Deposit the relay assets: script, stamper copy, counter dir."""
        relay_dir = self._root / "relay"
        relay_dir.mkdir(parents=True, exist_ok=True)
        self.counter_dir.mkdir(exist_ok=True)
        shutil.copyfile(_SPIKE_DIR / "relay_stamp.py", self.stamper_script)
        self.relay_script.write_text(relay_script_body, encoding="utf-8")
        self.relay_script.chmod(0o755)

    def remove(self) -> None:
        """Delete the config tree (contains credentials); no-op if absent."""
        shutil.rmtree(self._root, ignore_errors=True)

    def _seed_credentials(self, source: Path) -> None:
        if not source.exists():
            # No file-based credentials on this host (e.g. keychain
            # storage); the fork will demand login. Not a failure here --
            # the launch chain is still exercisable up to the login prompt
            # -- but the skip is exposed via ``credentials_seeded`` so the
            # runner can record it at seed time instead of surfacing as an
            # unexplained hooks timeout minutes later.
            return
        target = self._root / ".credentials.json"
        shutil.copyfile(source, target)
        target.chmod(0o600)
        self._credentials_seeded = True

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
