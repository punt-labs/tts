"""Sentinel stand-ins for the vox binaries, first on the spawned PATH.

The h7k8 isolation rule: no spawned session may reach a real `vox` or
`vox-panel` (and through them the live Lux hub). These stubs intercept any
such call, record it to a log, and exit 0 — an attempt becomes committed
evidence instead of a rogue panel.
"""

from __future__ import annotations

import shlex
from typing import TYPE_CHECKING, Self, final

if TYPE_CHECKING:
    from pathlib import Path

# The binaries a stray hook or plugin would reach for.
STUBBED_NAMES: tuple[str, ...] = ("vox", "vox-panel")


@final
class SentinelStubs:
    """A bin directory of recording stand-ins, prepended to PATH."""

    __slots__ = ("_root",)

    _root: Path

    def __new__(cls, root: Path) -> Self:
        self = super().__new__(cls)
        self._root = root
        return self

    @property
    def bin_dir(self) -> Path:
        """The directory to prepend to PATH."""
        return self._root / "bin"

    @property
    def log_path(self) -> Path:
        """Where intercepted invocations are recorded."""
        return self._root / "invocations.log"

    def create(self) -> None:
        """Write one recording stub per guarded binary name.

        The log file is created empty here, so "no hits" is always the
        observation of an existing empty log — never an inference from a
        missing file.
        """
        self.bin_dir.mkdir(parents=True, exist_ok=True)
        self.log_path.touch()
        for name in STUBBED_NAMES:
            stub = self.bin_dir / name
            stub.write_text(self._script_body(), encoding="utf-8")
            stub.chmod(0o755)

    def path_env(self, base_path: str) -> str:
        """The PATH value with the stubs resolving first."""
        return f"{self.bin_dir}:{base_path}"

    def invocations(self) -> tuple[str, ...]:
        """Every intercepted call, one line each; empty means none.

        Raises when the log file is gone: a missing log means the stubs
        were never created or the scratch root was already removed, and
        either way "zero hits" would be a fabricated all-clear. Harvest
        BEFORE teardown.
        """
        if not self.log_path.exists():
            msg = (
                "stub invocation log missing (harvest before teardown): "
                f"{self.log_path}"
            )
            raise FileNotFoundError(msg)
        return tuple(self.log_path.read_text(encoding="utf-8").splitlines())

    def _script_body(self) -> str:
        # $0 carries the resolved stub path (which names the binary);
        # "$*" carries the arguments the caller passed. The log path is
        # shell-quoted: it is harness-controlled and may contain spaces.
        log = shlex.quote(str(self.log_path))
        return f'#!/bin/sh\nprintf \'%s %s\\n\' "$0" "$*" >> {log}\nexit 0\n'
