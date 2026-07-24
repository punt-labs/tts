"""Tests for the rec + music-catalog ``mic`` MCP tools (D-7 parity).

The recordings-store verbs (``rec_new``/``rec_list``/``rec_play``/``rec_get``/
``rec_remove``) and the catalog-authoring verbs (``music_new``/``music_get``/
``music_remove``) live on the :class:`~punt_vox.server_audio_tools.RecTools`
and :class:`~punt_vox.server_audio_tools.MusicCatalogTools` humble objects.
Each is driven directly with an in-memory client factory -- no daemon, no
socket -- so the
argument passthrough, the bare-id result shape, MCP-appropriate ``get`` forms,
error surfacing, and the rec-vs-catalog routing are asserted without a wire.

The registration and one-code-path tests confirm every verb is exposed on the
``mic`` surface at parity with the CLI, and that a tool and its CLI twin issue
the same engine call against the same client (the projection model's one code
path).
"""

from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import TYPE_CHECKING, Self, cast, final
from unittest.mock import MagicMock

import pytest

from punt_vox.cli_rec import ClientRecordGateway, RecCli
from punt_vox.client import RecordingSummary, RecordResult
from punt_vox.client_errors import VoxdConnectionError, VoxdProtocolError
from punt_vox.output_formatter import OutputFormatter
from punt_vox.server import mcp
from punt_vox.server_audio_tools import MusicCatalogTools, RecTools

if TYPE_CHECKING:
    from collections.abc import Callable

    from punt_vox.client_sync import VoxClientSync
    from punt_vox.types_synthesis import SynthesisSpec


@pytest.fixture(autouse=True)
def _fresh_session(monkeypatch: pytest.MonkeyPatch) -> None:  # pyright: ignore[reportUnusedFunction]
    """Reset the module session and pin config discovery to a no-op.

    ``RecTools.new`` refreshes the session from config; a fresh session plus a
    ``None`` config dir keeps that a pure no-op so the tests exercise only the
    tool's own logic.
    """
    import punt_vox.server as srv

    monkeypatch.setattr(srv, "_session", srv.SessionConfig())
    monkeypatch.setattr(srv, "_find_config_dir", lambda: None)


@final
class _FakeClient:
    """A filesystem-free ``VoxClientSync`` stand-in: records calls, returns data.

    Matches the client surface the tools touch by shape (structural typing); a
    test seeds a store and reads back the recorded calls to assert passthrough.
    """

    __slots__ = ("_albums", "_store", "calls")
    _store: dict[str, bytes]
    _albums: dict[str, str]
    calls: list[tuple[str, tuple[object, ...]]]

    def __new__(
        cls,
        store: dict[str, bytes] | None = None,
        albums: dict[str, str] | None = None,
    ) -> Self:
        self = super().__new__(cls)
        self._store = dict(store) if store is not None else {}
        self._albums = dict(albums) if albums is not None else {}
        self.calls = []
        return self

    def record(
        self, text: str, spec: SynthesisSpec, *, name: str | None = None
    ) -> RecordResult:
        store_name = name or f"{abs(hash(text)) % (10**12):012d}.mp3"
        data = text.encode()
        self._store[store_name] = data
        self.calls.append(("record", (text, spec, name)))
        return RecordResult(
            id=store_name,
            name=store_name,
            store_path=Path("/daemon/store") / store_name,
            byte_count=len(data),
        )

    def rec_list(self) -> tuple[RecordingSummary, ...]:
        self.calls.append(("rec_list", ()))
        return tuple(
            RecordingSummary(name=n, byte_count=len(b)) for n, b in self._store.items()
        )

    def play(self, ref: str) -> None:
        self.calls.append(("play", (ref,)))
        if ref not in self._store:
            raise VoxdProtocolError(f"no recording named '{ref}'")

    def fetch(self, ref: str) -> bytes:
        self.calls.append(("fetch", (ref,)))
        if ref not in self._store:
            raise VoxdProtocolError(f"no recording named '{ref}'")
        return self._store[ref]

    def rec_remove(self, ref: str) -> None:
        self.calls.append(("rec_remove", (ref,)))
        if ref not in self._store:
            raise VoxdProtocolError(f"no recording named '{ref}'")
        del self._store[ref]

    def music_new(self, prompt: str, name: str | None = None) -> str:
        self.calls.append(("music_new", (prompt, name)))
        album_id = name or f"{len(self._albums):06x}"
        self._albums[album_id] = f"album-{album_id}"
        return album_id

    def music_get(self, album_id: str, dest_dir: Path) -> Path:
        self.calls.append(("music_get", (album_id, dest_dir)))
        if album_id not in self._albums:
            raise VoxdProtocolError(f"no album named '{album_id}'")
        return dest_dir / self._albums[album_id]

    def music_remove(self, album_id: str) -> None:
        self.calls.append(("music_remove", (album_id,)))
        if album_id not in self._albums:
            raise VoxdProtocolError(f"no album named '{album_id}'")
        if album_id == "playing":
            raise VoxdProtocolError(f"album {album_id} is playing; stop it first")
        del self._albums[album_id]


