"""FastMCP server for punt-vox -- mic API.

Thin client of the voxd audio daemon. Session state lives in an
in-memory dataclass; audio requests go to voxd over WebSocket
via VoxClient.
"""

from __future__ import annotations

import json
import logging
import random
import uuid
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any, final

from mcp.server.fastmcp import FastMCP
from websockets.exceptions import WebSocketException

from punt_vox import __version__
from punt_vox.client_catalog_gateway import ClientCatalogGateway
from punt_vox.client_errors import VoxdConnectionError, VoxdProtocolError
from punt_vox.client_gateway import ClientProgramGateway
from punt_vox.client_sync import VoxClientSync
from punt_vox.config import ConfigStore
from punt_vox.logging_config import (
    configure_client_logging,
    log_health,
    reapply_client_log_level,
)
from punt_vox.server_audio_tools import RecTools
from punt_vox.server_music_tool import MusicTool
from punt_vox.synthesis_batch import SegmentBatch
from punt_vox.types_programs.mode import Mode
from punt_vox.types_synthesis import SynthesisSpec
from punt_vox.vibe_command import MusicPreference, VibeCommand
from punt_vox.vibe_trace import VibeTraceLog
from punt_vox.voices import VOICE_BLURBS

if TYPE_CHECKING:  # annotation-only -- kept off the runtime import graph (PY-TS-7)
    from collections.abc import Sequence

    from mcp.types import ContentBlock

    from punt_vox.catalog_gateway import CatalogGateway
    from punt_vox.program_gateway import ProgramGateway
    from punt_vox.vibe import VibeChange

logger = logging.getLogger(__name__)


@final
class _LoggingFastMCP(FastMCP):
    """A ``FastMCP`` that logs one vox-owned ``mic:<tool>`` INFO line per call.

    ``call_tool`` is the one method every tool invocation flows through, so
    overriding it names each call in vox.log -- replacing the suppressed ``mcp``
    framework's tool-name-less "Processing request" noise. Unlike a per-function
    decorator (which FastMCP unwraps via ``__wrapped__`` and bypasses), the
    override is always on the invocation path, and it never touches a tool's
    signature or schema. It is also where the long-lived server picks up a
    ``vox log`` change: re-applying the level here means a change takes hold
    within a tool call or two, so DEBUG records ship and ``mic:status`` is honest.
    """

    async def call_tool(
        self,
        name: str,
        # ``dict[str, Any]`` mirrors the third-party ``FastMCP.call_tool`` exactly;
        # narrowing it would make this an invalid (contravariance-breaking) override.
        arguments: dict[str, Any],
    ) -> Sequence[ContentBlock] | dict[str, Any]:
        """Re-apply the effective log level, name the tool, then delegate."""
        reapply_client_log_level()
        logger.info("mic:%s", name)
        return await super().call_tool(name, arguments)


mcp = _LoggingFastMCP(
    "mic",
    instructions=(
        "Vox is a text-to-speech engine. Use these tools to speak text aloud "
        "and generate audio files.\n\n"
        "When a stop hook blocks with a \u266a phrase, write 1-2 sentences "
        "summarizing what you completed and call the unmute tool with "
        "ephemeral=true. Mood tags are pre-resolved in config \u2014 do not "
        "pass vibe_tags. No other output.\n\n"
        "Do NOT use Read, Write, or Bash tools to access "
        ".punt-labs/vox/vox.md or .punt-labs/vox/vox.local.md. "
        "All config state is available through "
        "MCP tools or hook context."
    ),
)
mcp._mcp_server.version = __version__  # pyright: ignore[reportPrivateUsage]

_VALID_NOTIFY_MODES = frozenset({"y", "n", "c"})
_VALID_SPEAK_MODES = frozenset({"y", "n"})


# ---------------------------------------------------------------------------
# Session state
# ---------------------------------------------------------------------------


