"""Parity: ``vox {model, provider, voice}`` CLI and ``mic:*`` MCP tools agree.

Both surfaces are thin adapters over the same three switch tools plus the
same ``ConfigStore.write_field`` choke-point. Driving both against one
in-memory session + one tmp-path config dir puts the same underlying state
behind both, and lets the two answers be compared field for field. A field
one surface reports and the other omits is the bug this file exists to catch
-- the vox-bx7b class of parity holes.

The comparison is on the *field set and values*, never the prose: the tool
returns JSON in its domain shape (``available`` / ``current``) and the CLI
emits JSON with a verb-shaped key (``names`` / ``current``), which is the
existing music-parity precedent (see ``_CLI_RENAMES``).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Any, Self, cast, final
from unittest.mock import MagicMock, patch

from typer.testing import CliRunner

from punt_vox.__main__ import app
from punt_vox.config import ConfigStore
from punt_vox.server_switches import ModelTool, ProviderTool, VoiceTool

if TYPE_CHECKING:
    from collections.abc import Callable

    import pytest

    from punt_vox.client_sync import VoxClientSync
    from punt_vox.server import SessionConfig

_CLI = "punt_vox.__main__"

# The CLI carries a verb-shaped key for the list ("names") while the tool
# carries the domain-shaped key ("available") -- the same _CLI_RENAMES axis
# test_music_surface_parity.py documents. Both keys hold the same list.
_CLI_LIST_KEY = "names"
_MCP_LIST_KEY = "available"


@final
class _FakeSession:
    """A minimal SessionConfig: mutable voice/provider/model, no config on disk."""

    __slots__ = ("model", "provider", "voice")
    voice: str | None
    provider: str | None
    model: str | None

    def __new__(
        cls,
        voice: str | None = None,
        provider: str | None = None,
        model: str | None = None,
    ) -> Self:
        self = super().__new__(cls)
        self.voice = voice
        self.provider = provider
        self.model = model
        return self

    def refresh_from_config(self) -> None:
        """Do nothing: the parity fixtures pin the session rather than read config."""


def _session_provider(
    session: _FakeSession,
) -> Callable[[], SessionConfig]:
    """Type-cast at the seam: the tools declare Callable[[], SessionConfig]."""
    return cast("Callable[[], SessionConfig]", lambda: session)


def _stub_client(voices: list[str] | None = None) -> MagicMock:
    """Return a MagicMock voxd client whose voices() returns *voices* (or [])."""
    client = MagicMock()
    client.voices.return_value = voices if voices is not None else []
    return client


def _client_factory(client: MagicMock) -> Callable[[], VoxClientSync]:
    """Type-cast at the seam: VoiceTool declares Callable[[], VoxClientSync]."""
    return cast("Callable[[], VoxClientSync]", lambda: client)


def _tool_list_payload(tool_result: str) -> dict[str, Any]:
    """Return the tool's no-arg list payload, mapped to the CLI's key shape."""
    payload = cast("dict[str, Any]", json.loads(tool_result))
    if _MCP_LIST_KEY in payload:
        payload[_CLI_LIST_KEY] = payload.pop(_MCP_LIST_KEY)
    return payload


def _cli_json_payload(result_output: str) -> dict[str, Any]:
    """Return the CLI's --json payload as a dict."""
    return cast("dict[str, Any]", json.loads(result_output))


# ---------------------------------------------------------------------------
# The verb sets themselves must match
# ---------------------------------------------------------------------------


def test_all_three_surfaces_expose_the_same_verbs() -> None:
    """A verb on one surface and not the other is a hole in the contract."""
    import punt_vox.server as srv

    cli_verbs = {c.name for c in app.registered_commands if c.name is not None}
    mcp_names = {tool.name for tool in srv.mcp._tool_manager.list_tools()}  # pyright: ignore[reportPrivateUsage]

    for verb in ("model", "provider", "voice"):
        assert verb in cli_verbs, f"CLI missing verb: {verb}"
        assert verb in mcp_names, f"MCP missing verb: {verb}"


# ---------------------------------------------------------------------------
# Model list -- same fields on both surfaces
# ---------------------------------------------------------------------------