def _factory(client: _FakeClient) -> Callable[[], VoxClientSync]:
    """Adapt the structural fake to the tools' concrete client-factory type."""
    return cast("Callable[[], VoxClientSync]", lambda: client)


def _rec_tool(client_factory: Callable[[], VoxClientSync]) -> RecTools:
    """Build a ``RecTools`` over *client_factory* and the fresh test session.

    The session provider yields the module session the ``_fresh_session``
    fixture pins fresh, so ``rec_new``'s config refresh is a pure no-op.
    """
    import punt_vox.server as srv

    return RecTools(client_factory, lambda: srv._session)


def _rec(client: _FakeClient) -> RecTools:
    return _rec_tool(_factory(client))


def _catalog(client: _FakeClient) -> MusicCatalogTools:
    return MusicCatalogTools(_factory(client))


# ---------------------------------------------------------------------------
# Registration -- every verb is exposed on the mic surface (D-7)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_all_rec_and_catalog_verbs_registered() -> None:
    names = {tool.name for tool in await mcp.list_tools()}
    assert {
        "rec_new",
        "rec_list",
        "rec_play",
        "rec_get",
        "rec_remove",
        "music_new",
        "music_get",
        "music_remove",
    } <= names


@pytest.mark.asyncio
async def test_music_program_tools_still_registered_alongside() -> None:
    """The catalog verbs are added *alongside* the existing music tools."""
    names = {tool.name for tool in await mcp.list_tools()}
    assert {"music", "music_play", "music_list", "music_next"} <= names


# ---------------------------------------------------------------------------
# rec_new -- bare id, no daemon path (D-7)
# ---------------------------------------------------------------------------


def test_rec_new_returns_bare_id_no_path_or_host() -> None:
    result = json.loads(_rec(_FakeClient()).new(text="the build is green"))

    assert isinstance(result, list)
    assert len(result) == 1
    entry = result[0]
    assert set(entry) == {"id", "bytes", "cached"}  # bare id -- no path/host/name
    assert entry["id"].endswith(".mp3")
    assert "path" not in entry
    assert "host" not in entry


def test_rec_new_name_passthrough_to_client() -> None:
    fake = _FakeClient()

    json.loads(_rec(fake).new(text="hi", name="greeting.mp3"))

    assert ("record", ("hi", MagicMock)) != fake.calls[0]  # sanity: recorded a call
    verb, (text, _spec, name) = fake.calls[0]
    assert verb == "record"
    assert text == "hi"
    assert name == "greeting.mp3"


def test_rec_new_no_input_returns_error() -> None:
    result = json.loads(_rec(_FakeClient()).new())
    assert "error" in result


def test_rec_new_empty_name_multi_segment_rejected() -> None:
    fake = _FakeClient()

    result = json.loads(
        _rec(fake).new(segments=[{"text": "a"}, {"text": "b"}], name="")
    )

    assert "error" in result
    assert "single-segment" in result["error"]
    assert fake.calls == []  # never reached the client


def test_rec_new_empty_name_single_segment_sent_to_daemon() -> None:
    """An explicit "" reaches the client for the daemon to reject pre-ack."""
    client = MagicMock()
    client.record.side_effect = VoxdProtocolError("empty recording name")

    result = json.loads(_rec_tool(lambda: client).new(text="hi", name=""))

    assert "error" in result  # the daemon's rejection is surfaced
    assert client.record.call_args.kwargs["name"] == ""  # "" reached the wire


