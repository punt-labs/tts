"""Hook dispatchers for Claude Code events.

Thin shell scripts read stdin and delegate to ``vox hook <event>``.
All business logic lives here as testable pure functions.

Audio playback goes through voxd via ``VoxClientSync``. Hooks never
do in-process synthesis, caching, or direct playback.

Events:
- **stop**: task-completion notification (decision-block pattern)
- **vibe-nudge**: cadence-gated reminder for the agent to set the auto vibe
- **notification**: permission/idle prompt audio alerts
- **pre-compact**: playful 'be right back' before context compaction
- **user-prompt-submit**: acknowledgment in continuous mode
- **subagent-start**: subagent spawn announcement in continuous mode
- **subagent-stop**: subagent completion announcement in continuous mode
- **session-end**: farewell speech
"""

from __future__ import annotations

import dataclasses
import json
import logging
import os
import random
import select
import sys
from pathlib import Path

import typer

from punt_vox.client_errors import VoxdConnectionError, VoxdProtocolError
from punt_vox.client_sync import VoxClientSync
from punt_vox.config import ConfigStore, VoxConfig
from punt_vox.dirs import find_config_dir, find_repo_root
from punt_vox.hook_envelope import HookEnvelope
from punt_vox.hook_payload import (
    NotificationPayload,
    StopPayload,
)
from punt_vox.nudge_hook import NudgeHook
from punt_vox.quips import (
    ACKNOWLEDGE_PHRASES,
    FAREWELL_PHRASES,
    IDLE_PHRASES,
    PERMISSION_PHRASES,
    PRE_COMPACT_PHRASES,
    STOP_PHRASES,
    SUBAGENT_START_PHRASES,
    SUBAGENT_STOP_PHRASES,
)
from punt_vox.session_spec import SessionSpec
from punt_vox.types_synthesis import SynthesisSpec
from punt_vox.types_synthesis_errors import (
    ModelNotAvailableError,
    ProviderNotConfiguredError,
)

logger = logging.getLogger(__name__)

hook_app = typer.Typer(
    help="Hook dispatchers (called by hook scripts).",
    no_args_is_help=True,
)


@hook_app.callback(invoke_without_command=True)
def _hook_callback(ctx: typer.Context) -> None:  # pyright: ignore[reportUnusedFunction]
    """Initialize logging for all hook subcommands."""
    if ctx.invoked_subcommand is not None:
        from punt_vox.logging_config import configure_client_logging

        configure_client_logging(role="hook")


# Shared helpers


def _warn_unexpected_read_error(exc: OSError) -> None:
    """Log a genuine stdin read failure; stay quiet on a non-fd stdin.

    A real empty or closed pipe returns ``b""`` and never raises, so any
    ``OSError`` carrying an errno is a genuine failure worth logging.
    ``errno`` is None only for a non-fd stdin (e.g. ``StringIO`` in tests).
    """
    if exc.errno is not None:
        logger.warning("hook stdin read failed: errno %s (%s)", exc.errno, exc)


def _read_hook_input() -> dict[str, object]:
    """Read JSON hook payload from stdin (non-blocking).

    Uses ``select`` + ``os.read`` to avoid blocking forever when
    Claude Code does not close the stdin pipe.
    """
    raw_bytes = b""
    try:
        fd = sys.stdin.fileno()
        if not select.select([fd], [], [], 0.1)[0]:
            return {}
        chunks: list[bytes] = []
        while True:
            chunk = os.read(fd, 65536)
            if not chunk:
                break
            chunks.append(chunk)
            if not select.select([fd], [], [], 0.05)[0]:
                break
        raw_bytes = b"".join(chunks)
        if not raw_bytes.strip():
            return {}
        data: object = json.loads(raw_bytes.decode())
    except OSError as exc:
        _warn_unexpected_read_error(exc)
        return {}
    except (json.JSONDecodeError, UnicodeDecodeError):
        # Byte count only, never the content -- an untrusted payload must not
        # forge a second log line or leak into the log.
        logger.warning(
            "hook: malformed JSON payload on stdin (%d bytes); treated as empty",
            len(raw_bytes),
        )
        return {}
    if not isinstance(data, dict):
        return {}
    return dict(data)  # pyright: ignore[reportUnknownArgumentType]


def _emit(output: dict[str, object]) -> None:
    """Write JSON response to stdout."""
    typer.echo(json.dumps(output))


# Repo name resolution from session cwd


def _repo_name_from_cwd(cwd: Path | None) -> str | None:
    """Derive repo name from the session's cwd via its git root."""
    if cwd is None:
        return None
    repo_root = find_repo_root(cwd)
    if repo_root is None:
        return None
    return repo_root.name or None


