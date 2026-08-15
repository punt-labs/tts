"""Typer CLI for punt-vox."""
# pyright: reportUnusedFunction=false
# Every @app.command function is referenced by typer at registration, not by name.

from __future__ import annotations

import asyncio
import json
import logging
import shutil
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, cast

import typer

from punt_vox import __version__, commands as cmds
from punt_vox.api_key_resolver import ApiKeyResolver
from punt_vox.cli_daemon import build_daemon_app
from punt_vox.cli_desktop import build_desktop_app
from punt_vox.cli_enablement import build_enablement_commands
from punt_vox.cli_io import OutputFlags, TextInput
from punt_vox.cli_music import MusicCli
from punt_vox.cli_rec import build_rec_app
from punt_vox.client_errors import VoxdConnectionError, VoxdProtocolError
from punt_vox.client_sync import VoxClientSync
from punt_vox.commands import CommandResult, Ctx
from punt_vox.config import ConfigStore
from punt_vox.dirs import DEFAULT_CONFIG_DIR, find_config_dir
from punt_vox.doctor import claude_desktop_config_path
from punt_vox.hooks import hook_app
from punt_vox.output_formatter import OutputFormatter
from punt_vox.session_spec import SessionSpec
from punt_vox.types_synthesis import SynthesisSpec
from punt_vox.types_synthesis_errors import (
    ModelNotAvailableError,
    ProviderNotConfiguredError,
)
from punt_vox.vibe import VibeChange

if TYPE_CHECKING:
    from collections.abc import Coroutine

    # Annotation-only; keeps `client` off __main__'s runtime import graph.
    from punt_vox.client import SynthesizeResult

logger = logging.getLogger(__name__)

app = typer.Typer(
    name="vox",
    help="Text-to-speech CLI.",
    no_args_is_help=True,
    context_settings={"help_option_names": ["-h", "--help"]},
)
app.add_typer(hook_app, name="hook", hidden=True)

# ---------------------------------------------------------------------------
# cache subcommand group
# ---------------------------------------------------------------------------

cache_app = typer.Typer(
    help="Manage the MP3 quip cache.",
    no_args_is_help=True,
)
app.add_typer(cache_app, name="cache")

# ---------------------------------------------------------------------------
# Display constants
# ---------------------------------------------------------------------------

_PROVIDER_DISPLAY = {
    "elevenlabs": "ElevenLabs",
    "polly": "Polly",
    "openai": "OpenAI",
    "say": "Say",
    "espeak": "eSpeak",
}

# ---------------------------------------------------------------------------
# Global state
# ---------------------------------------------------------------------------

_formatter = OutputFormatter()
_flags = OutputFlags(_formatter)
_text_input = TextInput(_formatter)


def _validated_spec(spec: SynthesisSpec) -> SynthesisSpec:
    """Validate a spec at the CLI boundary, returning it for chaining.

    Translates ``ValueError`` from :meth:`SynthesisSpec.validate` into
    ``typer.BadParameter`` so the CLI displays a user-friendly message. The
    caller builds the :class:`SynthesisSpec` (the bundle already names every
    field), so this stays a one-argument boundary check rather than re-listing
    a dozen parameters.
    """
    try:
        spec.validate()
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc
    return spec


def _fill_from_state(spec: SynthesisSpec) -> SynthesisSpec:
    """Fill unset fields from the repo's ``vox.md`` through :class:`SessionSpec`.

    The CLI counterpart to :meth:`SessionConfig.fill_defaults` (server.py):
    ``vox say`` used to ignore ``vox.md`` entirely, so a bare ``vox say "hi"``
    sent no provider and let the daemon guess. Filling here makes state the
    authority on ``vox say`` too. ``vox rec new`` fills through the same
    :class:`SessionSpec` from its own helper in :mod:`cli_rec`, matching
    the shape here.

    An unconfigured provider is the F1 refusal -- reported on stderr and
    exit 1 -- and an incompatible provider/model pair is the F7 refusal,
    same treatment.
    """
    config = ConfigStore(find_config_dir() or DEFAULT_CONFIG_DIR).read()
    try:
        return SessionSpec(config).fill(spec)
    except (ProviderNotConfiguredError, ModelNotAvailableError) as exc:
        message = str(exc)
        _formatter.error(message, f"Error: {message}")
        raise typer.Exit(code=1) from exc