def test_rec_new_daemon_error_surfaces_as_clean_error() -> None:
    client = MagicMock()
    client.record.side_effect = VoxdConnectionError("not running")

    result = json.loads(_rec_tool(lambda: client).new(text="hi"))

    assert "error" in result


# ---------------------------------------------------------------------------
# rec_list
# ---------------------------------------------------------------------------


def test_rec_list_returns_id_and_bytes_rows() -> None:
    store = {"a1b2c3d4e5f6.mp3": b"x" * 10, "greeting.mp3": b"yy"}

    result = json.loads(_rec(_FakeClient(store)).list_recordings())

    assert result == {
        "recordings": [
            {"id": "a1b2c3d4e5f6.mp3", "bytes": 10},
            {"id": "greeting.mp3", "bytes": 2},
        ]
    }


def test_rec_list_empty_store() -> None:
    result = json.loads(_rec(_FakeClient()).list_recordings())
    assert result == {"recordings": []}


def test_rec_list_daemon_error_surfaces() -> None:
    client = MagicMock()
    client.rec_list.side_effect = VoxdConnectionError("not running")

    result = json.loads(_rec_tool(lambda: client).list_recordings())

    assert "error" in result


# ---------------------------------------------------------------------------
# rec_play
# ---------------------------------------------------------------------------


def test_rec_play_delegates_id_and_reports() -> None:
    fake = _FakeClient({"a1b2c3.mp3": b"x"})

    result = json.loads(_rec(fake).play("a1b2c3.mp3"))

    assert result == {"played": "a1b2c3.mp3"}
    assert ("play", ("a1b2c3.mp3",)) in fake.calls


def test_rec_play_failure_surfaces_not_silent_success() -> None:
    """A playback failure is a clean error, never a silent success."""
    result = json.loads(_rec(_FakeClient()).play("missing.mp3"))
    assert "error" in result


# ---------------------------------------------------------------------------
# rec_get -- agent form: base64 bytes, not a host-file write
# ---------------------------------------------------------------------------


def test_rec_get_returns_base64_bytes() -> None:
    payload = b"\xff\xfb\x90\x00" * 5
    result = json.loads(_rec(_FakeClient({"rec.mp3": payload})).get("rec.mp3"))

    assert result["id"] == "rec.mp3"
    assert result["bytes"] == len(payload)
    assert base64.b64decode(result["base64"]) == payload


def test_rec_get_large_payload_roundtrips_byte_correct() -> None:
    big = bytes(range(256)) * 4000  # ~1 MB
    result = json.loads(_rec(_FakeClient({"big.mp3": big})).get("big.mp3"))

    assert base64.b64decode(result["base64"]) == big


def test_rec_get_not_found_surfaces_error() -> None:
    result = json.loads(_rec(_FakeClient()).get("missing.mp3"))
    assert "error" in result


# ---------------------------------------------------------------------------
# rec_remove
# ---------------------------------------------------------------------------


def test_rec_remove_delegates_and_reports() -> None:
    fake = _FakeClient({"rec.mp3": b"x"})

    result = json.loads(_rec(fake).remove("rec.mp3"))

    assert result == {"removed": "rec.mp3"}
    assert ("rec_remove", ("rec.mp3",)) in fake.calls


def test_rec_remove_not_found_surfaces_error() -> None:
    result = json.loads(_rec(_FakeClient()).remove("missing.mp3"))
    assert "error" in result


# ---------------------------------------------------------------------------
# music_new -- verbatim prompt, bare album id, no confirmation
# ---------------------------------------------------------------------------


def test_music_new_passes_prompt_verbatim_and_returns_bare_id() -> None:
    fake = _FakeClient()
    prompt = "warm analog pads, slow, D minor, instrumental, loopable"

    result = json.loads(_catalog(fake).new(prompt))

    assert set(result) == {"album_id"}
    assert fake.calls == [("music_new", (prompt, None))]  # verbatim, no expansion


