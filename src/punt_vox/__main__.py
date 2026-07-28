"""Typer CLI for punt-vox."""
# pyright: reportUnusedFunction=false
# Every @app.command function is referenced by typer at registration, not by name.

from __future__ import annotations

import json
import logging
import platform
import shutil
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING, Annotated

import typer

from punt_vox import __version__
from punt_vox.api_key_resolver import ApiKeyResolver
from punt_vox.cli_enablement import build_enablement_commands
from punt_vox.cli_io import OutputFlags, TextInput
from punt_vox.cli_music import build_music_app
from punt_vox.cli_rec import build_rec_app
from punt_vox.client_errors import VoxdConnectionError, VoxdProtocolError
from punt_vox.client_sync import VoxClientSync
from punt_vox.config import ConfigStore
from punt_vox.desktop_install import DesktopInstaller
from punt_vox.dirs import DEFAULT_CONFIG_DIR, default_output_dir, find_config_dir
from punt_vox.hooks import hook_app
from punt_vox.output_formatter import OutputFormatter
from punt_vox.types_synthesis import SynthesisSpec
from punt_vox.vibe import VibeChange

if TYPE_CHECKING:
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
    typer.Option("--rate", help="Speech rate as percentage (e.g. 90 = 90%% speed)."),
]
OutputDirOpt = Annotated[
    Path | None,
    typer.Option("--output-dir", "-d", help="Output directory. Default: ~/Music/vox."),
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
    if once == 0:
        once = None

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
    if api_key == "":
        api_key = None

    # Resolve the per-call API key from exactly one of the four
    # supported sources (file, stdin, env var, argv) and fire a
    # stderr warning when the argv path was used. See
    # ``_resolve_api_key`` for the full rationale.
    resolved_api_key = ApiKeyResolver(
        ctx, api_key, api_key_file, api_key_stdin=api_key_stdin
    ).resolve()

    boost = speaker_boost if speaker_boost else None
    spec = _validated_spec(
        SynthesisSpec(
            voice=voice,
            language=language,
            rate=rate,
            provider=provider,
            model=model,
            stability=stability,
            similarity=similarity,
            style=style,
            speaker_boost=boost,
            api_key=resolved_api_key,
        )
    )

    segments = _text_input.resolve(text, from_file)
    _speak_segments(segments, spec, once)


# ---------------------------------------------------------------------------
# vibe — set session mood
# ---------------------------------------------------------------------------


@app.command("vibe")
def vibe_cmd(  # pyright: ignore[reportUnusedFunction]
    mood: Annotated[str, typer.Argument(help="Mood description or 'auto'/'off'.")],
) -> None:
    """Set session mood for TTS voice."""
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
        typer.Argument(help="Speak mode: y (voice) or n (chimes only)."),
    ],
) -> None:
    """Toggle spoken notifications on or off."""
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
# voice — set session voice
# ---------------------------------------------------------------------------


@app.command("voice")
def voice_cmd(  # pyright: ignore[reportUnusedFunction]
    name: Annotated[str, typer.Argument(help="Voice name (e.g. matilda, roger).")],
) -> None:
    """Set the session voice, tolerating a stray leading '@' sigil."""
    voice = SynthesisSpec.normalize_voice(name)
    if voice is None:
        raise typer.BadParameter("voice name is empty")
    ConfigStore(find_config_dir() or DEFAULT_CONFIG_DIR).write_field("voice", voice)
    _formatter.emit({"voice": voice}, f"{voice}'s here.")


# ---------------------------------------------------------------------------
# voices — list available voices
# ---------------------------------------------------------------------------