# ---------------------------------------------------------------------------
# API key resolution
#
# Passing ``--api-key <value>`` literally on the command line exposes the
# value through ``ps`` (and ``/proc/<pid>/cmdline`` on Linux), shell history,
# and terminal recordings -- a real credential disclosure path even though
# voxd never logs or persists the key. The safer file/stdin/env sources and
# the mutual-exclusion priority live in ``ApiKeyResolver`` and the option help.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Annotated type aliases for shared options
# ---------------------------------------------------------------------------

Verbose = Annotated[
    bool,
    typer.Option("--verbose", "-v", help="Enable debug logging."),
]
Quiet = Annotated[
    bool,
    typer.Option("--quiet", "-q", help="Suppress non-JSON output."),
]
JsonOutput = Annotated[
    bool,
    typer.Option("--json", help="Output JSON."),
]
ProviderOpt = Annotated[
    str | None,
    typer.Option(
        "--provider",
        envvar="TTS_PROVIDER",
        help=(
            "TTS provider (elevenlabs, polly, openai, say, espeak)."
            " Default: auto-detect."
        ),
    ),
]
ModelOpt = Annotated[
    str | None,
    typer.Option(
        "--model",
        envvar="TTS_MODEL",
        help="Model name (e.g. eleven_v3, tts-1). Provider-specific.",
    ),
]
VoiceOpt = Annotated[
    str | None,
    typer.Option("--voice", help="Voice name. Default: provider-specific."),
]
LanguageOpt = Annotated[
    str | None,
    typer.Option("--language", "--lang", help="ISO 639-1 language code (e.g. de, ko)."),
]
RateOpt = Annotated[
    int,
    typer.Option("--rate", help="Speech rate as percentage (e.g. 90 = 90% speed)."),
]
StabilityOpt = Annotated[
    float | None,
    typer.Option("--stability", help="ElevenLabs voice stability (0.0-1.0)."),
]
SimilarityOpt = Annotated[
    float | None,
    typer.Option("--similarity", help="ElevenLabs voice similarity boost (0.0-1.0)."),
]
StyleOpt = Annotated[
    float | None,
    typer.Option("--style", help="ElevenLabs voice style/expressiveness (0.0-1.0)."),
]
SpeakerBoostFlag = Annotated[
    bool,
    typer.Option("--speaker-boost", help="Enable ElevenLabs speaker boost."),
]
OnceOpt = Annotated[
    int | None,
    typer.Option(
        "--once",
        help=(
            "Deduplicate identical text within N seconds. When set, voxd "
            "skips the play if the same text was played within the window "
            "(e.g. when multiple Claude Code sessions broadcast the same "
            "biff wall). Omit to play every time. Must be a positive "
            "integer when set."
        ),
    ),
]
ApiKeyOpt = Annotated[
    str | None,
    typer.Option(
        "--api-key",
        envvar="VOX_API_KEY",
        help=(
            "Per-call provider API key. Forwarded to voxd over the local "
            "WebSocket and used for this single synthesis request only. "
            "Lets a single user maintain multiple ElevenLabs/OpenAI keys "
            "for per-project billing attribution without juggling "
            "environment variables. Not persisted, not logged, never "
            "echoed to stdout. vox is single-user — this is cost-tracking, "
            "not multi-tenant isolation. Passing --api-key literally on "
            "the command line exposes the value via 'ps' and shell history; "
            "prefer VOX_API_KEY env var, --api-key-file, or --api-key-stdin "
            "for real credentials."
        ),
    ),
]
ApiKeyFileOpt = Annotated[
    Path | None,
    typer.Option(
        "--api-key-file",
        help=(
            "Read per-call provider API key from a file. Safer than "
            "--api-key on the command line because the value never "
            "appears in argv, shell history, or 'ps'. The file should "
            "be mode 0600; vox warns if any group or other permission "
            "bits are set. Empty files and non-files are rejected. "
            "Trailing whitespace and newlines are stripped."
        ),
    ),
]
ApiKeyStdinFlag = Annotated[
    bool,
    typer.Option(
        "--api-key-stdin",
        help=(
            "Read per-call provider API key from stdin (one line). "
            "Safer than --api-key on the command line because the "
            "value never appears in argv. Intended for piped input "
            "from a password manager, e.g. 'pass show vox/project | "
            "vox say ... --api-key-stdin'. Refuses to read from a "
            "tty."
        ),
    ),
]
FromOpt = Annotated[
    Path | None,
    typer.Option("--from", help="JSON file with segments array.", exists=True),
]
TextArg = Annotated[
    str | None, typer.Argument(help="Text to synthesize.", show_default=False)
]


