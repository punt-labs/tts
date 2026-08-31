"""Detached-tmux fork of a fresh claude session for the steering matrix.

Copied from the frozen vox-73y7 spike's ``launcher.py`` (itself the juhw
fork machinery) with the session prefix renamed for this harness's
teardown namespace, plus the send primitives the injection matrix needs:
literal-mode send-keys, a bracketed paste via tmux buffers, and named
keys (Escape, Enter) — the exact mechanics under test in Arm 2.
"""

from __future__ import annotations

import shlex
import subprocess
import time
from typing import TYPE_CHECKING, Self, final

if TYPE_CHECKING:
    from pathlib import Path

    from scratch import IsolatedConfig, ScratchProject

# Every tmux session the harness creates carries this prefix so teardown can
# find and kill them without touching anything else on the host.
SESSION_PREFIX = "vox04qy"

# Hard cap on forks per harness run -- the mission's session-bounding rule.
MAX_FORKS_PER_RUN = 2


@final
class LaunchCommand:
    """The exact command line the fork runs; testable without tmux."""

    __slots__ = ("_claude_bin", "_prompt")

    _claude_bin: Path
    _prompt: str

    def __new__(cls, claude_bin: Path, prompt: str) -> Self:
        if not prompt:
            msg = "refusing to fork a session with an empty prompt"
            raise ValueError(msg)
        self = super().__new__(cls)
        self._claude_bin = claude_bin
        self._prompt = prompt
        return self

    def to_shell(self) -> str:
        """The shell command tmux hands to the new session's pane."""
        return f"{shlex.quote(str(self._claude_bin))} {shlex.quote(self._prompt)}"


@final
class TmuxSession:
    """One named tmux session: spawn, observe, poke, kill."""

    __slots__ = ("_name",)

    _name: str

    def __new__(cls, name: str) -> Self:
        self = super().__new__(cls)
        self._name = name
        return self

    @property
    def name(self) -> str:
        """The tmux session name."""
        return self._name

    def spawn_argv(
        self, command: LaunchCommand, cwd: Path, env: dict[str, str]
    ) -> list[str]:
        """The tmux invocation; pure so tests can assert on it."""
        argv = ["tmux", "new-session", "-d", "-s", self._name, "-c", str(cwd)]
        for key, value in sorted(env.items()):
            argv.extend(["-e", f"{key}={value}"])
        argv.append(command.to_shell())
        return argv

    def spawn(self, command: LaunchCommand, cwd: Path, env: dict[str, str]) -> None:
        """Create the detached session running the fork."""
        subprocess.run(
            self.spawn_argv(command, cwd, env), check=True, capture_output=True
        )

    def alive(self) -> bool:
        """True while tmux still knows the session."""
        probe = subprocess.run(
            ["tmux", "has-session", "-t", f"={self._name}"],
            check=False,
            capture_output=True,
        )
        return probe.returncode == 0

    def capture(self) -> str:
        """The current pane contents -- the non-interactive `tmux attach`."""
        # Pane-targeted commands take `name:` (session's active window/pane);
        # the `=name` exact form only resolves for session-targeted commands.
        captured = subprocess.run(
            ["tmux", "capture-pane", "-p", "-t", f"{self._name}:"],
            check=True,
            capture_output=True,
            text=True,
        )
        return captured.stdout

    def send_line(self, text: str) -> None:
        """Type one line into the session literally, then press Enter.

        The text rides :meth:`send_literal` (``-l --``): without literal
        mode, a text that IS a tmux key token — steering text like
        ``C-c`` — would be sent as the key (an interrupt) instead of its
        characters. The Enter is a separate send-keys after a short
        settle: a TUI that treats rapid input as a bracketed paste can
        otherwise leave the whole line sitting unsubmitted in its
        composer -- observed with Claude Code's input box on longer
        prompts.
        """
        self.send_literal(text)
        time.sleep(0.5)
        self.send_key("Enter")

    def send_key(self, key: str) -> None:
        """Send one named key (e.g. ``Down``, ``Enter``, ``Escape``) to the pane."""
        subprocess.run(
            ["tmux", "send-keys", "-t", f"{self._name}:", key],
            check=True,
            capture_output=True,
        )

    def literal_argv(self, text: str) -> list[str]:
        """The ``-l`` (literal) send-keys invocation; pure for tests.

        Literal mode never interprets key names, so text containing
        ``Enter`` or ``C-c`` arrives as characters; ``--`` guards a
        leading dash from flag parsing.
        """
        return ["tmux", "send-keys", "-t", f"{self._name}:", "-l", "--", text]

    def send_literal(self, text: str) -> None:
        """Type ``text`` exactly, with no key-name interpretation."""
        subprocess.run(self.literal_argv(text), check=True, capture_output=True)

    def paste_argv(self, buffer_name: str) -> tuple[list[str], list[str]]:
        """The load-buffer / paste-buffer pair; pure for tests.

        ``paste-buffer -p`` wraps the text in bracketed-paste sequences
        when the pane's application requested them — the mechanic that
        makes a multi-line paste land in a TUI composer as ONE block
        instead of one submitted line per newline. ``-d`` deletes the
        buffer after pasting so reruns cannot pick up a stale one.
        """
        load = ["tmux", "load-buffer", "-b", buffer_name, "-"]
        paste = [
            "tmux",
            "paste-buffer",
            "-p",
            "-d",
            "-b",
            buffer_name,
            "-t",
            f"{self._name}:",
        ]
        return load, paste

    def paste_text(self, text: str) -> None:
        """Deliver ``text`` to the pane as one bracketed paste."""
        buffer_name = f"{self._name}-paste"
        load, paste = self.paste_argv(buffer_name)
        subprocess.run(load, input=text.encode(), check=True, capture_output=True)
        subprocess.run(paste, check=True, capture_output=True)

    def kill(self) -> None:
        """End the session; a second call is a no-op."""
        subprocess.run(
            ["tmux", "kill-session", "-t", f"={self._name}"],
            check=False,
            capture_output=True,
        )


