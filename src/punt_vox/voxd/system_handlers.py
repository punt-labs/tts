"""System-level WebSocket handlers: chime, voices, health."""
# pyright: reportPrivateUsage=false
# Internal module within the voxd package -- cross-module private access is expected.

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import Self

from starlette.websockets import WebSocket, WebSocketDisconnect

from punt_vox.providers import auto_detect_provider, get_provider
from punt_vox.voxd._parse import parse_optional_str
from punt_vox.voxd.chimes import ChimeResolver
from punt_vox.voxd.dedup import ChimeDedup
from punt_vox.voxd.health import DaemonHealth
from punt_vox.voxd.playback import PlaybackItem, PlaybackQueue
from punt_vox.voxd.types import MessageHandler
from punt_vox.voxd.wire_reply import WireReply

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
        """Play a bundled chime sound."""
        signal = str(msg.get("signal", "done"))
        path = self._chimes.resolve(signal)
        if path is None:
            logger.warning("Unknown chime signal: %r", signal)
            await websocket.send_json(
                {"type": "error", "id": "", "message": f"unknown chime: {signal}"}
            )
            return

        if not self._chime_dedup.should_play(signal):
            logger.debug("Dedup: skipping duplicate chime %s", signal)
            await websocket.send_json({"type": "done", "id": ""})
            return

        logger.info("played chime: %s", signal)
        done_event = asyncio.Event()
        await self._playback.enqueue(
            PlaybackItem(path=path, request_id=f"chime:{signal}", notify=done_event)
        )
        await websocket.send_json({"type": "playing", "id": f"chime:{signal}"})
        await done_event.wait()
        with contextlib.suppress(WebSocketDisconnect, RuntimeError):
            await websocket.send_json({"type": "done", "id": f"chime:{signal}"})


class VoicesHandler(MessageHandler):
    """Handle 'voices' messages: list available voices."""

    __slots__ = ()

    def __new__(cls) -> Self:
        return super().__new__(cls)

    async def __call__(self, msg: dict[str, object], websocket: WebSocket) -> None:
        """List available voices for the requested provider."""
        # Reply through WireReply so both frames survive a gone peer -- a send on
        # a disconnected socket no-ops rather than raising WebSocketDisconnect out
        # of the handler and tearing the connection down, matching the store
        # handlers. Parse inside the try so a non-string provider or a provider
        # fault is an id-stamped error frame; provider_name is bound first so the
        # except message is always defined.
        reply = WireReply(websocket, str(msg.get("id", "")))
        provider_name = ""
        try:
            provider_name = (
                parse_optional_str(msg, "provider") or auto_detect_provider()
            )
            provider = get_provider(provider_name, config_dir=None)
            voice_list = await asyncio.to_thread(provider.list_voices)
        except ValueError as exc:
            # A parse rejection (non-string provider) or an unknown provider name
            # is a client request error, not an operational fault: reply.error is
            # id-stamped, disconnect-safe, and WARNING-audited -- no full traceback.
            await reply.error(str(exc))
            return
        except Exception as exc:
            logger.exception("Voice listing failed for provider=%r", provider_name)
            await reply.send(
                {"type": "error", "message": f"voice listing failed: {exc}"}
            )
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

    async def __call__(
        self,
        msg: dict[str, object],  # noqa: ARG002
        websocket: WebSocket,
    ) -> None:
        """Return full health payload."""
        payload = self._health.full_payload()
        payload["type"] = "health"
        await websocket.send_json(payload)
