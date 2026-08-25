"""The daemon-backed audible cues a call speaks and chimes through.

Both cues share the same two collaborators -- the blocking
:class:`~punt_vox.client_sync.VoxClientSync` and the call's resolved
:class:`~punt_vox.types_synthesis.SynthesisSpec` -- and the same
"``asyncio.to_thread``, not a bare call" rationale (the client blocks its
calling thread for the full round trip to the daemon, and both cues run on
the call's single event loop). Bundled as methods on one class rather than
two module-level closures over the same captured state -- the free-function
version this replaced is exactly the pattern PY-OO-7 flags: two functions
that take no parameters of their own but close over identical shared state
belong together as a class.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Self, final

if TYPE_CHECKING:
    from punt_vox.client_sync import VoxClientSync
    from punt_vox.types_synthesis import SynthesisSpec

__all__ = ["DaemonCues"]

# VoxClient.synthesize's own default (client.py's _TIMEOUT_SYNTHESIS, 30s)
# bounds the wait for voxd's "playing" ack, which the daemon does not send
# until synthesis itself has actually finished -- a long reply's real
# ElevenLabs synthesis time can exceed 30s outright (a 1133-char reply
# measured ~43s), killing the call mid-reply with VoxdProtocolError. A call
# is exactly the case that needs a longer bound: a human is waiting live on
# the reply, and a longer wait is a far better experience than a killed
# call, since this timeout exists to bound a genuine hang, not to cap
# legitimate long-running synthesis.
_SPEAK_TIMEOUT_S = 120.0


@final
class DaemonCues:
    """Speak text and play the background wait chime through one daemon client."""

    __slots__ = ("_client", "_spec")
    _client: VoxClientSync
    _spec: SynthesisSpec

    def __new__(cls, client: VoxClientSync, spec: SynthesisSpec) -> Self:
        self = super().__new__(cls)
        self._client = client
        self._spec = spec
        return self

    async def speak(self, text: str) -> None:
        """Synthesize and play *text* -- :class:`~.call_session.SpeakFn`'s contract.

        Discards ``SynthesizeResult`` -- ``SpeakFn``'s contract is "spoken",
        not "the daemon's synthesis metadata", and a call orchestrator has
        no use for it. ``timeout=_SPEAK_TIMEOUT_S``, not the client's own
        30s default -- see that constant's own comment.
        """
        await asyncio.to_thread(
            self._client.synthesize, text, self._spec, timeout=_SPEAK_TIMEOUT_S
        )

    async def chime(self) -> None:
        """Play the background wait cue -- :class:`~.wait_cue.ChimeFn`'s contract.

        Uses the daemon's existing "acknowledge" chime asset
        (:mod:`punt_vox.voxd.chimes`'s ``_CHIME_MAP``), not a new synthesis
        -- see :mod:`~.wait_cue`'s module docstring for why.
        """
        await asyncio.to_thread(self._client.chime, "acknowledge")
