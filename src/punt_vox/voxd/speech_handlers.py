"""Speech synthesis and recording WebSocket handlers."""
# pyright: reportPrivateUsage=false
# Internal module within the voxd package -- cross-module private access is expected.

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Self

from starlette.websockets import WebSocket, WebSocketDisconnect

from punt_vox.providers import auto_detect_provider
from punt_vox.types_synthesis import SynthesisSpec
from punt_vox.voxd._parse import (
    parse_optional_float,
    parse_optional_int,
    parse_optional_str,
    parse_required_str,
)
from punt_vox.voxd.dedup import OnceDedup
from punt_vox.voxd.playback import PlaybackItem, PlaybackQueue, PlaybackResult
from punt_vox.voxd.synthesis import (  # pyright: ignore[reportPrivateUsage]
    _LOCAL_PROVIDERS,
    SynthesisPipeline,
)
from punt_vox.voxd.types import MessageHandler

__all__ = ["SynthesizeHandler"]

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class _SpeechRequest:
    """One synthesize/record request: the parsed wire fields plus its reply channel.

    Both handlers previously carried an identical copy of the ``SynthesisSpec``
    parsing and the id-stamped reply plumbing. Bundling text, spec, request id,
    and the socket here lets a handler thread one ``req`` through its steps
    instead of four loose primitives, and gives both handlers one parser and one
    reply path.
    """

    text: str
    spec: SynthesisSpec
    request_id: str
    websocket: WebSocket

    @classmethod
    def from_msg(cls, msg: dict[str, object], websocket: WebSocket) -> Self:
        """Parse a wire message into a request, validating at the boundary.

        A non-string typed field raises ``ValueError`` (via the parse helpers)
        and empty text is rejected here, so both handlers share one validation
        point and neither needs its own text guard.
        """
        text = parse_required_str(msg, "text")
        if not text:
            raise ValueError("empty text")
        speaker_boost_raw = msg.get("speaker_boost")
        spec = SynthesisSpec(
            voice=parse_optional_str(msg, "voice"),
            provider=parse_optional_str(msg, "provider") or auto_detect_provider(),
            model=parse_optional_str(msg, "model"),
            rate=parse_optional_int(msg, "rate"),
            language=parse_optional_str(msg, "language"),
            vibe_tags=parse_optional_str(msg, "vibe_tags"),
            stability=parse_optional_float(msg, "stability"),
            similarity=parse_optional_float(msg, "similarity"),
            style=parse_optional_float(msg, "style"),
            speaker_boost=(
                bool(speaker_boost_raw) if speaker_boost_raw is not None else None
            ),
            api_key=parse_optional_str(msg, "api_key"),
        )
        return cls(
            text=text,
            spec=spec,
            request_id=str(msg.get("id", "")),
            websocket=websocket,
        )

    async def reply(self, payload: dict[str, object]) -> None:
        """Send *payload* to the client, stamped with this request's id."""
        await self.websocket.send_json({"id": self.request_id, **payload})

    async def error(self, message: str) -> None:
        """Send an error reply for this request."""
        await self.reply({"type": "error", "message": message})


