"""macOS launchd backend for voxd system service."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING, Self, final

from punt_vox.service.launchctl import LaunchctlAgent
from punt_vox.service.voxd_plist import VoxdPlist

if TYPE_CHECKING:
    from collections.abc import Callable

    # Runtime dependency is injected via __new__; the import is annotation-only,
    # so keeping it out of the runtime graph avoids coupling launchd to process.
    from punt_vox.service.process import ProcessManager

logger = logging.getLogger(__name__)

_LABEL = "com.punt-labs.voxd"
_LAUNCHD_DIR = Path.home() / "Library" / "LaunchAgents"
_LAUNCHD_PLIST = _LAUNCHD_DIR / f"{_LABEL}.plist"


@final
class LaunchdBackend:
    """Drive the voxd LaunchAgent's launchd lifecycle.

    Composes two collaborators and owns neither of their jobs: a
    :class:`VoxdPlist` authors the plist content and file, and a
    :class:`LaunchctlAgent` runs every ``launchctl`` invocation, serialising the
    bootout/bootstrap race that leaves voxd down on a first restart.
    """

    __slots__ = ("_agent", "_plist", "_process_mgr")

    _process_mgr: ProcessManager
    _plist: VoxdPlist
    _agent: LaunchctlAgent

    def __new__(
        cls,
        process_mgr: ProcessManager,
        voxd_exec_args_fn: Callable[[], list[str]],
    ) -> Self:
        self = super().__new__(cls)
        self._process_mgr = process_mgr
        self._plist = VoxdPlist(_LABEL, _LAUNCHD_PLIST, voxd_exec_args_fn)
        self._agent = LaunchctlAgent(_LABEL, str(_LAUNCHD_PLIST))
        return self

    def plist_content(self) -> str:
        """Return the LaunchAgent plist XML the install would write."""
        return self._plist.content()

    def stop(self) -> None:
        """Bootout voxd from launchd if loaded.  Idempotent.

        Called as a pre-flight step by ``install()`` before
        ``ensure_port_free`` so launchd's ``KeepAlive=true`` does not
        respawn the daemon the instant the port-cleanup step kills it.
        The agent waits for the job to actually leave the GUI domain, so a
        following ``bootstrap`` does not race the asynchronous bootout.
        """
        if not self._plist.exists():
            return
        self._agent.bootout()

    def install(self) -> None:
        """Install the LaunchAgent plist and bring the job up.  No sudo required."""
        self._plist.write()
        self._agent.start()
        logger.info("Bootstrapped and kickstarted %s into launchd", _LABEL)

    def uninstall(self) -> bool:
        """Remove the LaunchAgent plist; return ``kill_stale_daemon()``'s result."""
        if self._plist.exists():
            self._agent.bootout()
            self._plist.remove()
        else:
            logger.info(
                "No plist found at %s -- nothing to uninstall", self._plist.path
            )
        return self._process_mgr.kill_stale_daemon()

    def status(self) -> bool:
        """Return True if voxd is loaded under launchd.

        Delegates the ``launchctl list`` probe to the composed agent so this
        backend never shells out to ``launchctl`` itself.
        """
        return self._agent.is_loaded()