def _with_repo_name(config: VoxConfig, cwd: Path | None) -> VoxConfig:
    """Override config.repo_name from session cwd when config was inherited."""
    repo_name = _repo_name_from_cwd(cwd)
    if repo_name and repo_name != config.repo_name:
        return dataclasses.replace(config, repo_name=repo_name)
    return config


# Voxd client helpers


def _make_client() -> VoxClientSync:
    """Create a VoxClientSync for hook use."""
    return VoxClientSync()


def _speak_via_voxd(text: str, config: VoxConfig) -> None:
    """Synthesize and play a phrase via voxd.

    Builds the wire spec through :class:`SessionSpec` so state -- not a
    daemon-side probe -- is the sole authority on which provider voxd runs.
    A hook has no caller to answer, so a client-side refusal
    (:class:`ProviderNotConfiguredError` for empty provider state,
    :class:`ModelNotAvailableError` for a hand-edited provider/model pair)
    is logged as a WARNING in ``vox.log`` and no audio plays -- the F1
    contract in ``docs/provider-authority.md`` §4. ``VoxdConnectionError``
    and ``VoxdProtocolError`` are still caught so a missing daemon never
    crashes a hook either.
    """
    try:
        spec = SessionSpec(config).fill(SynthesisSpec(rate=90))
    except ProviderNotConfiguredError:
        logger.warning("hook speech skipped: no TTS provider configured for this repo")
        return
    except ModelNotAvailableError as exc:
        logger.warning("hook speech skipped: %s", exc)
        return
    try:
        _make_client().synthesize(text, spec)
    except (VoxdConnectionError, VoxdProtocolError):
        logger.warning("voxd not running, skipping speech")


def _chime_via_voxd(signal: str, *, wait: bool = True) -> None:
    """Play a chime via voxd.

    When *wait* is False, spawns a detached subprocess to avoid blocking
    the hook.  Used by sync hooks (Stop) that must return quickly.

    Catches ``VoxdConnectionError`` and ``VoxdProtocolError`` so a
    missing or misbehaving daemon never crashes a hook.
    """
    if not wait:
        # Fire-and-forget: spawn a detached process so the hook returns
        # immediately -- a side-effect hook must not block the event it observes.
        import subprocess as _sp

        try:
            _sp.Popen(
                [sys.executable, "-m", "punt_vox", "hook", "_chime", signal],
                stdout=_sp.DEVNULL,
                stderr=_sp.DEVNULL,
                start_new_session=True,
            )
        except OSError as e:
            logger.warning("Could not spawn chime subprocess: %s", e)
        return
    try:
        client = _make_client()
        client.chime(signal)
    except (VoxdConnectionError, VoxdProtocolError):
        logger.warning("voxd not running, skipping chime")


# Stop handler — decision-block pattern


def handle_stop(
    payload: StopPayload, config: VoxConfig, config_dir: Path
) -> dict[str, object] | None:
    """Decide whether to block Claude from stopping.

    Returns a decision-block dict if Claude should speak a summary, or None to
    let it stop normally.  The summary fires when notifications and speech are
    both enabled and this is not a re-entrant stop; the vibe the agent set
    during the session already colors the synthesized speech via config tags.
    """
    # Not enabled
    if config.notify == "n":
        logger.info("Stop hook: skip (notify=n)")
        return None

    # Already continuing from a previous Stop hook — prevent infinite loop
    if payload.stop_hook_active is True:
        logger.info("Stop hook: skip (stop_hook_active=True, preventing loop)")
        return None

    # Chime mode: fire-and-forget chime, let Claude stop immediately.
    # Must not block — the Stop hook is sync and Claude waits.
    if config.speak == "n":
        logger.info("Stop hook: chime mode, requesting done chime from voxd")
        _chime_via_voxd("done", wait=False)
        return None

    # Voice mode: block the stop, ask Claude to summarize and speak. The session
    # vibe tags are already in config; no data goes in the reason string.
    phrase = random.choice(STOP_PHRASES)
    if config.repo_name:
        phrase = f"{config.repo_name}. {phrase}"
    logger.info("Stop hook: blocking for voice summary")
    # config_dir is threaded for symmetry with the other stop-path handlers.
    _ = config_dir
    return {"decision": "block", "reason": phrase}


# UserPromptSubmit — cadence-gated auto-vibe reminder


def handle_vibe_nudge(config: VoxConfig, config_dir: Path) -> dict[str, object] | None:
    """Return a UserPromptSubmit additionalContext envelope, or None to stay silent.

    Thin adapter over :class:`NudgeHook`, which owns the cadence advance, the
    persist, and the ``[vibe-trace]`` nudge event. Non-blocking always -- this
    never emits a decision.
    """
    return NudgeHook(config_dir).run(config)