@dataclass
class SessionConfig:
    """In-memory session config. Seeded from vox.md + vox.local.md."""

    _session_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    _notify: str = "n"
    _speak: str = "n"
    _voice: str | None = None
    _provider: str | None = None
    _model: str | None = None
    _vibe_mode: str = "off"
    _vibe: str | None = None
    _vibe_tags: str | None = None
    _speak_explicit: bool = False

    # -- Properties (read access) ------------------------------------------

    @property
    def session_id(self) -> str:
        """Return the unique session identifier."""
        return self._session_id

    @property
    def notify(self) -> str:
        """Return the notification mode ('y', 'n', or 'c')."""
        return self._notify

    @property
    def speak(self) -> str:
        """Return the speak mode ('y' or 'n')."""
        return self._speak

    @property
    def voice(self) -> str | None:
        """Return the current voice name, or None for provider default."""
        return self._voice

    @voice.setter
    def voice(self, value: str | None) -> None:
        self._voice = value

    @property
    def provider(self) -> str | None:
        """Return the current TTS provider name."""
        return self._provider

    @provider.setter
    def provider(self, value: str | None) -> None:
        self._provider = value

    @property
    def model(self) -> str | None:
        """Return the current TTS model name."""
        return self._model

    @model.setter
    def model(self, value: str | None) -> None:
        self._model = value

    @property
    def vibe_mode(self) -> str:
        """Return the vibe detection mode ('auto', 'manual', or 'off')."""
        return self._vibe_mode

    @property
    def vibe(self) -> str | None:
        """Return the current vibe/mood description."""
        return self._vibe

    @property
    def vibe_tags(self) -> str | None:
        """Return the current ElevenLabs expressive tags."""
        return self._vibe_tags

    @property
    def speak_explicit(self) -> bool:
        """Return whether the user has explicitly set speak mode."""
        return self._speak_explicit

    # -- Validated setters -------------------------------------------------

    def set_notify(self, mode: str) -> None:
        """Set notification mode with validation."""
        if mode not in _VALID_NOTIFY_MODES:
            msg = f"invalid notify mode: {mode!r}"
            raise ValueError(msg)
        self._notify = mode

    def set_speak(self, mode: str, *, explicit: bool = True) -> None:
        """Set speak mode with validation.

        When *explicit* is True (the default), marks the choice as
        user-initiated so future notify-enable calls preserve it.
        """
        if mode not in _VALID_SPEAK_MODES:
            msg = f"invalid speak mode: {mode!r}"
            raise ValueError(msg)
        self._speak = mode
        if explicit:
            self._speak_explicit = True

    def set_vibe(self, mood: str | None = None, tags: str | None = None) -> None:
        """Set vibe mood and/or tags together."""
        if mood is not None:
            self._vibe = mood
        if tags is not None:
            self._vibe_tags = tags

    def set_voice(self, voice: str | None) -> str | None:
        """Set the session voice, stripping a stray leading '@' sigil.

        Returns the normalized name that was stored, or ``None`` when *voice*
        carries no usable name (blank or a lone '@') -- the session voice is
        left unchanged in that case. Shares one normalization rule with the
        synthesis path via ``SynthesisSpec.normalize_voice``.
        """
        normalized = SynthesisSpec.normalize_voice(voice)
        if normalized is not None:
            self._voice = normalized
        return normalized

    def summary_sentence(self) -> str:
        """Return the startup state as a plain sentence, not a developer dump.

        Translates the internal ``notify``/``speak`` codes into words -- e.g.
        ``ready -- voice roger, chimes only, auto vibe`` -- so the one startup
        INFO reads like intent rather than ``notify=c speak=n voice=roger ...``.
        """
        voice = f"voice {self._voice}" if self._voice else "default voice"
        if self._notify == "n":
            delivery = "notifications off"
        else:
            delivery = "chimes only" if self._speak == "n" else "spoken"
        return f"ready -- {voice}, {delivery}, {self._vibe_mode} vibe"

    def fill_defaults(self, spec: SynthesisSpec) -> SynthesisSpec:
        """Return *spec* with unset voice/provider/model/vibe_tags from session."""
        return replace(
            spec,
            voice=spec.voice or self._voice,
            provider=spec.provider or self._provider,
            model=spec.model or self._model,
            vibe_tags=spec.vibe_tags or self._vibe_tags,
        )

    def change_vibe(self, change: VibeChange) -> dict[str, str]:
        """Apply an authoritative vibe change; return the fields to persist.

        The transition rules live on ``VibeChange``; this method mirrors the
        resolved updates into the in-memory session (an empty string clears
        the field back to ``None``).  Raises ``ValueError`` for a bad mode.
        """
        updates = change.resolve()
        if "vibe" in updates:
            self._vibe = updates["vibe"] or None
        if "vibe_tags" in updates:
            self._vibe_tags = updates["vibe_tags"] or None
        if "vibe_mode" in updates:
            self._vibe_mode = updates["vibe_mode"]
        return updates

    @classmethod
    def from_config(cls, config_dir: Path | None) -> SessionConfig:
        """Read per-repo config once and return a SessionConfig."""
        if config_dir is None:
            return cls()

        cfg = ConfigStore(config_dir).read()
        return cls(
            _notify=cfg.notify,
            _speak=cfg.speak,
            _voice=cfg.voice,
            _provider=cfg.provider,
            _model=cfg.model,
            _vibe_mode=cfg.vibe_mode,
            _vibe=cfg.vibe,
            _vibe_tags=cfg.vibe_tags,
        )

    def refresh_from_config(self) -> None:
        """Re-read config files and update self with current values.

        Config-sourced fields (notify, speak, vibe_mode, vibe, vibe_tags)
        always take the config value -- the config file is the source of
        truth since CLI and hooks write there directly.

        For voice, provider, and model the MCP tool may have set a value
        that was not persisted to config (e.g. an in-tool override).  Only
        overwrite self when config has a non-None value so those
        overrides survive.
        """
        config_dir = _find_config_dir()
        if config_dir is None:
            return

        cfg = ConfigStore(config_dir).read()

        self._vibe = cfg.vibe
        self._vibe_tags = cfg.vibe_tags
        self._vibe_mode = cfg.vibe_mode
        self._notify = cfg.notify
        self._speak = cfg.speak

        if ConfigStore(config_dir).read_field("speak") is not None:
            self._speak_explicit = True

        if cfg.voice is not None:
            self._voice = cfg.voice
        if cfg.provider is not None:
            self._provider = cfg.provider
        if cfg.model is not None:
            self._model = cfg.model


