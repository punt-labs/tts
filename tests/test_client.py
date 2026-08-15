"""Tests for punt_vox.client -- WebSocket client for voxd."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from punt_vox.client import (
    VoxClient,
    read_port_file,
    read_token_file,
)
from punt_vox.client_errors import VoxdConnectionError, VoxdProtocolError, VoxError
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


class TestVoxClientSyncDispatch:
    """Verify every ``VoxClientSync`` method dispatches to the correct
    ``VoxClient`` async method with the arguments the caller supplied.

    These are DISPATCH tests, not synthesis tests. The point is to catch
    the class of break a generic Callable-factory bridge would leak through
    the type checker: a lambda that passes positional-when-keyword,
    calls the wrong method, drops an argument, or reorders positional
    args. `AsyncMock(spec=VoxClient)` enforces the signature of each
    method it stands in for -- a wrong keyword or arity raises at call
    time, and ``assert_awaited_once_with(...)`` pins the exact argument
    shape the lambda produced.

    Rationale: 18 methods on ``VoxClientSync`` had no end-to-end
    coverage of the async-to-sync bridge; ``TestVoxClientSync`` above
    exercises 5 through a mock websocket. This class exercises the
    remaining 18 (plus ``chime`` again for the sentinel-passthrough
    shape, and ``_run_in_thread`` once because the thread-pool
    path is code the bridge refactor rewrote too). Each test runs
    against ``AsyncMock(spec=VoxClient)`` -- no network, no daemon --
    which makes them fast and lets ``spec=`` do the signature-enforcement
    the type checker cannot.
    """

    @staticmethod
    def _sync_and_mock_client() -> tuple[VoxClientSync, AsyncMock]:
        """Return a real ``VoxClientSync`` and the mock its ``_drive`` will get.

        ``patch.object(sync, "_make_client")`` inside each test swaps the
        real VoxClient construction for the returned AsyncMock, so the
        lambda inside ``_drive`` calls the mock. ``AsyncMock(spec=VoxClient)``
        gives every async method on the mock the SIGNATURE of the real
        VoxClient method -- a lambda that passes a wrong keyword or
        arity fails at await time rather than passing silently. That is
        the whole reason this test class exists.
        """
        sync = VoxClientSync(port=8421, token="tok")
        mock_client = AsyncMock(spec=VoxClient)
        return sync, mock_client

    # -- synthesis surface (chime not repeated; covered above) ------------

    def test_play_dispatches_ref(self) -> None:
        sync, mock_client = self._sync_and_mock_client()
        mock_client.play.return_value = None
        with patch.object(VoxClientSync, "_make_client", return_value=mock_client):
            sync.play("recordings/x.mp3")
        mock_client.play.assert_awaited_once_with("recordings/x.mp3")

    def test_fetch_dispatches_ref_and_returns_bytes(self) -> None:
        sync, mock_client = self._sync_and_mock_client()
        mock_client.fetch.return_value = b"audio-bytes"
        with patch.object(VoxClientSync, "_make_client", return_value=mock_client):
            result = sync.fetch("recordings/y.mp3")
        mock_client.fetch.assert_awaited_once_with("recordings/y.mp3")
        assert result == b"audio-bytes"

    # -- program transport (11 methods) -----------------------------------

    def test_program_status_dispatches_no_args(self) -> None:
        sync, mock_client = self._sync_and_mock_client()
        expected = MagicMock(name="ProgramStatus")
        mock_client.program_status.return_value = expected
        with patch.object(VoxClientSync, "_make_client", return_value=mock_client):
            result = sync.program_status()
        mock_client.program_status.assert_awaited_once_with()
        assert result is expected

    def test_program_on_dispatches_all_keywords(self) -> None:
        # This is the method with the most argument-shape risk: four
        # keyword-only parameters that the lambda forwards. A swap or
        # a dropped keyword would type-check clean and break at runtime.
        sync, mock_client = self._sync_and_mock_client()
        expected = MagicMock(name="CommandOutcome")
        mock_client.program_on.return_value = expected
        prompts = MagicMock(name="PromptSet")
        with patch.object(VoxClientSync, "_make_client", return_value=mock_client):
            result = sync.program_on(
                style="lofi", vibe="focus", name="album", prompts=prompts
            )
        mock_client.program_on.assert_awaited_once_with(
            style="lofi", vibe="focus", name="album", prompts=prompts
        )
        assert result is expected

    def test_program_stop_dispatches_no_args(self) -> None:
        sync, mock_client = self._sync_and_mock_client()
        expected = MagicMock(name="CommandOutcome")
        mock_client.program_stop.return_value = expected
        with patch.object(VoxClientSync, "_make_client", return_value=mock_client):
            result = sync.program_stop()
        mock_client.program_stop.assert_awaited_once_with()
        assert result is expected

    def test_program_next_dispatches_no_args(self) -> None:
        sync, mock_client = self._sync_and_mock_client()
        expected = MagicMock(name="CommandOutcome")
        mock_client.program_next.return_value = expected
        with patch.object(VoxClientSync, "_make_client", return_value=mock_client):
            result = sync.program_next()
        mock_client.program_next.assert_awaited_once_with()
        assert result is expected

    def test_program_prev_dispatches_no_args(self) -> None:
        sync, mock_client = self._sync_and_mock_client()
        expected = MagicMock(name="CommandOutcome")
        mock_client.program_prev.return_value = expected
        with patch.object(VoxClientSync, "_make_client", return_value=mock_client):
            result = sync.program_prev()
        mock_client.program_prev.assert_awaited_once_with()
        assert result is expected

    def test_program_pause_dispatches_no_args(self) -> None:
        sync, mock_client = self._sync_and_mock_client()
        expected = MagicMock(name="CommandOutcome")
        mock_client.program_pause.return_value = expected
        with patch.object(VoxClientSync, "_make_client", return_value=mock_client):
            result = sync.program_pause()
        mock_client.program_pause.assert_awaited_once_with()
        assert result is expected

    def test_program_resume_dispatches_no_args(self) -> None:
        sync, mock_client = self._sync_and_mock_client()
        expected = MagicMock(name="CommandOutcome")
        mock_client.program_resume.return_value = expected
        with patch.object(VoxClientSync, "_make_client", return_value=mock_client):
            result = sync.program_resume()
        mock_client.program_resume.assert_awaited_once_with()
        assert result is expected

    def test_program_select_dispatches_all_keywords(self) -> None:
        # Same shape as program_on: four keyword-only parameters.
        sync, mock_client = self._sync_and_mock_client()
        expected = MagicMock(name="CommandOutcome")
        mock_client.program_select.return_value = expected
        with patch.object(VoxClientSync, "_make_client", return_value=mock_client):
            result = sync.program_select(
                style="lofi", vibe="focus", name="album", album_id="a1"
            )
        mock_client.program_select.assert_awaited_once_with(
            style="lofi", vibe="focus", name="album", album_id="a1"
        )
        assert result is expected

    def test_program_list_dispatches_no_args(self) -> None:
        sync, mock_client = self._sync_and_mock_client()
        expected = MagicMock(name="tuple[ProgramSummary, ...]")
        mock_client.program_list.return_value = expected
        with patch.object(VoxClientSync, "_make_client", return_value=mock_client):
            result = sync.program_list()
        mock_client.program_list.assert_awaited_once_with()
        assert result is expected

    # -- recordings store -------------------------------------------------

    def test_rec_list_dispatches_no_args(self) -> None:
        sync, mock_client = self._sync_and_mock_client()
        expected = MagicMock(name="tuple[RecordingSummary, ...]")
        mock_client.rec_list.return_value = expected
        with patch.object(VoxClientSync, "_make_client", return_value=mock_client):
            result = sync.rec_list()
        mock_client.rec_list.assert_awaited_once_with()
        assert result is expected

    def test_rec_remove_dispatches_ref(self) -> None:
        sync, mock_client = self._sync_and_mock_client()
        mock_client.rec_remove.return_value = None
        with patch.object(VoxClientSync, "_make_client", return_value=mock_client):
            sync.rec_remove("z.mp3")
        mock_client.rec_remove.assert_awaited_once_with("z.mp3")

    # -- cache ------------------------------------------------------------

    def test_cache_status_dispatches_no_args(self) -> None:
        sync, mock_client = self._sync_and_mock_client()
        expected = MagicMock(name="CacheStatus")
        mock_client.cache_status.return_value = expected
        with patch.object(VoxClientSync, "_make_client", return_value=mock_client):
            result = sync.cache_status()
        mock_client.cache_status.assert_awaited_once_with()
        assert result is expected

    def test_cache_clear_dispatches_no_args_and_returns_int(self) -> None:
        sync, mock_client = self._sync_and_mock_client()
        mock_client.cache_clear.return_value = 42
        with patch.object(VoxClientSync, "_make_client", return_value=mock_client):
            result = sync.cache_clear()
        mock_client.cache_clear.assert_awaited_once_with()
        assert result == 42

    def test_set_log_level_dispatches_level_and_returns_str(self) -> None:
        sync, mock_client = self._sync_and_mock_client()
        mock_client.set_log_level.return_value = "info"
        with patch.object(VoxClientSync, "_make_client", return_value=mock_client):
            result = sync.set_log_level("debug")
        mock_client.set_log_level.assert_awaited_once_with("debug")
        assert result == "info"

    # -- music catalog ----------------------------------------------------

    def test_music_new_dispatches_prompts_and_name(self) -> None:
        # music_new(prompts, name=None): positional prompts, optional
        # positional-or-keyword name. Both my lambda and VoxClient use
        # positional forwarding; a mismatch (e.g. lambda passing name
        # by keyword when the SDK expected positional-only) would fail
        # here at spec-check time.
        sync, mock_client = self._sync_and_mock_client()
        mock_client.music_new.return_value = "album-id"
        prompts = MagicMock(name="PromptSet")
        with patch.object(VoxClientSync, "_make_client", return_value=mock_client):
            result = sync.music_new(prompts, "my-album")
        mock_client.music_new.assert_awaited_once_with(prompts, "my-album")
        assert result == "album-id"

    def test_music_new_default_name_forwards_none(self) -> None:
        # music_new(prompts) with no name: the sync signature has a
        # default of None, and the lambda captures that default and
        # forwards it explicitly. Pinning the default's traversal.
        sync, mock_client = self._sync_and_mock_client()
        mock_client.music_new.return_value = "album-id"
        prompts = MagicMock(name="PromptSet")
        with patch.object(VoxClientSync, "_make_client", return_value=mock_client):
            sync.music_new(prompts)
        mock_client.music_new.assert_awaited_once_with(prompts, None)

    def test_music_get_dispatches_id_and_dest(self) -> None:
        sync, mock_client = self._sync_and_mock_client()
        dest = Path("/tmp/x")
        mock_client.music_get.return_value = dest
        with patch.object(VoxClientSync, "_make_client", return_value=mock_client):
            result = sync.music_get("album-1", dest)
        mock_client.music_get.assert_awaited_once_with("album-1", dest)
        assert result == dest

    def test_music_remove_dispatches_album_id(self) -> None:
        sync, mock_client = self._sync_and_mock_client()
        mock_client.music_remove.return_value = None
        with patch.object(VoxClientSync, "_make_client", return_value=mock_client):
            sync.music_remove("album-2")
        mock_client.music_remove.assert_awaited_once_with("album-2")

    # -- bridge lifecycle (verify connect + close bracket every call) -----

    def test_every_dispatch_opens_and_closes_a_connection(self) -> None:
        # The _drive helper's connect-await-close bracket runs on EVERY
        # call. This test exercises one arbitrary method and asserts the
        # bracket, so a refactor that dropped the finally: close (or the
        # await) is caught even when the method under test does not
        # otherwise assert on side effects.
        sync, mock_client = self._sync_and_mock_client()
        mock_client.program_status.return_value = MagicMock(name="ProgramStatus")
        with patch.object(VoxClientSync, "_make_client", return_value=mock_client):
            sync.program_status()
        mock_client.connect.assert_awaited_once_with()
        mock_client.close.assert_awaited_once_with()

    def test_close_awaited_even_when_op_raises(self) -> None:
        # The finally clause in _drive is load-bearing: a synthesize
        # that raises must still close the connection, or a hook that
        # fails silently leaks sockets over a session. A refactor that
        # dropped the try/finally would pass every happy-path test and
        # fail this one.
        sync, mock_client = self._sync_and_mock_client()
        mock_client.program_status.side_effect = RuntimeError("boom")
        with (
            patch.object(VoxClientSync, "_make_client", return_value=mock_client),
            pytest.raises(RuntimeError, match="boom"),
        ):
            sync.program_status()
        mock_client.close.assert_awaited_once_with()


class TestVoxClientSyncRunnerInRunningLoop:
    """The ``_run_in_thread`` branch runs when the caller is already inside
    an event loop (e.g. an MCP tool handler). The refactor rewrote both
    the runner method and its thread-pool sibling to be generic; this
    test executes the thread-pool path so `pytest --cov` does not report
    it uncovered.
    """

    @pytest.mark.asyncio
    async def test_sync_call_inside_a_running_loop_uses_thread_pool(
        self,
    ) -> None:
        # ``asyncio.get_running_loop().is_running()`` returns True here
        # because pytest-asyncio has already started a loop for us, so
        # ``_SyncRunner.run`` takes the ``_run_in_thread`` branch. The
        # test proves the whole path (spawn thread, asyncio.run in it,
        # get the result back) still returns the coroutine's value.
        sync = VoxClientSync(port=8421, token="tok")
        mock_client = AsyncMock(spec=VoxClient)
        expected = MagicMock(name="ProgramStatus")
        mock_client.program_status.return_value = expected
        with patch.object(VoxClientSync, "_make_client", return_value=mock_client):
            result = sync.program_status()
        assert result is expected
        mock_client.program_status.assert_awaited_once_with()


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