# ---------------------------------------------------------------------------
# callback (global flags)
# ---------------------------------------------------------------------------


@app.callback()
def _callback(  # pyright: ignore[reportUnusedFunction]
    json_output: JsonOutput = False,  # noqa: FBT002 -- typer CLI requires bool default
    verbose: Verbose = False,  # noqa: FBT002 -- typer CLI requires bool default
    quiet: Quiet = False,  # noqa: FBT002 -- typer CLI requires bool default
) -> None:
    """Text-to-speech CLI."""
    _flags.reset()
    _flags.apply(json_output=json_output, verbose=verbose, quiet=quiet)


# ---------------------------------------------------------------------------
# say — play audio
# ---------------------------------------------------------------------------


def _dedup_fields(result: SynthesizeResult) -> dict[str, object]:
    """Return the dedup annotations for a played result, empty if fresh."""
    if not result.deduped:
        return {}
    fields: dict[str, object] = {"deduped": True}
    if result.original_played_at is not None:
        fields["original_played_at"] = result.original_played_at
    if result.ttl_seconds_remaining is not None:
        fields["ttl_seconds_remaining"] = result.ttl_seconds_remaining
    return fields


def _speak_segments(
    segments: list[str],
    spec: SynthesisSpec,
    once: int | None,
) -> None:
    """Synthesize and emit each segment; map voxd errors to a CLI exit."""
    client = VoxClientSync()
    for seg_text in segments:
        try:
            result = client.synthesize(seg_text, spec, once=once)
        except (VoxdConnectionError, VoxdProtocolError) as exc:
            _formatter.error(str(exc), f"Error: {exc}")
            raise typer.Exit(code=1) from exc
        payload: dict[str, object] = {"id": result.request_id, **_dedup_fields(result)}
        _formatter.emit(payload, seg_text)


@app.command()
def say(  # pyright: ignore[reportUnusedFunction]
    ctx: typer.Context,
    text: TextArg = None,
    from_file: FromOpt = None,
    voice: VoiceOpt = None,
    language: LanguageOpt = None,
    rate: RateOpt = 90,
    provider: ProviderOpt = None,
    model: ModelOpt = None,
    stability: StabilityOpt = None,
    similarity: SimilarityOpt = None,
    style: StyleOpt = None,
    speaker_boost: SpeakerBoostFlag = False,  # noqa: FBT002 -- typer CLI requires bool default
    once: OnceOpt = None,
    api_key: ApiKeyOpt = None,
    api_key_file: ApiKeyFileOpt = None,
    api_key_stdin: ApiKeyStdinFlag = False,  # noqa: FBT002 -- typer CLI requires bool default
    *,
    json_output: JsonOutput = False,
    verbose: Verbose = False,
    quiet: Quiet = False,
) -> None:
    """Synthesize and play audio via voxd.

    Reads the text from the TEXT argument, from ``--from`` (a JSON segments
    file), or from stdin when TEXT is ``-`` or the input is piped.
    """
    _flags.apply(json_output=json_output, verbose=verbose, quiet=quiet)
    # Negative values are a user error. Zero is accepted and treated
    # as unset (no dedup) — matches the server-side semantics so the
    # two surfaces are consistent. Scripts can safely pass
    # ``--once ${ONCE_TTL:-0}`` as a default.
    if once is not None and once < 0:
        raise typer.BadParameter(
            "--once must be a non-negative integer (seconds). "
            "Use 0 or omit the flag to disable dedup."
        )
    once = once or None

    # Empty string from ``VOX_API_KEY=""`` or a literal ``--api-key ""``
    # is normalized to ``None`` so it does not shadow the mutual
    # exclusion rules in ``_resolve_api_key``. Real-world trigger: a CI
    # pipeline that exports ``VOX_API_KEY=""`` globally (because some
    # jobs use vox and others don't) would otherwise be unable to pass
    # ``--api-key-file`` or ``--api-key-stdin`` — typer hands the empty
    # env value to ``api_key``, and without this normalization the
    # mutual-exclusion check counts it as a fourth source. The
    # individual readers (``_read_api_key_file``, ``_read_api_key_stdin``)
    # still reject their own empty content with their own BadParameter
    # messages, so there is no silent fall-through for paths where
    # emptiness is actually a user error.
    api_key = api_key or None

    # Resolve the per-call API key from exactly one of the four
    # supported sources (file, stdin, env var, argv) and fire a
    # stderr warning when the argv path was used. See
    # ``_resolve_api_key`` for the full rationale.
    resolved_api_key = ApiKeyResolver(
        ctx, api_key, api_key_file, api_key_stdin=api_key_stdin
    ).resolve()

    spec = _fill_from_state(
        _validated_spec(
            SynthesisSpec(
                voice=voice,
                language=language,
                rate=rate,
                provider=provider,
                model=model,
                stability=stability,
                similarity=similarity,
                style=style,
                speaker_boost=speaker_boost or None,
                api_key=resolved_api_key,
            )
        )
    )

    segments = _text_input.resolve(text, from_file)
    _speak_segments(segments, spec, once)


