"""The single logging owner: one append handler over one vox.log for every process.

The daemon and every client (MCP server, hook, CLI, detached playback) install
the same :class:`AppendLogHandler` pointed at one ``vox.log``. Each process writes
its own records there by a local ``O_APPEND`` append -- no ship transport, no
fallback file, no daemon round-trip on a hook's logging hot path (DES-017). A
client stamps ``client.<role>.`` onto its logger name so its lines grep apart from
the daemon's; the multi-writer-safe :class:`AtomicAppendLog` behind the handler
owns rotation and 0600, so the two configs can never drift into two rotation
mechanisms racing on the file.

Both entry points first tighten ``~/.punt-labs/vox`` and ``…/logs`` to 0700 via
:class:`PrivateState`, so a client that never ran ``ensure_user_dirs`` still
re-tightens a pre-existing loose directory. Client tightening is best-effort
(never crash a hook); the daemon additionally fail-closes in ``main`` before this.

The active level follows the ``log_level`` config key (default INFO): a client
re-reads it each run, so ``/vox log debug`` applies immediately; the daemon reads
it at startup, so a running daemon picks up a change on ``vox daemon restart``.
"""

from __future__ import annotations

import contextlib
import logging
import logging.config

from punt_vox.append_log import AtomicAppendLog, SinkHealth
from punt_vox.config import ConfigStore
from punt_vox.log_format import Role
from punt_vox.paths import log_dir as _paths_log_dir
from punt_vox.private_state import PrivateState

__all__ = [
    "Role",
    "apply_log_level",
    "configure_client_logging",
    "configure_daemon_logging",
    "log_health",
    "reapply_client_log_level",
]

logger = logging.getLogger(__name__)

_LOG_DIR = _paths_log_dir()
_LOG_FILE = _LOG_DIR / "vox.log"

_APPEND_FACTORY = "punt_vox.log_append_handler.AppendLogHandler.for_file"

# Third-party and mcp-framework loggers pinned to WARNING so vox owns the INFO
# surface -- notably the 38x "Processing request of type CallToolRequest" noise.
_SUPPRESSED: tuple[str, ...] = (
    "boto3",
    "botocore",
    "urllib3",
    "s3transfer",
    "httpx",
    "websockets",
    "mcp",
    "mcp.server",
    "mcp.server.lowlevel",
)


def configure_daemon_logging() -> None:
    """Configure the daemon to append its records to the shared 0600 ``vox.log``.

    One append handler, no stderr handler: a parallel ``StreamHandler`` would have
    the service manager capture a second, unprotected copy of every record. The
    ``AtomicAppendLog`` behind the handler re-tightens the file to 0600 on every
    write. Re-tightens the log tree first (fail-closed on a tree it cannot create);
    any directory it could not force to 0700 or file it could not force to 0600
    surfaces as one durable WARNING *after* the handlers exist, so the note lands in
    the now-live ``vox.log`` -- a debug line emitted before ``dictConfig`` could
    never land.
    """
    untightened = _tighten_daemon_tree()
    level = _resolve_level(verbose=False)
    logging.config.dictConfig(_config(name_prefix="", level=level))
    if untightened:
        loose = "; ".join(untightened)
        logger.warning(
            "could not enforce private permissions on log path(s): %s", loose
        )


def configure_client_logging(*, role: Role, verbose: bool = False) -> None:
    """Configure a client to append its records to the shared ``vox.log``.

    The root logger's one handler appends each record locally under the sink's
    ``flock`` -- no socket, no daemon dependency -- and stamps ``client.<role>.``
    onto the logger name. ``verbose`` (the CLI ``--verbose``) is a one-shot
    ``debug``; otherwise the level follows the ``log_level`` config key.
    """
    _tighten_client_tree()
    level = _resolve_level(verbose=verbose)
    logging.config.dictConfig(_config(name_prefix=f"client.{role}.", level=level))


def log_health() -> SinkHealth:
    """Return the unified ``vox.log`` sink's client-observable health.

    Surfaced through ``mic:status`` so an operator can query whether the one log
    every process appends to is itself writable -- the same treatment the
    ``vibe-trace`` sink already gets.
    """
    return AtomicAppendLog(_LOG_FILE).health()


