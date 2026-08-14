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

from punt_vox.types_errors import VoiceNotFoundError
from punt_vox.types_provider_errors import ProviderAuthError, ProviderUnavailableError
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
from punt_vox.voxd.wire_fault import SafeFault
from punt_vox.voxd.wire_reply import WireReply

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

        A string-typed field raises ``ValueError`` on a non-string value while a
        numeric field accepts a JSON number (via the parse helpers); empty text
        is rejected here, so both handlers share one text-validation point.
        """
        text = parse_required_str(msg, "text")
        if not text:
            raise ValueError("empty text")
        speaker_boost_raw = msg.get("speaker_boost")
        # ``provider`` is required on the wire (design §3.7): every client
        # surface (MCP, hook, CLI, panel, record) fills it from state via
        # :class:`SessionSpec` before crossing the wire. A hand-rolled
        # client that omits it gets an id-stamped rejection here rather
        # than a daemon-side guess -- the substitution this bead closes.
        spec = SynthesisSpec(
            voice=parse_optional_str(msg, "voice"),
            provider=parse_required_str(msg, "provider"),
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
        """Send *payload* stamped with this request's id, safe on a gone peer.

        Routing through :class:`WireReply` gives this request the same id-stamped,
        disconnect-safe send the store handlers use, instead of a raw
        ``send_json`` that raises out of the handler when the client has left.
        """
        await WireReply(self.websocket, self.request_id).send(payload)

    async def fault(self, fault: SafeFault) -> None:
        """Audit a server-side OPERATIONAL failure and send a prefix-free frame.

        A failed synthesis or a nonzero direct-play exit is a daemon-side fault,
        not a client rejection, so it routes through :meth:`WireReply.fault` (the
        ERROR "operation failed" audit) -- matching the record handler's
        store-write fault -- never a WARNING "rejected op" that blames the client.
        The *fault* carries a prefix-free wire message and the raw log detail, so
        no absolute prefix reaches the client.
        """
        await WireReply(self.websocket, self.request_id).fault(fault)

    async def reject(self, message: str) -> None:
        """Audit a client-side rejection and send the *message* verbatim.

        A synthesis whose named provider has no credentials, whose voice
        the provider does not offer, or whose credentials the provider
        rejects on the wire is a CALLER-side failure: state named
        something the daemon cannot honour. Routing through
        :meth:`WireReply.error` (the ``error`` frame + WARNING
        ``rejected op`` audit line) crosses the sentence verbatim
        rather than laundering it to ``"operation failed"``. The whole
        F2/F3/F5 promise (design §3.5) is that a diagnosable failure
        reaches the caller with a message they can act on.
        """
        await WireReply(self.websocket, self.request_id).error(message)


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
            # WireReply makes a gone-peer send a clean no-op, matching the siblings.
            await WireReply(websocket, str(msg.get("id", ""))).error(str(exc))
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
        if isinstance(result, ProviderUnavailableError | VoiceNotFoundError):
            # Diagnosable, caller-side: the provider has no credentials
            # on this host, or the voice is not in its roster. Route
            # verbatim through error() so the sentence crosses the wire
            # rather than getting laundered to "operation failed" by
            # fault(). See design §3.5, F2/F5.
            self._rollback(req, dedup_recorded=dedup_recorded)
            await req.reject(str(result))
        elif isinstance(result, Exception):
            self._rollback(req, dedup_recorded=dedup_recorded)
            await req.fault(SafeFault.from_exception(result))
        elif result == 0:
            await req.reply({"type": "done"})
        else:
            self._rollback(req, dedup_recorded=dedup_recorded)
            await req.fault(SafeFault.opaque(f"play_directly failed with rc={result}"))
        return True

    async def _synthesize_and_enqueue(
        self, req: _SpeechRequest, *, dedup_recorded: bool
    ) -> None:
        """Synthesize to a file, enqueue it, and drive the playing/done replies."""
        try:
            outcome = await self._synthesis.synthesize_to_file(req.text, req.spec)
        except (ProviderUnavailableError, ProviderAuthError, VoiceNotFoundError) as exc:
            # Diagnosable, caller-side (design §3.5): route verbatim
            # through error() so the sentence naming the missing
            # credential (F2), the rejected credential (F3), or the
            # unrecognised voice (F5) crosses the wire, instead of
            # being laundered by the broad guard below into
            # "operation failed".
            self._rollback(req, dedup_recorded=dedup_recorded)
            logger.warning("Rejected synthesis for id=%r: %s", req.request_id, exc)
            await req.reject(str(exc))
            return
        except Exception as exc:
            self._rollback(req, dedup_recorded=dedup_recorded)
            logger.exception("Synthesis failed for id=%r", req.request_id)
            await req.fault(SafeFault.from_exception(exc))
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
