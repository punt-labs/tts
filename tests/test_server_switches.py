"""Tests for the three switch MCP tools -- model, provider, voice.

Each tool is a humble object: driven directly with an in-memory session and
config-dir factory, plus (for VoiceTool) an in-memory voxd client fake. No
daemon, no socket. Asserts:

* the no-arg path returns ``{"available", "current"}`` for model/provider and
  ``{"provider", "current", "available", "featured"}`` for voice;
* the name-given path writes to the session AND to the config file, matching
  the ``ConfigStore.write_field`` choke-point the CLI already uses;
* shorthand resolution on ``mic:model``, closed-enum rejection on
  ``mic:provider`` (schema layer is tested in ``test_switches_schema.py``);
* a daemon fault on the voice roster returns ``{"error": ...}``, matching
  every other tool's contract.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, NoReturn, Self, cast, final
from unittest.mock import MagicMock

import pytest

from punt_vox.client_errors import VoxdConnectionError, VoxdProtocolError
from punt_vox.config import ConfigStore
from punt_vox.server_switches import ModelTool, ProviderTool, VoiceTool

if TYPE_CHECKING:
    from collections.abc import Callable

    from punt_vox.client_sync import VoxClientSync
    from punt_vox.server import SessionConfig


@final
class _FakeSession:
    """A minimal SessionConfig: mutable voice/provider/model + refresh counter.

    Mirrors just the fields the switch tools touch, so a test can drive the
    tools without importing ``server.SessionConfig`` (which pulls the whole
    module import graph and its module-level singleton).
    """

    __slots__ = ("model", "provider", "refreshes", "voice")
    voice: str | None
    provider: str | None
    model: str | None
    refreshes: int

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
        self.refreshes = 0
        return self

    def refresh_from_config(self) -> None:
        self.refreshes += 1


def _session_provider(
    session: _FakeSession,
) -> Callable[[], SessionConfig]:
    """Return a callable typed as producing SessionConfig -- casts at the seam."""
    return cast("Callable[[], SessionConfig]", lambda: session)


def _client_factory(client: MagicMock) -> Callable[[], VoxClientSync]:
    """Return a callable typed as producing VoxClientSync -- casts at the seam."""
    return cast("Callable[[], VoxClientSync]", lambda: client)


def _tools(
    session: _FakeSession,
    tmp_path: Path,
    voices: list[str] | None = None,
    voices_by_provider: dict[str, list[str]] | None = None,
) -> tuple[ModelTool, ProviderTool, VoiceTool]:
    """Build the three tools wired to *session*, *tmp_path*, and a voices fake.

    ``voices`` gives the default voice list. ``voices_by_provider`` lets a
    test supply distinct rosters per provider so cascade tests can verify the
    tool queried the correct one.
    """
    client = MagicMock()

    def _voices_side_effect(provider: str | None = None) -> list[str]:
        if voices_by_provider is not None and provider in voices_by_provider:
            return voices_by_provider[provider]
        return voices if voices is not None else []

    client.voices.side_effect = _voices_side_effect

    def _finder() -> Path | None:
        return tmp_path

    provider_of_session = _session_provider(session)
    factory = _client_factory(client)
    return (
        ModelTool(provider_of_session, _finder, factory),
        ProviderTool(provider_of_session, _finder, factory),
        VoiceTool(provider_of_session, _finder, factory),
    )


# ---------------------------------------------------------------------------
# ModelTool
# ---------------------------------------------------------------------------


def test_model_no_arg_lists_the_current_providers_models(tmp_path: Path) -> None:
    """No-arg model call reads the enum for the session's provider."""
    session = _FakeSession(provider="elevenlabs", model="eleven_v3")
    model, _, _ = _tools(session, tmp_path)

    result = json.loads(model.dispatch())

    assert result["current"] == "eleven_v3"
    assert result["available"] == [
        "eleven_v3",
        "eleven_flash_v2_5",
        "eleven_turbo_v2_5",
        "eleven_turbo_v2",
        "eleven_multilingual_v2",
    ]