# Module-level singleton; initialized in run_server().
_session: SessionConfig = SessionConfig()

# The genre the agent last set music to. The music tools keep it current on every
# playback change so the vibe re-pool hint never names a stale style (the daemon
# status deliberately omits subject data). Session-scoped, held apart from the
# vibe cluster so SessionConfig stays cohesive.
_music_pref: MusicPreference = MusicPreference()


# ---------------------------------------------------------------------------
# Config discovery and seeding
# ---------------------------------------------------------------------------


def _find_config_dir() -> Path | None:
    """Walk up from cwd to find per-repo .punt-labs/vox/ directory."""
    from punt_vox.dirs import find_config_dir

    return find_config_dir()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _voxd_client() -> VoxClientSync:
    """Create a VoxClientSync instance."""
    return VoxClientSync()


# The daemon-facing seam the music/status tools drive. A module-level value (not
# a factory) so it adds no procedural surface; tests replace it with an in-memory
# FakeProgramGateway. It holds a VoxClientSync that opens a fresh connection per
# call, so no session or owner travels with a command (design section 4).
_program_tools: ProgramGateway = ClientProgramGateway(VoxClientSync())

# The daemon-transport faults every tool boundary funnels to a JSON _error; named
# once so the music/status tools share one contract instead of repeating the tuple.
_DAEMON_ERRORS = (VoxdConnectionError, VoxdProtocolError, WebSocketException, OSError)


def _error(message: str) -> str:
    """Return a JSON error string."""
    return json.dumps({"error": message})


# ---------------------------------------------------------------------------
# MCP tools
# ---------------------------------------------------------------------------


