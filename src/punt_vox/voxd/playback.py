"""Playback queue and audio player for voxd."""
# pyright: reportPrivateUsage=false
# Internal module within the voxd package -- cross-module private access is expected.

from __future__ import annotations

import asyncio
import contextlib
import logging
import math
import os
import platform
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Self

from punt_vox.voxd.data_root_boundary import relativize_to_data_root
from punt_vox.voxd.wire_text import SafeText

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Audio environment variables we capture for every playback. These determine
# whether ffplay can reach PulseAudio/PipeWire and dbus at the moment of the
# call, which is exactly the failure mode we saw on Linux in v4.0.3.
_AUDIO_ENV_KEYS: tuple[str, ...] = (
    "XDG_RUNTIME_DIR",
    "PULSE_SERVER",
    "DBUS_SESSION_BUS_ADDRESS",
    "DISPLAY",
    "WAYLAND_DISPLAY",
    "HOME",
    "USER",
)

# Playback under 50ms is a "success" that almost certainly played nothing.
_SUSPICIOUS_ELAPSED_S = 0.05

_PLAYBACK_TIMEOUT_DEFAULT_S = 120.0
_PLAYBACK_TIMEOUT_PADDING_S = 10.0
_PROBE_TIMEOUT_S = 5.0

# Cap on the stderr blob we keep per playback. ffplay without -loglevel
# quiet can emit kilobytes of progress lines; we want enough for triage
# without unbounded growth in memory or log files.
_MAX_STDERR_LEN = 2000


# ---------------------------------------------------------------------------
# PlaybackResult / PlaybackItem dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PlaybackResult:
    """Outcome of a single audio playback."""

    path: Path
    rc: int
    elapsed_s: float
    stderr: str
    ts: float

    @property
    def ok(self) -> bool:
        """True when the player exited cleanly and actually played something.

        A clean exit that lasted under ``_SUSPICIOUS_ELAPSED_S`` almost
        certainly played nothing (missing audio backend, silently dropped
        output), so it is treated as a failure for the user-facing ``play``
        command -- the same signal ``_log_result`` warns on.
        """
        return self.rc == 0 and self.elapsed_s >= _SUSPICIOUS_ELAPSED_S

    def failure_detail(self) -> str:
        """Raw one-line failure reason (absolute paths and all) for the log."""
        if self.rc != 0:
            base = f"player exited rc={self.rc}"
        else:
            base = f"played nothing (elapsed {self.elapsed_s:.3f}s)"
        return f"{base}: {self.stderr}" if self.stderr else base

    def wire_failure_detail(self) -> str:
        """Prefix-free failure reason for the client frame.

        The raw :meth:`failure_detail` with its host paths stripped: the rc and
        elapsed verdict carry no path, so they pass through, while an in-jail
        stderr token relativizes and an out-of-jail one strips via
        :class:`SafeText`. The client learns why playback failed without seeing
        an absolute prefix; the raw text stays in the host-local log.
        """
        return SafeText.of(self.failure_detail()).text

    def to_health_dict(self) -> dict[str, object]:
        """Serialize for the authenticated health payload with no host paths.

        ``file`` is an in-jail recording or album path, so it crosses relativized
        to its data root (``recordings/foo.mp3``) -- or ``None`` if it resolves
        out of jail. ``stderr`` echoes absolute player paths, so its in-jail
        tokens relativize and its out-of-jail ones strip via :class:`SafeText`.
        ``rc``, ``elapsed_s``, and ``ts`` are service verdicts and cross as-is.
        """
        rel = relativize_to_data_root(self.path)
        return {
            "file": None if rel is None else str(rel.path),
            "rc": self.rc,
            "elapsed_s": self.elapsed_s,
            "stderr": SafeText.of(self.stderr).text,
            "ts": self.ts,
        }


@dataclass(frozen=True, slots=True)
class PlaybackItem:
    """An item in the playback queue.

    ``outcome``, when set, is resolved by the consumer with this item's
    :class:`PlaybackResult` the moment its playback finishes -- letting a
    user-invoked ``play`` await the real host-side result rather than returning
    at enqueue. Fire-and-forget callers (say/chime/music) leave it ``None``.
    """

    path: Path
    request_id: str
    notify: asyncio.Event
    outcome: asyncio.Future[PlaybackResult] | None = None