@app.command("voices")
def voices_cmd(  # pyright: ignore[reportUnusedFunction]
    provider: ProviderOpt = None,
    *,
    json_output: JsonOutput = False,
    verbose: Verbose = False,
    quiet: Quiet = False,
) -> None:
    """List the voices the active (or given) provider offers.

    Human output lists the voice names one per line, with the current session
    voice marked ``(current)``. ``--json`` emits both the full list and the
    current voice as one object for machine consumers.
    """
    _flags.apply(json_output=json_output, verbose=verbose, quiet=quiet)
    current = ConfigStore(find_config_dir() or DEFAULT_CONFIG_DIR).read().voice or None
    try:
        names = VoxClientSync().voices(provider)
    except (VoxdConnectionError, VoxdProtocolError) as exc:
        _formatter.error(str(exc), f"Error: {exc}")
        raise typer.Exit(code=1) from exc

    payload: dict[str, object] = {"voices": names, "current": current}
    if provider is not None:
        payload["provider"] = provider
    _formatter.emit(payload, _voices_text(names, current))


def _voices_text(names: list[str], current: str | None) -> str:
    """Render the voice list for humans, marking the current session voice."""
    return "\n".join(f"{n} (current)" if n == current else n for n in names)


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
    """Show current state (daemon, voice, vibe, notify)."""
    _flags.apply(json_output=json_output, verbose=verbose, quiet=quiet)
    cfg = ConfigStore(find_config_dir() or DEFAULT_CONFIG_DIR).read()

    # Try to get provider from voxd health
    daemon_provider: str | None = None
    daemon_status = "not running"
    try:
        health = VoxClientSync().health()
        daemon_provider = health.provider
        daemon_status = "running"
    except (VoxdConnectionError, VoxdProtocolError):
        pass

    provider_name = daemon_provider or "unknown"
    display_name = _PROVIDER_DISPLAY.get(provider_name, provider_name)

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
    }

    text_lines = [
        f"Daemon:    {daemon_status}",
        f"Provider:  {display_name}",
        f"Voice:     {info['voice'] or '(default)'}",
        f"Notify:    {info['notify']}",
        f"Speak:     {info['speak']}",
        f"Vibe mode: {info['vibe_mode']}",
        f"Log level: {info['log_level']}",
    ]
    if cfg.vibe:
        text_lines.append(f"Vibe:      {cfg.vibe}")
    if cfg.vibe_tags:
        text_lines.append(f"Tags:      {cfg.vibe_tags}")
    _formatter.emit(info, "\n".join(text_lines))


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
# install-desktop (Claude Desktop MCP server registration)
# ---------------------------------------------------------------------------