class SynthesizeHandler(MessageHandler):
    """Handle 'synthesize' WebSocket messages: TTS + enqueue playback."""

    __slots__ = (
        "_once_dedup",
        "_playback",
        "_synthesis",
    )

    _once_dedup: OnceDedup
    _playback: PlaybackQueue
    _synthesis: SynthesisPipeline

    def __new__(
        cls,
        *,
        synthesis: SynthesisPipeline,
        playback: PlaybackQueue,
        once_dedup: OnceDedup,
    ) -> Self:
        self = super().__new__(cls)
        self._synthesis = synthesis
        self._playback = playback
        self._once_dedup = once_dedup
        return self

    async def __call__(self, msg: dict[str, object], websocket: WebSocket) -> None:
        """Synthesize speech and enqueue for playback."""
        # Parse at the boundary: a non-string wire field (or empty text) is an
        # id-stamped error frame, never a ValueError that tears the connection down.
        try:
            req = _SpeechRequest.from_msg(msg, websocket)
            once = parse_optional_int(msg, "once")
        except ValueError as exc:
            await websocket.send_json(
                {"id": str(msg.get("id", "")), "type": "error", "message": str(exc)}
            )
            return

        dedup_recorded = await self._respond_if_deduped(req, once)
        if dedup_recorded is None:
            return

        logger.info(
            "Synthesize: id=%r provider=%r voice=%r chars=%d",
            req.request_id,
            req.spec.provider or "",
            req.spec.voice or "",
            len(req.text),
        )
        await self._dispatch(req, dedup_recorded=dedup_recorded)

    async def _dispatch(self, req: _SpeechRequest, *, dedup_recorded: bool) -> None:
        """Play a local provider directly, else synthesize to a file and enqueue."""
        local = (req.spec.provider or "") in _LOCAL_PROVIDERS
        if local and await self._play_local(req, dedup_recorded=dedup_recorded):
            return
        await self._synthesize_and_enqueue(req, dedup_recorded=dedup_recorded)

    async def _respond_if_deduped(
        self, req: _SpeechRequest, once: int | None
    ) -> bool | None:
        """Check the once-dedup window.

        Return ``None`` when the request was a dedup hit (a 'done' reply was sent
        and the caller must stop); otherwise return whether this call recorded a
        dedup entry that a later failure must roll back.
        """
        if once is None or once <= 0:
            return False
        hit = self._once_dedup.check_and_record(req.text, float(once))
        if hit is None:
            return True
        logger.info(
            "Dedup hit: id=%r text=%d chars original=%.3f ttl_remaining=%.1fs",
            req.request_id,
            len(req.text),
            hit.original_played_at,
            hit.ttl_seconds_remaining,
        )
        await req.reply(
            {
                "type": "done",
                "deduped": True,
                "original_played_at": hit.original_played_at,
                "ttl_seconds_remaining": hit.ttl_seconds_remaining,
            }
        )
        return None

    async def _play_local(self, req: _SpeechRequest, *, dedup_recorded: bool) -> bool:
        """Play a local provider (espeak/say) straight to the device.

        Return True when the request was fully handled (a terminal reply was
        sent); False when no local path applied and the caller should fall
        through to file synthesis.
        """
        result = await self._synthesis.try_direct_play(
            req.text, req.spec, record_result=self._record_playback_result
        )
        if result is None:
            return False
        if isinstance(result, Exception):
            self._rollback(req, dedup_recorded=dedup_recorded)
            await req.error(str(result))
        elif result == 0:
            await req.reply({"type": "done"})
        else:
            self._rollback(req, dedup_recorded=dedup_recorded)
            await req.error(f"play_directly failed with rc={result}")
        return True

    async def _synthesize_and_enqueue(
        self, req: _SpeechRequest, *, dedup_recorded: bool
    ) -> None:
        """Synthesize to a file, enqueue it, and drive the playing/done replies."""
        try:
            outcome = await self._synthesis.synthesize_to_file(req.text, req.spec)
        except Exception as exc:
            self._rollback(req, dedup_recorded=dedup_recorded)
            logger.exception("Synthesis failed for id=%r", req.request_id)
            await req.error(str(exc))
            return

        # `cached` rides the 'playing' response (the client's terminal).
        done_event = asyncio.Event()
        item = PlaybackItem(
            path=outcome.path, request_id=req.request_id, notify=done_event
        )
        await self._playback.enqueue(item)
        await req.reply({"type": "playing", "cached": outcome.cached})
        await done_event.wait()
        with contextlib.suppress(WebSocketDisconnect, RuntimeError):
            await req.reply({"type": "done"})

    def _rollback(self, req: _SpeechRequest, *, dedup_recorded: bool) -> None:
        """Undo a recorded dedup entry when synthesis fails after recording it."""
        if dedup_recorded:
            self._once_dedup.rollback(req.text)

    def _record_playback_result(
        self, *, path: Path, rc: int, elapsed: float, stderr: str
    ) -> None:
        """Update the playback queue's last_result with a freshly-observed result."""
        self._playback.set_last_result(
            PlaybackResult(
                path=path,
                rc=rc,
                elapsed_s=round(elapsed, 4),
                stderr=stderr,
                ts=time.time(),
            )
        )