def reapply_client_log_level() -> None:
    """Re-resolve ``log_level`` and apply it to the running client's handlers.

    A long-lived client (the MCP server) configures logging once at startup, so
    a later ``vox log debug`` would otherwise never take effect and ``mic:status``
    would claim a level the process is not using. Called on the per-tool path,
    this re-reads the effective level and sets the root logger and its handlers --
    a cheap level flip, not a full ``dictConfig`` rebuild -- so a change takes hold
    within a tool call or two and status reflects what the process actually does.
    """
    level = logging.getLevelNamesMapping().get(_resolve_level(verbose=False))
    if level is not None:
        apply_log_level(level)


def apply_log_level(numeric: int) -> None:
    """Set the root logger and every handler to *numeric* -- the live level switch.

    The one place a running process re-points its level: shared by the client's
    ``reapply_client_log_level`` and the daemon's ``set_log_level`` wire op, so a
    running daemon and a long-lived client flip the root logger and its handlers
    the same way rather than duplicating the walk.
    """
    root = logging.getLogger()
    root.setLevel(numeric)
    for handler in root.handlers:
        handler.setLevel(numeric)


def _config(*, name_prefix: str, level: str) -> dict[str, object]:
    """Return the dictConfig for one append handler on the root logger.

    Shared by both entry points so the daemon and client differ only in the
    logger-name prefix; the handler and suppression table are identical.
    """
    return {
        "version": 1,
        "disable_existing_loggers": False,
        "handlers": {
            "append": {
                "()": _APPEND_FACTORY,
                "filename": str(_LOG_FILE),
                "name_prefix": name_prefix,
                "level": level,
            },
        },
        "root": {"level": level, "handlers": ["append"]},
        "loggers": _suppression_table(),
    }


def _suppression_table() -> dict[str, dict[str, str]]:
    """Return the shared third-party + framework logger level overrides."""
    return {name: {"level": "WARNING"} for name in _SUPPRESSED}


def _resolve_level(*, verbose: bool) -> str:
    """Return the active level: DEBUG when verbose or configured, else INFO."""
    if verbose:
        return "DEBUG"
    return _config_level()


def _config_level() -> str:
    """Return the effective log level for the handler: DEBUG or INFO.

    Delegates to the shared resolver -- a repo-local override, else the global
    ``vox log`` setting, else INFO -- so a service-started daemon (which never
    runs from a repo) reads the same value ``vox log`` wrote. Reading config
    needs no logging configured, so this is safe to call before the handlers exist.
    """
    return ConfigStore.resolve_log_level().upper()


def _tighten_daemon_tree() -> list[str]:
    """Create the log dir tree and re-tighten vox.log + backups, collecting notes.

    Fails closed on a tree it cannot create (the ``mkdir`` ``OSError`` propagates).
    Both a *directory* it cannot force to 0700 and a *file* it cannot force to 0600
    are *collected* -- not logged here, since logging is not configured yet -- so
    the caller can WARN durably once the handlers exist. Directory failures come
    through a collecting reporter on the guard; the file sweep is the sink's own
    ``tighten_existing`` over the active log, every backup slot, and the rotate lock.
    """
    failures: list[str] = []
    PrivateState(_LOG_FILE, on_failure=failures.append).ensure_private_tree()
    failures.extend(str(path) for path in AtomicAppendLog(_LOG_FILE).tighten_existing())
    return failures


def _tighten_client_tree() -> None:
    """Best-effort tighten for a client; never crash a hook.

    Directory-tighten failures route to stderr (``report_to_stderr``), never to a
    debug logger that runs before ``dictConfig`` and so could never land. The
    append sink re-tightens the file to 0600 on every write, so a client's
    dir-tighten is defense-in-depth -- a ``mkdir`` failure is swallowed, not raised.
    """
    with contextlib.suppress(OSError):
        PrivateState(
            _LOG_FILE, on_failure=PrivateState.report_to_stderr
        ).ensure_private_tree()
