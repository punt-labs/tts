"""WebSocket client for voxd audio daemon.

Lightweight -- imports only stdlib + websockets.
Used by the MCP server, hook handlers, and CLI commands.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import json
import logging
import os
import shutil
import tempfile
import uuid
from collections.abc import Generator
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Self

import websockets
import websockets.asyncio.client

if TYPE_CHECKING:
    from types import TracebackType

from punt_vox.bare_name import BareName
from punt_vox.client_env import DaemonEnv
from punt_vox.client_errors import VoxdConnectionError, VoxdProtocolError
from punt_vox.paths import run_dir as _user_run_dir
from punt_vox.types_programs import (
    CommandOutcome,
    HealthStatus,
    JsonObject,
    ProgramStatus,
    ProgramSummary,
    PromptSet,
)
from punt_vox.types_synthesis import SynthesisSpec

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RecordResult:
    """Locator for a recording the daemon stored -- never a client path.

    ``record`` captures to the daemon-owned store; the client never names a
    daemon path. ``id`` and ``name`` are the store reference (the store
    filename) a caller passes to :meth:`VoxClient.play` or
    :meth:`VoxClient.fetch`. ``store_path`` is the daemon-side path -- usable
    directly when the client shares the daemon's filesystem, and the source for
    a local :meth:`VoxClient.fetch` copy. ``byte_count`` is the size the daemon
    wrote; ``cached`` reports a content-addressed cache hit.
    """

    id: str
    name: str
    store_path: Path
    byte_count: int
    cached: bool = False


@dataclass(frozen=True, slots=True)
class RecordingSummary:
    """One recording in the store's list: its bare name and byte count."""

    name: str
    byte_count: int


@dataclass(frozen=True, slots=True)
class SynthesizeResult:
    """Result of a ``synthesize`` call on voxd.

    ``deduped`` is True when the caller passed an ``once=<ttl>`` window and
    the server found an identical text already played within it -- the
    audio was NOT re-played; treat it as success, not an error. Non-deduped
    results leave ``original_played_at`` and ``ttl_seconds_remaining`` None.

    ``cached`` reports whether voxd served the audio from its
    content-addressed cache (True) or synthesized it fresh (False) -- the
    observability signal a caller asserts to confirm a cache hit. A dedup
    short-circuit leaves it False since no synthesis path ran.
    """

    request_id: str
    deduped: bool = False
    original_played_at: float | None = None
    ttl_seconds_remaining: float | None = None
    cached: bool = False


# ---------------------------------------------------------------------------
# Timeouts — values in seconds
# ---------------------------------------------------------------------------

_TIMEOUT_SYNTHESIS = 30.0
_TIMEOUT_SHORT = 5.0
# play waits for the host-side playback to FINISH (not just enqueue) so a
# failure surfaces, so its deadline must cover a full track. Bounded like the
# record cap so a wedged daemon is still detected within ten minutes.
_TIMEOUT_PLAYBACK = 600.0
# fetch reassembles the chunked stream (fetch_begin -> chunk* -> fetch_end); the
# 120s is a whole-stream deadline, not per-frame, so a large recording over the
# documented remote/SSH-tunnel path must finish within it. A cold one-shot
# retrieval affords the wait; raise it if a big transfer is abandoned mid-stream.
_TIMEOUT_FETCH = 120.0

# record synthesizes to a file that may take minutes for long text (a fresh
# 6000-char ElevenLabs synthesis was measured at ~2m). Scale the wait with the
# text length rather than a fixed cap so a legitimate long synthesis is never
# abandoned, while an absolute ceiling still detects a hung daemon within a
# bounded window regardless of input length.
_RECORD_TIMEOUT_BASE = 60.0
_RECORD_TIMEOUT_PER_CHAR = 0.05
_RECORD_TIMEOUT_MAX = 600.0

# music_new generates one ElevenLabs track (measured minutes for a fresh track);
# the daemon acks 'generating' first, but the client still bounds the wait so a
# wedged provider is detected. Generous, matching the record ceiling.
_TIMEOUT_MUSIC_NEW = 600.0

# ---------------------------------------------------------------------------
# Path resolution — delegated to punt_vox.paths so every module agrees.
# ``~/.punt-labs/vox/run`` holds ``serve.port`` / ``serve.token``.
# ---------------------------------------------------------------------------