def test_model_no_arg_on_modelless_provider_returns_empty(tmp_path: Path) -> None:
    """Polly has no user-selectable model -- the list is empty, current is null."""
    session = _FakeSession(provider="polly")
    model, _, _ = _tools(session, tmp_path)

    result = json.loads(model.dispatch())

    assert result == {"available": [], "current": None}


def test_model_no_arg_defaults_to_elevenlabs_when_provider_unset(
    tmp_path: Path,
) -> None:
    """When no session provider, model lists the ElevenLabs enum (the default)."""
    session = _FakeSession()
    model, _, _ = _tools(session, tmp_path)

    result = json.loads(model.dispatch())

    assert result["available"][0] == "eleven_v3"


def test_model_shorthand_resolves_and_persists(tmp_path: Path) -> None:
    """A shorthand resolves via resolve_model and lands on both surfaces.

    The cascade rule (vox-awm9) also sets voice to first-from-roster on
    every model set. Here the roster is ``["matilda"]``, so voice lands as
    ``matilda`` alongside the resolved model.
    """
    session = _FakeSession(provider="elevenlabs")
    model, _, _ = _tools(session, tmp_path, voices=["matilda", "roger"])

    result = json.loads(model.dispatch("v3"))

    assert result == {"model": "eleven_v3", "voice": "matilda"}
    assert session.model == "eleven_v3"
    assert session.voice == "matilda"
    assert ConfigStore(tmp_path).read_field("model") == "eleven_v3"
    assert ConfigStore(tmp_path).read_field("voice") == "matilda"


def test_model_full_name_persists_unchanged(tmp_path: Path) -> None:
    session = _FakeSession(provider="elevenlabs")
    model, _, _ = _tools(session, tmp_path, voices=["matilda"])

    json.loads(model.dispatch("eleven_flash_v2_5"))

    assert session.model == "eleven_flash_v2_5"
    # Cascade rule: setting model also writes voice = first from roster.
    assert session.voice == "matilda"


def test_model_set_cascade_uses_empty_when_roster_is_empty(tmp_path: Path) -> None:
    """A daemon with no voices for the provider cascades voice = ''."""
    session = _FakeSession(provider="elevenlabs")
    model, _, _ = _tools(session, tmp_path, voices=[])

    result = json.loads(model.dispatch("v3"))

    assert result == {"model": "eleven_v3", "voice": ""}
    assert session.voice is None
    # Frontmatter treats "" as absent; the field is cleared on disk.
    assert not ConfigStore(tmp_path).read_field("voice")


def test_model_set_roster_fetch_error_returns_error_envelope(tmp_path: Path) -> None:
    """A daemon fault on the cascade roster fetch aborts the write cleanly.

    Same pattern as vox-w79f on the panel: the model MUST NOT persist when
    we cannot read the voice roster to compute the cascaded default.
    """
    from unittest.mock import MagicMock

    from punt_vox.client_errors import VoxdConnectionError

    client = MagicMock()
    client.voices.side_effect = VoxdConnectionError("voxd unreachable")
    session = _FakeSession(provider="elevenlabs")
    model = ModelTool(
        _session_provider(session),
        lambda: tmp_path,
        _client_factory(client),
    )

    result = json.loads(model.dispatch("v3"))

    assert "error" in result
    assert session.model is None
    assert ConfigStore(tmp_path).read_field("model") is None


def test_model_unknown_shorthand_returns_error(tmp_path: Path) -> None:
    """Unknown shorthand is a clean JSON error, no ValueError across the boundary."""
    session = _FakeSession(provider="elevenlabs")
    model, _, _ = _tools(session, tmp_path)

    result = json.loads(model.dispatch("made-up"))

    assert "error" in result
    assert "elevenlabs" in result["error"]


def test_model_on_modelless_provider_returns_error(tmp_path: Path) -> None:
    """Setting a model on a modelless provider is a clean error, not a crash."""
    session = _FakeSession(provider="polly")
    model, _, _ = _tools(session, tmp_path)

    result = json.loads(model.dispatch("eleven_v3"))

    assert "error" in result
    assert "polly" in result["error"]
    assert session.model is None  # untouched


def test_model_dispatch_refreshes_the_session(tmp_path: Path) -> None:
    session = _FakeSession()
    model, _, _ = _tools(session, tmp_path)

    model.dispatch()

    assert session.refreshes == 1


