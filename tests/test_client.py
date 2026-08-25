"""Tests for punt_vox.client -- WebSocket client for voxd."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable, Sequence
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from punt_vox.client import (
    CacheStatus,
    VoxClient,
    read_port_file,
    read_token_file,
)
from punt_vox.client_errors import (
    VoxdConnectionError,
    VoxdProtocolError,
    VoxdRejectionError,
    VoxError,
)
from punt_vox.client_sync import VoxClientSync
from punt_vox.paths import run_dir
from punt_vox.types_programs.prompts import PromptSet
from punt_vox.types_programs.status import ProgramStatus
from punt_vox.types_synthesis import SynthesisSpec

# ---------------------------------------------------------------------------
# Path resolution
# ---------------------------------------------------------------------------


def test_run_dir_is_user_state() -> None:
    """The run dir is ``~/.punt-labs/vox/run`` — same on macOS and Linux."""
    assert run_dir() == Path.home() / ".punt-labs" / "vox" / "run"


# ---------------------------------------------------------------------------
# Port / token file readers
# ---------------------------------------------------------------------------


def test_read_port_file(tmp_path: Path) -> None:
    port_file = tmp_path / "serve.port"
    port_file.write_text("9999")
    with patch("punt_vox.client._user_run_dir", return_value=tmp_path):
        assert read_port_file() == 9999


def test_read_port_file_missing(tmp_path: Path) -> None:
    with patch("punt_vox.client._user_run_dir", return_value=tmp_path):
        assert read_port_file() is None


def test_read_token_file(tmp_path: Path) -> None:
    token_file = tmp_path / "serve.token"
    token_file.write_text("secret123")
    with patch("punt_vox.client._user_run_dir", return_value=tmp_path):
        assert read_token_file() == "secret123"


def test_read_token_file_missing(tmp_path: Path) -> None:
    with patch("punt_vox.client._user_run_dir", return_value=tmp_path):
        assert read_token_file() is None


# ---------------------------------------------------------------------------
# VoxClient -- unit tests (mock WebSocket)
# ---------------------------------------------------------------------------


def _make_mock_ws() -> AsyncMock:
    """Create an AsyncMock that behaves like a websockets connection."""
    ws = AsyncMock()
    ws.close = AsyncMock()
    ws.send = AsyncMock()
    ws.ping = AsyncMock()
    return ws


def _fetch_frames(blob: bytes, *, chunk: int) -> list[dict[str, object]]:
    """Build a valid chunked-fetch frame sequence (begin, chunk*, end) for *blob*."""
    import base64
    import hashlib

    slices = [blob[i : i + chunk] for i in range(0, len(blob), chunk)]
    frames: list[dict[str, object]] = [
        {
            "type": "fetch_begin",
            "id": "f1",
            "ref": "x.mp3",
            "bytes": len(blob),
            "chunks": len(slices),
            "sha256": hashlib.sha256(blob).hexdigest(),
        }
    ]
    frames.extend(
        {"type": "chunk", "id": "f1", "seq": seq, "data": base64.b64encode(s).decode()}
        for seq, s in enumerate(slices)
    )
    frames.append({"type": "fetch_end", "id": "f1", "ref": "x.mp3", "bytes": len(blob)})
    return frames


class TestVoxClientConnect:
    """Test connection lifecycle."""

    @pytest.mark.asyncio
    async def test_connect_success(self) -> None:
        mock_ws = _make_mock_ws()
        with patch(
            "punt_vox.client.websockets.asyncio.client.connect",
            new_callable=AsyncMock,
            return_value=mock_ws,
        ):
            client = VoxClient(port=8421, token="tok")
            await client.connect()
            assert client._transport._ws is mock_ws  # pyright: ignore[reportPrivateUsage]
            await client.close()
            mock_ws.close.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_connect_refused_raises(self) -> None:
        with patch(
            "punt_vox.client.websockets.asyncio.client.connect",
            new_callable=AsyncMock,
            side_effect=OSError("Connection refused"),
        ):
            client = VoxClient(port=8421, token="tok")
            with pytest.raises(VoxdConnectionError, match="Cannot connect"):
                await client.connect()

    @pytest.mark.asyncio
    async def test_connect_reads_port_file(self, tmp_path: Path) -> None:
        port_file = tmp_path / "serve.port"
        port_file.write_text("9999")
        token_file = tmp_path / "serve.token"
        token_file.write_text("mytoken")

        mock_ws = _make_mock_ws()
        with (
            patch("punt_vox.client._user_run_dir", return_value=tmp_path),
            patch(
                "punt_vox.client.websockets.asyncio.client.connect",
                new_callable=AsyncMock,
                return_value=mock_ws,
            ) as mock_connect,
        ):
            client = VoxClient()  # no port/token args
            await client.connect()
            call_args = mock_connect.call_args
            uri = call_args[0][0]
            assert "9999" in uri
            assert "token=mytoken" in uri
            await client.close()

    @pytest.mark.asyncio
    async def test_connect_no_port_file_raises(self, tmp_path: Path) -> None:
        with patch("punt_vox.client._user_run_dir", return_value=tmp_path):
            client = VoxClient()
            with pytest.raises(VoxdConnectionError, match="port file not found"):
                await client.connect()


class TestVoxClientContextManager:
    """The async context manager connects on entry and closes on exit."""

    @pytest.mark.asyncio
    async def test_enter_connects_and_returns_self(self) -> None:
        mock_ws = _make_mock_ws()
        with patch(
            "punt_vox.client.websockets.asyncio.client.connect",
            new_callable=AsyncMock,
            return_value=mock_ws,
        ):
            client = VoxClient(port=8421, token="tok")
            async with client as entered:
                assert entered is client
                assert client._transport._ws is mock_ws  # pyright: ignore[reportPrivateUsage]
            mock_ws.close.assert_awaited_once()
            assert client._transport._ws is None  # pyright: ignore[reportPrivateUsage]

    @pytest.mark.asyncio
    async def test_exit_closes_even_when_body_raises(self) -> None:
        mock_ws = _make_mock_ws()
        with patch(
            "punt_vox.client.websockets.asyncio.client.connect",
            new_callable=AsyncMock,
            return_value=mock_ws,
        ):
            client = VoxClient(port=8421, token="tok")
            with pytest.raises(ValueError, match="boom"):
                async with client:
                    raise ValueError("boom")
            mock_ws.close.assert_awaited_once()


class TestVoxClientBuildUri:
    """Test URI construction."""

    def test_uri_with_token(self) -> None:
        client = VoxClient(port=8421, token="abc")
        uri = client._transport._build_uri()  # pyright: ignore[reportPrivateUsage]
        assert uri == "ws://127.0.0.1:8421/ws?token=abc"

    def test_uri_without_token(self) -> None:
        with patch("punt_vox.client.read_token_file", return_value=None):
            client = VoxClient(port=8421)
            uri = client._transport._build_uri()  # pyright: ignore[reportPrivateUsage]
            assert uri == "ws://127.0.0.1:8421/ws"

    def test_uri_custom_host(self) -> None:
        client = VoxClient(host="10.0.0.1", port=9000, token="t")
        uri = client._transport._build_uri()  # pyright: ignore[reportPrivateUsage]
        assert uri == "ws://10.0.0.1:9000/ws?token=t"


class TestVoxClientSynthesize:
    """Test synthesize method."""

    @pytest.mark.asyncio
    async def test_synthesize_returns_request_id(self) -> None:
        mock_ws = _make_mock_ws()
        # Server sends "playing" then "done".
        mock_ws.recv = AsyncMock(
            side_effect=[
                json.dumps({"type": "playing", "id": "req1"}),
                json.dumps({"type": "done", "id": "req1"}),
            ]
        )
        client = VoxClient(port=8421, token="tok")
        client._transport._ws = mock_ws  # pyright: ignore[reportPrivateUsage]

        result = await client.synthesize("Hello world")
        assert isinstance(result.request_id, str)
        assert len(result.request_id) == 12
        assert result.deduped is False
        assert result.original_played_at is None
        assert result.ttl_seconds_remaining is None

        # Verify the message sent to the server.
        sent_raw = mock_ws.send.call_args[0][0]
        sent = json.loads(sent_raw)
        assert sent["type"] == "synthesize"
        assert sent["text"] == "Hello world"
        # No spec -> the wire still carries the historical 90% default so
        # providers do not silently fall back to their own 100% speed.
        assert sent["rate"] == 90

    @pytest.mark.asyncio
    async def test_synthesize_with_all_params(self) -> None:
        mock_ws = _make_mock_ws()
        mock_ws.recv = AsyncMock(
            side_effect=[
                json.dumps({"type": "playing", "id": "x"}),
                json.dumps({"type": "done", "id": "x"}),
            ]
        )
        client = VoxClient(port=8421, token="tok")
        client._transport._ws = mock_ws  # pyright: ignore[reportPrivateUsage]

        await client.synthesize(
            "Test",
            SynthesisSpec(
                voice="drew",
                provider="elevenlabs",
                model="eleven_turbo_v2_5",
                rate=100,
                language="en",
                vibe_tags="calm",
                stability=0.5,
                similarity=0.8,
                style=0.3,
                speaker_boost=True,
                api_key="sk-test",
            ),
        )

        sent = json.loads(mock_ws.send.call_args[0][0])
        assert sent["voice"] == "drew"
        assert sent["provider"] == "elevenlabs"
        assert sent["model"] == "eleven_turbo_v2_5"
        assert sent["rate"] == 100
        assert sent["language"] == "en"
        assert sent["vibe_tags"] == "calm"
        assert sent["stability"] == 0.5
        assert sent["similarity"] == 0.8
        assert sent["style"] == 0.3
        assert sent["speaker_boost"] is True
        assert sent["api_key"] == "sk-test"

    @pytest.mark.asyncio
    async def test_synthesize_error_raises(self) -> None:
        mock_ws = _make_mock_ws()
        mock_ws.recv = AsyncMock(
            return_value=json.dumps(
                {"type": "error", "id": "x", "message": "empty text"}
            )
        )
        client = VoxClient(port=8421, token="tok")
        client._transport._ws = mock_ws  # pyright: ignore[reportPrivateUsage]

        with pytest.raises(VoxdProtocolError, match="empty text"):
            await client.synthesize("")

    @pytest.mark.asyncio
    async def test_synthesize_omits_none_params(self) -> None:
        mock_ws = _make_mock_ws()
        mock_ws.recv = AsyncMock(
            side_effect=[
                json.dumps({"type": "done", "id": "x"}),
            ]
        )
        client = VoxClient(port=8421, token="tok")
        client._transport._ws = mock_ws  # pyright: ignore[reportPrivateUsage]

        await client.synthesize("Hello")
        sent = json.loads(mock_ws.send.call_args[0][0])
        # None params should not appear in the message.
        assert "voice" not in sent
        assert "provider" not in sent
        assert "model" not in sent
        assert "language" not in sent
        assert "vibe_tags" not in sent
        assert "stability" not in sent
        assert "similarity" not in sent
        assert "style" not in sent
        assert "speaker_boost" not in sent
        assert "api_key" not in sent

    @pytest.mark.asyncio
    async def test_synthesize_returns_on_playing_not_done(self) -> None:
        """synthesize() returns as soon as 'playing' arrives; 'done' not required."""
        mock_ws = _make_mock_ws()
        mock_ws.recv = AsyncMock(
            side_effect=[
                json.dumps({"type": "playing", "id": "req1"}),
                # If the client reads past this it would get StopAsyncIteration.
            ]
        )
        client = VoxClient(port=8421, token="tok")
        client._transport._ws = mock_ws  # pyright: ignore[reportPrivateUsage]

        result = await client.synthesize("Hello world")
        assert isinstance(result.request_id, str)
        assert result.deduped is False
        assert mock_ws.recv.call_count == 1
        mock_ws.close.assert_awaited_once()
        assert client._transport._ws is None  # pyright: ignore[reportPrivateUsage]

    @pytest.mark.asyncio
    async def test_synthesize_dedup_returns_on_done(self) -> None:
        """synthesize() handles dedup path: 'done' with deduped=True, no 'playing'."""
        mock_ws = _make_mock_ws()
        mock_ws.recv = AsyncMock(
            side_effect=[
                json.dumps(
                    {
                        "type": "done",
                        "id": "req1",
                        "deduped": True,
                        "original_played_at": 1700000000.0,
                        "ttl_seconds_remaining": 550.0,
                    }
                ),
            ]
        )
        client = VoxClient(port=8421, token="tok")
        client._transport._ws = mock_ws  # pyright: ignore[reportPrivateUsage]

        result = await client.synthesize("Hello world", once=600)
        assert result.deduped is True
        assert result.original_played_at == 1700000000.0
        assert result.ttl_seconds_remaining == 550.0
        assert result.cached is False
        assert mock_ws.recv.call_count == 1

    @pytest.mark.asyncio
    async def test_synthesize_reports_cache_hit_from_playing(self) -> None:
        """A 'playing' response carrying cached=true surfaces as result.cached."""
        mock_ws = _make_mock_ws()
        mock_ws.recv = AsyncMock(
            side_effect=[json.dumps({"type": "playing", "id": "r", "cached": True})]
        )
        client = VoxClient(port=8421, token="tok")
        client._transport._ws = mock_ws  # pyright: ignore[reportPrivateUsage]

        result = await client.synthesize("Hello world")
        assert result.cached is True

    @pytest.mark.asyncio
    async def test_synthesize_reports_cache_miss_from_playing(self) -> None:
        """A 'playing' response carrying cached=false surfaces as result.cached."""
        mock_ws = _make_mock_ws()
        mock_ws.recv = AsyncMock(
            side_effect=[json.dumps({"type": "playing", "id": "r", "cached": False})]
        )
        client = VoxClient(port=8421, token="tok")
        client._transport._ws = mock_ws  # pyright: ignore[reportPrivateUsage]

        result = await client.synthesize("Hello world")
        assert result.cached is False

    @pytest.mark.asyncio
    async def test_explicit_timeout_covers_a_slow_synthesis_the_default_would_kill(
        self,
    ) -> None:
        """Regression: a long reply's real synthesis time (a 1133-char
        reply measured ~43s live) exceeded the fixed 30s default and
        killed the call mid-reply with VoxdProtocolError. A caller (vox
        call's speak()) that knows its own text can run long passes a
        longer, explicit timeout instead -- proven here with a delay
        longer than a deliberately tiny default, comfortably covered by
        the explicit value.
        """
        mock_ws = _make_mock_ws()

        async def _delayed_recv() -> str:
            await asyncio.sleep(0.05)
            return json.dumps({"type": "playing", "id": "req1"})

        mock_ws.recv = AsyncMock(side_effect=_delayed_recv)
        client = VoxClient(port=8421, token="tok")
        client._transport._ws = mock_ws  # pyright: ignore[reportPrivateUsage]

        with patch("punt_vox.client._TIMEOUT_SYNTHESIS", 0.01):
            # The default (0.01s) is shorter than the 0.05s delay -- an
            # explicit timeout=1.0 must still cover it.
            result = await client.synthesize("Hello world", timeout=1.0)
        assert isinstance(result.request_id, str)

    @pytest.mark.asyncio
    async def test_omitted_timeout_falls_back_to_the_default(self) -> None:
        """Absence of an explicit timeout keeps every existing caller's
        current behavior unchanged (mic:unmute, vox say, the Stop-hook
        speech helper) -- only a caller that opts in gets a longer bound.
        """
        mock_ws = _make_mock_ws()

        async def _delayed_recv() -> str:
            await asyncio.sleep(0.05)
            return json.dumps({"type": "playing", "id": "req1"})

        mock_ws.recv = AsyncMock(side_effect=_delayed_recv)
        client = VoxClient(port=8421, token="tok")
        client._transport._ws = mock_ws  # pyright: ignore[reportPrivateUsage]

        with (
            patch("punt_vox.client._TIMEOUT_SYNTHESIS", 0.01),
            pytest.raises(VoxdProtocolError, match="Timeout"),
        ):
            await client.synthesize("Hello world")


class TestVoxClientChime:
    """Test chime method."""

    @pytest.mark.asyncio
    async def test_chime(self) -> None:
        mock_ws = _make_mock_ws()
        mock_ws.recv = AsyncMock(
            side_effect=[
                json.dumps({"type": "playing", "id": "chime:done"}),
                json.dumps({"type": "done", "id": "chime:done"}),
            ]
        )
        client = VoxClient(port=8421, token="tok")
        client._transport._ws = mock_ws  # pyright: ignore[reportPrivateUsage]

        await client.chime("done")
        sent = json.loads(mock_ws.send.call_args[0][0])
        assert sent == {"type": "chime", "signal": "done"}

    @pytest.mark.asyncio
    async def test_chime_returns_on_playing_not_done(self) -> None:
        """chime() returns on 'playing'; 'done' is not required."""
        mock_ws = _make_mock_ws()
        mock_ws.recv = AsyncMock(
            side_effect=[
                json.dumps({"type": "playing", "id": "chime:done"}),
            ]
        )
        client = VoxClient(port=8421, token="tok")
        client._transport._ws = mock_ws  # pyright: ignore[reportPrivateUsage]

        await client.chime("done")
        assert mock_ws.recv.call_count == 1
        mock_ws.close.assert_awaited_once()
        assert client._transport._ws is None  # pyright: ignore[reportPrivateUsage]

    @pytest.mark.asyncio
    async def test_chime_dedup_returns_on_done(self) -> None:
        """chime() handles dedup path: 'done' with no preceding 'playing'."""
        mock_ws = _make_mock_ws()
        mock_ws.recv = AsyncMock(
            side_effect=[
                json.dumps({"type": "done", "id": ""}),
            ]
        )
        client = VoxClient(port=8421, token="tok")
        client._transport._ws = mock_ws  # pyright: ignore[reportPrivateUsage]

        await client.chime("done")
        assert mock_ws.recv.call_count == 1

    @pytest.mark.asyncio
    async def test_chime_error_raises(self) -> None:
        mock_ws = _make_mock_ws()
        mock_ws.recv = AsyncMock(
            return_value=json.dumps(
                {"type": "error", "id": "", "message": "unknown chime: bad"}
            )
        )
        client = VoxClient(port=8421, token="tok")
        client._transport._ws = mock_ws  # pyright: ignore[reportPrivateUsage]

        with pytest.raises(VoxdProtocolError, match="unknown chime"):
            await client.chime("bad")


class TestVoxClientRecord:
    """Test record method."""

    @pytest.mark.asyncio
    async def test_record_returns_store_locator(self) -> None:
        # The daemon sends the RELATIVE store path (`recordings/x.mp3`), never an
        # absolute prefix; the client parses that relative form into store_path.
        mock_ws = _make_mock_ws()
        mock_ws.recv = AsyncMock(
            return_value=json.dumps(
                {
                    "type": "audio",
                    "id": "r1",
                    "name": "x.mp3",
                    "path": "recordings/x.mp3",
                    "bytes": 40,
                    "cached": False,
                }
            )
        )
        client = VoxClient(port=8421, token="tok")
        client._transport._ws = mock_ws  # pyright: ignore[reportPrivateUsage]

        result = await client.record("Hello")
        assert result.id == "x.mp3"
        assert result.name == "x.mp3"
        assert result.store_path == Path("recordings/x.mp3")
        assert not result.store_path.is_absolute()  # no host prefix crosses
        assert result.byte_count == 40
        assert result.cached is False

    @pytest.mark.asyncio
    async def test_record_sends_optional_name(self) -> None:
        mock_ws = _make_mock_ws()
        mock_ws.recv = AsyncMock(
            return_value=json.dumps(
                {
                    "type": "audio",
                    "id": "r1",
                    "name": "y.mp3",
                    "path": "/s/y.mp3",
                    "bytes": 12,
                }
            )
        )
        client = VoxClient(port=8421, token="tok")
        client._transport._ws = mock_ws  # pyright: ignore[reportPrivateUsage]

        await client.record("Hi", name="y.mp3")
        sent = json.loads(mock_ws.send.call_args.args[0])
        assert sent["type"] == "record"
        assert sent["name"] == "y.mp3"
        assert "output_dir" not in sent  # the #351 path contract is gone
        assert "output_path" not in sent

    @pytest.mark.asyncio
    async def test_record_omits_name_when_unset(self) -> None:
        mock_ws = _make_mock_ws()
        mock_ws.recv = AsyncMock(
            return_value=json.dumps(
                {
                    "type": "audio",
                    "id": "r1",
                    "name": "h.mp3",
                    "path": "/s/h.mp3",
                    "bytes": 1,
                }
            )
        )
        client = VoxClient(port=8421, token="tok")
        client._transport._ws = mock_ws  # pyright: ignore[reportPrivateUsage]

        await client.record("Hi")
        sent = json.loads(mock_ws.send.call_args.args[0])
        assert "name" not in sent  # content-addressed daemon-side

    @pytest.mark.asyncio
    async def test_record_empty_name_sent_for_daemon_to_reject(self) -> None:
        """An explicit "" is sent so the daemon -- the single authority -- rejects it.

        The client does not silently drop "": only an absent (None) name is
        content-addressed; "" goes on the wire and the daemon rejects it pre-ack.
        """
        mock_ws = _make_mock_ws()
        mock_ws.recv = AsyncMock(
            return_value=json.dumps(
                {
                    "type": "audio",
                    "id": "r1",
                    "name": "h.mp3",
                    "path": "/s/h.mp3",
                    "bytes": 1,
                }
            )
        )
        client = VoxClient(port=8421, token="tok")
        client._transport._ws = mock_ws  # pyright: ignore[reportPrivateUsage]

        await client.record("Hi", name="")
        sent = json.loads(mock_ws.send.call_args.args[0])
        assert sent["name"] == ""

    @pytest.mark.asyncio
    async def test_record_audio_without_path_raises(self) -> None:
        mock_ws = _make_mock_ws()
        mock_ws.recv = AsyncMock(
            return_value=json.dumps(
                {"type": "audio", "id": "r1", "name": "x.mp3", "bytes": 1}
            )
        )
        client = VoxClient(port=8421, token="tok")
        client._transport._ws = mock_ws  # pyright: ignore[reportPrivateUsage]

        with pytest.raises(VoxdProtocolError, match="missing 'path'"):
            await client.record("Hello")

    @pytest.mark.asyncio
    async def test_record_audio_without_name_raises(self) -> None:
        mock_ws = _make_mock_ws()
        mock_ws.recv = AsyncMock(
            return_value=json.dumps(
                {"type": "audio", "id": "r1", "path": "/s/x.mp3", "bytes": 1}
            )
        )
        client = VoxClient(port=8421, token="tok")
        client._transport._ws = mock_ws  # pyright: ignore[reportPrivateUsage]

        with pytest.raises(VoxdProtocolError, match="missing 'name'"):
            await client.record("Hello")

    @pytest.mark.asyncio
    async def test_record_malformed_bytes_raises_voxerror(self) -> None:
        """A non-int 'bytes' is a VoxdProtocolError, not a raw ValueError."""
        mock_ws = _make_mock_ws()
        mock_ws.recv = AsyncMock(
            return_value=json.dumps(
                {
                    "type": "audio",
                    "id": "r1",
                    "name": "x.mp3",
                    "path": "/o/x.mp3",
                    "bytes": "nope",
                }
            )
        )
        client = VoxClient(port=8421, token="tok")
        client._transport._ws = mock_ws  # pyright: ignore[reportPrivateUsage]

        with pytest.raises(VoxdProtocolError, match="non-integer 'bytes'"):
            await client.record("hi")

    @pytest.mark.asyncio
    async def test_record_missing_bytes_raises_voxerror(self) -> None:
        """A missing 'bytes' is a VoxdProtocolError, not a silent default of 0."""
        mock_ws = _make_mock_ws()
        mock_ws.recv = AsyncMock(
            return_value=json.dumps(
                {"type": "audio", "id": "r1", "name": "x.mp3", "path": "/o/x.mp3"}
            )
        )
        client = VoxClient(port=8421, token="tok")
        client._transport._ws = mock_ws  # pyright: ignore[reportPrivateUsage]

        with pytest.raises(VoxdProtocolError, match="missing 'bytes'"):
            await client.record("hi")

    @pytest.mark.asyncio
    async def test_record_non_json_frame_raises_voxerror(self) -> None:
        """A truncated/non-JSON drain frame is a VoxError, not JSONDecodeError."""
        mock_ws = _make_mock_ws()
        mock_ws.recv = AsyncMock(return_value="not json{{{")
        client = VoxClient(port=8421, token="tok")
        client._transport._ws = mock_ws  # pyright: ignore[reportPrivateUsage]

        with pytest.raises(VoxdProtocolError, match="invalid JSON"):
            await client.record("hi")

    @pytest.mark.asyncio
    async def test_send_and_recv_non_json_frame_raises_voxerror(self) -> None:
        """A non-JSON single-response frame is a VoxError, not JSONDecodeError."""
        mock_ws = _make_mock_ws()
        mock_ws.recv = AsyncMock(return_value="<<garbage>>")
        client = VoxClient(port=8421, token="tok")
        client._transport._ws = mock_ws  # pyright: ignore[reportPrivateUsage]

        with pytest.raises(VoxdProtocolError, match="invalid JSON"):
            await client.health()

    @pytest.mark.asyncio
    async def test_record_transport_close_is_wrapped(self) -> None:
        """A dropped connection surfaces as a VoxError, never a raw traceback."""
        mock_ws = _make_mock_ws()
        mock_ws.recv = AsyncMock(side_effect=OSError("socket gone"))
        client = VoxClient(port=8421, token="tok")
        client._transport._ws = mock_ws  # pyright: ignore[reportPrivateUsage]

        with pytest.raises(VoxdConnectionError, match="connection to voxd lost"):
            await client.record("Hello")

    @pytest.mark.asyncio
    async def test_long_synthesis_is_not_abandoned(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A long synthesis gets a deadline well past the old fixed 30s."""
        captured: dict[str, float] = {}

        async def fake_drain(
            _self: object,
            _msg: dict[str, object],
            *,
            timeout: float,
            terminal_type: str,
        ) -> list[dict[str, object]]:
            captured["timeout"] = timeout
            return [
                {"type": "recording", "id": "r1"},
                {
                    "type": "audio",
                    "id": "r1",
                    "name": "x.mp3",
                    "path": "/o/x.mp3",
                    "bytes": 1,
                },
            ]

        monkeypatch.setattr("punt_vox.client._VoxdTransport.send_and_drain", fake_drain)
        client = VoxClient(port=8421, token="tok")

        result = await client.record("a" * 6000)

        # A fresh 6000-char synthesis was measured at ~124s; the deadline must
        # comfortably exceed that (and the old fixed 30s) so it is not abandoned.
        assert captured["timeout"] > 124
        assert result.store_path == Path("/o/x.mp3")

    @pytest.mark.asyncio
    async def test_record_timeout_is_capped(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The length-scaled deadline is bounded so a hung daemon is detected."""
        captured: dict[str, float] = {}

        async def fake_drain(
            _self: object,
            _msg: dict[str, object],
            *,
            timeout: float,
            terminal_type: str,
        ) -> list[dict[str, object]]:
            captured["timeout"] = timeout
            return [
                {
                    "type": "audio",
                    "id": "r1",
                    "name": "x.mp3",
                    "path": "/o/x.mp3",
                    "bytes": 1,
                }
            ]

        monkeypatch.setattr("punt_vox.client._VoxdTransport.send_and_drain", fake_drain)
        client = VoxClient(port=8421, token="tok")

        # Uncapped this text would scale to ~50000s; the cap holds it at 600s.
        await client.record("a" * 1_000_000)
        assert captured["timeout"] == 600.0


class TestVoxClientPlayFetch:
    """Test the play and fetch store-reference methods."""

    @pytest.mark.asyncio
    async def test_play_waits_for_done_after_playing(self) -> None:
        """play waits for the terminal 'done' (playback finished), not enqueue."""
        mock_ws = _make_mock_ws()
        # 'playing' is the ack; the client must keep waiting for 'done'.
        mock_ws.recv = AsyncMock(
            side_effect=[
                json.dumps({"type": "playing", "id": "p1"}),
                json.dumps({"type": "done", "id": "p1"}),
            ]
        )
        client = VoxClient(port=8421, token="tok")
        client._transport._ws = mock_ws  # pyright: ignore[reportPrivateUsage]

        await client.play("a1b2c3.mp3")
        sent = json.loads(mock_ws.send.call_args.args[0])
        assert sent["type"] == "play"
        assert sent["ref"] == "a1b2c3.mp3"
        assert mock_ws.recv.await_count == 2  # did not return at 'playing'

    @pytest.mark.asyncio
    async def test_play_host_failure_raises(self) -> None:
        """A host-side playback failure arrives as an error and raises, not exit 0."""
        mock_ws = _make_mock_ws()
        mock_ws.recv = AsyncMock(
            side_effect=[
                json.dumps({"type": "playing", "id": "p1"}),
                json.dumps(
                    {
                        "type": "error",
                        "id": "p1",
                        "message": "playback failed: no player",
                    }
                ),
            ]
        )
        client = VoxClient(port=8421, token="tok")
        client._transport._ws = mock_ws  # pyright: ignore[reportPrivateUsage]

        with pytest.raises(VoxdProtocolError, match="playback failed"):
            await client.play("a1b2c3.mp3")

    @pytest.mark.asyncio
    async def test_fetch_reassembles_chunked_stream(self) -> None:
        """fetch reassembles fetch_begin -> chunk* -> fetch_end into the file bytes."""
        blob = b"\xff\xfb\x90\x00" * 4  # 16 bytes
        mock_ws = _make_mock_ws()
        frames = [json.dumps(f) for f in _fetch_frames(blob, chunk=6)]
        mock_ws.recv = AsyncMock(side_effect=frames)
        client = VoxClient(port=8421, token="tok")
        client._transport._ws = mock_ws  # pyright: ignore[reportPrivateUsage]

        assert await client.fetch("x.mp3") == blob

    @pytest.mark.asyncio
    async def test_fetch_part_reassembles_stream(self) -> None:
        """fetch_part addresses an album id + part and reassembles the same way."""
        blob = b"music-bytes"
        mock_ws = _make_mock_ws()
        frames = [json.dumps(f) for f in _fetch_frames(blob, chunk=4)]
        mock_ws.recv = AsyncMock(side_effect=frames)
        client = VoxClient(port=8421, token="tok")
        client._transport._ws = mock_ws  # pyright: ignore[reportPrivateUsage]

        assert await client.fetch_part("7f3a91", "001.mp3") == blob
        sent = json.loads(mock_ws.send.call_args.args[0])
        assert sent["album"] == "7f3a91"
        assert sent["part"] == "001.mp3"

    @pytest.mark.asyncio
    async def test_fetch_uses_generous_fetch_timeout(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """fetch waits on the dedicated _TIMEOUT_FETCH, not the synthesis one."""
        from punt_vox.client import _TIMEOUT_FETCH

        captured: dict[str, float] = {}

        async def fake_stream(
            _self: object, _msg: dict[str, object], *, timeout: float
        ) -> bytes:
            captured["timeout"] = timeout
            return b""

        monkeypatch.setattr("punt_vox.client._VoxdTransport.fetch_stream", fake_stream)
        client = VoxClient(port=8421, token="tok")

        await client.fetch("x.mp3")

        assert captured["timeout"] == _TIMEOUT_FETCH
        assert captured["timeout"] > 30.0  # clearly larger than synthesis

    @pytest.mark.asyncio
    async def test_fetch_wrong_first_frame_raises(self) -> None:
        """A stream not opening with fetch_begin is a protocol error."""
        mock_ws = _make_mock_ws()
        mock_ws.recv = AsyncMock(return_value=json.dumps({"type": "done", "id": "f1"}))
        client = VoxClient(port=8421, token="tok")
        client._transport._ws = mock_ws  # pyright: ignore[reportPrivateUsage]

        with pytest.raises(VoxdProtocolError, match="fetch_begin"):
            await client.fetch("x.mp3")

    @pytest.mark.asyncio
    async def test_fetch_out_of_order_chunk_raises(self) -> None:
        """A chunk whose seq is not the next expected is rejected."""
        blob = b"abcdefgh"
        frames = _fetch_frames(blob, chunk=4)
        frames[2]["seq"] = 5  # corrupt the second chunk's order
        mock_ws = _make_mock_ws()
        mock_ws.recv = AsyncMock(side_effect=[json.dumps(f) for f in frames])
        client = VoxClient(port=8421, token="tok")
        client._transport._ws = mock_ws  # pyright: ignore[reportPrivateUsage]

        with pytest.raises(VoxdProtocolError, match="chunk seq"):
            await client.fetch("x.mp3")

    @pytest.mark.asyncio
    async def test_fetch_byte_count_mismatch_raises(self) -> None:
        """A declared byte count disagreeing with the reassembly is an error."""
        blob = b"abcdefgh"
        frames = _fetch_frames(blob, chunk=4)
        frames[0]["bytes"] = 99  # lie about the total
        mock_ws = _make_mock_ws()
        mock_ws.recv = AsyncMock(side_effect=[json.dumps(f) for f in frames])
        client = VoxClient(port=8421, token="tok")
        client._transport._ws = mock_ws  # pyright: ignore[reportPrivateUsage]

        with pytest.raises(VoxdProtocolError, match="byte-count mismatch"):
            await client.fetch("x.mp3")

    @pytest.mark.asyncio
    async def test_fetch_sha_mismatch_raises(self) -> None:
        """A stream whose bytes do not match the declared sha256 is discarded."""
        blob = b"abcdefgh"
        frames = _fetch_frames(blob, chunk=4)
        frames[0]["sha256"] = "0" * 64  # wrong digest
        mock_ws = _make_mock_ws()
        mock_ws.recv = AsyncMock(side_effect=[json.dumps(f) for f in frames])
        client = VoxClient(port=8421, token="tok")
        client._transport._ws = mock_ws  # pyright: ignore[reportPrivateUsage]

        with pytest.raises(VoxdProtocolError, match="sha256 mismatch"):
            await client.fetch("x.mp3")

    @pytest.mark.asyncio
    async def test_fetch_mid_stream_error_discards_partial(self) -> None:
        """An error frame mid-stream raises, so no partial is returned to a caller."""
        blob = b"abcdefgh"
        frames = _fetch_frames(blob, chunk=4)
        # Replace the last chunk with an abort error terminal.
        frames[-2] = {"type": "error", "id": "f1", "message": "read fault"}
        mock_ws = _make_mock_ws()
        mock_ws.recv = AsyncMock(side_effect=[json.dumps(f) for f in frames[:-1]])
        client = VoxClient(port=8421, token="tok")
        client._transport._ws = mock_ws  # pyright: ignore[reportPrivateUsage]

        with pytest.raises(VoxdProtocolError, match="read fault"):
            await client.fetch("x.mp3")

    @pytest.mark.asyncio
    async def test_fetch_fault_taints_and_closes_connection(self) -> None:
        """A mid-stream fault closes the connection and nulls it for a reconnect.

        The daemon may still be sending this fetch's remaining chunk frames; the
        poisoned socket must be discarded so those stale frames cannot be read by
        the next request. The failure still surfaces as a VoxdProtocolError.
        """
        blob = b"abcdefgh"
        frames = _fetch_frames(blob, chunk=4)
        frames[0]["sha256"] = "0" * 64  # force a mid-stream integrity fault
        mock_ws = _make_mock_ws()
        mock_ws.recv = AsyncMock(side_effect=[json.dumps(f) for f in frames])
        client = VoxClient(port=8421, token="tok")
        client._transport._ws = mock_ws  # pyright: ignore[reportPrivateUsage]

        with pytest.raises(VoxdProtocolError, match="sha256 mismatch"):
            await client.fetch("x.mp3")

        mock_ws.close.assert_awaited_once()
        assert client._transport._ws is None  # pyright: ignore[reportPrivateUsage]

    @pytest.mark.asyncio
    async def test_subsequent_fetch_after_fault_reads_no_stale_frames(self) -> None:
        """After a fault, the next fetch reconnects and reads a clean stream.

        The first connection carries a corrupt stream followed by leftover chunk
        frames the daemon was still sending. If that socket were reused, the next
        fetch would read those stale frames and desync. Tainting the connection
        forces a reconnect, so the second fetch reads only the fresh stream.
        """
        good = b"clean-bytes"
        # First socket: a sha-mismatch fault, then stale frames that would poison
        # a reused connection (the daemon still flushing the aborted fetch).
        bad = _fetch_frames(b"abcdefgh", chunk=4)
        bad[0]["sha256"] = "0" * 64
        stale = _fetch_frames(b"STALE!!!", chunk=4)  # would be misread if reused
        first_ws = _make_mock_ws()
        first_ws.recv = AsyncMock(side_effect=[json.dumps(f) for f in (*bad, *stale)])
        # Second socket (post-reconnect): the clean stream the caller asked for.
        second_ws = _make_mock_ws()
        second_ws.recv = AsyncMock(
            side_effect=[json.dumps(f) for f in _fetch_frames(good, chunk=4)]
        )

        client = VoxClient(port=8421, token="tok")
        client._transport._ws = first_ws  # pyright: ignore[reportPrivateUsage]

        with pytest.raises(VoxdProtocolError, match="sha256 mismatch"):
            await client.fetch("x.mp3")

        with patch(
            "punt_vox.client.websockets.asyncio.client.connect",
            new_callable=AsyncMock,
            return_value=second_ws,
        ):
            assert await client.fetch("x.mp3") == good

        # The second fetch never touched the first (stale) socket's leftover
        # frames -- only fetch_begin/chunk/fetch_end were read from it (5 frames).
        assert first_ws.recv.call_count == len(bad)
        assert client._transport._ws is second_ws  # pyright: ignore[reportPrivateUsage]


class TestVoxClientRecMusic:
    """The rec/music catalog client methods over a mock WebSocket."""

    @pytest.mark.asyncio
    async def test_rec_list_parses_entries(self) -> None:
        mock_ws = _make_mock_ws()
        mock_ws.recv = AsyncMock(
            return_value=json.dumps(
                {
                    "type": "recordings",
                    "id": "l1",
                    "entries": [
                        {"name": "a.mp3", "bytes": 5},
                        {"name": "b.mp3", "bytes": 2},
                    ],
                }
            )
        )
        client = VoxClient(port=8421, token="tok")
        client._transport._ws = mock_ws  # pyright: ignore[reportPrivateUsage]

        rows = await client.rec_list()
        assert [(r.name, r.byte_count) for r in rows] == [("a.mp3", 5), ("b.mp3", 2)]

    @pytest.mark.asyncio
    async def test_rec_remove_error_raises(self) -> None:
        mock_ws = _make_mock_ws()
        mock_ws.recv = AsyncMock(
            return_value=json.dumps(
                {"type": "error", "id": "r1", "message": "no recording named 'x.mp3'"}
            )
        )
        client = VoxClient(port=8421, token="tok")
        client._transport._ws = mock_ws  # pyright: ignore[reportPrivateUsage]

        with pytest.raises(VoxdProtocolError, match="no recording named"):
            await client.rec_remove("x.mp3")

    @pytest.mark.asyncio
    async def test_cache_status_parses_daemon_fields(self) -> None:
        """cache_status returns the daemon's entries/size/path, not a local read."""
        mock_ws = _make_mock_ws()
        mock_ws.recv = AsyncMock(
            return_value=json.dumps(
                {
                    "type": "cache_status",
                    "id": "s1",
                    "entries": 4,
                    "size_bytes": 8192,
                    "path": "/daemon/home/.cache/vox",
                }
            )
        )
        client = VoxClient(port=8421, token="tok")
        client._transport._ws = mock_ws  # pyright: ignore[reportPrivateUsage]

        status = await client.cache_status()
        assert status.entries == 4
        assert status.size_bytes == 8192
        assert str(status.path) == "/daemon/home/.cache/vox"

    @pytest.mark.asyncio
    async def test_cache_clear_returns_count(self) -> None:
        mock_ws = _make_mock_ws()
        mock_ws.recv = AsyncMock(
            return_value=json.dumps(
                {"type": "cache_cleared", "id": "c1", "cleared": 12}
            )
        )
        client = VoxClient(port=8421, token="tok")
        client._transport._ws = mock_ws  # pyright: ignore[reportPrivateUsage]

        assert await client.cache_clear() == 12

    @pytest.mark.asyncio
    async def test_cache_status_daemon_fault_raises(self) -> None:
        """A daemon fault frame surfaces as a VoxError, never a wrong/empty status."""
        mock_ws = _make_mock_ws()
        mock_ws.recv = AsyncMock(
            return_value=json.dumps(
                {"type": "error", "id": "s1", "message": "permission denied"}
            )
        )
        client = VoxClient(port=8421, token="tok")
        client._transport._ws = mock_ws  # pyright: ignore[reportPrivateUsage]

        with pytest.raises(VoxdProtocolError, match="permission denied"):
            await client.cache_status()

    @pytest.mark.asyncio
    async def test_cache_status_malformed_reply_raises(self) -> None:
        """A missing field is a malformed reply, raising rather than defaulting."""
        mock_ws = _make_mock_ws()
        mock_ws.recv = AsyncMock(
            return_value=json.dumps({"type": "cache_status", "id": "s1", "entries": 4})
        )
        client = VoxClient(port=8421, token="tok")
        client._transport._ws = mock_ws  # pyright: ignore[reportPrivateUsage]

        with pytest.raises(VoxdProtocolError, match="malformed reply"):
            await client.cache_status()

    @pytest.mark.asyncio
    async def test_music_new_returns_album_id_after_ack(self) -> None:
        """music_new consumes the 'generating' ack then returns the 'album' id."""
        mock_ws = _make_mock_ws()
        mock_ws.recv = AsyncMock(
            side_effect=[
                json.dumps({"type": "generating", "id": "n1"}),
                json.dumps(
                    {"type": "album", "id": "n1", "album_id": "7f3a91", "parts": 1}
                ),
            ]
        )
        client = VoxClient(port=8421, token="tok")
        client._transport._ws = mock_ws  # pyright: ignore[reportPrivateUsage]

        assert await client.music_new(PromptSet.single("warm pads")) == "7f3a91"
        sent = json.loads(mock_ws.send.call_args.args[0])
        assert sent["base_prompt"] == "warm pads"

    @pytest.mark.asyncio
    async def test_music_new_bad_prompt_raises(self) -> None:
        mock_ws = _make_mock_ws()
        mock_ws.recv = AsyncMock(
            side_effect=[
                json.dumps({"type": "generating", "id": "n1"}),
                json.dumps({"type": "error", "id": "n1", "message": "bad_prompt"}),
            ]
        )
        client = VoxClient(port=8421, token="tok")
        client._transport._ws = mock_ws  # pyright: ignore[reportPrivateUsage]

        with pytest.raises(VoxdProtocolError, match="bad_prompt"):
            await client.music_new(PromptSet.single("rejected"))

    @pytest.mark.asyncio
    async def test_music_remove_ok(self) -> None:
        mock_ws = _make_mock_ws()
        mock_ws.recv = AsyncMock(
            return_value=json.dumps(
                {"type": "removed", "id": "r1", "album_id": "7f3a91"}
            )
        )
        client = VoxClient(port=8421, token="tok")
        client._transport._ws = mock_ws  # pyright: ignore[reportPrivateUsage]

        await client.music_remove("7f3a91")  # no raise

    @pytest.mark.asyncio
    async def test_music_get_writes_album_dir(self, tmp_path: Path) -> None:
        """music_get creates ./<album>/ and writes each chunk-fetched part."""
        part = b"track-bytes"
        frames = [
            json.dumps(
                {
                    "type": "manifest",
                    "id": "m1",
                    "album": "warm-pads-7f3a91",
                    "parts": [{"part": "001.mp3", "bytes": len(part)}],
                }
            ),
            *[json.dumps(f) for f in _fetch_frames(part, chunk=4)],
        ]
        mock_ws = _make_mock_ws()
        mock_ws.recv = AsyncMock(side_effect=frames)
        client = VoxClient(port=8421, token="tok")
        client._transport._ws = mock_ws  # pyright: ignore[reportPrivateUsage]

        target = await client.music_get("7f3a91", tmp_path)
        assert target == tmp_path / "warm-pads-7f3a91"
        assert (target / "001.mp3").read_bytes() == part

    @pytest.mark.asyncio
    async def test_music_get_refuses_collision(self, tmp_path: Path) -> None:
        """A pre-existing target directory is a collision error (D-1)."""
        (tmp_path / "warm-pads-7f3a91").mkdir()
        mock_ws = _make_mock_ws()
        mock_ws.recv = AsyncMock(
            return_value=json.dumps(
                {
                    "type": "manifest",
                    "id": "m1",
                    "album": "warm-pads-7f3a91",
                    "parts": [{"part": "001.mp3", "bytes": 3}],
                }
            )
        )
        client = VoxClient(port=8421, token="tok")
        client._transport._ws = mock_ws  # pyright: ignore[reportPrivateUsage]

        with pytest.raises(FileExistsError):
            await client.music_get("7f3a91", tmp_path)

    @pytest.mark.asyncio
    async def test_music_get_rejects_traversing_part(self, tmp_path: Path) -> None:
        """A manifest naming a traversing part raises and writes nothing."""
        mock_ws = _make_mock_ws()
        mock_ws.recv = AsyncMock(
            return_value=json.dumps(
                {
                    "type": "manifest",
                    "id": "m1",
                    "album": "warm-pads-7f3a91",
                    "parts": [{"part": "../escape.mp3", "bytes": 3}],
                }
            )
        )
        client = VoxClient(port=8421, token="tok")
        client._transport._ws = mock_ws  # pyright: ignore[reportPrivateUsage]

        with pytest.raises(VoxError):
            await client.music_get("7f3a91", tmp_path)
        assert list(tmp_path.iterdir()) == []

    @pytest.mark.asyncio
    async def test_music_get_rejects_absolute_album_name(self, tmp_path: Path) -> None:
        """A manifest with an absolute album name raises and writes nothing."""
        mock_ws = _make_mock_ws()
        mock_ws.recv = AsyncMock(
            return_value=json.dumps(
                {
                    "type": "manifest",
                    "id": "m1",
                    "album": "/etc/pwned",
                    "parts": [{"part": "001.mp3", "bytes": 3}],
                }
            )
        )
        client = VoxClient(port=8421, token="tok")
        client._transport._ws = mock_ws  # pyright: ignore[reportPrivateUsage]

        with pytest.raises(VoxError):
            await client.music_get("7f3a91", tmp_path)
        assert list(tmp_path.iterdir()) == []

    @pytest.mark.asyncio
    async def test_music_get_empty_album_raises_and_writes_nothing(
        self, tmp_path: Path
    ) -> None:
        """A zero-ready-part album raises before mkdir, leaving dest_dir untouched.

        The manifest lists only ready parts, so an empty list means the album is
        still generating. Exporting it must not leave a hollow ``<album>/`` dir,
        which would then block a real export on the collision guard (D-1).
        """
        mock_ws = _make_mock_ws()
        mock_ws.recv = AsyncMock(
            return_value=json.dumps(
                {
                    "type": "manifest",
                    "id": "m1",
                    "album": "warm-pads-7f3a91",
                    "parts": [],
                }
            )
        )
        client = VoxClient(port=8421, token="tok")
        client._transport._ws = mock_ws  # pyright: ignore[reportPrivateUsage]

        with pytest.raises(ValueError, match="no ready parts"):
            await client.music_get("7f3a91", tmp_path)
        assert list(tmp_path.iterdir()) == []

    @pytest.mark.asyncio
    async def test_music_get_after_empty_succeeds_once_parts_exist(
        self, tmp_path: Path
    ) -> None:
        """A get that failed empty leaves nothing, so a later real get still works."""
        empty_ws = _make_mock_ws()
        empty_ws.recv = AsyncMock(
            return_value=json.dumps(
                {
                    "type": "manifest",
                    "id": "m1",
                    "album": "warm-pads-7f3a91",
                    "parts": [],
                }
            )
        )
        client = VoxClient(port=8421, token="tok")
        client._transport._ws = empty_ws  # pyright: ignore[reportPrivateUsage]
        with pytest.raises(ValueError, match="no ready parts"):
            await client.music_get("7f3a91", tmp_path)

        part = b"track-bytes"
        ready_ws = _make_mock_ws()
        ready_ws.recv = AsyncMock(
            side_effect=[
                json.dumps(
                    {
                        "type": "manifest",
                        "id": "m2",
                        "album": "warm-pads-7f3a91",
                        "parts": [{"part": "001.mp3", "bytes": len(part)}],
                    }
                ),
                *[json.dumps(f) for f in _fetch_frames(part, chunk=4)],
            ]
        )
        client._transport._ws = ready_ws  # pyright: ignore[reportPrivateUsage]

        target = await client.music_get("7f3a91", tmp_path)
        assert target == tmp_path / "warm-pads-7f3a91"
        assert (target / "001.mp3").read_bytes() == part

    @pytest.mark.asyncio
    async def test_music_get_writes_all_parts_of_multi_part_album(
        self, tmp_path: Path
    ) -> None:
        """A multi-part album chunk-fetches and writes every ready part."""
        first, second = b"first-track", b"second"
        mock_ws = _make_mock_ws()
        mock_ws.recv = AsyncMock(
            side_effect=[
                json.dumps(
                    {
                        "type": "manifest",
                        "id": "m1",
                        "album": "warm-pads-7f3a91",
                        "parts": [
                            {"part": "001.mp3", "bytes": len(first)},
                            {"part": "002.mp3", "bytes": len(second)},
                        ],
                    }
                ),
                *[json.dumps(f) for f in _fetch_frames(first, chunk=4)],
                *[json.dumps(f) for f in _fetch_frames(second, chunk=4)],
            ]
        )
        client = VoxClient(port=8421, token="tok")
        client._transport._ws = mock_ws  # pyright: ignore[reportPrivateUsage]

        target = await client.music_get("7f3a91", tmp_path)
        assert (target / "001.mp3").read_bytes() == first
        assert (target / "002.mp3").read_bytes() == second


class TestVoxClientVoices:
    """Test voices method."""

    @pytest.mark.asyncio
    async def test_voices(self) -> None:
        mock_ws = _make_mock_ws()
        mock_ws.recv = AsyncMock(
            return_value=json.dumps(
                {
                    "type": "voices",
                    "provider": "elevenlabs",
                    "voices": ["drew", "matilda"],
                }
            )
        )
        client = VoxClient(port=8421, token="tok")
        client._transport._ws = mock_ws  # pyright: ignore[reportPrivateUsage]

        result = await client.voices(provider="elevenlabs")
        assert result == ["drew", "matilda"]

        sent = json.loads(mock_ws.send.call_args[0][0])
        assert sent["provider"] == "elevenlabs"

    @pytest.mark.asyncio
    async def test_voices_always_sends_provider_on_the_wire(self) -> None:
        """The wire always carries a provider -- state, not the daemon, decides."""
        mock_ws = _make_mock_ws()
        mock_ws.recv = AsyncMock(
            return_value=json.dumps(
                {"type": "voices", "provider": "say", "voices": ["fred"]}
            )
        )
        client = VoxClient(port=8421, token="tok")
        client._transport._ws = mock_ws  # pyright: ignore[reportPrivateUsage]

        result = await client.voices("say")
        assert result == ["fred"]
        sent = json.loads(mock_ws.send.call_args[0][0])
        assert sent["provider"] == "say"

    @pytest.mark.asyncio
    async def test_voices_missing_key_raises_protocol_error(self) -> None:
        """A response lacking 'voices' is a protocol error, not an empty list.

        A silent ``[]`` would make a misbehaving daemon indistinguishable
        from a provider that genuinely offers no voices.
        """
        mock_ws = _make_mock_ws()
        mock_ws.recv = AsyncMock(
            return_value=json.dumps({"type": "voices", "provider": "say"})
        )
        client = VoxClient(port=8421, token="tok")
        client._transport._ws = mock_ws  # pyright: ignore[reportPrivateUsage]

        with pytest.raises(VoxdProtocolError, match="missing 'voices' key"):
            await client.voices("say")

    @pytest.mark.asyncio
    async def test_voices_wire_rejection_raises_typed_error(self) -> None:
        """A ``{"type": "error"}`` frame surfaces as ``VoxdRejectionError``.

        The daemon carries the reason (unknown provider name) in ``message``;
        the client must raise a rejection subtype so surface code renders it
        distinctly from a legitimate empty roster.
        """
        mock_ws = _make_mock_ws()
        mock_ws.recv = AsyncMock(
            return_value=json.dumps(
                {
                    "type": "error",
                    "message": "Unknown provider ''. Available: elevenlabs, say",
                }
            )
        )
        client = VoxClient(port=8421, token="tok")
        client._transport._ws = mock_ws  # pyright: ignore[reportPrivateUsage]

        with pytest.raises(VoxdRejectionError, match="Unknown provider"):
            await client.voices("")


class TestVoxClientHealth:
    """Test health method."""

    @pytest.mark.asyncio
    async def test_health(self) -> None:
        mock_ws = _make_mock_ws()
        mock_ws.recv = AsyncMock(
            return_value=json.dumps(
                {
                    "type": "health",
                    "status": "ok",
                    "uptime_seconds": 42.5,
                    "queued": 0,
                    "port": 8421,
                    "pid": 4242,
                    "provider": "elevenlabs",
                    "daemon_version": "5.0.0",
                }
            )
        )
        client = VoxClient(port=8421, token="tok")
        client._transport._ws = mock_ws  # pyright: ignore[reportPrivateUsage]

        result = await client.health()
        assert result.status == "ok"
        assert result.uptime_seconds == 42.5
        assert result.port == 8421
        assert result.pid == 4242
        # ``provider`` is not on the wire (design §3.6 / D4): the daemon
        # has no provider of its own; per-provider readiness moved to the
        # ``provider_status`` op delivered by PR 3. An older-daemon
        # payload that still carries the key is silently ignored.
        assert not hasattr(result, "provider")
        assert result.daemon_version == "5.0.0"


class TestVoxClientProgram:
    """The program_* methods parse the daemon's wire replies into typed values."""

    def _client_returning(self, resp: dict[str, object]) -> VoxClient:
        client = VoxClient(port=8421, token="tok")
        mock_ws = _make_mock_ws()
        mock_ws.recv = AsyncMock(return_value=json.dumps(resp))
        client._transport._ws = mock_ws  # pyright: ignore[reportPrivateUsage]
        return client

    @pytest.mark.asyncio
    async def test_program_status_parses_into_program_status(self) -> None:
        client = self._client_returning(
            {
                "type": "program_status",
                "id": "x",
                "status": ProgramStatus.idle().to_dict(),
            }
        )

        status = await client.program_status()

        assert isinstance(status, ProgramStatus)
        assert status.is_idle

    @pytest.mark.asyncio
    async def test_program_stop_returns_applied_outcome(self) -> None:
        """A bare ack (no 'applied') reads as an applied CommandOutcome."""
        client = self._client_returning({"type": "program_stop", "id": "x"})

        outcome = await client.program_stop()

        assert outcome.applied is True

    @pytest.mark.asyncio
    async def test_program_next_reads_a_rejection(self) -> None:
        """An 'applied: false' reply becomes a rejected outcome with its reason."""
        client = self._client_returning(
            {
                "type": "program_next",
                "id": "x",
                "applied": False,
                "message": "lost race",
            }
        )

        outcome = await client.program_next()

        assert outcome.applied is False
        assert outcome.message == "lost race"

    @pytest.mark.asyncio
    async def test_program_list_parses_summaries(self) -> None:
        client = self._client_returning(
            {
                "type": "program_list",
                "id": "x",
                "programs": [
                    {
                        "id": "a3f1c9",
                        "style": "trance",
                        "vibe": "calm",
                        "name": "mix",
                        "format": "music",
                        "ready": 5,
                        "total": 12,
                    }
                ],
            }
        )

        catalog = await client.program_list()

        assert len(catalog) == 1
        assert catalog[0].id == "a3f1c9"
        assert catalog[0].ready == 5
        assert catalog[0].total == 12
        assert catalog[0].name == "mix"

    @pytest.mark.asyncio
    async def test_malformed_status_raises_protocol_error(self) -> None:
        """A malformed status payload surfaces as VoxdProtocolError, not ValueError.

        VoxClient promises every failure is a VoxError; a wire-parse ValueError
        from the daemon's reply must be wrapped, or an MCP tool catching only
        the Voxd* errors would leak a raw traceback.
        """
        client = self._client_returning(
            {"type": "program_status", "id": "x", "status": {"mode": "off"}}
        )
        with pytest.raises(VoxdProtocolError, match="malformed reply"):
            await client.program_status()

    @pytest.mark.asyncio
    async def test_malformed_catalog_raises_protocol_error(self) -> None:
        """A catalogue row missing a required field surfaces as VoxdProtocolError."""
        client = self._client_returning(
            {"type": "program_list", "id": "x", "programs": [{"id": "a3f1c9"}]}
        )
        with pytest.raises(VoxdProtocolError, match="malformed reply"):
            await client.program_list()


class TestVoxClientReconnect:
    """Test automatic reconnection."""

    @pytest.mark.asyncio
    async def test_reconnect_on_dead_connection(self) -> None:
        mock_ws_old = _make_mock_ws()
        mock_ws_old.ping = AsyncMock(side_effect=OSError("closed"))

        mock_ws_new = _make_mock_ws()
        mock_ws_new.recv = AsyncMock(
            return_value=json.dumps(
                {"type": "health", "status": "ok", "uptime_seconds": 1.0, "queued": 0}
            )
        )

        with (
            patch("punt_vox.client.read_port_file", return_value=8421),
            patch("punt_vox.client.read_token_file", return_value="tok"),
            patch(
                "punt_vox.client.websockets.asyncio.client.connect",
                new_callable=AsyncMock,
                return_value=mock_ws_new,
            ),
        ):
            client = VoxClient(port=8421, token="tok")
            client._transport._ws = mock_ws_old  # pyright: ignore[reportPrivateUsage]

            result = await client.health()
            assert result.status == "ok"
            assert client._transport._ws is mock_ws_new  # pyright: ignore[reportPrivateUsage]


# ---------------------------------------------------------------------------
# VoxClientSync
# ---------------------------------------------------------------------------


class TestVoxClientSync:
    """Test synchronous wrapper."""

    def test_health(self) -> None:
        health_resp = {
            "type": "health",
            "status": "ok",
            "uptime_seconds": 10.0,
            "queued": 0,
        }

        mock_ws = _make_mock_ws()
        mock_ws.recv = AsyncMock(return_value=json.dumps(health_resp))

        with patch(
            "punt_vox.client.websockets.asyncio.client.connect",
            new_callable=AsyncMock,
            return_value=mock_ws,
        ):
            sync_client = VoxClientSync(port=8421, token="tok")
            result = sync_client.health()
            assert result.status == "ok"

    def test_synthesize(self) -> None:
        mock_ws = _make_mock_ws()
        mock_ws.recv = AsyncMock(
            side_effect=[
                json.dumps({"type": "playing", "id": "x"}),
                json.dumps({"type": "done", "id": "x"}),
            ]
        )

        with patch(
            "punt_vox.client.websockets.asyncio.client.connect",
            new_callable=AsyncMock,
            return_value=mock_ws,
        ):
            sync_client = VoxClientSync(port=8421, token="tok")
            result = sync_client.synthesize("Hello")
            assert isinstance(result.request_id, str)
            assert result.deduped is False

    def test_synthesize_forwards_api_key(self) -> None:
        """Sync wrapper forwards api_key through to the WebSocket message.

        The CLI --api-key flag builds a SynthesisSpec(api_key=...), so this is
        the load-bearing wiring that carries the billing key from the command
        line into the ``synthesize`` JSON envelope.
        """
        mock_ws = _make_mock_ws()
        mock_ws.recv = AsyncMock(
            side_effect=[
                json.dumps({"type": "playing", "id": "x"}),
                json.dumps({"type": "done", "id": "x"}),
            ]
        )

        with patch(
            "punt_vox.client.websockets.asyncio.client.connect",
            new_callable=AsyncMock,
            return_value=mock_ws,
        ):
            sync_client = VoxClientSync(port=8421, token="tok")
            sync_client.synthesize(
                "Bill to project A", SynthesisSpec(api_key="sk_project_a")
            )

        sent = json.loads(mock_ws.send.call_args[0][0])
        assert sent["api_key"] == "sk_project_a"
        assert sent["text"] == "Bill to project A"

    def test_synthesize_forwards_a_longer_timeout(self) -> None:
        """Regression: vox call's speak() needs a longer bound than the
        default (a long reply's real synthesis time can exceed 30s) --
        the sync wrapper must actually forward *timeout* through to
        VoxClient.synthesize, not silently drop it on the bridge.
        """
        mock_ws = _make_mock_ws()

        async def _delayed_recv() -> str:
            await asyncio.sleep(0.05)
            return json.dumps({"type": "playing", "id": "x"})

        mock_ws.recv = AsyncMock(side_effect=_delayed_recv)

        with (
            patch(
                "punt_vox.client.websockets.asyncio.client.connect",
                new_callable=AsyncMock,
                return_value=mock_ws,
            ),
            patch("punt_vox.client._TIMEOUT_SYNTHESIS", 0.01),
        ):
            sync_client = VoxClientSync(port=8421, token="tok")
            # The default (0.01s) is shorter than the 0.05s delay --
            # timeout=1.0 must still cover it, proving it reached the
            # underlying VoxClient.synthesize call.
            result = sync_client.synthesize("Hello", timeout=1.0)
        assert isinstance(result.request_id, str)

    def test_chime(self) -> None:
        mock_ws = _make_mock_ws()
        mock_ws.recv = AsyncMock(
            side_effect=[
                json.dumps({"type": "playing", "id": "chime:done"}),
                json.dumps({"type": "done", "id": "chime:done"}),
            ]
        )

        with patch(
            "punt_vox.client.websockets.asyncio.client.connect",
            new_callable=AsyncMock,
            return_value=mock_ws,
        ):
            sync_client = VoxClientSync(port=8421, token="tok")
            sync_client.chime("done")

    def test_record(self) -> None:
        mock_ws = _make_mock_ws()
        mock_ws.recv = AsyncMock(
            return_value=json.dumps(
                {
                    "type": "audio",
                    "id": "r1",
                    "name": "z.mp3",
                    "path": "/store/z.mp3",
                    "bytes": 20,
                }
            )
        )

        with patch(
            "punt_vox.client.websockets.asyncio.client.connect",
            new_callable=AsyncMock,
            return_value=mock_ws,
        ):
            sync_client = VoxClientSync(port=8421, token="tok")
            result = sync_client.record("Hello")
            assert result.name == "z.mp3"
            assert result.store_path == Path("/store/z.mp3")
            assert result.byte_count == 20

    def test_voices(self) -> None:
        mock_ws = _make_mock_ws()
        mock_ws.recv = AsyncMock(
            return_value=json.dumps(
                {"type": "voices", "provider": "say", "voices": ["fred"]}
            )
        )

        with patch(
            "punt_vox.client.websockets.asyncio.client.connect",
            new_callable=AsyncMock,
            return_value=mock_ws,
        ):
            sync_client = VoxClientSync(port=8421, token="tok")
            result = sync_client.voices("say")
            assert result == ["fred"]


class TestVoxClientSyncFrameDispatch:
    """Pin every ``VoxClientSync`` method to the frame it puts on the wire.

    The bug class this class defends against is *wrong arguments reaching
    the wire*: a swapped positional, a dropped keyword, a default that
    differs between the sync signature and the async signature, or a
    lambda that calls the wrong client method entirely. A test that only
    asserts on the return value is blind to every one of these -- the
    mocked websocket returns whatever the test told it to regardless of
    what was sent. Only the sent frame proves the arguments survived
    both the sync-to-async bridge in ``client_sync.py`` and VoxClient's
    own message assembly.

    Each test wires ``punt_vox.client.websockets.asyncio.client.connect``
    to an ``AsyncMock`` websocket that yields the RESPONSE frames the
    real daemon would send for the op under test. It calls the sync
    method with concrete arguments, then reads
    ``mock_ws.send.call_args_list`` to parse every frame that reached
    the wire. Assertions check the op name and every argument-derived
    field. Return-value checks appear only where the argument path
    passes through the return value (e.g. ``set_log_level`` echoes
    back the effective level).

    Note on the async-context path. ``_SyncRunner._run_in_thread``
    fires when the caller is already inside a running event loop; a
    dedicated test at the bottom exercises that branch under
    ``@pytest.mark.asyncio`` so both runner paths are executed.
    """

    @staticmethod
    def _sync() -> VoxClientSync:
        return VoxClientSync(port=8421, token="tok")

    @staticmethod
    def _mock_ws_yielding(*frames: dict[str, object]) -> AsyncMock:
        """Return a mock websocket whose recv() yields *frames* in order.

        Each frame is JSON-encoded to match the wire; the underlying
        transport reads recv() as a string and json.loads() it.
        """
        ws = _make_mock_ws()
        ws.recv = AsyncMock(side_effect=[json.dumps(f) for f in frames])
        return ws

    @staticmethod
    def _sent_frames(mock_ws: AsyncMock) -> list[dict[str, object]]:
        """Return every frame the client sent, parsed from JSON.

        A single call may put more than one frame on the wire (e.g.
        ``music_get`` sends manifest + one fetch per part). Order is
        preserved; each entry is the parsed JSON object.
        """
        parsed: list[dict[str, object]] = []
        for call in mock_ws.send.call_args_list:
            (payload,) = call.args
            frame = json.loads(payload)
            parsed.append(frame)
        return parsed

    @classmethod
    def _run(
        cls,
        sync_call: Callable[[VoxClientSync], object],
        response_frames: Sequence[dict[str, object]],
    ) -> tuple[object, list[dict[str, object]]]:
        """Drive *sync_call* against a mock ws that yields *response_frames*.

        Returns the sync method's return value and the list of sent
        frames, parsed. This is the one entry point every test uses so
        the connect-patch shape lives in one place.
        """
        sync = cls._sync()
        mock_ws = cls._mock_ws_yielding(*response_frames)
        with patch(
            "punt_vox.client.websockets.asyncio.client.connect",
            new_callable=AsyncMock,
            return_value=mock_ws,
        ):
            result = sync_call(sync)
        return result, cls._sent_frames(mock_ws)

    # -- recordings store surface -----------------------------------------

    def test_play_puts_type_and_ref_on_the_wire(self) -> None:
        _, sent = self._run(
            lambda s: s.play("greeting.mp3"),
            [{"type": "playing", "id": "x"}, {"type": "done", "id": "x"}],
        )
        assert len(sent) == 1
        assert sent[0]["type"] == "play"
        assert sent[0]["ref"] == "greeting.mp3"

    def test_fetch_puts_type_and_ref_on_the_wire(self) -> None:
        # fetch reassembles a chunked stream; give the transport a
        # valid one-chunk sequence so it returns and we can inspect
        # the request frame that started it.
        blob = b"payload"
        chunks = _fetch_frames(blob, chunk=64)
        chunks[0]["ref"] = "greeting.mp3"
        chunks[-1]["ref"] = "greeting.mp3"
        result, sent = self._run(lambda s: s.fetch("greeting.mp3"), chunks)
        assert result == blob
        assert len(sent) == 1
        assert sent[0]["type"] == "fetch"
        assert sent[0]["ref"] == "greeting.mp3"

    def test_rec_list_puts_type_on_the_wire(self) -> None:
        _, sent = self._run(
            lambda s: s.rec_list(),
            [{"type": "rec_list", "entries": []}],
        )
        assert len(sent) == 1
        assert sent[0]["type"] == "rec_list"

    def test_rec_remove_puts_type_and_ref_on_the_wire(self) -> None:
        _, sent = self._run(
            lambda s: s.rec_remove("stale.mp3"),
            [{"type": "rec_remove"}],
        )
        assert len(sent) == 1
        assert sent[0]["type"] == "rec_remove"
        assert sent[0]["ref"] == "stale.mp3"

    # -- cache surface ----------------------------------------------------

    def test_cache_status_puts_type_on_the_wire(self) -> None:
        result, sent = self._run(
            lambda s: s.cache_status(),
            [
                {
                    "type": "cache_status",
                    "entries": 3,
                    "size_bytes": 4096,
                    "path": "cache",
                }
            ],
        )
        assert len(sent) == 1
        assert sent[0]["type"] == "cache_status"
        assert isinstance(result, CacheStatus)

    def test_cache_clear_puts_type_on_the_wire(self) -> None:
        result, sent = self._run(
            lambda s: s.cache_clear(),
            [{"type": "cache_clear", "cleared": 7}],
        )
        assert len(sent) == 1
        assert sent[0]["type"] == "cache_clear"
        assert result == 7

    def test_set_log_level_puts_type_and_level_on_the_wire(self) -> None:
        # set_log_level's level argument is the one the wire carries and
        # the one the daemon may clamp -- a lambda that dropped it would
        # silently send an empty request, and the daemon would clamp to
        # the audit floor and return "info", passing the return-value
        # check but breaking real callers.
        result, sent = self._run(
            lambda s: s.set_log_level("debug"),
            [{"type": "set_log_level", "level": "debug"}],
        )
        assert len(sent) == 1
        assert sent[0]["type"] == "set_log_level"
        assert sent[0]["level"] == "debug"
        assert result == "debug"

    # -- music catalog ----------------------------------------------------

    def test_music_new_puts_prompts_and_name_on_the_wire(self) -> None:
        # music_new drops the ``name`` field when it is None; supply a
        # non-None name and assert it makes the trip.
        prompts = PromptSet(base="lofi hip hop, slow", variations=())
        result, sent = self._run(
            lambda s: s.music_new(prompts, "focus-set"),
            [
                {"type": "generating", "id": "n1"},
                {"type": "album", "id": "n1", "album_id": "alb-9"},
            ],
        )
        assert len(sent) == 1
        assert sent[0]["type"] == "music_new"
        assert sent[0]["base_prompt"] == "lofi hip hop, slow"
        assert sent[0]["name"] == "focus-set"
        assert result == "alb-9"

    def test_music_new_default_name_omits_name_from_the_wire(self) -> None:
        # music_new(prompts) with name=None must send base_prompt without
        # a "name" key -- the daemon's spec is omit-the-absent-optional.
        # A lambda that always forwarded name (even when None) would
        # serialize a JSON null and change the daemon's parse path.
        prompts = PromptSet(base="ambient drones", variations=())
        _, sent = self._run(
            lambda s: s.music_new(prompts),
            [
                {"type": "generating", "id": "n2"},
                {"type": "album", "id": "n2", "album_id": "alb-10"},
            ],
        )
        assert len(sent) == 1
        assert sent[0]["type"] == "music_new"
        assert sent[0]["base_prompt"] == "ambient drones"
        assert "name" not in sent[0]

    def test_music_remove_puts_type_and_album_on_the_wire(self) -> None:
        # music_remove's wire key is ``album`` (not album_id) -- easy to
        # get wrong when the sync signature says album_id. Pin it.
        _, sent = self._run(
            lambda s: s.music_remove("alb-2"),
            [{"type": "music_remove"}],
        )
        assert len(sent) == 1
        assert sent[0]["type"] == "music_remove"
        assert sent[0]["album"] == "alb-2"

    def test_music_get_puts_manifest_request_on_the_wire(self, tmp_path: Path) -> None:
        # music_get first requests the album manifest, then chunk-fetches
        # each part. Give the manifest zero parts so it raises before any
        # mkdir/fetch happens -- the FIRST sent frame still proves the
        # album id reached the wire. dest_dir is client-side and never
        # crosses the wire, so it is not asserted here; the traversal
        # test in test_client for a real manifest covers that path.
        sync = self._sync()
        mock_ws = self._mock_ws_yielding(
            {"type": "music_manifest", "album": "alb-3", "parts": []}
        )
        with (
            patch(
                "punt_vox.client.websockets.asyncio.client.connect",
                new_callable=AsyncMock,
                return_value=mock_ws,
            ),
            pytest.raises(ValueError, match="no ready parts"),
        ):
            sync.music_get("alb-3", tmp_path)
        sent = self._sent_frames(mock_ws)
        assert len(sent) == 1
        assert sent[0]["type"] == "music_manifest"
        assert sent[0]["album"] == "alb-3"

    # -- program transport (session-free command group) -------------------

    def test_program_status_puts_type_on_the_wire(self) -> None:
        _, sent = self._run(
            lambda s: s.program_status(),
            [
                {
                    "type": "program_status",
                    "status": ProgramStatus.idle().to_dict(),
                }
            ],
        )
        assert len(sent) == 1
        assert sent[0]["type"] == "program_status"

    def test_program_on_puts_every_provided_kwarg_on_the_wire(self) -> None:
        # program_on has four keyword-only parameters and the code path
        # that translates ``prompts`` into two wire fields
        # (base_prompt + variations). A return-value test would pass
        # even with all four missing. Assert each present field.
        prompts = PromptSet(base="lofi base", variations=("v1", "v2"))
        _, sent = self._run(
            lambda s: s.program_on(
                style="lofi", vibe="focus", name="deep-work", prompts=prompts
            ),
            [{"type": "program_on"}],
        )
        assert len(sent) == 1
        frame = sent[0]
        assert frame["type"] == "program_on"
        assert frame["style"] == "lofi"
        assert frame["vibe"] == "focus"
        assert frame["name"] == "deep-work"
        assert frame["base_prompt"] == "lofi base"
        assert frame["variations"] == ["v1", "v2"]

    def test_program_on_omits_absent_kwargs_from_the_wire(self) -> None:
        # program_on with no args must send only ``type`` and ``id``.
        # A default of None on any of the four kwargs that leaked
        # through as a JSON null would change the daemon's parse
        # behaviour -- pin the omit-the-absent-optional contract.
        _, sent = self._run(
            lambda s: s.program_on(),
            [{"type": "program_on"}],
        )
        assert len(sent) == 1
        frame = sent[0]
        assert frame["type"] == "program_on"
        for absent in ("style", "vibe", "name", "base_prompt", "variations"):
            assert absent not in frame

    def test_program_stop_puts_type_on_the_wire(self) -> None:
        _, sent = self._run(
            lambda s: s.program_stop(),
            [{"type": "program_stop"}],
        )
        assert len(sent) == 1
        assert sent[0]["type"] == "program_stop"

    def test_program_next_puts_type_on_the_wire(self) -> None:
        _, sent = self._run(
            lambda s: s.program_next(),
            [{"type": "program_next"}],
        )
        assert len(sent) == 1
        assert sent[0]["type"] == "program_next"

    def test_program_prev_puts_type_on_the_wire(self) -> None:
        _, sent = self._run(
            lambda s: s.program_prev(),
            [{"type": "program_prev"}],
        )
        assert len(sent) == 1
        assert sent[0]["type"] == "program_prev"

    def test_program_pause_puts_type_on_the_wire(self) -> None:
        _, sent = self._run(
            lambda s: s.program_pause(),
            [{"type": "program_pause"}],
        )
        assert len(sent) == 1
        assert sent[0]["type"] == "program_pause"

    def test_program_resume_puts_type_on_the_wire(self) -> None:
        _, sent = self._run(
            lambda s: s.program_resume(),
            [{"type": "program_resume"}],
        )
        assert len(sent) == 1
        assert sent[0]["type"] == "program_resume"

    def test_program_select_puts_every_provided_kwarg_on_the_wire(self) -> None:
        # program_select has four keyword-only parameters. album_id is
        # the direct-selection field; style/vibe/name are the tag path.
        # A single test with all four exercises the whole assembly.
        _, sent = self._run(
            lambda s: s.program_select(
                style="lofi", vibe="focus", name="deep-work", album_id="alb-4"
            ),
            [{"type": "program_select"}],
        )
        assert len(sent) == 1
        frame = sent[0]
        assert frame["type"] == "program_select"
        assert frame["album_id"] == "alb-4"
        assert frame["style"] == "lofi"
        assert frame["vibe"] == "focus"
        assert frame["name"] == "deep-work"

    def test_program_list_puts_type_on_the_wire(self) -> None:
        _, sent = self._run(
            lambda s: s.program_list(),
            [{"type": "program_list", "programs": []}],
        )
        assert len(sent) == 1
        assert sent[0]["type"] == "program_list"

    # -- runner path when the caller is inside a running event loop -------

    @pytest.mark.asyncio
    async def test_sync_call_inside_running_loop_uses_thread_pool(self) -> None:
        # asyncio.get_running_loop().is_running() is True here, so
        # ``_SyncRunner.run`` takes the ``_run_in_thread`` branch --
        # the code the bridge refactor rewrote alongside the direct
        # path. Exercising one method under this branch proves the
        # generic Callable-factory return path threads a value out
        # of the worker thread. Every other test in this class uses
        # the direct ``asyncio.run`` path.
        _, sent = self._run(
            lambda s: s.program_status(),
            [
                {
                    "type": "program_status",
                    "status": ProgramStatus.idle().to_dict(),
                }
            ],
        )
        assert len(sent) == 1
        assert sent[0]["type"] == "program_status"


# ---------------------------------------------------------------------------
# Env var resolution (VOXD_HOST, VOXD_PORT, VOXD_TOKEN)
# ---------------------------------------------------------------------------


class TestEnvVarResolution:
    def test_voxd_host_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("VOXD_HOST", "10.0.0.1")
        client = VoxClient(port=8421, token="tok")
        assert client._transport._host == "10.0.0.1"  # pyright: ignore[reportPrivateUsage]

    def test_voxd_host_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("VOXD_HOST", raising=False)
        client = VoxClient(port=8421, token="tok")
        assert client._transport._host == "127.0.0.1"  # pyright: ignore[reportPrivateUsage]

    def test_voxd_host_explicit_wins(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("VOXD_HOST", "10.0.0.1")
        client = VoxClient(host="192.168.1.1", port=8421, token="tok")
        assert client._transport._host == "192.168.1.1"  # pyright: ignore[reportPrivateUsage]

    def test_voxd_port_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("VOXD_PORT", "9999")
        client = VoxClient(token="tok")
        assert client._transport._resolve_port() == 9999  # pyright: ignore[reportPrivateUsage]

    def test_voxd_port_invalid_falls_through(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setenv("VOXD_PORT", "not_a_number")
        monkeypatch.setattr("punt_vox.client._user_run_dir", lambda: tmp_path)
        client = VoxClient(token="tok")
        with pytest.raises(VoxdConnectionError, match="port file not found"):
            client._transport._resolve_port()  # pyright: ignore[reportPrivateUsage]

    def test_voxd_port_explicit_wins(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("VOXD_PORT", "9999")
        client = VoxClient(port=1234, token="tok")
        assert client._transport._resolve_port() == 1234  # pyright: ignore[reportPrivateUsage]

    def test_voxd_token_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("VOXD_TOKEN", "remote-secret")
        client = VoxClient(port=8421)
        assert client._transport._resolve_token() == "remote-secret"  # pyright: ignore[reportPrivateUsage]

    def test_voxd_token_explicit_wins(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("VOXD_TOKEN", "remote-secret")
        client = VoxClient(port=8421, token="local-secret")
        assert client._transport._resolve_token() == "local-secret"  # pyright: ignore[reportPrivateUsage]

    def test_sync_client_inherits_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("VOXD_HOST", "10.0.0.1")
        sync_client = VoxClientSync(port=8421, token="tok")
        assert sync_client._host == "10.0.0.1"  # pyright: ignore[reportPrivateUsage]