@final
class SessionLauncher:
    """Forks bounded claude sessions into scratch projects."""

    __slots__ = ("_claude_bin", "_forked")

    _claude_bin: Path
    _forked: int

    def __new__(cls, claude_bin: Path) -> Self:
        self = super().__new__(cls)
        self._claude_bin = claude_bin
        self._forked = 0
        return self

    def launch(
        self,
        name: str,
        project: ScratchProject,
        config: IsolatedConfig,
        prompt: str,
        extra_env: dict[str, str] | None = None,  # None: config env only
    ) -> TmuxSession:
        """Fork one session; refuses past the per-run cap.

        ``extra_env`` entries (e.g. the sentinel-stub PATH) are merged
        over the isolated config's own env and ride the same tmux ``-e``
        injection.
        """
        if self._forked >= MAX_FORKS_PER_RUN:
            msg = f"fork cap reached ({MAX_FORKS_PER_RUN} per run)"
            raise RuntimeError(msg)
        if not name.startswith(SESSION_PREFIX):
            msg = f"session name must carry the {SESSION_PREFIX!r} prefix: {name}"
            raise ValueError(msg)
        env = config.env()
        if extra_env is not None:
            env.update(extra_env)
        session = TmuxSession(name)
        session.spawn(LaunchCommand(self._claude_bin, prompt), project.path, env)
        # Budget is consumed only by a session that verifiably exists: a
        # spawn failure (tmux absent, bad cwd) or a fork that died at
        # startup (bad claude binary exits instantly, taking the tmux
        # session with it despite spawn's exit 0) must leave room for a
        # retry.
        if not session.alive():
            msg = f"fork died at startup: tmux session {name} vanished"
            raise RuntimeError(msg)
        self._forked += 1
        return session