# Notification — permission/idle prompt audio alerts


def _pick_notification_phrase(
    notification_type: str,
    message: str,
    *,
    repo_name: str | None = None,
) -> str:
    """Pick a phrase for a notification type.

    When *repo_name* is set, prepends it with a period separator so
    the TTS engine inserts a natural pause.
    """
    if notification_type == "permission_prompt":
        text = random.choice(PERMISSION_PHRASES)
    elif notification_type == "idle_prompt":
        text = random.choice(IDLE_PHRASES)
    else:
        text = f"Notification: {message[:80]}"
    if repo_name:
        text = f"{repo_name}. {text}"
    return text


def handle_notification(payload: NotificationPayload, config: VoxConfig) -> None:
    """Handle permission/idle prompt notifications.

    In chime mode, plays a chime via voxd. In voice mode, synthesizes
    and plays a short spoken phrase via voxd.
    """
    # Not enabled
    if config.notify == "n":
        logger.info("Notification hook: skip (notify=n)")
        return

    logger.info("Notification hook: type=%s", payload.notification_type)

    # Chime mode
    if config.speak == "n":
        logger.info("Notification hook: chime mode, requesting prompt chime from voxd")
        _chime_via_voxd("prompt")
        return

    # Voice mode: synthesize via voxd
    text = _pick_notification_phrase(
        payload.notification_type, payload.message, repo_name=config.repo_name
    )
    _speak_via_voxd(text, config)


# Shared speech helper — used by continuous mode hooks


def _speak_phrase(
    phrases: tuple[str, ...] | list[str],
    config: VoxConfig,
    *,
    chime_signal: str = "done",
) -> None:
    """Pick a random phrase and speak or chime it via voxd.

    Common pattern for async continuous-mode hooks: check speak mode,
    play a chime or synthesize speech.  When ``config.repo_name`` is
    set, prepends it with a period separator so the TTS engine inserts
    a natural pause between the repo name and the phrase.
    """
    if config.speak == "n":
        _chime_via_voxd(chime_signal)
        return

    text = random.choice(phrases)
    if config.repo_name:
        text = f"{config.repo_name}. {text}"
    _speak_via_voxd(text, config)


# Continuous-mode announcements (pre-compact, prompt ack, subagent lifecycle)


@dataclasses.dataclass(frozen=True, slots=True)
class _ContinuousAnnouncement:
    """A continuous-mode spoken announcement: a phrase pool, chime, and label.

    The continuous-mode hooks differ only in these three values, so each is a
    constant instance and the shared guard lives in one place.
    """

    phrases: tuple[str, ...]
    chime_signal: str
    event: str

    def announce(self, config: VoxConfig) -> None:
        """Speak the announcement in continuous mode; a no-op otherwise."""
        if config.notify != "c":
            logger.info(
                "%s hook: skip (notify=%s, not continuous)", self.event, config.notify
            )
            return
        logger.info("%s hook: announcing", self.event)
        _speak_phrase(self.phrases, config, chime_signal=self.chime_signal)


_PRE_COMPACT = _ContinuousAnnouncement(PRE_COMPACT_PHRASES, "compact", "PreCompact")
_PROMPT_ACK = _ContinuousAnnouncement(
    ACKNOWLEDGE_PHRASES, "acknowledge", "UserPromptSubmit"
)
_SUBAGENT_START = _ContinuousAnnouncement(
    SUBAGENT_START_PHRASES, "subagent", "SubagentStart"
)
_SUBAGENT_STOP = _ContinuousAnnouncement(
    SUBAGENT_STOP_PHRASES, "subagent", "SubagentStop"
)


def handle_pre_compact(config: VoxConfig) -> None:
    """Play a playful 'be right back' before context compaction."""
    _PRE_COMPACT.announce(config)


def handle_user_prompt_submit(config: VoxConfig) -> None:
    """Speak a short acknowledgment when the user submits a prompt."""
    _PROMPT_ACK.announce(config)


def handle_subagent_start(config: VoxConfig) -> None:
    """Announce that a subagent is being spawned."""
    _SUBAGENT_START.announce(config)


def handle_subagent_stop(config: VoxConfig) -> None:
    """Announce that a subagent has completed."""
    _SUBAGENT_STOP.announce(config)


# SessionEnd — farewell speech


def _reset_nudge_cadence(config_dir: Path) -> None:
    """Reset the nudge cadence counter, warning instead of raising on failure."""
    try:
        ConfigStore(config_dir).write_field("vibe_nudge_turns", "0")
    except OSError as exc:
        logger.warning("session-end: cannot reset cadence in %s: %s", config_dir, exc)