# ---------------------------------------------------------------------------
# vibe — set session mood
# ---------------------------------------------------------------------------


@app.command("vibe")
def vibe_cmd(  # pyright: ignore[reportUnusedFunction]
    mood: Annotated[
        str,
        typer.Argument(
            help=(
                "Mood word (e.g. 'excited', 'weary') pinned as manual, "
                "'auto' (mood follows session signals), or 'off' (neutral)."
            )
        ),
    ],
) -> None:
    """Set the session mood the voice director translates into voice tags.

    The daemon resolves the mood into 1-3 expressive tags (e.g. [excited],
    [weary]) that ride the next synthesis call. A mode change ('auto' or
    'off') resets the nudge cadence.

    Example: vox vibe excited
    Example: vox vibe auto

    See also: vox status (current mood), mic:vibe (MCP peer).
    """
    # Route through VibeChange so the CLI and MCP tool share one transition rule
    # (a mode change resets the nudge cadence).
    is_mode = mood in ("auto", "off")
    change = VibeChange(
        mood=None if is_mode else mood, tags=None, mode=mood if is_mode else "manual"
    )
    ConfigStore(find_config_dir() or DEFAULT_CONFIG_DIR).write_fields(change.resolve())
    if is_mode:
        _formatter.emit({"vibe_mode": mood}, f"Vibe mode: {mood}")
    else:
        _formatter.emit({"vibe": mood, "vibe_mode": "manual"}, f"Vibe: {mood}")


# ---------------------------------------------------------------------------
# enable / disable / notify — per-repo enablement + notification level
#
# vox enable / vox disable (+ --purge) and vox notify normal|continuous live in
# cli_enablement as bound methods, registered here as top-level commands so they
# read `vox enable`, not `vox enablement enable`.
# ---------------------------------------------------------------------------

build_enablement_commands(app, _formatter, _flags)


# ---------------------------------------------------------------------------
# speak — toggle spoken vs chime notifications (y/n)
# ---------------------------------------------------------------------------


@app.command("speak")
def speak_cmd(  # pyright: ignore[reportUnusedFunction]
    mode: Annotated[
        str,
        typer.Argument(
            help=(
                "'y' plays notifications as spoken voice; "
                "'n' plays them as chimes only (muted voice)."
            )
        ),
    ],
) -> None:
    """Toggle whether notifications are spoken or chimed.

    Sets the per-repo ``speak`` field. When 'n', the daemon still plays a
    chime for each notification event but does not synthesize speech --
    useful when the room is shared or the mic budget is tight.

    Example: vox speak y
    Example: vox speak n

    See also: vox notify (level within 'on'), mic:speak (MCP peer).
    """
    if mode not in ("y", "n"):
        typer.echo("Error: mode must be y or n.", err=True)
        raise typer.Exit(code=1)

    ConfigStore(find_config_dir() or DEFAULT_CONFIG_DIR).write_field("speak", mode)
    label = "Voice on." if mode == "y" else "Muted — chimes only."
    _formatter.emit({"speak": mode}, label)