@mcp.tool()
def unmute(
    text: str | None = None,
    voice: str | None = None,
    language: str | None = None,
    segments: list[dict[str, str]] | None = None,
    rate: int = 90,
    pause_ms: int = 500,  # noqa: ARG001 -- reserved for future multi-segment pause
    ephemeral: bool = True,  # noqa: FBT001, FBT002 -- MCP tool schema requires bool param
    stability: float | None = None,
    similarity: float | None = None,
    style: float | None = None,
    speaker_boost: bool | None = None,  # noqa: FBT001 -- MCP tool schema requires bool param
    vibe_tags: str | None = None,
    provider: str | None = None,
    model: str | None = None,
) -> str:
    """Synthesize and play audio sequentially.

    Pass either a simple ``text`` string or a ``segments`` list for
    multi-voice sequential playback. Top-level ``voice`` is the default;
    per-segment ``voice`` overrides it.

    Args:
        text: Simple text to speak. Ignored when segments is provided.
        voice: Default voice for all segments. If omitted, uses the
            session voice or provider default.
        language: Default ISO 639-1 language code (e.g. 'de', 'ko').
            Per-segment "language" overrides this.
        segments: List of segment objects, each with "text" (required)
            and optional "voice", "language", and "vibe_tags".
            Per-segment "vibe_tags" override the top-level default.
            Example:
            [{"voice": "roger", "text": "Hello.", "vibe_tags": "[excited]"},
             {"text": "Hi."}]
        rate: Speech rate as percentage. Defaults to 90.
        pause_ms: Pause between segments in milliseconds. Defaults to 500.
        ephemeral: Write to .punt-labs/vox/ephemeral/ and clean up previous files.
            Defaults to true (unmute is for playback, not saving).
        stability: ElevenLabs voice stability (0.0-1.0).
        similarity: ElevenLabs voice similarity boost (0.0-1.0).
        style: ElevenLabs voice style/expressiveness (0.0-1.0).
        speaker_boost: ElevenLabs speaker boost toggle.
        vibe_tags: ElevenLabs expressive tags (e.g. "[warm] [satisfied]").
            When provided, writes tags to config.
        provider: TTS provider override (elevenlabs, openai, polly, say,
            espeak). When provided, persists to session config for
            subsequent calls.
        model: TTS model override (e.g. eleven_v3, eleven_flash_v2_5,
            tts-1). When provided, persists to session config for
            subsequent calls.

    Returns:
        JSON string with synthesis results.
    """
    _session.refresh_from_config()

    # Validate voice settings via SynthesisSpec (single validation path).
    SynthesisSpec(stability=stability, similarity=similarity, style=style).validate()

    # ephemeral is accepted for callers but voxd cleans up internally today.
    _ = ephemeral

    # Persist explicit overrides; a None argument leaves the session untouched.
    _session.provider = provider if provider is not None else _session.provider
    _session.model = model if model is not None else _session.model
    _session.set_vibe(tags=vibe_tags)

    # Normalize input: text -> single segment.
    if segments is None:
        if text is None:
            given = {"provider": provider, "model": model, "vibe_tags": vibe_tags}
            updates = {key: val for key, val in given.items() if val is not None}
            if updates:
                return json.dumps({"status": "config updated", **updates})
            return _error("Provide text or segments.")
        segments = [{"text": text}]

    defaults = _session.fill_defaults(
        SynthesisSpec(
            voice=voice,
            language=language,
            rate=rate,
            provider=provider,
            model=model,
            stability=stability,
            similarity=similarity,
            style=style,
            speaker_boost=speaker_boost,
            vibe_tags=vibe_tags,
        )
    )
    client = _voxd_client()

    def _synth_handler(seg_text: str, seg_spec: SynthesisSpec) -> dict[str, object]:
        result = client.synthesize(seg_text, seg_spec)
        entry: dict[str, object] = {
            "id": result.request_id,
            "text": seg_text,
            "voice": seg_spec.voice,
            "provider": seg_spec.provider,
            "cached": result.cached,
        }
        if result.deduped:
            entry["deduped"] = True
            if result.original_played_at is not None:
                entry["original_played_at"] = result.original_played_at
            if result.ttl_seconds_remaining is not None:
                entry["ttl_seconds_remaining"] = result.ttl_seconds_remaining
        return entry

    return SegmentBatch(segments, defaults).render(
        handler=_synth_handler, error_label="Synthesis"
    )


# Register the recordings verbs as `mic` tools at parity with the CLI. Each is a
# bound method of a humble object, so FastMCP builds the schema from the method
# signature (minus self) and the daemon still owns containment and audit -- the
# tool is a thin caller. Bare registration statements add no module-level public
# name; the `_LoggingFastMCP.call_tool` override names each. rec_new fills its
# synthesis defaults from a closure yielding the live session, refreshed on each
# call; the class lives in server_audio_tools. The music catalog verbs are folded
# into the single `music` tool below (server_music_tool).
_rec_tools = RecTools(_voxd_client, lambda: _session)