# ---------------------------------------------------------------------------
# Pure functions
# ---------------------------------------------------------------------------


def _truncate_stderr(text: str) -> str:
    """Return ``text`` clipped to ``_MAX_STDERR_LEN`` with head + tail kept."""
    if len(text) <= _MAX_STDERR_LEN:
        return text
    half = _MAX_STDERR_LEN // 2
    dropped = len(text) - _MAX_STDERR_LEN
    return f"{text[:half]}\n... [truncated {dropped} bytes] ...\n{text[-half:]}"


def _monotonic() -> float:
    """Indirection for ``time.monotonic`` so tests can stub playback timing.

    Patching ``time.monotonic`` directly would also affect asyncio internals.
    """
    return time.monotonic()


def _snapshot_env(keys: tuple[str, ...]) -> dict[str, str]:
    """Return a dict of env var values, using <unset> for missing keys."""
    return {k: os.environ.get(k, "<unset>") for k in keys}


def _is_darwin() -> bool:
    """Return True on macOS.

    Wrapped in a function so mypy doesn't narrow ``sys.platform`` to a
    single value at the call site, which would mark the non-matching
    branch as unreachable for cross-platform development.
    """
    return platform.system() == "Darwin"


def _player_command(path: Path) -> list[str]:
    """Return the argv for playing ``path`` on this platform.

    No ``-loglevel quiet`` on ffplay -- we want its stream summary and errors.
    """
    if _is_darwin():
        return ["afplay", str(path)]
    return ["ffplay", "-nodisp", "-autoexit", str(path)]


async def _probe_duration(path: Path) -> float | None:
    """Return the duration in seconds of an audio file, or None on failure.

    Uses ffprobe with a 5-second timeout. Returns None if ffprobe is not
    installed, the file is unreadable, or the output is not a valid float.
    """
    cmd = [
        "ffprobe",
        "-v",
        "quiet",
        "-show_entries",
        "format=duration",
        "-of",
        "csv=p=0",
        str(path),
    ]
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
    except (FileNotFoundError, OSError):
        return None
    try:
        stdout_bytes, _ = await asyncio.wait_for(
            proc.communicate(), timeout=_PROBE_TIMEOUT_S
        )
    except TimeoutError:
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
        with contextlib.suppress(Exception):
            await proc.wait()
        return None
    try:
        duration = float((stdout_bytes or b"").strip())
    except ValueError:
        return None
    logger.debug("Probed duration for %s: %.3fs", path.name, duration)
    return duration


# ---------------------------------------------------------------------------
# PlaybackQueue class
# ---------------------------------------------------------------------------