# ---------------------------------------------------------------------------
# log — set the local log verbosity (a low-key debugging aid)
# ---------------------------------------------------------------------------


@app.command("log")
def log_cmd(
    level: Annotated[
        str,
        typer.Argument(help="Log verbosity: info (quiet default) or debug."),
    ],
) -> None:
    """Set the daemon's log verbosity live over the wire (info or debug).

    Routes to voxd so the running daemon -- including a remote one over
    ``VOXD_HOST`` -- applies the level immediately; the daemon clamps to the INFO
    audit floor server-side, so the audit trail is never blinded. Use
    ``--verbose`` to raise a single client command instead.
    """
    normalized = level.lower()
    if normalized not in ("info", "debug"):
        typer.echo("Error: level must be info or debug.", err=True)
        raise typer.Exit(code=1)
    try:
        effective = VoxClientSync().set_log_level(normalized)
    except (VoxdConnectionError, VoxdProtocolError) as exc:
        _formatter.error(str(exc), f"Error: {exc}")
        raise typer.Exit(code=1) from exc
    label = (
        "Debug logging on — the daemon is now verbose."
        if effective == "debug"
        else "Log level back to info."
    )
    _formatter.emit({"log_level": effective}, label)


# ---------------------------------------------------------------------------
# model / provider / voice -- switch tools (vox-0rp9). Each takes an optional
# positional NAME: no arg lists (marking the current selection); NAME given
# writes to the same ConfigStore field the MCP tools write, so the CLI and
# the mic:{model,provider,voice} tools share one write path.
# ---------------------------------------------------------------------------


_ModelName = Annotated[
    str | None,
    typer.Argument(
        help=(
            "Model name (e.g. eleven_v3, tts-1) or an elevenlabs shorthand "
            "(v3, flash, turbo, multilingual). Omit to list."
        ),
        show_default=False,
    ),
]
_ProviderName = Annotated[
    str | None,
    typer.Argument(
        help="One of elevenlabs, openai, polly, say, espeak. Omit to list.",
        show_default=False,
    ),
]
_VoiceName = Annotated[
    str | None,
    typer.Argument(
        help="Voice name (e.g. matilda, roger). Omit to list.",
        show_default=False,
    ),
]


def _ctx() -> Ctx:
    """Wire the collaborators the command layer needs."""
    return Ctx(ConfigStore(find_config_dir() or DEFAULT_CONFIG_DIR), VoxClientSync())


def _run(coro: Coroutine[object, object, CommandResult]) -> None:
    """Drive *coro* to completion and route its result through the formatter.

    JSON mode wraps errors under an ``error`` key so ``--json`` consumers parse
    one object; text mode routes the human message to stderr on failure. The
    exit code the result carries becomes the process exit on failure.
    """
    result = asyncio.run(coro)
    if result.error:
        detail = result.json_data if result.json_data is not None else result.text
        _formatter.error(detail, result.text)
        raise typer.Exit(code=result.exit_code)
    payload = (
        result.json_data if result.json_data is not None else {"text": result.text}
    )
    _formatter.emit(payload, result.text)


@app.command("model")
def model_cmd(  # pyright: ignore[reportUnusedFunction]
    name: _ModelName = None,
    *,
    json_output: JsonOutput = False,
    verbose: Verbose = False,
    quiet: Quiet = False,
) -> None:
    """List or set the TTS model for the current provider.

    ``vox model`` lists the models the current provider offers, marking the
    current selection. ``vox model <name>`` resolves elevenlabs shorthand
    (``v3`` -> ``eleven_v3``, etc.) and writes it to ``.punt-labs/vox/vox.md``.
    """
    _flags.apply(json_output=json_output, verbose=verbose, quiet=quiet)
    _run(cmds.model(_ctx(), name))


@app.command("provider")
def provider_cmd(  # pyright: ignore[reportUnusedFunction]
    name: _ProviderName = None,
    *,
    json_output: JsonOutput = False,
    verbose: Verbose = False,
    quiet: Quiet = False,
) -> None:
    """List or set the TTS provider.

    ``vox provider`` lists the five providers, marking the current selection.
    ``vox provider <name>`` writes to ``.punt-labs/vox/vox.md``. On a
    genuine provider change the stale model is cleared in the same write --
    model names are provider-scoped, so ``eleven_v3`` reaching an OpenAI
    request is an invalid API call.
    """
    _flags.apply(json_output=json_output, verbose=verbose, quiet=quiet)
    _run(cmds.provider(_ctx(), name))


