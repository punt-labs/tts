"""System-level WebSocket handlers: chime, voices, health."""
# pyright: reportPrivateUsage=false
# Internal module within the voxd package -- cross-module private access is expected.

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Self

from punt_vox.providers import get_provider
from punt_vox.voxd._parse import parse_required_str
from punt_vox.voxd.chimes import ChimeResolver
from punt_vox.voxd.dedup import ChimeDedup
from punt_vox.voxd.health import DaemonHealth
from punt_vox.voxd.playback import PlaybackItem, PlaybackQueue
from punt_vox.voxd.types import MessageHandler
from punt_vox.voxd.wire_fault import SafeFault
from punt_vox.voxd.wire_reply import WireReply

if TYPE_CHECKING:
    from starlette.websockets import WebSocket

__all__ = ["ChimeHandler", "HealthHandler", "VoicesHandler"]

logger = logging.getLogger(__name__)


class ChimeHandler(MessageHandler):
    """Handle 'chime' messages: play a bundled chime sound."""

    __slots__ = (
        "_chime_dedup",
        "_chimes",
        "_playback",
    )

    _chime_dedup: ChimeDedup
    _chimes: ChimeResolver
    _playback: PlaybackQueue

    def __new__(
        cls,
        *,
        chimes: ChimeResolver,
        chime_dedup: ChimeDedup,
        playback: PlaybackQueue,
    ) -> Self:
        self = super().__new__(cls)
        self._chimes = chimes
        self._chime_dedup = chime_dedup
        self._playback = playback
        return self

    async def __call__(self, msg: dict[str, object], websocket: WebSocket) -> None:
        """Play a bundled chime; reply through the gone-peer-safe WireReply."""
        reply = WireReply(websocket, str(msg.get("id", "")))
        signal = str(msg.get("signal", "done"))
        path = self._chimes.resolve(signal)
        if path is None:
            await reply.error(f"unknown chime: {signal}")
            return

        if not self._chime_dedup.should_play(signal):
            logger.debug("Dedup: skipping duplicate chime %s", signal)
            await reply.send({"type": "done"})
            return

        logger.info("played chime: %s", signal)
        done_event = asyncio.Event()
        await self._playback.enqueue(
            PlaybackItem(path=path, request_id=f"chime:{signal}", notify=done_event)
        )
        await reply.send({"type": "playing"})
        await done_event.wait()
        await reply.send({"type": "done"})


class VoicesHandler(MessageHandler):
    """Handle 'voices' messages: list available voices."""

    __slots__ = ()

    def __new__(cls) -> Self:
        return super().__new__(cls)

    async def __call__(self, msg: dict[str, object], websocket: WebSocket) -> None:
        """List available voices for the requested provider.

        ``provider`` is required on the wire (design §3.7): every
        client fills it from state via :class:`SessionSpec` before
        crossing, so a missing field is a hand-rolled caller and gets
        an id-stamped rejection here rather than a daemon-side guess.
        A known provider with no credentials raises
        :class:`ProviderUnavailableError` from inside
        :meth:`ProviderRegistry.get`; ``ValueError``-family exceptions
        (unknown provider name, unavailable credentials) route
        through ``error()`` with the sentence verbatim, so
        ``mic:voice`` and ``mic:unmute`` see the same message for the
        same underlying condition.
        """
        # Reply via WireReply (a gone peer no-ops, not a teardown), and parse inside
        # the try so domain failures classify like the store handlers.
        reply = WireReply(websocket, str(msg.get("id", "")))
        provider_name = ""
        try:
            provider_name = parse_required_str(msg, "provider")
            provider = get_provider(provider_name)
            voice_list = await asyncio.to_thread(provider.list_voices)
        except (ValueError, LookupError, OSError) as exc:
            # Shared taxonomy: ValueError = rejected client request (WARNING),
            # LookupError/OSError = server-side fault (ERROR).
            await reply.reject_or_fault(exc)
            return
        except Exception:  # PY-EH-6 request-handler boundary: must not tear the socket
            # A provider SDK can raise outside the trio (boto3 ClientError); the
            # router has no guard, so log the traceback and reply a generic fault.
            logger.exception("voices op failed id=%r", reply.request_id)
            await reply.fault(SafeFault.opaque("operation failed"))
            return
        await reply.send(
            {"type": "voices", "provider": provider_name, "voices": voice_list}
        )


class HealthHandler(MessageHandler):
    """Handle 'health' messages over the authenticated WebSocket."""

    __slots__ = ("_health",)

    _health: DaemonHealth

    def __new__(
        cls,
        *,
        health: DaemonHealth,
    ) -> Self:
        self = super().__new__(cls)
        self._health = health
        return self

    async def __call__(self, msg: dict[str, object], websocket: WebSocket) -> None:
        """Return the full health payload through the gone-peer-safe reply channel.

        Routing through :class:`WireReply` means a peer that disconnects before
        the payload lands yields a clean no-op instead of a raw ``send_json``
        raising into the router's broad guard and logging an uncorrelated
        "WebSocket error" traceback.
        """
        reply = WireReply(websocket, str(msg.get("id", "")))
        payload = self._health.full_payload()
        payload["type"] = "health"
        await reply.send(payload)