def read_port_file() -> int | None:
    """Read the daemon port from the port file. Returns None if missing."""
    port_file = _user_run_dir() / "serve.port"
    try:
        return int(port_file.read_text().strip())
    except (FileNotFoundError, ValueError, OSError):
        return None


def read_token_file() -> str | None:
    """Read the daemon auth token. Returns None if missing."""
    token_file = _user_run_dir() / "serve.token"
    try:
        return token_file.read_text().strip()
    except (FileNotFoundError, OSError):
        return None


# ---------------------------------------------------------------------------
# Transport — owns the WebSocket connection and the wire I/O primitives.
# ---------------------------------------------------------------------------


class _VoxdTransport:
    """Own the voxd WebSocket: URI resolution, connect/close, send/recv.

    Split out of :class:`VoxClient` so the transport (connection lifecycle
    and framing) and the RPC surface (synthesize, program, ...) are two
    single-purpose objects rather than one class doing both jobs.
    """

    __slots__ = (
        "_explicit_port",
        "_explicit_token",
        "_host",
        "_port",
        "_token",
        "_ws",
    )

    _host: str
    _explicit_port: int | None
    _explicit_token: str | None
    _port: int | None
    _token: str | None
    _ws: websockets.asyncio.client.ClientConnection | None

    def __new__(
        cls,
        host: str | None,
        port: int | None,
        token: str | None,
    ) -> Self:
        self = super().__new__(cls)
        self._host = host if host is not None else DaemonEnv.host()
        self._explicit_port = port
        self._explicit_token = token
        self._port = port
        self._token = token
        self._ws = None
        return self

    def _resolve_port(self) -> int:
        """Return the port, reading from file if needed."""
        if self._port is not None:
            return self._port
        env = DaemonEnv.port()
        if env is not None:
            return env
        port = read_port_file()
        if port is None:
            msg = "voxd port file not found. Is the daemon running? Start it with: voxd"
            raise VoxdConnectionError(msg)
        return port

    def _resolve_token(self) -> str | None:
        """Return the token, reading from file if needed."""
        if self._token is not None:
            return self._token
        env = DaemonEnv.token()
        if env is not None:
            return env
        return read_token_file()

    def _build_uri(self) -> str:
        """Build the WebSocket URI with auth token."""
        port = self._resolve_port()
        token = self._resolve_token()
        uri = f"ws://{self._host}:{port}/ws"
        if token:
            uri += f"?token={token}"
        return uri

    async def connect(self) -> None:
        """Connect to voxd WebSocket. Call once before sending messages."""
        uri = self._build_uri()
        try:
            self._ws = await websockets.asyncio.client.connect(uri)
        except OSError as exc:
            msg = f"Cannot connect to voxd at {uri.split('?')[0]}: {exc}"
            raise VoxdConnectionError(msg) from exc

    async def close(self) -> None:
        """Close the WebSocket connection."""
        if self._ws is not None:
            await self._ws.close()
            self._ws = None

    async def _ensure_connected(self) -> websockets.asyncio.client.ClientConnection:
        """Return the active connection, reconnecting if needed.

        On reconnect, re-reads port and token files (daemon may have
        restarted on a different port).
        """
        ws = self._ws
        if ws is not None:
            try:
                await ws.ping()
                return ws
            except (OSError, websockets.exceptions.WebSocketException):
                # Connection is dead; fall through to reconnect.
                self._ws = None

        # Restore explicit values; re-read from disk only if None.
        self._port = self._explicit_port
        self._token = self._explicit_token
        await self.connect()
        # connect() sets self._ws or raises VoxdConnectionError.
        ws = self._ws
        if ws is None:
            msg = "connect() succeeded but self._ws is None"
            raise VoxdConnectionError(msg)
        return ws

    async def send_and_recv(
        self,
        msg: dict[str, object],
        *,
        timeout: float = _TIMEOUT_SHORT,
    ) -> dict[str, Any]:
        """Send a JSON message and wait for a single JSON response."""
        ws = await self._ensure_connected()
        await self._send_frame(ws, msg)
        msg_type = str(msg.get("type", ""))
        raw = await self._recv_frame(
            ws, timeout, f"Timeout waiting for response to '{msg_type}'", msg
        )
        return self._decode(raw)

    async def _send_frame(
        self, ws: websockets.asyncio.client.ClientConnection, msg: dict[str, object]
    ) -> None:
        """Send one JSON frame, wrapping a lost connection as a ``VoxError``."""
        try:
            await ws.send(json.dumps(msg))
        except (websockets.exceptions.WebSocketException, OSError) as exc:
            raise VoxdConnectionError(self._transport_failure(msg, exc)) from exc

    async def _recv_frame(
        self,
        ws: websockets.asyncio.client.ClientConnection,
        timeout: float,
        timeout_msg: str,
        msg: dict[str, object],
    ) -> object:
        """Receive one frame within *timeout*, wrapping every transport failure.

        A ``ConnectionClosed`` or ``OSError`` from ``ws.recv()`` must surface as
        a :class:`VoxError` -- otherwise it escapes a CLI command's
        ``(VoxdConnectionError, VoxdProtocolError)`` catch as a raw traceback.
        A timeout is a protocol failure; a dropped socket is a connection one.
        """
        try:
            return await asyncio.wait_for(ws.recv(), timeout=timeout)
        except TimeoutError as exc:
            raise VoxdProtocolError(timeout_msg) from exc
        except (websockets.exceptions.WebSocketException, OSError) as exc:
            raise VoxdConnectionError(self._transport_failure(msg, exc)) from exc

    @staticmethod
    def _transport_failure(msg: dict[str, object], exc: Exception) -> str:
        """Describe a lost/closed connection for a wrapped transport error."""
        msg_type = str(msg.get("type", ""))
        return f"connection to voxd lost during '{msg_type}': {exc}"

    @staticmethod
    def _decode(raw: object) -> dict[str, Any]:
        """Parse a wire frame, raising ``VoxdProtocolError`` on bad JSON or error.

        The one place both drain and single-response paths turn a malformed or
        truncated frame, and ``voxd``'s ``{"type": "error", "message": ...}``,
        into a raised protocol error -- so a non-JSON frame surfaces as a
        one-line ``VoxError`` instead of a raw ``JSONDecodeError`` traceback, and
        the error contract lives in a single spot rather than at every site.
        """
        try:
            resp: dict[str, Any] = json.loads(str(raw))
        except json.JSONDecodeError as exc:
            raise VoxdProtocolError(f"invalid JSON from voxd: {exc}") from exc
        if resp.get("type") == "error":
            raise VoxdProtocolError(str(resp.get("message", "unknown error")))
        return resp

    async def fetch_stream(
        self, msg: dict[str, object], *, timeout: float = _TIMEOUT_FETCH
    ) -> bytes:
        """Send a fetch request and reassemble the chunked stream into bytes.

        Consumes ``fetch_begin`` -> ordered ``chunk``* -> ``fetch_end``, appending
        one frame at a time so client memory holds only the reassembled file plus
        a single chunk. The running total is checked against the declared byte
        count and the sha256 is verified, so a short, reordered, or corrupted
        stream raises. A mid-stream ``error`` frame surfaces through ``_decode`` as
        a :class:`VoxdProtocolError`, and the partial buffer is discarded.
        """
        ws = await self._ensure_connected()
        await self._send_frame(ws, msg)
        deadline = asyncio.get_running_loop().time() + timeout
        begin = self._decode(await self._recv(ws, deadline, "fetch_begin", msg))
        if begin.get("type") != "fetch_begin":
            got = begin.get("type")
            raise VoxdProtocolError(f"expected 'fetch_begin', got {got!r}")
        declared, expected_chunks, digest = self._begin_fields(begin)
        buffer, hasher = bytearray(), hashlib.sha256()
        for seq in range(expected_chunks):
            frame = self._decode(await self._recv(ws, deadline, "chunk", msg))
            self._append_chunk(frame, seq, buffer, hasher)
        end = self._decode(await self._recv(ws, deadline, "fetch_end", msg))
        if end.get("type") != "fetch_end":
            raise VoxdProtocolError(f"expected 'fetch_end', got {end.get('type')!r}")
        self._verify(buffer, declared, hasher, digest)
        return bytes(buffer)

    async def _recv(
        self,
        ws: websockets.asyncio.client.ClientConnection,
        deadline: float,
        expected: str,
        msg: dict[str, object],
    ) -> object:
        """Receive one frame before *deadline*, or raise a timeout protocol error."""
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            raise VoxdProtocolError(f"timeout waiting for '{expected}' in a fetch")
        return await self._recv_frame(
            ws, remaining, f"timeout waiting for '{expected}' in a fetch", msg
        )

    @staticmethod
    def _begin_fields(begin: dict[str, Any]) -> tuple[int, int, str]:
        """Return ``(declared_bytes, chunk_count, sha256)`` from a fetch_begin frame."""
        try:
            return int(begin["bytes"]), int(begin["chunks"]), str(begin["sha256"])
        except (KeyError, TypeError, ValueError) as exc:
            raise VoxdProtocolError(f"malformed 'fetch_begin': {exc}") from exc

    @staticmethod
    def _append_chunk(
        frame: dict[str, Any], seq: int, buffer: bytearray, hasher: Any
    ) -> None:
        """Validate one chunk's order and append its decoded bytes to *buffer*."""
        if frame.get("type") != "chunk" or frame.get("seq") != seq:
            raise VoxdProtocolError(
                f"expected chunk seq {seq}, got "
                f"{frame.get('type')!r} seq {frame.get('seq')!r}"
            )
        try:
            data = base64.b64decode(str(frame["data"]), validate=True)
        except (KeyError, ValueError, TypeError) as exc:
            raise VoxdProtocolError(f"chunk {seq} has invalid base64: {exc}") from exc
        buffer.extend(data)
        hasher.update(data)

    @staticmethod
    def _verify(buffer: bytearray, declared: int, hasher: Any, digest: str) -> None:
        """Raise unless the reassembly matches the declared byte count and sha256."""
        if len(buffer) != declared:
            raise VoxdProtocolError(
                f"fetch byte-count mismatch: got {len(buffer)}, declared {declared}"
            )
        if hasher.hexdigest() != digest:
            raise VoxdProtocolError("fetch sha256 mismatch: stream corrupted")

    async def send_and_drain(
        self,
        msg: dict[str, object],
        *,
        timeout: float = _TIMEOUT_SYNTHESIS,
        terminal_type: str = "done",
        early_terminal: str | None = None,
    ) -> list[dict[str, Any]]:
        """Send a message and collect responses until a terminal response arrives.

        Stops when the response type matches *terminal_type* or, when set,
        *early_terminal*. Used for synthesize/chime: the server sends
        'playing' once synthesis is enqueued, then 'done' when playback
        finishes. Passing early_terminal='playing' lets the client return
        as soon as the audio is queued without waiting for playback.
        Dedup short-circuits still send 'done' directly (no 'playing'),
        so terminal_type='done' handles that path correctly.
        """
        ws = await self._ensure_connected()
        await self._send_frame(ws, msg)
        responses: list[dict[str, Any]] = []
        deadline = asyncio.get_running_loop().time() + timeout
        # One message serves both timeout paths (deadline exceeded, recv timeout).
        expected = early_terminal or terminal_type
        timeout_msg = f"Timeout waiting for '{expected}' in '{msg.get('type', '')}'"
        while True:
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                raise VoxdProtocolError(timeout_msg)
            raw = await self._recv_frame(ws, remaining, timeout_msg, msg)
            resp = self._decode(raw)  # wraps bad JSON + error frames as VoxError
            responses.append(resp)
            resp_type = resp.get("type")
            if resp_type == terminal_type:
                return responses
            if early_terminal is not None and resp_type == early_terminal:
                # Server will still send terminal_type (e.g., 'done' after
                # 'playing'). Close the connection so that stale message
                # cannot be consumed by the next request on a reused
                # connection. Suppress close errors — the connection may
                # already be gone, but the audio is queued.
                with contextlib.suppress(Exception):
                    await ws.close()
                self._ws = None
                return responses