@app.command("voice")
def voice_cmd(  # pyright: ignore[reportUnusedFunction]
    name: _VoiceName = None,
    provider: ProviderOpt = None,
    *,
    json_output: JsonOutput = False,
    verbose: Verbose = False,
    quiet: Quiet = False,
) -> None:
    """List or set the session voice for the active (or given) provider.

    ``vox voice`` lists the roster; ``vox voice <name>`` writes to
    ``.punt-labs/vox/vox.md`` (a stray leading ``@`` is stripped).
    """
    _flags.apply(json_output=json_output, verbose=verbose, quiet=quiet)
    _run(cmds.voice(_ctx(), name, provider))


# ---------------------------------------------------------------------------
# version
# ---------------------------------------------------------------------------


@app.command("version")
def version_cmd(  # pyright: ignore[reportUnusedFunction]
    *,
    json_output: JsonOutput = False,
    verbose: Verbose = False,
    quiet: Quiet = False,
) -> None:
    """Print version."""
    _flags.apply(json_output=json_output, verbose=verbose, quiet=quiet)
    _formatter.emit({"version": __version__}, f"vox {__version__}")


# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------


@app.command("status")
def status_cmd(  # pyright: ignore[reportUnusedFunction]
    *,
    json_output: JsonOutput = False,
    verbose: Verbose = False,
    quiet: Quiet = False,
) -> None:
    """Show current state (daemon, voice, vibe, notify, desktop registration)."""
    _flags.apply(json_output=json_output, verbose=verbose, quiet=quiet)
    cfg = ConfigStore(find_config_dir() or DEFAULT_CONFIG_DIR).read()

    # Ping voxd for liveness only; the provider comes from state, not
    # from the daemon (design §3.6 -- the daemon owns no provider of its
    # own, and the health probe used to invent one, which was the
    # substitution defect this bead closes).
    daemon_status = "not running"
    try:
        VoxClientSync().health()
        daemon_status = "running"
    except (VoxdConnectionError, VoxdProtocolError):
        pass

    provider_name = cfg.provider or "unknown"
    display_name = _PROVIDER_DISPLAY.get(provider_name, provider_name)
    desktop_reg = _desktop_registration()

    info: dict[str, str | None] = {
        "daemon": daemon_status,
        "provider": provider_name,
        "voice": cfg.voice or None,
        "notify": cfg.notify,
        "speak": cfg.speak,
        "vibe_mode": cfg.vibe_mode,
        "vibe": cfg.vibe,
        "vibe_tags": cfg.vibe_tags,
        # The effective level (global vox log setting, or a repo override), not
        # this dir's raw field -- so status reflects what the daemon/clients use.
        "log_level": ConfigStore.resolve_log_level(),
        "desktop": desktop_reg,
    }

    text_lines = [
        f"Daemon:    {daemon_status}",
        f"Provider:  {display_name}",
        f"Voice:     {info['voice'] or '(default)'}",
        f"Notify:    {info['notify']}",
        f"Speak:     {info['speak']}",
        f"Vibe mode: {info['vibe_mode']}",
        f"Log level: {info['log_level']}",
        f"Desktop:   {desktop_reg}",
    ]
    if cfg.vibe:
        text_lines.append(f"Vibe:      {cfg.vibe}")
    if cfg.vibe_tags:
        text_lines.append(f"Tags:      {cfg.vibe_tags}")
    _formatter.emit(info, "\n".join(text_lines))


