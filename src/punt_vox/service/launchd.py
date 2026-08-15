"""macOS launchd backend for voxd system service."""

from __future__ import annotations

import html
import logging
import os
import textwrap
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Self, final

from punt_vox.service.launchctl import LaunchctlAgent

if TYPE_CHECKING:
    # Runtime dependency is injected via __new__; the import is annotation-only,
    # so keeping it out of the runtime graph avoids coupling launchd to process.
    from punt_vox.service.process import ProcessManager

logger = logging.getLogger(__name__)

_LABEL = "com.punt-labs.voxd"
_LAUNCHD_DIR = Path.home() / "Library" / "LaunchAgents"
_LAUNCHD_PLIST = _LAUNCHD_DIR / f"{_LABEL}.plist"


@final
class LaunchdBackend:
    """Author the voxd LaunchAgent plist and drive its launchd lifecycle.

    Owns the plist content and location; delegates every ``launchctl``
    invocation to a composed :class:`LaunchctlAgent`, which serialises the
    bootout/bootstrap race that leaves voxd down on a first restart.
    """

    __slots__ = ("_agent", "_process_mgr", "_voxd_exec_args_fn")

    _process_mgr: ProcessManager
    _voxd_exec_args_fn: Callable[[], list[str]]
    _agent: LaunchctlAgent

    def __new__(
        cls,
        process_mgr: ProcessManager,
        voxd_exec_args_fn: Callable[[], list[str]],
    ) -> Self:
        self = super().__new__(cls)
        self._process_mgr = process_mgr
        self._voxd_exec_args_fn = voxd_exec_args_fn
        self._agent = LaunchctlAgent(_LABEL, str(_LAUNCHD_PLIST))
        return self

    @staticmethod
    def _extra_env() -> dict[str, str]:
        """Return extra env vars to bake into the launchd plist."""
        extras: dict[str, str] = {}
        bind = os.environ.get("VOXD_BIND")
        if bind:
            extras["VOXD_BIND"] = bind
        return extras

    def _program_args_xml(self) -> str:
        """Return the ``<string>...</string>`` block for ProgramArguments.

        ``html.escape`` XML-escapes each arg -- launchd reads the ``<string>``
        content literally, so ``&``, ``<``, ``>`` and quotes in a path or
        argument have to be entity-encoded, not shell-quoted.
        """
        return "\n".join(
            f"        <string>{html.escape(a)}</string>"
            for a in self._voxd_exec_args_fn()
        )

    def _extra_env_xml(self) -> str:
        """Return the trailing key/string pairs for EnvironmentVariables."""
        return "".join(
            f"\n            <key>{html.escape(k)}</key>"
            f"\n            <string>{html.escape(v)}</string>"
            for k, v in self._extra_env().items()
        )

    def plist_content(self) -> str:
        """Generate the LaunchAgent plist XML.

        LaunchAgents run as the session user by default -- no ``UserName``
        key is needed (and it is invalid for agents).  ``ProcessType=Interactive``
        prevents App Nap-style throttling on the windowless daemon.

        ``LimitLoadToSessionType=Aqua`` pins the job to the graphical login
        session, so voxd inherits the Aqua bootstrap context that grants
        CoreAudio queue access.  Without it, the agent can end up in a
        Background context that mostly works but intermittently fails
        ``AudioQueueStart`` with ``-66681`` after a ~15s block -- 72 such
        failures were logged in one session before the pin was added.
        """
        path_value = html.escape(
            os.environ.get("PATH", "/usr/bin:/bin:/usr/sbin:/sbin")
        )
        return textwrap.dedent(f"""\
            <?xml version="1.0" encoding="UTF-8"?>
            <!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
              "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
            <plist version="1.0">
            <dict>
                <key>Label</key>
                <string>{_LABEL}</string>
                <key>ProcessType</key>
                <string>Interactive</string>
                <key>LimitLoadToSessionType</key>
                <string>Aqua</string>
                <key>ProgramArguments</key>
                <array>
            {self._program_args_xml()}
                </array>
                <key>EnvironmentVariables</key>
                <dict>
                    <key>PATH</key>
                    <string>{path_value}</string>{self._extra_env_xml()}
                </dict>
                <key>RunAtLoad</key>
                <true/>
                <key>KeepAlive</key>
                <true/>
            </dict>
            </plist>
        """)

    def stop(self) -> None:
        """Bootout voxd from launchd if loaded.  Idempotent.

        Called as a pre-flight step by ``install()`` before
        ``ensure_port_free`` so launchd's ``KeepAlive=true`` does not
        respawn the daemon the instant the port-cleanup step kills it.
        The agent waits for the job to actually leave the GUI domain, so a
        following ``bootstrap`` does not race the asynchronous bootout.
        """
        if not _LAUNCHD_PLIST.exists():
            return
        self._agent.bootout()

    def _write_plist(self) -> None:
        """Author the LaunchAgent plist on disk with 0644 permissions."""
        _LAUNCHD_DIR.mkdir(parents=True, exist_ok=True)
        _LAUNCHD_PLIST.write_text(self.plist_content())
        _LAUNCHD_PLIST.chmod(0o644)
        logger.info("Wrote plist to %s", _LAUNCHD_PLIST)

    def install(self) -> None:
        """Install the LaunchAgent plist and bring the job up.  No sudo required."""
        self._write_plist()
        self._agent.start()
        logger.info("Bootstrapped and kickstarted %s into launchd", _LABEL)

    def uninstall(self) -> bool:
        """Remove the LaunchAgent plist; return ``kill_stale_daemon()``'s result."""
        if _LAUNCHD_PLIST.exists():
            self._agent.bootout()
            _LAUNCHD_PLIST.unlink(missing_ok=True)
            logger.info("Removed %s", _LAUNCHD_PLIST)
        else:
            logger.info("No plist found at %s -- nothing to uninstall", _LAUNCHD_PLIST)
        return self._process_mgr.kill_stale_daemon()

    def status(self) -> bool:
        """Return True if voxd is loaded under launchd.

        Delegates the ``launchctl list`` probe to the composed agent so this
        backend never shells out to ``launchctl`` itself.
        """
        return self._agent.is_loaded()