# ---------------------------------------------------------------------------
# ProviderTool
# ---------------------------------------------------------------------------


def test_provider_no_arg_lists_the_closed_enum(tmp_path: Path) -> None:
    session = _FakeSession(provider="openai")
    _, provider, _ = _tools(session, tmp_path)

    result = json.loads(provider.dispatch())

    assert result == {
        "available": ["elevenlabs", "openai", "polly", "say", "espeak"],
        "current": "openai",
    }


def test_provider_no_arg_reports_null_when_none_set(tmp_path: Path) -> None:
    session = _FakeSession()
    _, provider, _ = _tools(session, tmp_path)

    assert json.loads(provider.dispatch())["current"] is None


def test_provider_set_writes_session_and_cascades_defaults(tmp_path: Path) -> None:
    """The cascade rule (vox-awm9): set provider populates model + voice defaults.

    ``openai`` first model is ``tts-1``; the fake client returns
    ``["alloy", "nova"]``, so voice cascades to ``alloy``.
    """
    session = _FakeSession()
    _, provider, _ = _tools(session, tmp_path, voices=["alloy", "nova"])

    result = json.loads(provider.dispatch("openai"))

    assert result == {
        "provider": "openai",
        "model": "tts-1",
        "voice": "alloy",
    }
    assert session.provider == "openai"
    assert session.model == "tts-1"
    assert session.voice == "alloy"
    assert ConfigStore(tmp_path).read_field("provider") == "openai"
    assert ConfigStore(tmp_path).read_field("model") == "tts-1"
    assert ConfigStore(tmp_path).read_field("voice") == "alloy"


def test_provider_set_modelless_leaves_model_empty_but_cascades_voice(
    tmp_path: Path,
) -> None:
    """A modelless provider (polly/say/espeak) cascades voice, leaves model blank."""
    session = _FakeSession()
    _, provider, _ = _tools(session, tmp_path, voices=["joanna", "matthew"])

    result = json.loads(provider.dispatch("polly"))

    assert result == {"provider": "polly", "model": "", "voice": "joanna"}
    assert session.model is None  # "" write clears the session field
    assert session.voice == "joanna"


def test_provider_dispatch_refreshes_the_session(tmp_path: Path) -> None:
    session = _FakeSession()
    _, provider, _ = _tools(session, tmp_path)

    provider.dispatch()

    assert session.refreshes == 1


def test_provider_switch_cascades_across_providers(tmp_path: Path) -> None:
    """Genuine switch overwrites model + voice with the new provider's defaults.

    Was: 'clears stale model' (vox-0rp9.1). Under vox-awm9 the rule is
    'sets model to first, voice to first' -- deterministic default rather than
    a cleared placeholder.
    """
    ConfigStore(tmp_path).write_fields({"provider": "elevenlabs", "model": "eleven_v3"})
    session = _FakeSession(provider="elevenlabs", model="eleven_v3", voice="matilda")
    _, provider, _ = _tools(
        session,
        tmp_path,
        voices_by_provider={
            "elevenlabs": ["matilda"],
            "openai": ["alloy", "nova"],
        },
    )

    provider.dispatch("openai")

    assert session.provider == "openai"
    assert session.model == "tts-1"
    assert session.voice == "alloy"
    assert ConfigStore(tmp_path).read_field("provider") == "openai"
    assert ConfigStore(tmp_path).read_field("model") == "tts-1"
    assert ConfigStore(tmp_path).read_field("voice") == "alloy"


def test_provider_no_op_when_unchanged_preserves_model_and_voice(
    tmp_path: Path,
) -> None:
    """Re-publishing the same provider does not rewrite the disk or drop state."""
    ConfigStore(tmp_path).write_fields(
        {"provider": "elevenlabs", "model": "eleven_v3", "voice": "roger"}
    )
    session = _FakeSession(provider="elevenlabs", model="eleven_v3", voice="roger")
    _, provider, _ = _tools(session, tmp_path, voices=["matilda", "aria"])

    result = json.loads(provider.dispatch("elevenlabs"))

    # No-op returns the provider only; no cascade fires.
    assert result == {"provider": "elevenlabs"}
    assert session.model == "eleven_v3"
    assert session.voice == "roger"
    assert ConfigStore(tmp_path).read_field("model") == "eleven_v3"
    assert ConfigStore(tmp_path).read_field("voice") == "roger"