def _desktop_registration() -> str:
    """Return the Claude Desktop registration state as one operator-visible line.

    Four states are surfaced so the operator sees exactly what
    ``vox desktop uninstall`` would (or would not) clean up:

    - ``"registered"`` -- a ``vox`` entry lives under ``mcpServers``.
    - ``"not registered"`` -- config exists, no ``vox`` entry (or the top
      level is not an object, so no ``mcpServers`` map can be present).
    - ``"no config"`` -- the config file does not exist.
    - ``"config unreadable"`` -- the file exists but is malformed or
      permission-denied. Surfaced distinctly from ``not registered``
      because the operator cannot conclude anything about the
      registration from a file the CLI cannot parse.
    """
    config_path = claude_desktop_config_path()
    if not config_path.exists():
        return "no config"
    try:
        data = json.loads(config_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return "config unreadable"
    if not isinstance(data, dict):
        return "not registered"
    servers = cast("dict[str, object]", data).get("mcpServers")
    if isinstance(servers, dict) and "vox" in servers:
        return "registered"
    return "not registered"


# ---------------------------------------------------------------------------
# doctor
# ---------------------------------------------------------------------------


@app.command()
def doctor() -> None:  # pyright: ignore[reportUnusedFunction]
    """Check system health for vox."""
    from punt_vox.doctor import DoctorCheck, format_results

    check = DoctorCheck(client=VoxClientSync())
    results = check.run_all()
    payload, text = format_results(results)
    _formatter.emit(payload, text)

    if payload.get("failed", 0):
        raise typer.Exit(code=1)


# ---------------------------------------------------------------------------
# install / uninstall (Claude Code marketplace)
# ---------------------------------------------------------------------------

_PLUGIN_ID = "vox@punt-labs"


@app.command()
def install() -> None:
    """Install the Claude Code plugin and daemon service."""
    # Step 1: Claude Code plugin
    typer.echo("[1/3] Installing Claude Code plugin...")
    claude = shutil.which("claude")
    if not claude:
        typer.echo("Error: claude CLI not found on PATH", err=True)
        raise typer.Exit(code=1)

    result = subprocess.run(
        [claude, "plugin", "install", _PLUGIN_ID, "--scope", "user"],
        check=False,
    )
    if result.returncode != 0:
        typer.echo("Error: plugin install failed", err=True)
        raise typer.Exit(code=1)
    typer.echo("  \u2713 plugin installed")

    # Step 2: daemon service (best-effort — not available in CI/containers)
    # SystemExit: service.detect_platform() raises on unsupported platforms.
    # OSError/CalledProcessError: subprocess and filesystem failures during install.
    # LaunchctlError: macOS bring-up (bootstrap/kickstart) fails on a GUI-less host.
    # ServiceHealthError: voxd registered but never answered health (silent-down).
    typer.echo("[2/3] Registering vox daemon...")
    from punt_vox.service import install as svc_install
    from punt_vox.service.health_verify import ServiceHealthError
    from punt_vox.service.launchctl import LaunchctlError

    try:
        msg = svc_install()
        typer.echo(f"  \u2713 {msg}")
    except (
        SystemExit,
        OSError,
        subprocess.CalledProcessError,
        LaunchctlError,
        ServiceHealthError,
    ) as exc:
        typer.echo(f"  \u2022 Skipped: {exc}")
        typer.echo("    Daemon registration is optional — vox works without it.")

    # Step 3: write the usage guide and register its @-import so it loads in
    # every Claude Code session. OSError only -- a read-only home should warn,
    # not abort an otherwise-successful plugin install.
    typer.echo("[3/3] Registering vox usage guide...")
    from punt_vox.guidance import VoxGuidance

    try:
        typer.echo(f"  ✓ {VoxGuidance.for_current_user().install()}")
    except OSError as exc:
        typer.echo(f"  • Skipped: {exc}")

    typer.echo()
    _formatter.emit(
        {"installed": True},
        "Installed. Restart Claude Code to activate.",
    )


@app.command()
def uninstall() -> None:
    """Uninstall the Claude Code plugin."""
    claude = shutil.which("claude")
    if not claude:
        typer.echo("Error: claude CLI not found on PATH", err=True)
        raise typer.Exit(code=1)

    result = subprocess.run(
        [claude, "plugin", "uninstall", _PLUGIN_ID, "--scope", "user"],
        check=False,
    )
    plugin_failed = result.returncode != 0
    if plugin_failed:
        typer.echo("Error: plugin uninstall failed", err=True)

    # Prune the usage guide + its @-import regardless of the plugin outcome:
    # uninstall must be idempotent and self-healing, so a plugin step that fails
    # (e.g. the plugin was already gone) must not orphan ~/.punt-labs/vox/CLAUDE.md
    # or its global import line. A teardown failure (OSError -- a permissions
    # error or a filesystem hiccup) is surfaced distinctly from the plugin
    # outcome and forces a non-zero exit: reporting ``Uninstalled.`` while the
    # guide or its global @-import survives would be a silent failure.
    from punt_vox.guidance import VoxGuidance

    guide = VoxGuidance.for_current_user()
    guide_failed = False
    try:
        typer.echo(guide.uninstall())
    except OSError as exc:
        guide_failed = True
        typer.echo(f"Error: vox usage guide teardown failed: {exc}", err=True)
        typer.echo(
            f"  These may remain -- remove by hand or re-run 'vox uninstall': "
            f"guide {guide.doc_path}; import {guide.import_line} "
            f"in {guide.global_path}",
            err=True,
        )

    if plugin_failed or guide_failed:
        raise typer.Exit(code=1)

    _formatter.emit({"uninstalled": True}, "Uninstalled.")


@app.command("register-guidance", hidden=True)
def register_guidance(
    *,
    remove: Annotated[
        bool,
        typer.Option("--remove", "-r", help="Prune the guide instead of writing it."),
    ] = False,
) -> None:
    """Write (or remove) the usage guide and its ``@``-import.

    Hidden plumbing for install scripts (``install.sh``) that register the
    plugin directly via ``claude plugin install`` and so never reach the
    ``vox install`` command. Idempotent: the global ``CLAUDE.md`` is rewritten
    only when the import line actually changes.
    """
    from punt_vox.guidance import VoxGuidance

    guide = VoxGuidance.for_current_user()
    try:
        typer.echo(guide.uninstall() if remove else guide.install())
    except OSError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=1) from exc


