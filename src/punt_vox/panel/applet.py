"""``VoxPanelApplet`` -- assembles the panel for a session, and the CLI that runs it.

Modeled directly on lux's own ``lux-beads`` applet (``punt_lux.applets.beads``):
one program, one session, one entry in the Lux menu, spawned by the session's
own session-start hook because ``voxd`` cannot do this -- launchd starts it
with no repository working directory, while a session has one.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from typing import Self, final

import typer
from punt_lux.applets import AppletIdentity
from punt_lux.applets.claim import NoClaim, SessionClaim
from punt_lux.applets.watch import NoSession, SessionWatch
from punt_lux.log_level import level_from_env

from punt_vox.panel.leg import VoxPanelLeg
from punt_vox.panel.program import VoxPanelProgram
from punt_vox.panel.service import VoxPanelService
from punt_vox.panel.topics import PanelTopic

logger = logging.getLogger(__name__)

__all__ = ["VoxPanelApplet", "app"]

app = typer.Typer(
    name="vox-panel",
    help="The Vox control panel: notifications, mic mode, and voice, one click away.",
    add_completion=False,
)

# What this program is called wherever a session's files are named after it: its
# console script and the claim it takes on the session it serves.
_PROGRAM = "vox-panel"


@final
class VoxPanelApplet:
    """The Vox control panel applet: how it is assembled for a session."""

    _program: VoxPanelProgram
    __slots__ = ("_program",)

    def __new__(cls, program: VoxPanelProgram) -> Self:
        self = super().__new__(cls)
        self._program = program
        return self

    @classmethod
    def for_session(cls, session_pid: int) -> Self:
        """Assemble the applet for the session that spawned it, and bound to it."""
        return cls(
            VoxPanelProgram(
                SessionClaim.for_session(_PROGRAM, session_pid),
                cls._leg_for(session_pid),
                SessionWatch(session_pid),
            )
        )

    @classmethod
    def unattended(cls) -> Self:
        """Assemble the applet with nothing to outlive: a hand-run invocation."""
        return cls(VoxPanelProgram(NoClaim(), cls._leg_for(os.getpid()), NoSession()))

    @staticmethod
    def _leg_for(session_pid: int) -> VoxPanelLeg:
        """The leg this applet serves on, identified to the Hub by its session."""
        identity = AppletIdentity.for_session(session_pid)
        topics = tuple(topic.value for topic in PanelTopic)
        return VoxPanelLeg(identity.client, VoxPanelService(), topics=topics)

    async def run(self) -> None:
        """Run the applet, which is to say run its program."""
        await self._program.run()


@app.command()
def main(
    session_pid: int = typer.Option(
        0,
        "--session-pid",
        help="The Claude Code process to live alongside; 0 runs until killed.",
    ),
) -> None:
    """Run the Vox control panel applet for one session."""
    logging.basicConfig(
        stream=sys.stderr,
        level=level_from_env("INFO"),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    applet = (
        VoxPanelApplet.for_session(session_pid)
        if session_pid > 0
        else VoxPanelApplet.unattended()
    )
    asyncio.run(applet.run())


if __name__ == "__main__":
    app()
