"""Doctor sub-check for the ``mpv`` program-audio player.

``mpv`` plays the program audio tier (music, and later audiobooks and podcasts)
over its JSON IPC socket. It is a hard dependency with no fallback --
notifications keep the built-in ``afplay`` / ``say`` / ``espeak``, but program
audio needs ``mpv``, and the IPC contract (the command set, the ``end-file``
reasons, the per-file ``pause`` option) holds only at or above
``MPV_MIN_VERSION`` (docs/mpv-program-player.md §1).

The version-detection dance (subprocess call, parse ``mpv --version``, gate
against the pinned minimum) clustered enough logic to earn its own module;
``doctor.py`` was tracking well past the 300-line module_size threshold and this
class was one of its natural fault lines. The public :meth:`MpvCheck.run`
returns a :class:`~punt_vox.doctor.CheckResult` the way every other doctor
sub-check does, so extraction is behaviour-preserving.
"""

from __future__ import annotations

import platform
import re
import shutil
import subprocess
from typing import Self, final

from punt_vox.doctor_result import CheckResult
from punt_vox.voxd.programs.mpv import MPV_MIN_VERSION

__all__ = ["MPV_HINTS", "MPV_MIN_STR", "MpvCheck"]


# The authoritative minimum mpv version lives with the mpv program player
# (``MPV_MIN_VERSION`` in ``punt_vox.voxd.programs.mpv``); this module imports
# that single source of truth and derives the display string from it.
MPV_MIN_STR: str = ".".join(str(part) for part in MPV_MIN_VERSION)

# Per-platform install / upgrade hints. ``default`` covers any host not named.
MPV_HINTS: dict[str, str] = {
    "Darwin": "brew install mpv",
    "Linux": "sudo apt-get install mpv (or dnf/pacman)",
    "Windows": "see https://mpv.io/installation/",
    "default": "see https://mpv.io/installation/",
}


@final
class MpvCheck:
    """Verdict for an installed mpv: absent, unreadable, too old, or acceptable.

    Nothing about this check needs shared doctor state -- it is stateless past
    the imports -- so ``__new__`` takes no arguments and the class is a small
    stateless facade over three subprocess-boundary helpers.
    """

    __slots__ = ()

    def __new__(cls) -> Self:
        return super().__new__(cls)

    def run(self) -> CheckResult:
        """Return the ``mpv`` verdict as a doctor :class:`CheckResult`."""
        hint = MPV_HINTS.get(platform.system(), MPV_HINTS["default"])
        if shutil.which("mpv") is None:
            return CheckResult.fail(f"mpv: not found — {hint}")
        version = self._detect_version()
        if version is None:
            return CheckResult.fail(
                "mpv: present but version unreadable —"
                f" verify 'mpv --version' is >= {MPV_MIN_STR}"
            )
        detected = ".".join(str(part) for part in version)
        if version < MPV_MIN_VERSION:
            return CheckResult.fail(
                f"mpv {detected}: too old (needs >= {MPV_MIN_STR}) — {hint}"
            )
        return CheckResult.ok(f"mpv: present ({detected})")

    def _detect_version(self) -> tuple[int, int, int] | None:
        # ``None`` is the documented "cannot determine" outcome at this
        # subprocess boundary (mpv vanished from PATH mid-check, a broken
        # binary, a timeout, or unparseable output). The caller surfaces it as
        # a failing check, so this is absence-as-contract, not a value a caller
        # must defensively treat as success (PY-TS-14). The binary is resolved
        # to an absolute path first, mirroring the provider subprocess callers.
        mpv_path = shutil.which("mpv")
        if mpv_path is None:
            return None
        try:
            proc = subprocess.run(
                [mpv_path, "--version"],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        return self.parse_version(proc.stdout)

    @staticmethod
    def parse_version(output: str) -> tuple[int, int, int] | None:
        """Parse ``mpv <major>.<minor>[.<patch>]`` from ``mpv --version`` output.

        ``mpv`` prints ``mpv <major>.<minor>.<patch> Copyright ...`` on line
        one; some builds prefix a ``v`` or append ``-git-<hash>``. Returns
        ``None`` when no version token is present (absence-as-contract, see
        :meth:`_detect_version`).
        """
        match = re.search(r"\bmpv\s+v?(\d+)\.(\d+)(?:\.(\d+))?", output)
        if match is None:
            return None
        return (int(match.group(1)), int(match.group(2)), int(match.group(3) or 0))