class PlaybackQueue:
    """Own the playback queue, mutex, and last-result state for voxd."""

    __slots__ = ("_last_result", "_mutex", "_queue")

    _last_result: PlaybackResult | None
    _mutex: asyncio.Lock
    _queue: asyncio.Queue[PlaybackItem]

    def __new__(cls) -> Self:
        self = super().__new__(cls)
        self._queue = asyncio.Queue()
        self._mutex = asyncio.Lock()
        self._last_result = None
        return self

    # -- Properties ----------------------------------------------------------

    @property
    def last_result(self) -> PlaybackResult | None:
        """Return the most recent playback result, or None."""
        return self._last_result

    @property
    def queue_size(self) -> int:
        """Return the number of items waiting in the queue."""
        return self._queue.qsize()

    @property
    def mutex(self) -> asyncio.Lock:
        """Return the playback mutex for external serialization."""
        return self._mutex

    # -- Public methods ------------------------------------------------------

    async def enqueue(self, item: PlaybackItem) -> None:
        """Add an item to the playback queue."""
        await self._queue.put(item)

    def set_last_result(self, value: PlaybackResult | None) -> None:
        """Set the last playback result. Used by delegation and tests."""
        self._last_result = value

    async def play_audio(self, path: Path) -> None:
        """Play an audio file and record a rich result in ``last_result``.

        Coordinates file validation, timeout computation, subprocess lifecycle,
        and result logging. Each concern is handled by a focused private method.
        """
        cmd = _player_command(path)
        env_snapshot = _snapshot_env(_AUDIO_ENV_KEYS)

        size = self._validate_file(path)
        if size is None:
            return

        timeout = await self._compute_timeout(path)

        logger.debug(
            "Playback spawn: cmd=%s size=%d audio_env=%s timeout=%.1fs",
            cmd,
            size,
            env_snapshot,
            timeout,
        )

        result = await self._spawn_and_wait(cmd, timeout, env_snapshot, path)
        if result is None:
            return

        rc, elapsed, stderr_text = result
        recorded = self._record_result(
            path=path, rc=rc, elapsed=elapsed, stderr=stderr_text
        )
        self._log_result(recorded, size, cmd, env_snapshot)

    def _validate_file(self, path: Path) -> int | None:
        """Return file size in bytes, or None after recording an error."""
        try:
            size = path.stat().st_size
        except OSError as exc:
            logger.error("Playback aborted: cannot stat %s: %s", path, exc)
            self._record_result(
                path=path, rc=-1, elapsed=0.0, stderr=f"stat failed: {exc}"
            )
            return None

        if size == 0:
            logger.error(
                "Playback aborted: 0-byte audio file %s -- synthesis bug upstream",
                path,
            )
            self._record_result(path=path, rc=-1, elapsed=0.0, stderr="0-byte file")
            return None

        return size

    async def _compute_timeout(self, path: Path) -> float:
        """Probe audio duration and return a padded timeout in seconds."""
        duration = await _probe_duration(path)
        if duration is not None and math.isfinite(duration) and duration > 0:
            return max(
                duration + _PLAYBACK_TIMEOUT_PADDING_S,
                _PLAYBACK_TIMEOUT_DEFAULT_S,
            )
        return _PLAYBACK_TIMEOUT_DEFAULT_S

    async def _spawn_and_wait(
        self,
        cmd: list[str],
        timeout: float,
        env_snapshot: dict[str, str],
        path: Path,
    ) -> tuple[int, float, str] | None:
        """Spawn the player subprocess and wait for completion.

        Return ``(rc, elapsed, stderr)`` on normal completion, or ``None``
        after recording an error result for spawn/timeout failures.
        """
        start = _monotonic()
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=True,
            )
        except FileNotFoundError as exc:
            elapsed = _monotonic() - start
            logger.error(
                "Playback FAILED: binary not found: %s (%s) cmd=%s audio_env=%s",
                cmd[0],
                exc,
                cmd,
                env_snapshot,
            )
            self._record_result(
                path=path,
                rc=-1,
                elapsed=elapsed,
                stderr=f"FileNotFoundError: {exc}",
            )
            return None
        except OSError as exc:
            elapsed = _monotonic() - start
            logger.error(
                "Playback FAILED: OSError spawning %s: %s audio_env=%s",
                cmd[0],
                exc,
                env_snapshot,
            )
            self._record_result(
                path=path, rc=-1, elapsed=elapsed, stderr=f"OSError: {exc}"
            )
            return None

        try:
            _, stderr_bytes = await asyncio.wait_for(
                proc.communicate(), timeout=timeout
            )
        except TimeoutError:
            elapsed = _monotonic() - start
            logger.error(
                "Playback FAILED: timed out after %.1fs for %s audio_env=%s",
                timeout,
                path.name,
                env_snapshot,
            )
            # The player can exit between the timeout firing and the kill, so a
            # ProcessLookupError here is benign -- suppress it to keep
            # play_audio's never-raise contract.
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
            with contextlib.suppress(Exception):
                await proc.wait()
            self._record_result(
                path=path,
                rc=-1,
                elapsed=elapsed,
                stderr=f"timeout after {timeout:.1f}s",
            )
            return None

        elapsed = _monotonic() - start
        rc = proc.returncode if proc.returncode is not None else -1
        raw_stderr = (stderr_bytes or b"").decode("utf-8", errors="replace").strip()
        return rc, elapsed, _truncate_stderr(raw_stderr)

    def _log_result(
        self,
        result: PlaybackResult,
        size: int,
        cmd: list[str],
        env_snapshot: dict[str, str],
    ) -> None:
        """Interpret a playback *result* and log at the appropriate level.

        Takes the recorded :class:`PlaybackResult` -- which already bundles the
        file, exit code, elapsed time, and stderr -- plus the spawn context
        (``size``, ``cmd``, ``env_snapshot``) the result does not carry, so the
        log line reproduces the exact invocation for triage.
        """
        name = result.path.name
        if result.rc != 0:
            logger.error(
                "Playback FAILED: rc=%d elapsed=%.3fs file=%s size=%d "
                "cmd=%s audio_env=%s stderr=%r",
                result.rc,
                result.elapsed_s,
                name,
                size,
                cmd,
                env_snapshot,
                result.stderr,
            )
            return

        if result.elapsed_s < _SUSPICIOUS_ELAPSED_S:
            logger.warning(
                "Playback SUSPICIOUS: rc=0 but elapsed=%.4fs (<%.2fs) file=%s "
                "size=%d audio_env=%s stderr=%r -- probably played nothing",
                result.elapsed_s,
                _SUSPICIOUS_ELAPSED_S,
                name,
                size,
                env_snapshot,
                result.stderr,
            )
            return

        logger.debug(
            "Playback ok: elapsed=%.3fs file=%s size=%d stderr=%r",
            result.elapsed_s,
            name,
            size,
            result.stderr,
        )

    async def consumer(self) -> None:
        """Single consumer: play audio sequentially from the queue.

        Holds ``self._mutex`` for the duration of each item so the
        direct-play path can't produce overlapping audio from another
        coroutine.
        """
        while True:
            item = await self._queue.get()
            logger.debug("Playback start: %s", item.path.name)
            # One bad item must never leave the outcome future or notify unset:
            # that would hang the awaiting play client for its full deadline and,
            # because this consumer is an unsupervised task, kill it -- wedging
            # every later play AND synthesize while the daemon still looks up.
            # play_audio is written never to raise; the try/finally is the
            # backstop for the case it ever does.
            try:
                async with self._mutex:
                    await self.play_audio(item.path)
                logger.debug("Playback done: %s", item.path.name)
                # Resolve the outcome from the result play_audio just recorded,
                # here -- synchronously, before the next item is dequeued -- so
                # an awaiting play handler sees its own item's result, never a
                # later item's (the queue is single-consumer and serialized).
                self._resolve_outcome(item)
            except Exception:
                logger.exception("Playback consumer error for %s", item.path.name)
                self._fail_outcome(item, "playback failed unexpectedly")
            finally:
                # Catch-all in the finally so cancellation (daemon shutdown) or
                # any non-Exception BaseException still resolves the outcome --
                # PlayHandler awaits that future with no timeout, so a pending
                # future would hang the request forever and leak the task. A
                # no-op when the success/error path already resolved it (guards
                # done()).
                self._fail_outcome(item, "playback interrupted (daemon shutting down)")
                item.notify.set()
                self._queue.task_done()

    def _resolve_outcome(self, item: PlaybackItem) -> None:
        """Complete *item*'s outcome future, if any, with the recorded result.

        ``play_audio`` always records a result (success, spawn failure, timeout,
        or bad file), so ``_last_result`` is set here; the ``None`` guard is
        defensive so a missing result surfaces as a failure rather than a hang.
        """
        future = item.outcome
        if future is None or future.done():
            return
        result = self._last_result
        if result is None:
            result = self._failure_result(item.path, "no playback result recorded")
        future.set_result(result)

    def _fail_outcome(self, item: PlaybackItem, reason: str) -> None:
        """Resolve *item*'s outcome with a failure result after a consumer crash."""
        future = item.outcome
        if future is None or future.done():
            return
        future.set_result(self._failure_result(item.path, reason))

    @staticmethod
    def _failure_result(path: Path, reason: str) -> PlaybackResult:
        """Build a failing :class:`PlaybackResult` (rc=-1) carrying *reason*."""
        return PlaybackResult(
            path=path, rc=-1, elapsed_s=0.0, stderr=reason, ts=time.time()
        )

    # -- Private helpers -----------------------------------------------------

    def _record_result(
        self,
        *,
        path: Path,
        rc: int,
        elapsed: float,
        stderr: str,
    ) -> PlaybackResult:
        """Store and return a freshly-observed playback result as ``last_result``."""
        self._last_result = PlaybackResult(
            path=path,
            rc=rc,
            elapsed_s=round(elapsed, 4),
            stderr=stderr,
            ts=time.time(),
        )
        return self._last_result