def test_provider_set_roster_fetch_error_returns_error_envelope(
    tmp_path: Path,
) -> None:
    """A daemon fault on the cascade roster fetch aborts the write cleanly.

    Same pattern as vox-w79f on the panel: the provider MUST NOT persist
    when we cannot read the voice roster to compute the cascaded default.
    """
    from unittest.mock import MagicMock

    from punt_vox.client_errors import VoxdConnectionError

    client = MagicMock()
    client.voices.side_effect = VoxdConnectionError("voxd unreachable")
    session = _FakeSession(provider="elevenlabs")
    provider = ProviderTool(
        _session_provider(session),
        lambda: tmp_path,
        _client_factory(client),
    )

    result = json.loads(provider.dispatch("openai"))

    assert "error" in result
    # The failed switch left the previous state intact.
    assert session.provider == "elevenlabs"
    assert ConfigStore(tmp_path).read_field("provider") is None


# ---------------------------------------------------------------------------
# VoiceTool
# ---------------------------------------------------------------------------


def test_voice_no_arg_returns_the_roster_and_current(tmp_path: Path) -> None:
    session = _FakeSession(provider="elevenlabs", voice="matilda")
    _, _, voice = _tools(session, tmp_path, voices=["matilda", "roger", "aria"])

    result = json.loads(voice.dispatch())

    assert result["provider"] == "elevenlabs"
    assert result["current"] == "matilda"
    assert result["available"] == ["matilda", "roger", "aria"]
    assert isinstance(result["featured"], list)


def test_voice_no_arg_carries_blurbs_on_featured(tmp_path: Path) -> None:
    """The featured list carries the VOICE_BLURBS entries for the current provider."""
    session = _FakeSession(provider="elevenlabs")
    _, _, voice = _tools(session, tmp_path, voices=["matilda", "roger", "aria"])

    result = json.loads(voice.dispatch())
    entries = {entry["name"] for entry in result["featured"]}

    assert entries.issubset({"matilda", "roger", "aria"})
    for entry in result["featured"]:
        assert entry["blurb"]  # non-empty blurb


def test_voice_set_writes_session_and_config(tmp_path: Path) -> None:
    session = _FakeSession(provider="elevenlabs")
    _, _, voice = _tools(session, tmp_path)

    result = json.loads(voice.dispatch("matilda"))

    assert result == {"voice": "matilda"}
    assert session.voice == "matilda"
    assert ConfigStore(tmp_path).read_field("voice") == "matilda"


def test_voice_set_strips_leading_at_sigil(tmp_path: Path) -> None:
    """A ``@`` prefix is a common typo -- the tool strips it before writing."""
    session = _FakeSession(provider="elevenlabs")
    _, _, voice = _tools(session, tmp_path)

    result = json.loads(voice.dispatch("@sarah"))

    assert result == {"voice": "sarah"}
    assert session.voice == "sarah"


def test_voice_set_rejects_blank(tmp_path: Path) -> None:
    """A blank name (or lone ``@``) is a clean error, session untouched."""
    session = _FakeSession(provider="elevenlabs", voice="matilda")
    _, _, voice = _tools(session, tmp_path)

    result = json.loads(voice.dispatch("@"))

    assert "error" in result
    assert session.voice == "matilda"  # untouched


def test_voice_no_arg_reports_daemon_connect_error(tmp_path: Path) -> None:
    """When voxd is unreachable, the roster path returns an error envelope."""

    def _fail(*_args: object, **_kwargs: object) -> NoReturn:
        raise VoxdConnectionError("voxd not running")

    session = _FakeSession(provider="elevenlabs")
    client = MagicMock()
    client.voices.side_effect = _fail

    voice = VoiceTool(
        _session_provider(session),
        lambda: tmp_path,
        _client_factory(client),
    )

    result = json.loads(voice.dispatch())

    assert result == {"error": "voxd not running"}