# ---------------------------------------------------------------------------
# cache commands
# ---------------------------------------------------------------------------


@cache_app.command("status")
def cache_status_cmd() -> None:  # pyright: ignore[reportUnusedFunction]
    """Show the daemon cache's entry count, size, and path (honors VOXD_HOST)."""
    try:
        info = VoxClientSync().cache_status()
    except (VoxdConnectionError, VoxdProtocolError) as exc:
        _formatter.error(str(exc), f"Error: {exc}")
        raise typer.Exit(code=1) from exc
    size_kb = info.size_bytes / 1024
    payload = {
        "entries": info.entries,
        "size_bytes": info.size_bytes,
        "path": str(info.path),
    }
    text = f"Entries: {info.entries}\nSize:    {size_kb:.1f} KB\nPath:    {info.path}"
    _formatter.emit(payload, text)


@cache_app.command("clear")
def cache_clear_cmd() -> None:  # pyright: ignore[reportUnusedFunction]
    """Delete all cached MP3 files on the daemon host."""
    try:
        count = VoxClientSync().cache_clear()
    except (VoxdConnectionError, VoxdProtocolError) as exc:
        _formatter.error(str(exc), f"Error: {exc}")
        raise typer.Exit(code=1) from exc
    _formatter.emit({"cleared": count}, f"Cleared {count} cached files.")


# ---------------------------------------------------------------------------
# mcp (stdio transport for Claude Code plugin)
# ---------------------------------------------------------------------------


@app.command()
def mcp() -> None:
    """Run the MCP server with stdio transport."""
    from punt_vox.server import run_server

    run_server()


# ---------------------------------------------------------------------------
# music subcommand group (consume-only; implementation in cli_music)
# ---------------------------------------------------------------------------

app.add_typer(MusicCli.build_app(_formatter, _flags), name="music")


# ---------------------------------------------------------------------------
# rec subcommand group (recordings store; implementation in cli_rec)
# ---------------------------------------------------------------------------

app.add_typer(build_rec_app(_formatter, _flags), name="rec")


# ---------------------------------------------------------------------------
# daemon subcommand group (implementation in cli_daemon)
# ---------------------------------------------------------------------------

app.add_typer(build_daemon_app(_formatter, _flags), name="daemon")


# ---------------------------------------------------------------------------
# desktop subcommand group (implementation in cli_desktop)
# ---------------------------------------------------------------------------

app.add_typer(build_desktop_app(_formatter, _flags), name="desktop")


if __name__ == "__main__":
    app()