@app.command("install-desktop")
def install_desktop(
    output_dir: OutputDirOpt = None,
    uvx_path: Annotated[
        str | None,
        typer.Option("--uvx-path", help="Path to uvx binary. Default: auto-detect."),
    ] = None,
    install_provider: Annotated[
        str | None,
        typer.Option(
            "--provider",
            help="TTS provider. Default: auto-detect.",
        ),
    ] = None,
) -> None:
    """Register the MCP server with Claude Desktop."""
    if platform.system() != "Darwin":
        typer.echo(
            "Warning: Claude Desktop config path is only known for macOS. "
            "You may need to configure manually on this platform.",
            err=True,
        )

    uvx = uvx_path or shutil.which("uvx")
    if not uvx:
        typer.echo(
            "Error: uvx not found. Install uv (https://docs.astral.sh/uv/) first.",
            err=True,
        )
        raise typer.Exit(code=1)

    audio_dir = output_dir or default_output_dir()
    audio_dir.mkdir(parents=True, exist_ok=True)

    installer = DesktopInstaller.detect(install_provider, audio_dir)

    from punt_vox.doctor import claude_desktop_config_path

    config_path = claude_desktop_config_path()
    config_path.parent.mkdir(parents=True, exist_ok=True)

    if config_path.exists():
        try:
            data = json.loads(config_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as e:
            typer.echo(f"Error: Could not read {config_path}: {e}", err=True)
            raise typer.Exit(code=1) from e
    else:
        data = {}

    if "mcpServers" not in data:
        data["mcpServers"] = {}

    overwriting = "vox" in data["mcpServers"]

    data["mcpServers"]["vox"] = {
        "command": uvx,
        "args": ["--from", "punt-vox", "vox", "mcp"],
        "env": installer.server_env(),
    }

    config_path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")

    if overwriting:
        typer.echo("Updated existing vox entry.")
    else:
        typer.echo("Registered vox MCP server.")

    typer.echo(f"Provider: {installer.provider}")
    typer.echo(f"Config: {config_path}")
    typer.echo(f"Output: {audio_dir}")
    if not installer.daemon_can_authenticate():
        typer.echo(installer.credential_guidance(), err=True)
    typer.echo("Restart Claude Desktop to activate.")


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

app.add_typer(build_music_app(_formatter, _flags), name="music")


# ---------------------------------------------------------------------------
# rec subcommand group (recordings store; implementation in cli_rec)
# ---------------------------------------------------------------------------

app.add_typer(build_rec_app(_formatter, _flags), name="rec")


# ---------------------------------------------------------------------------
# daemon subcommand group
# ---------------------------------------------------------------------------

daemon_app = typer.Typer(
    help="Manage the vox daemon service.",
    no_args_is_help=True,
)
app.add_typer(daemon_app, name="daemon")


@daemon_app.command("install")
def daemon_install_cmd() -> None:  # pyright: ignore[reportUnusedFunction]
    """Register vox as a system service (launchd/systemd).

    Run as your normal user, NOT under ``sudo``. On macOS no sudo is
    needed — the LaunchAgent installs to ``~/Library/LaunchAgents/``.
    On Linux, vox will prompt once for your sudo password to place
    the systemd unit. Running under sudo yourself would cause per-user
    state to land under ``/root/.punt-labs/vox/`` — wrong on both
    platforms.
    """
    from punt_vox.service import install as svc_install

    result = svc_install()
    typer.echo(result)


@daemon_app.command("uninstall")
def daemon_uninstall_cmd() -> None:  # pyright: ignore[reportUnusedFunction]
    """Remove the vox system service."""
    from punt_vox.service import uninstall as svc_uninstall

    result = svc_uninstall()
    typer.echo(result)


@daemon_app.command("restart")
def daemon_restart_cmd() -> None:  # pyright: ignore[reportUnusedFunction]
    """Restart the voxd system service and verify it is back up.

    Use this after ``uv tool upgrade punt-vox`` so the running daemon
    picks up the new wheel. A plain ``uv tool upgrade`` replaces the
    on-disk binary but does not cycle the long-running voxd process —
    so changes to the WebSocket protocol or playback behavior do not
    take effect until the service is restarted. ``vox daemon restart``
    is the supported way to do that.

    Runs as your normal user, NOT under ``sudo``. On macOS, no sudo
    is needed (LaunchAgent). On Linux, vox will prompt once for your
    sudo password when it drives ``systemctl``.
    """
    from punt_vox.daemon_restarter import DaemonRestarter

    restarter = DaemonRestarter(_formatter)
    restarter.run()


@daemon_app.command("status")
def daemon_status_cmd() -> None:  # pyright: ignore[reportUnusedFunction]
    """Check if the vox daemon is reachable.

    Routes through the client so it queries the configured daemon --
    honoring ``VOXD_HOST`` / ``VOXD_PORT`` / ``VOXD_TOKEN`` -- rather than
    a hardcoded ``127.0.0.1``, matching ``vox status`` and ``vox doctor``.
    """
    try:
        health = VoxClientSync().health()
    except (VoxdConnectionError, VoxdProtocolError) as exc:
        typer.echo(f"Daemon: not running ({exc})")
        raise typer.Exit(code=1) from exc

    typer.echo(f"Daemon: running on port {health.port}")
    typer.echo(f"  Uptime:   {health.uptime_seconds}s")
    typer.echo(f"  Sessions: {health.active_sessions}")


if __name__ == "__main__":
    app()