# ---------------------------------------------------------------------------
# Async client
# ---------------------------------------------------------------------------


class VoxClient:
    """Asynchronous client for the voxd audio daemon.

    Speaks voxd's RPC surface over one WebSocket connection: play speech
    (:meth:`synthesize`), play a bundled chime (:meth:`chime`), synthesize
    to MP3 bytes (:meth:`record`), list voices (:meth:`voices`), read daemon
    health (:meth:`health`), and drive the audio *program* controls
    (:meth:`program_on`, :meth:`program_status`, and the rest).

    Lifecycle: call :meth:`connect` once before the first request and
    :meth:`close` when finished, or use the client as an async context
    manager, which connects on entry and closes on exit::

        async with VoxClient() as vox:
            await vox.synthesize("build finished")

    A single client is meant to be reused across many calls; it re-reads the
    daemon's port and reconnects on its own if voxd restarts between
    requests. Every failure raises a :class:`~punt_vox.VoxError`
    (:class:`~punt_vox.VoxdConnectionError` when the daemon is unreachable,
    :class:`~punt_vox.VoxdProtocolError` on an unexpected reply).

    With no *host*/*port*/*token*, the client resolves them from the
    ``VOXD_HOST``/``VOXD_PORT``/``VOXD_TOKEN`` environment variables and the
    daemon's run-directory files -- the usual local-daemon case.
    """

    __slots__ = ("_transport",)

    _transport: _VoxdTransport

    def __new__(
        cls,
        host: str | None = None,
        port: int | None = None,
        token: str | None = None,
    ) -> Self:
        self = super().__new__(cls)
        self._transport = _VoxdTransport(host, port, token)
        return self

    # -- connection lifecycle ------------------------------------------------

    async def connect(self) -> None:
        """Connect to voxd WebSocket. Call once before sending messages."""
        await self._transport.connect()

    async def close(self) -> None:
        """Close the WebSocket connection."""
        await self._transport.close()

    async def __aenter__(self) -> Self:
        """Connect on entry so the client is ready to send."""
        await self.connect()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        """Close the connection on exit, whether or not the body raised."""
        await self.close()

    # -- public API ----------------------------------------------------------

    async def synthesize(
        self,
        text: str,
        spec: SynthesisSpec | None = None,
        *,
        once: int | None = None,
    ) -> SynthesizeResult:
        """Send synthesize request. Audio plays on server.

        *spec* bundles the voice/provider/rate parameters; *once* is the dedup
        TTL window (seconds). Returns a ``SynthesizeResult`` carrying the request
        id, ``cached`` (cache-hit signal), and any dedup status.
        """
        request_id = uuid.uuid4().hex[:12]
        msg: dict[str, object] = {
            "type": "synthesize",
            "id": request_id,
            "text": text,
            **(spec or SynthesisSpec()).to_client_kwargs(),
        }
        if once is not None:
            msg["once"] = once

        responses = await self._transport.send_and_drain(
            msg,
            timeout=_TIMEOUT_SYNTHESIS,
            terminal_type="done",
            early_terminal="playing",
        )
        terminal = responses[-1] if responses else {}
        if terminal.get("deduped"):
            return SynthesizeResult(
                request_id=request_id,
                deduped=True,
                original_played_at=(
                    float(terminal["original_played_at"])
                    if "original_played_at" in terminal
                    else None
                ),
                ttl_seconds_remaining=(
                    float(terminal["ttl_seconds_remaining"])
                    if "ttl_seconds_remaining" in terminal
                    else None
                ),
            )
        return SynthesizeResult(
            request_id=request_id,
            cached=bool(terminal.get("cached", False)),
        )

    async def chime(self, signal: str) -> None:
        """Play a bundled chime asset."""
        msg: dict[str, object] = {"type": "chime", "signal": signal}
        await self._transport.send_and_drain(
            msg, timeout=_TIMEOUT_SHORT, terminal_type="done", early_terminal="playing"
        )

    async def record(
        self,
        text: str,
        spec: SynthesisSpec | None = None,
        *,
        name: str | None = None,
    ) -> RecordResult:
        """Synthesize into the daemon's store; return a locator, not a path.

        No audio crosses the wire and the client names no daemon path. The
        daemon stores the MP3 under a validated bare *name* or, when that is
        None, a content-addressed name, and returns a :class:`RecordResult`
        locator (store id/name, store path, byte count). *spec* bundles the
        voice/provider/rate parameters. The response deadline scales with the
        text length (bounded) so a long synthesis is not abandoned by a
        premature timeout.
        """
        request_id = uuid.uuid4().hex[:12]
        msg: dict[str, object] = {
            "type": "record",
            "id": request_id,
            "text": text,
            **(spec or SynthesisSpec()).to_client_kwargs(),
        }
        # Send name whenever it is not None -- including an explicit "" -- so
        # the daemon is the single authority on name validity: it rejects an
        # empty name pre-ack. Only an absent name (None) is content-addressed.
        if name is not None:
            msg["name"] = name

        scaled = _RECORD_TIMEOUT_BASE + _RECORD_TIMEOUT_PER_CHAR * len(text)
        timeout = min(scaled, _RECORD_TIMEOUT_MAX)
        responses = await self._transport.send_and_drain(
            msg, timeout=timeout, terminal_type="audio"
        )
        terminal = responses[-1] if responses else {}
        if terminal.get("type") != "audio":
            raise VoxdProtocolError(
                f"Expected 'audio' response, got '{terminal.get('type')}'"
            )
        for field in ("name", "path", "bytes"):
            if field not in terminal:
                raise VoxdProtocolError(f"'audio' response missing {field!r}")
        try:
            byte_count = int(terminal["bytes"])
        except (TypeError, ValueError) as exc:
            # A non-int bytes must surface as a VoxError, not a raw ValueError a
            # CLI/tool catch would miss (leaking a traceback).
            raise VoxdProtocolError(
                f"'audio' response has non-integer 'bytes': {terminal['bytes']!r}"
            ) from exc
        store_name = str(terminal["name"])
        return RecordResult(
            id=store_name,
            name=store_name,
            store_path=Path(str(terminal["path"])),
            byte_count=byte_count,
            cached=bool(terminal.get("cached", False)),
        )

    async def play(self, ref: str) -> None:
        """Play a stored recording on the daemon host by its store reference.

        *ref* is a bare store name (never a path). The daemon resolves it inside
        its recordings root, refusing any absolute or traversing reference, and
        plays it through its serialized queue -- so audio comes out on the
        machine with speakers, not on a remote client.

        Waits for the terminal ``done`` after the ``playing`` ack, i.e. for
        playback to actually finish, so a host-side failure (missing player,
        unplayable file, played-nothing) arrives as an error frame -- the daemon
        turns it into a ``VoxdProtocolError`` here -- rather than a silent
        success. This mirrors local ``vox play <file>``, which blocks until the
        player exits.
        """
        msg: dict[str, object] = {
            "type": "play",
            "id": uuid.uuid4().hex[:12],
            "ref": ref,
        }
        await self._transport.send_and_drain(
            msg, timeout=_TIMEOUT_PLAYBACK, terminal_type="done"
        )

    async def fetch(self, ref: str) -> bytes:
        """Return a stored recording's bytes, reassembled from the chunked stream.

        *ref* is a bare store name. The daemon streams the file as
        ``fetch_begin`` -> ``chunk``* -> ``fetch_end`` in bounded per-frame memory,
        so a recording of any size retrieves in full; the transport reassembles
        the chunks in order and verifies the byte count and sha256 before
        returning. A mid-stream fault raises a :class:`~punt_vox.VoxdProtocolError`
        and the partial is discarded.
        """
        msg: dict[str, object] = {
            "type": "fetch",
            "id": uuid.uuid4().hex[:12],
            "ref": ref,
        }
        return await self._transport.fetch_stream(msg, timeout=_TIMEOUT_FETCH)

    async def fetch_part(self, album_id: str, part: str) -> bytes:
        """Return one album part's bytes, reassembled from the chunked stream.

        The album is addressed by its catalog id and the *part* by its bare name;
        the daemon catalog-resolves the album and validates the part inside it.
        Reassembly, ordering, and integrity checks are the recording ``fetch`` path.
        """
        msg: dict[str, object] = {
            "type": "fetch",
            "id": uuid.uuid4().hex[:12],
            "album": album_id,
            "part": part,
        }
        return await self._transport.fetch_stream(msg, timeout=_TIMEOUT_FETCH)

    # -- recordings store (rec group) ---------------------------------------

    async def rec_list(self) -> tuple[RecordingSummary, ...]:
        """Return the store's recordings (name + bytes), the ``rec list`` view."""
        resp = await self._command("rec_list")
        with self._wire_guard():
            obj = JsonObject.coerce(resp, "rec_list")
            return tuple(
                self._recording_row(JsonObject.coerce(item, "rec_list.entries"))
                for item in obj.require_list("entries")
            )

    @staticmethod
    def _recording_row(row: JsonObject) -> RecordingSummary:
        """Parse one ``rec_list`` entry (name + bytes) from the wire."""
        return RecordingSummary(
            name=row.require_str("name"), byte_count=row.require_int("bytes")
        )

    async def rec_remove(self, ref: str) -> None:
        """Delete recording *ref* from the store (a hostile/absent ref raises)."""
        await self._command("rec_remove", ref=ref)

    # -- music catalog (music group) ----------------------------------------

    async def music_new(self, prompt: str, name: str | None = None) -> str:
        """Author one track into a fresh catalog album; return its bare album id.

        The daemon sends a ``generating`` ack before the long generation, then the
        terminal ``album`` frame; an empty/bad prompt raises. *prompt* is the
        verbatim ElevenLabs descriptive prompt -- the client expands nothing.
        """
        fields: dict[str, object] = {"prompt": prompt}
        if name is not None:
            fields["name"] = name
        msg: dict[str, object] = {
            "type": "music_new",
            "id": uuid.uuid4().hex[:12],
            **fields,
        }
        responses = await self._transport.send_and_drain(
            msg, timeout=_TIMEOUT_MUSIC_NEW, terminal_type="album"
        )
        terminal = responses[-1] if responses else {}
        if terminal.get("type") != "album":
            raise VoxdProtocolError(f"expected 'album', got {terminal.get('type')!r}")
        with self._wire_guard():
            return JsonObject.coerce(terminal, "album").require_str("album_id")

    async def music_remove(self, album_id: str) -> None:
        """Delete a catalog album by id (a playing album is refused, D-2)."""
        await self._command("music_remove", album=album_id)

    async def music_get(self, album_id: str, dest_dir: Path) -> Path:
        """Copy an album into *dest_dir* as ``<album-name>/`` of its parts.

        Fetches the manifest and refuses an album with no ready parts before
        touching *dest_dir* -- exporting an empty album would leave a hollow
        directory that then blocks a real export of the same album on the
        collision guard. With parts to write, it creates the album directory
        (refusing a collision, D-1), then chunk-fetches every part and writes it
        atomically. On any failure the half-written directory is removed, so a
        fault leaves nothing.
        """
        manifest = await self._command("music_manifest", album=album_id)
        with self._wire_guard():
            obj = JsonObject.coerce(manifest, "music_manifest")
            # Validate daemon-supplied names as bare filenames before joining, so
            # a tampered frame cannot write outside dest_dir (guard raises pre-mkdir).
            album_name = BareName(obj.require_str("album"), "album name").value
            parts = [
                BareName(
                    JsonObject.coerce(item, "manifest.parts").require_str("part"),
                    "part name",
                ).value
                for item in obj.require_list("parts")
            ]
        if not parts:
            # The manifest lists only ready parts, so an empty list means the album
            # is still generating -- fail before mkdir so the CWD is left untouched.
            msg = f"album {album_id} has no ready parts to export (still generating)"
            raise ValueError(msg)
        target = dest_dir / album_name
        target.mkdir(parents=True)  # exist_ok=False: a collision raises (D-1)
        try:
            for part in parts:
                data = await self.fetch_part(album_id, part)
                self._write_atomically(target / part, data)
        except BaseException:
            shutil.rmtree(target, ignore_errors=True)
            raise
        return target

    @staticmethod
    def _write_atomically(dest: Path, data: bytes) -> None:
        """Write *data* to a temp sibling then ``os.replace`` onto *dest*."""
        fd, tmp_name = tempfile.mkstemp(dir=dest.parent, suffix=".part")
        tmp = Path(tmp_name)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
            tmp.replace(dest)
        except OSError:
            tmp.unlink(missing_ok=True)
            raise

    async def voices(self, provider: str | None = None) -> list[str]:
        """List available voices; a missing ``voices`` key is a protocol error.

        Defaulting to ``[]`` would hide a misbehaving daemon behind a
        provider that genuinely offers no voices.
        """
        msg: dict[str, object] = {"type": "voices"}
        if provider is not None:
            msg["provider"] = provider
        resp = await self._transport.send_and_recv(msg, timeout=_TIMEOUT_SHORT)
        if "voices" not in resp:
            raise VoxdProtocolError(f"'voices' response missing 'voices' key: {resp}")
        voice_list: list[str] = resp["voices"]
        return voice_list

    async def health(self) -> HealthStatus:
        """Return the daemon's health snapshot (liveness, port, version)."""
        resp = await self._transport.send_and_recv(
            {"type": "health"}, timeout=_TIMEOUT_SHORT
        )
        return HealthStatus.from_wire(resp)

    # -- program surface (session-free; the daemon-facing wire, design section 4)

    async def _command(self, msg_type: str, **fields: object) -> dict[str, Any]:
        """Send a session-free program command and return the single response.

        The one primitive behind every ``program_*`` method: it stamps the wire
        type and a fresh request id, so a new command needs no bespoke message
        assembly. No ``owner_id`` -- ``voxd`` state is machine-universal.
        """
        msg: dict[str, object] = {
            "type": msg_type,
            "id": uuid.uuid4().hex[:12],
            **fields,
        }
        return await self._transport.send_and_recv(msg, timeout=_TIMEOUT_SHORT)

    async def program_status(self) -> ProgramStatus:
        """Return the daemon's authoritative Program status, parsed from the wire."""
        resp = await self._command("program_status")
        with self._wire_guard():
            obj = JsonObject.coerce(resp, "program_status")
            return ProgramStatus.from_wire(obj.require_object("status"))

    async def program_on(
        self,
        *,
        style: str | None = None,
        vibe: str | None = None,
        name: str | None = None,
        prompts: PromptSet | None = None,
    ) -> CommandOutcome:
        """Turn a Program on, forwarding the session vibe and authored prompts."""
        fields: dict[str, object] = {}
        if style is not None:
            fields["style"] = style
        if vibe is not None:
            fields["vibe"] = vibe
        if name is not None:
            fields["name"] = name
        if prompts is not None:
            fields["base_prompt"] = prompts.base
            fields["variations"] = list(prompts.variations)
        return self._outcome(await self._command("program_on", **fields))

    async def program_off(self) -> CommandOutcome:
        """Turn the active Program off."""
        return self._outcome(await self._command("program_off"))

    async def program_next(self) -> CommandOutcome:
        """Advance to another Part (the one ungated skip/next/loop transition)."""
        return self._outcome(await self._command("program_next"))

    async def program_select(
        self,
        *,
        style: str | None = None,
        vibe: str | None = None,
        name: str | None = None,
        album_id: str | None = None,
    ) -> CommandOutcome:
        """Replay a Selection resolved by album id (direct) or by tags."""
        fields: dict[str, object] = {}
        if album_id is not None:
            fields["album_id"] = album_id
        if style is not None:
            fields["style"] = style
        if vibe is not None:
            fields["vibe"] = vibe
        if name is not None:
            fields["name"] = name
        return self._outcome(await self._command("program_select", **fields))

    async def program_list(self) -> tuple[ProgramSummary, ...]:
        """Return every album as a catalogue summary, parsed from the wire."""
        resp = await self._command("program_list")
        with self._wire_guard():
            obj = JsonObject.coerce(resp, "program_list")
            return tuple(
                self._summary(JsonObject.coerce(item, "program_list.programs"))
                for item in obj.require_list("programs")
            )

    @staticmethod
    @contextlib.contextmanager
    def _wire_guard() -> Generator[None]:
        """Surface a wire-parse failure as a ``VoxdProtocolError``.

        ``JsonObject`` raises ``ValueError`` on a malformed or missing field,
        carrying its own field-path context. At the public client boundary that
        must become the client's own ``VoxdProtocolError``, so a caller catches
        every failure as a :class:`~punt_vox.VoxError` rather than a bare
        ``ValueError`` leaking the daemon's wire shape.
        """
        try:
            yield
        except ValueError as exc:
            raise VoxdProtocolError(f"malformed reply from voxd: {exc}") from exc

    @staticmethod
    def _outcome(resp: dict[str, Any]) -> CommandOutcome:
        """Read the applied/rejected result from a command reply.

        The live daemon acks at enqueue with a bare reply carrying no
        ``applied`` flag, so this returns ``applied=True`` today; the parse is
        written to also read a future ``applied=false`` rejection with no
        client change (see :class:`~punt_vox.CommandOutcome`). Absence of
        ``applied`` is the "it went through" contract; a rejection, if one is
        ever sent, is guaranteed a non-empty ``message`` so a surface never
        renders a blank line for a refused command.
        """
        with VoxClient._wire_guard():
            obj = JsonObject.coerce(resp, "command")
            applied = obj.opt_bool("applied") is not False
            message = obj.opt_str("message") or ("" if applied else "command rejected")
            return CommandOutcome(applied=applied, message=message)

    @staticmethod
    def _summary(obj: JsonObject) -> ProgramSummary:
        """Parse one catalogue entry (id, tags, counts) from the wire."""
        return ProgramSummary(
            id=obj.require_str("id"),
            style=obj.require_str("style"),
            vibe=obj.require_str("vibe"),
            format=obj.require_str("format"),
            ready=obj.require_int("ready"),
            total=obj.opt_int("total") or 0,
            name=obj.opt_str("name"),
        )