def handle_session_end(config: VoxConfig, config_dir: Path) -> None:
    """Speak a farewell and clean up session state.

    Fires when notify != 'n' (both on-demand and continuous).
    Resets the nudge cadence so the counter never leaks into the next session.
    """
    if config.notify == "n":
        logger.info("SessionEnd hook: skip (notify=n)")
        return

    logger.info("SessionEnd hook: farewell")
    _speak_phrase(FAREWELL_PHRASES, config, chime_signal="farewell")

    # Clean slate for next session
    if config.vibe_nudge_turns:
        _reset_nudge_cadence(config_dir)


# CLI commands


def _resolve_continuous_config() -> tuple[VoxConfig, Path] | None:
    """Read stdin, resolve the session's repo config from its cwd.

    Returns ``(config, config_dir)`` or None when the session's cwd
    resolves to no ``.punt-labs/vox`` config — the hook stays silent.
    Shared by the continuous-mode commands that carry only a cwd.
    """
    cwd = HookEnvelope.parse(_read_hook_input()).cwd
    config_dir = find_config_dir(cwd)
    if config_dir is None:
        # DEBUG, not INFO: hooks are globally installed, so this fires on every
        # event in every non-vox repo -- noise at the default level, visible only
        # when the operator raises log_level to debug for wiring diagnosis.
        logger.debug("hook: no vox config for cwd; staying silent")
        return None
    return _with_repo_name(ConfigStore(config_dir).read(), cwd), config_dir


@hook_app.command("stop")
def stop_cmd() -> None:  # pyright: ignore[reportUnusedFunction]
    """Stop hook: task-completion notification."""
    payload = StopPayload.parse(_read_hook_input())
    config_dir = find_config_dir(payload.cwd)
    if config_dir is None:
        logger.debug("hook stop: no vox config for cwd; staying silent")
        return
    config = _with_repo_name(ConfigStore(config_dir).read(), payload.cwd)
    result = handle_stop(payload, config, config_dir)
    if result is not None:
        _emit(result)


@hook_app.command("vibe-nudge")
def vibe_nudge_cmd() -> None:  # pyright: ignore[reportUnusedFunction]
    """UserPromptSubmit hook: cadence-gated auto-vibe reminder."""
    resolved = _resolve_continuous_config()
    if resolved is None:
        return
    config, config_dir = resolved
    result = handle_vibe_nudge(config, config_dir)
    if result is not None:
        _emit(result)


@hook_app.command("notification")
def notification_cmd() -> None:  # pyright: ignore[reportUnusedFunction]
    """Notification hook: permission/idle prompt audio alerts."""
    payload = NotificationPayload.parse(_read_hook_input())
    config_dir = find_config_dir(payload.cwd)
    if config_dir is None:
        logger.debug("hook notification: no vox config for cwd; staying silent")
        return
    config = _with_repo_name(ConfigStore(config_dir).read(), payload.cwd)
    handle_notification(payload, config)


@hook_app.command("pre-compact")
def pre_compact_cmd() -> None:  # pyright: ignore[reportUnusedFunction]
    """PreCompact hook: playful 'be right back' message."""
    resolved = _resolve_continuous_config()
    if resolved is not None:
        handle_pre_compact(resolved[0])


@hook_app.command("user-prompt-submit")
def user_prompt_submit_cmd() -> None:  # pyright: ignore[reportUnusedFunction]
    """UserPromptSubmit hook: acknowledgment in continuous mode."""
    resolved = _resolve_continuous_config()
    if resolved is not None:
        handle_user_prompt_submit(resolved[0])


@hook_app.command("subagent-start")
def subagent_start_cmd() -> None:  # pyright: ignore[reportUnusedFunction]
    """SubagentStart hook: announce subagent spawn."""
    resolved = _resolve_continuous_config()
    if resolved is not None:
        handle_subagent_start(resolved[0])


@hook_app.command("subagent-stop")
def subagent_stop_cmd() -> None:  # pyright: ignore[reportUnusedFunction]
    """SubagentStop hook: announce subagent completion."""
    resolved = _resolve_continuous_config()
    if resolved is not None:
        handle_subagent_stop(resolved[0])


@hook_app.command("session-end")
def session_end_cmd() -> None:  # pyright: ignore[reportUnusedFunction]
    """SessionEnd hook: farewell speech."""
    resolved = _resolve_continuous_config()
    if resolved is None:
        return
    config, config_dir = resolved
    handle_session_end(config, config_dir)


@hook_app.command("_chime", hidden=True)
def chime_cmd(signal: str) -> None:  # pyright: ignore[reportUnusedFunction]
    """Internal: play a chime (used by fire-and-forget detached process)."""
    _chime_via_voxd(signal)