mcp.tool(name="rec_new")(_rec_tools.new)
mcp.tool(name="rec_list")(_rec_tools.list_recordings)
mcp.tool(name="rec_play")(_rec_tools.play)
mcp.tool(name="rec_get")(_rec_tools.get)
mcp.tool(name="rec_remove")(_rec_tools.remove)


@mcp.tool()
def vibe(
    mood: str | None = None,
    tags: str | None = None,
    mode: str | None = None,
) -> str:
    """Set session mood and expressive tags.

    Controls how TTS voices sound during the session. Mood is a
    human-readable description; tags are ElevenLabs performance cues.

    Args:
        mood: Human-readable mood (e.g. "3am debugging", "excited").
            Stored as the ``vibe`` config field.
        tags: ElevenLabs expressive tags (e.g. "[tired] [slow]").
            Stored as ``vibe_tags``.
        mode: Vibe detection mode: "auto", "manual", or "off".
            In auto mode a prompt-time reminder nudges you to set the
            vibe from the conversation; manual uses the mood/tags you
            set here.

    Returns:
        JSON string with the updated vibe state. When a Program is playing, the
        reply also carries the music state and a ``music_hint`` directive telling
        you to re-pool the music to the new mood (see the ``/vibe`` skill).
    """
    _session.refresh_from_config()
    return VibeCommand(_session, _program_tools, _find_config_dir(), _music_pref).apply(
        mood, tags, mode
    )


# The single `music` tool: one subcommand-dispatched verb (on/off/play/next/
# list/new/get/remove), replacing the seven separate music tools. It reads the
# module globals through call-time closures so a test patching `_program_tools`,
# `_session`, or `_music_pref` is honoured on the next call; the classes live in
# server_music_tool. The bare registration adds no module-level public name; the
# `_LoggingFastMCP.call_tool` override names each call.
def _catalog_gateway() -> CatalogGateway:
    """Build the production catalog gateway -- a fresh WebSocket client per call."""
    return ClientCatalogGateway(VoxClientSync())


_music_tool = MusicTool(
    lambda: _program_tools,
    _catalog_gateway,
    lambda: _session,
    lambda: _music_pref,
)

mcp.tool(name="music")(_music_tool.dispatch)


@mcp.tool()
def who() -> str:
    """List available voices for the current provider.

    Returns the voice roster with personality blurbs, the full list
    of available voices, and the current session voice.

    Returns:
        JSON string with provider, current voice, featured voices,
        and full voice list.
    """
    _session.refresh_from_config()
    client = _voxd_client()

    try:
        all_voices = client.voices(provider=_session.provider)
    except VoxdConnectionError as exc:
        return _error(str(exc))
    except (VoxdProtocolError, WebSocketException, OSError, ValueError) as exc:
        logger.exception("Voice listing failed")
        return _error(str(exc))

    # Determine effective provider name for blurb lookup.
    provider_name = _session.provider or "elevenlabs"

    featured = [
        {"name": name, "blurb": blurb}
        for (prov, name), blurb in VOICE_BLURBS.items()
        if prov == provider_name and name in all_voices
    ]

    return json.dumps(
        {
            "provider": provider_name,
            "current": _session.voice,
            "featured": random.sample(featured, min(6, len(featured))),
            "all": all_voices,
        }
    )