def test_voice_no_arg_reports_protocol_error(tmp_path: Path) -> None:
    """A protocol-level fault also returns an error envelope, not an exception."""

    def _fail(*_args: object, **_kwargs: object) -> NoReturn:
        raise VoxdProtocolError("bad frame")

    session = _FakeSession(provider="elevenlabs")
    client = MagicMock()
    client.voices.side_effect = _fail

    voice = VoiceTool(
        _session_provider(session),
        lambda: tmp_path,
        _client_factory(client),
    )

    result = json.loads(voice.dispatch())

    assert result["error"] == "bad frame"


def test_voice_dispatch_refreshes_the_session(tmp_path: Path) -> None:
    session = _FakeSession(provider="elevenlabs")
    _, _, voice = _tools(session, tmp_path)

    voice.dispatch()

    assert session.refreshes == 1


# ---------------------------------------------------------------------------
# Cascade: model/provider list is roster-free; write fires the cascade fetch
# ---------------------------------------------------------------------------
#
# Was (pre-vox-awm9): model and provider tools MUST NOT touch the voice
# roster on any path. Now: no-arg list is still roster-free, but a write
# fires exactly one voices() call to fetch the cascaded default. The two
# tests below enforce both halves of the new invariant.


def test_model_no_arg_does_not_touch_voice_roster(tmp_path: Path) -> None:
    """Listing models is roster-free -- only a write fires the cascade fetch."""
    session = _FakeSession(provider="elevenlabs")
    client = MagicMock()
    client.voices.return_value = []

    model = ModelTool(
        _session_provider(session), lambda: tmp_path, _client_factory(client)
    )
    _ = model.dispatch()

    assert client.voices.call_count == 0


def test_model_set_fires_exactly_one_voices_call(tmp_path: Path) -> None:
    """The cascade fetches the roster once per model write (no duplicate)."""
    session = _FakeSession(provider="elevenlabs")
    client = MagicMock()
    client.voices.return_value = ["matilda"]

    model = ModelTool(
        _session_provider(session), lambda: tmp_path, _client_factory(client)
    )
    _ = model.dispatch("v3")

    assert client.voices.call_count == 1


def test_provider_no_arg_does_not_touch_voice_roster(tmp_path: Path) -> None:
    """Listing providers is roster-free -- only a genuine change fires the fetch."""
    session = _FakeSession()
    client = MagicMock()
    client.voices.return_value = []

    provider = ProviderTool(
        _session_provider(session), lambda: tmp_path, _client_factory(client)
    )
    _ = provider.dispatch()

    assert client.voices.call_count == 0


def test_provider_no_op_re_publish_does_not_touch_voice_roster(
    tmp_path: Path,
) -> None:
    """Re-publishing the same provider still short-circuits before the roster fetch."""
    session = _FakeSession(provider="polly")
    client = MagicMock()
    client.voices.return_value = []

    provider = ProviderTool(
        _session_provider(session), lambda: tmp_path, _client_factory(client)
    )
    _ = provider.dispatch("polly")

    assert client.voices.call_count == 0


def test_provider_set_fires_exactly_one_voices_call(tmp_path: Path) -> None:
    """A genuine provider switch fetches the roster once for the cascade."""
    session = _FakeSession(provider="elevenlabs")
    client = MagicMock()
    client.voices.return_value = ["joanna"]

    provider = ProviderTool(
        _session_provider(session), lambda: tmp_path, _client_factory(client)
    )
    _ = provider.dispatch("polly")

    assert client.voices.call_count == 1


@pytest.mark.parametrize(
    "provider_name", ["elevenlabs", "openai", "polly", "say", "espeak"]
)
def test_voice_carries_provider_name_from_session(
    tmp_path: Path, provider_name: str
) -> None:
    """The voice roster is fetched per the session's current provider."""
    session = _FakeSession(provider=provider_name)
    client = MagicMock()
    client.voices.return_value = ["a", "b"]

    voice = VoiceTool(
        _session_provider(session),
        lambda: tmp_path,
        _client_factory(client),
    )

    voice.dispatch()

    client.voices.assert_called_once_with(provider=provider_name)