def test_music_new_name_passthrough() -> None:
    fake = _FakeClient()

    result = json.loads(_catalog(fake).new("ambient drone", name="focus-bed"))

    assert result["album_id"] == "focus-bed"
    assert fake.calls[0] == ("music_new", ("ambient drone", "focus-bed"))


def test_music_new_bad_prompt_surfaces_error() -> None:
    client = MagicMock()
    client.music_new.side_effect = VoxdProtocolError("bad_prompt")

    result = json.loads(MusicCatalogTools(lambda: client).new("copyrighted work"))

    assert "error" in result


# ---------------------------------------------------------------------------
# music_get -- agent form: export to a named destination, return the locator
# ---------------------------------------------------------------------------


def test_music_get_exports_to_named_dest_and_returns_path(tmp_path: Path) -> None:
    fake = _FakeClient(albums={"7f3a91": "warm-pads-7f3a91"})

    result = json.loads(_catalog(fake).get("7f3a91", str(tmp_path)))

    assert result["album_id"] == "7f3a91"
    assert result["path"] == str(tmp_path / "warm-pads-7f3a91")
    assert fake.calls[0] == ("music_get", ("7f3a91", tmp_path))


def test_music_get_unknown_album_surfaces_error(tmp_path: Path) -> None:
    result = json.loads(_catalog(_FakeClient()).get("missing", str(tmp_path)))
    assert "error" in result


# ---------------------------------------------------------------------------
# music_remove -- delete idle, refuse playing (D-2)
# ---------------------------------------------------------------------------


def test_music_remove_deletes_idle_album() -> None:
    fake = _FakeClient(albums={"7f3a91": "warm-pads-7f3a91"})

    result = json.loads(_catalog(fake).remove("7f3a91"))

    assert result == {"removed": "7f3a91"}
    assert ("music_remove", ("7f3a91",)) in fake.calls


def test_music_remove_playing_album_surfaces_refusal() -> None:
    fake = _FakeClient(albums={"playing": "live-album"})

    result = json.loads(_catalog(fake).remove("playing"))

    assert "error" in result
    assert "playing" in result["error"]


# ---------------------------------------------------------------------------
# Routing -- rec verbs hit rec ops, catalog verbs hit catalog ops
# ---------------------------------------------------------------------------


def test_rec_get_routes_to_fetch_not_music_get() -> None:
    fake = _FakeClient({"rec.mp3": b"data"})

    _rec(fake).get("rec.mp3")

    assert [verb for verb, _ in fake.calls] == ["fetch"]


def test_music_get_routes_to_music_get_not_fetch(tmp_path: Path) -> None:
    fake = _FakeClient(albums={"7f3a91": "warm-pads-7f3a91"})

    _catalog(fake).get("7f3a91", str(tmp_path))

    assert [verb for verb, _ in fake.calls] == ["music_get"]


# ---------------------------------------------------------------------------
# One code path -- the mic tool and its CLI twin issue the same engine call
# ---------------------------------------------------------------------------


def test_rec_new_and_cli_issue_the_same_client_record_call() -> None:
    """The mic ``rec_new`` tool and ``vox rec new`` hit the same engine op.

    Driving both surfaces against the same client proves neither reimplements
    the recording logic -- both funnel to ``VoxClientSync.record`` with the same
    arguments (the projection model's single code path).
    """
    tool_client = MagicMock()
    tool_client.record.return_value = RecordResult(
        id="x.mp3", name="x.mp3", store_path=Path("/s/x.mp3"), byte_count=3
    )
    cli_client = MagicMock()
    cli_client.record.return_value = RecordResult(
        id="x.mp3", name="x.mp3", store_path=Path("/s/x.mp3"), byte_count=3
    )

    _rec_tool(lambda: tool_client).new(text="hello", name="x.mp3")
    cli = RecCli(
        MagicMock(spec=OutputFormatter), lambda: ClientRecordGateway(cli_client)
    )
    cli.new(text="hello", name="x.mp3")

    tool_call = tool_client.record.call_args
    cli_call = cli_client.record.call_args
    assert tool_call.args[0] == cli_call.args[0] == "hello"  # same text
    assert tool_call.kwargs["name"] == cli_call.kwargs["name"] == "x.mp3"  # same name