def test_model_list_reports_the_same_fields(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No-arg model returns the same {names/available, current} on both surfaces."""
    (tmp_path / "vox.md").write_text(
        '---\nprovider: "elevenlabs"\nmodel: "eleven_v3"\n---\n'
    )

    # MCP surface: drive ModelTool directly with a matching fake session.
    session = _FakeSession(provider="elevenlabs", model="eleven_v3")
    model_tool = ModelTool(
        _session_provider(session), lambda: tmp_path, _client_factory(_stub_client())
    )
    tool_payload = _tool_list_payload(model_tool.dispatch())

    # CLI surface: run `vox model --json` against the same config dir.
    monkeypatch.setattr(f"{_CLI}.find_config_dir", lambda: tmp_path)
    runner = CliRunner()
    result = runner.invoke(app, ["model", "--json"])
    assert result.exit_code == 0
    cli_payload = _cli_json_payload(result.output)

    assert cli_payload["names"] == tool_payload["names"]
    assert cli_payload["current"] == tool_payload["current"]


def test_model_shorthand_resolves_identically(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``v3`` on both surfaces writes ``eleven_v3`` and cascades to the same voice.

    Under vox-awm9 both surfaces cascade voice = first-from-roster; both
    stub their voxd client to the same fake roster so both should return
    identical {model, voice} payloads and write identical fields on disk.
    """
    (tmp_path / "vox.md").write_text('---\nprovider: "elevenlabs"\n---\n')

    stub = _stub_client(voices=["matilda", "roger"])

    # MCP surface: dispatch through ModelTool.
    session = _FakeSession(provider="elevenlabs")
    model_tool = ModelTool(
        _session_provider(session), lambda: tmp_path, _client_factory(stub)
    )
    tool_payload = cast("dict[str, Any]", json.loads(model_tool.dispatch("v3")))
    tool_written = (
        ConfigStore(tmp_path).read_field("model"),
        ConfigStore(tmp_path).read_field("voice"),
    )

    # Reset config for the CLI leg.
    (tmp_path / "vox.md").write_text('---\nprovider: "elevenlabs"\n---\n')
    monkeypatch.setattr(f"{_CLI}.find_config_dir", lambda: tmp_path)
    runner = CliRunner()
    with patch(f"{_CLI}.VoxClientSync", return_value=stub):
        result = runner.invoke(app, ["model", "v3", "--json"])
    assert result.exit_code == 0
    cli_payload = _cli_json_payload(result.output)
    cli_written = (
        ConfigStore(tmp_path).read_field("model"),
        ConfigStore(tmp_path).read_field("voice"),
    )

    assert cli_payload == tool_payload == {"model": "eleven_v3", "voice": "matilda"}
    assert cli_written == tool_written == ("eleven_v3", "matilda")


# ---------------------------------------------------------------------------
# Provider list -- same fields on both surfaces
# ---------------------------------------------------------------------------


def test_provider_list_reports_the_same_fields(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "vox.md").write_text('---\nprovider: "openai"\n---\n')

    session = _FakeSession(provider="openai")
    provider_tool = ProviderTool(
        _session_provider(session), lambda: tmp_path, _client_factory(_stub_client())
    )
    tool_payload = _tool_list_payload(provider_tool.dispatch())

    monkeypatch.setattr(f"{_CLI}.find_config_dir", lambda: tmp_path)
    runner = CliRunner()
    result = runner.invoke(app, ["provider", "--json"])
    assert result.exit_code == 0
    cli_payload = _cli_json_payload(result.output)

    assert cli_payload["names"] == tool_payload["names"]
    assert cli_payload["current"] == tool_payload["current"]


def test_provider_set_writes_the_same_field(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Both surfaces cascade provider → model + voice from the same fake roster."""
    stub = _stub_client(voices=["alloy", "nova"])
    session = _FakeSession()
    provider_tool = ProviderTool(
        _session_provider(session), lambda: tmp_path, _client_factory(stub)
    )
    tool_payload = cast("dict[str, Any]", json.loads(provider_tool.dispatch("openai")))
    tool_written = (
        ConfigStore(tmp_path).read_field("provider"),
        ConfigStore(tmp_path).read_field("model"),
        ConfigStore(tmp_path).read_field("voice"),
    )

    # Reset the file and drive the CLI.
    (tmp_path / "vox.md").unlink(missing_ok=True)
    monkeypatch.setattr(f"{_CLI}.find_config_dir", lambda: tmp_path)
    runner = CliRunner()
    with patch(f"{_CLI}.VoxClientSync", return_value=stub):
        result = runner.invoke(app, ["provider", "openai", "--json"])
    assert result.exit_code == 0
    cli_payload = _cli_json_payload(result.output)
    cli_written = (
        ConfigStore(tmp_path).read_field("provider"),
        ConfigStore(tmp_path).read_field("model"),
        ConfigStore(tmp_path).read_field("voice"),
    )

    assert (
        cli_payload
        == tool_payload
        == {
            "provider": "openai",
            "model": "tts-1",
            "voice": "alloy",
        }
    )
    assert cli_written == tool_written == ("openai", "tts-1", "alloy")


# ---------------------------------------------------------------------------
# Voice list -- same fields on both surfaces
# ---------------------------------------------------------------------------


def test_voice_list_reports_the_same_names_and_current(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "vox.md").write_text(
        '---\nprovider: "elevenlabs"\nvoice: "matilda"\n---\n'
    )
    roster = ["matilda", "roger", "aria"]

    session = _FakeSession(provider="elevenlabs", voice="matilda")
    client = MagicMock()
    client.voices.return_value = roster
    voice_tool = VoiceTool(
        _session_provider(session), lambda: tmp_path, _client_factory(client)
    )
    tool_payload = cast("dict[str, Any]", json.loads(voice_tool.dispatch()))

    monkeypatch.setattr(f"{_CLI}.find_config_dir", lambda: tmp_path)
    with patch(f"{_CLI}.VoxClientSync") as cli_client_cls:
        cli_client_cls.return_value.voices.return_value = roster
        runner = CliRunner()
        result = runner.invoke(app, ["voice", "--json"])
    assert result.exit_code == 0
    cli_payload = _cli_json_payload(result.output)

    # The CLI list uses "names"; the tool list uses "available". Same values.
    assert cli_payload["names"] == tool_payload["available"] == roster
    assert cli_payload["current"] == tool_payload["current"] == "matilda"


def test_voice_set_writes_the_same_field(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``matilda`` on both surfaces writes ``voice: matilda`` to the config."""
    session = _FakeSession(provider="elevenlabs")
    client = MagicMock()
    client.voices.return_value = []
    voice_tool = VoiceTool(
        _session_provider(session), lambda: tmp_path, _client_factory(client)
    )
    tool_payload = cast("dict[str, Any]", json.loads(voice_tool.dispatch("matilda")))
    tool_written = ConfigStore(tmp_path).read_field("voice")

    (tmp_path / "vox.md").unlink(missing_ok=True)
    # Setting a voice with no provider configured refuses (F1); seed
    # provider so the CLI side hits the write, not the refusal -- the
    # tool side reads its provider from the in-memory _FakeSession above.
    (tmp_path / "vox.md").write_text('---\nprovider: "elevenlabs"\n---\n')
    monkeypatch.setattr(f"{_CLI}.find_config_dir", lambda: tmp_path)
    runner = CliRunner()
    result = runner.invoke(app, ["voice", "matilda", "--json"])
    assert result.exit_code == 0
    cli_payload = _cli_json_payload(result.output)
    cli_written = ConfigStore(tmp_path).read_field("voice")

    assert cli_payload == tool_payload == {"voice": "matilda"}
    assert cli_written == tool_written == "matilda"


def test_voice_set_strips_leading_at_on_both_surfaces(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The ``@`` normalization is the shared SynthesisSpec.normalize_voice
    choke-point -- both surfaces write the sigil-stripped name."""
    session = _FakeSession(provider="elevenlabs")
    voice_tool = VoiceTool(
        _session_provider(session),
        lambda: tmp_path,
        _client_factory(MagicMock()),
    )
    tool_payload = cast("dict[str, Any]", json.loads(voice_tool.dispatch("@sarah")))
    tool_written = ConfigStore(tmp_path).read_field("voice")

    (tmp_path / "vox.md").unlink(missing_ok=True)
    # Same F1 seed as the sibling parity test above; the CLI reads state
    # from vox.md and the F1 refusal would fire on the write path.
    (tmp_path / "vox.md").write_text('---\nprovider: "elevenlabs"\n---\n')
    monkeypatch.setattr(f"{_CLI}.find_config_dir", lambda: tmp_path)
    runner = CliRunner()
    result = runner.invoke(app, ["voice", "@sarah", "--json"])
    assert result.exit_code == 0
    cli_payload = _cli_json_payload(result.output)
    cli_written = ConfigStore(tmp_path).read_field("voice")

    assert cli_payload == tool_payload == {"voice": "sarah"}
    assert cli_written == tool_written == "sarah"