@mcp.tool()
def notify(
    mode: str,
    voice: str | None = None,
) -> str:
    """Set notification mode and optionally the session voice.

    Controls whether vox sends notification events. Whether notifications
    are heard as chimes or TTS speech is controlled by the separate
    ``speak`` field (see the ``speak`` tool).

    When enabling notifications (mode "y" or "c"), initializes speak to
    "y" (voice) if the user has not yet made an explicit speak choice.
    Once the user has set speak via ``/mute`` or ``/unmute``, enabling
    notifications preserves that choice.

    Args:
        mode: Notification mode -- "y" (notifications on), "n" (off),
            or "c" (continuous with real-time signal announcements).
        voice: Optional session voice to set (e.g. "matilda", "roger").

    Returns:
        JSON string with the updated config fields.
    """
    _session.refresh_from_config()
    if mode not in _VALID_NOTIFY_MODES:
        return _error(f"Invalid mode '{mode}'. Use y/n/c.")

    updates: dict[str, str] = {"notify": mode}
    _session.set_notify(mode)

    # Initialize speak to "y" if not explicitly set yet.
    # "n" is the default sentinel; if it's still at default and we're
    # enabling notifications, default to voice mode.
    if mode in ("y", "c") and _session.speak == "n" and not _session.speak_explicit:
        updates["speak"] = "y"
        _session.set_speak("y", explicit=False)

    stored_voice = _session.set_voice(voice)
    if stored_voice is not None:
        updates["voice"] = stored_voice

    # Persist to disk so hooks (which read config independently) see the change
    ConfigStore(_find_config_dir()).write_fields(updates)

    return json.dumps({"notify": updates})


@mcp.tool()
def speak(
    mode: str,
    voice: str | None = None,
) -> str:
    """Toggle spoken notifications on or off.

    Args:
        mode: "y" for voice (TTS speech) or "n" for chimes only.
        voice: Optional session voice to set (e.g. "matilda", "roger").

    Returns:
        JSON string with the updated fields.
    """
    _session.refresh_from_config()

    if mode not in _VALID_SPEAK_MODES:
        return _error(f"Invalid mode '{mode}'. Use y/n.")

    updates: dict[str, str] = {"speak": mode}
    _session.set_speak(mode)

    stored_voice = _session.set_voice(voice)
    if stored_voice is not None:
        updates["voice"] = stored_voice

    # Persist to disk so hooks (which read config independently) see the change
    ConfigStore(_find_config_dir()).write_fields(updates)

    return json.dumps(updates)


@mcp.tool()
def status() -> str:
    """Show current vox state (provider, voice, notify, vibe) and the Program.

    Both the ``program`` block and the ``music_mode`` label are the daemon's
    *authoritative* Program status, read fresh from ``voxd`` on every call --
    never a server-side cache, which could serve a stale music shadow:
    ``music_mode`` is derived from ``program.mode`` here,
    so another client stopping or starting music can never leave the two fields
    contradicting each other. When ``voxd`` is unreachable the block carries an
    ``error`` and ``music_mode`` reports ``off`` (nothing can be confirmed playing).

    Returns:
        JSON string with the session display fields plus the authoritative
        ``program`` status and its derived ``music_mode``.
    """
    _session.refresh_from_config()
    payload: dict[str, object] = {
        "provider": _session.provider,
        "voice": _session.voice,
        "notify": _session.notify,
        "speak": _session.speak,
        "vibe_mode": _session.vibe_mode,
        "vibe": _session.vibe,
        "vibe_tags": _session.vibe_tags,
        "style": _music_pref.style,
        "vibe_trace": VibeTraceLog.default().health(),
        "log": log_health(),
        "log_level": ConfigStore.resolve_log_level(),
    }
    try:
        program_status = _program_tools.status()
    except _DAEMON_ERRORS as exc:
        payload["program"] = {"error": str(exc)}
        payload["music_mode"] = "off"
    else:
        payload["program"] = program_status.to_dict()
        payload["music_mode"] = "off" if program_status.mode is Mode.OFF else "on"
    return json.dumps(payload)


# ---------------------------------------------------------------------------
# Server lifecycle
# ---------------------------------------------------------------------------


def run_server() -> None:
    """Run the MCP server with stdio transport."""
    global _session

    configure_client_logging(role="mcp")
    logger.info("Starting vox MCP server (mic)")

    # Seed session config from per-repo config if it exists.
    config_dir = _find_config_dir()
    _session = SessionConfig.from_config(config_dir)

    # Mark speak as explicitly set if the config file had it.
    if (
        config_dir is not None
        and ConfigStore(config_dir).read_field("speak") is not None
    ):
        _session.set_speak(_session.speak)

    logger.info("%s", _session.summary_sentence())

    mcp.run(transport="stdio")


if __name__ == "__main__":
    run_server()
