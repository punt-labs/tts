"""Tests for the unified logging owner (src/punt_vox/logging_config.py)."""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from punt_vox import logging_config, paths
from punt_vox.config import ConfigStore
from punt_vox.log_append_handler import AppendLogHandler
from punt_vox.voxd.audit_log_level import AuditFloorLevel

if TYPE_CHECKING:
    from collections.abc import Iterator


def _redirect_log_tree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[None]:
    """Redirect the log tree to tmp and restore the root logger afterward."""
    log_dir = tmp_path / "logs"
    monkeypatch.setattr(logging_config, "_LOG_DIR", log_dir)
    monkeypatch.setattr(logging_config, "_LOG_FILE", log_dir / "vox.log")
    # Point the state root at tmp so the sink's health relativizes in-jail
    # (``logs/vox.log``) instead of falling back to the bare name out of jail.
    monkeypatch.setattr(paths, "user_state_dir", lambda: tmp_path)
    # Default: level resolves to the quiet INFO. Individual tests override.
    _pin_level(monkeypatch, "info")
    root = logging.getLogger()
    saved_handlers = root.handlers[:]
    saved_level = root.level
    yield
    for handler in root.handlers[:]:
        handler.close()
    root.handlers[:] = saved_handlers
    root.setLevel(saved_level)


def _pin_level(monkeypatch: pytest.MonkeyPatch, level: str) -> None:
    """Pin the resolved log level so a test's env/config can't leak in.

    ``resolve_log_level`` is a classmethod, so the replacement must be one too --
    a zero-arg lambda takes ``cls`` as its first positional and raises on an
    instance-bound call.
    """
    monkeypatch.setattr(
        ConfigStore, "resolve_log_level", classmethod(lambda _cls: level)
    )


def _append_handlers(root: logging.Logger) -> list[AppendLogHandler]:
    return [h for h in root.handlers if isinstance(h, AppendLogHandler)]


def _no_stream_handler(root: logging.Logger) -> bool:
    """Return whether no bare stderr StreamHandler is installed (DES-046)."""
    return not any(
        isinstance(h, logging.StreamHandler) and not isinstance(h, logging.FileHandler)
        for h in root.handlers
    )


class TestConfigureDaemonLogging:
    """The daemon appends to one private vox.log -- no stderr handler."""

    @pytest.fixture(autouse=True)
    def _isolate(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> Iterator[None]:
        yield from _redirect_log_tree(tmp_path, monkeypatch)

    def test_installs_one_append_handler_no_stderr(self, tmp_path: Path) -> None:
        logging_config.configure_daemon_logging()
        root = logging.getLogger()
        assert len(_append_handlers(root)) == 1
        assert _no_stream_handler(root)  # no stderr double-write (DES-046)

    def test_daemon_record_lands_in_vox_log_without_client_prefix(
        self, tmp_path: Path
    ) -> None:
        logging_config.configure_daemon_logging()
        logging.getLogger("punt_vox.voxd.router").info("daemon up")
        contents = (tmp_path / "logs" / "vox.log").read_text(encoding="utf-8")
        assert "punt_vox.voxd.router: daemon up" in contents
        assert "client." not in contents  # the daemon carries no client prefix

    def test_untightenable_file_warned_durably_in_vox_log(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A file it cannot force to 0600 becomes one WARNING in the live vox.log.

        The note is emitted after ``dictConfig``, so it lands in the now-open log
        (a debug line before configuration could never land) and never re-enters
        logging via the sink's per-write tighten.
        """
        log = tmp_path / "logs" / "vox.log"
        log.parent.mkdir(parents=True)
        log.write_text("existing\n")

        def _deny_fchmod(_fd: int, _mode: int) -> None:
            raise PermissionError("cannot fchmod")

        monkeypatch.setattr(os, "fchmod", _deny_fchmod)
        logging_config.configure_daemon_logging()
        logging.getLogger("punt_vox.voxd").info("up")
        contents = log.read_text(encoding="utf-8")
        assert "could not enforce private permissions on log path(s)" in contents

    def test_untightenable_dir_warned_durably_in_vox_log(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A logs DIR it cannot force to 0700 also lands in the durable WARNING.

        The dir-tighten runs before dictConfig, so its failure is collected (not
        debug-logged where it could never land) and folded into the same
        post-dictConfig WARNING as the file sweep.
        """
        (tmp_path / "logs").mkdir()
        real_chmod = Path.chmod

        def _deny_dir_chmod(
            self: Path, mode: int, *args: object, **kwargs: object
        ) -> None:
            if self.is_dir():
                raise PermissionError("cannot chmod dir")
            real_chmod(self, mode, *args, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(Path, "chmod", _deny_dir_chmod)
        logging_config.configure_daemon_logging()
        logging.getLogger("punt_vox.voxd").info("up")
        contents = (tmp_path / "logs" / "vox.log").read_text(encoding="utf-8")
        assert "could not enforce private permissions on log path(s)" in contents
        assert "0o700" in contents  # the directory failure is named

    def test_symlink_log_path_refused_at_write_time(self, tmp_path: Path) -> None:
        """A symlinked vox.log is refused at write (O_NOFOLLOW) and reads unhealthy.

        The append sink never follows the link: the write goes to stderr, the
        target is untouched, and ``log_health`` reports ``degraded`` so a
        symlink-redirect attempt is queryable instead of falsely reporting healthy.
        """
        target = tmp_path / "target.txt"
        target.write_text("do not write here\n")
        log = tmp_path / "logs" / "vox.log"
        log.parent.mkdir(parents=True)
        log.symlink_to(target)
        logging_config.configure_daemon_logging()
        assert logging_config.log_health()["status"] == "degraded"  # symlink != healthy
        logging.getLogger("punt_vox.voxd").info("blocked")
        assert target.read_text() == "do not write here\n"  # link never followed


class TestConfigureClientLogging:
    """A client appends to the shared vox.log, stamped with its role."""

    @pytest.fixture(autouse=True)
    def _isolate(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> Iterator[None]:
        yield from _redirect_log_tree(tmp_path, monkeypatch)

    def test_installs_one_append_handler_no_stderr(self) -> None:
        logging_config.configure_client_logging(role="cli")
        root = logging.getLogger()
        assert len(_append_handlers(root)) == 1
        assert _no_stream_handler(root)

    def test_skip_hook_line_lands_in_vox_log_no_fallback(self, tmp_path: Path) -> None:
        """A hook that does no daemon work still lands its line in vox.log.

        No daemon is reachable in this test, yet the record appends locally -- and
        no ``vox-fallback.log`` is ever created.
        """
        logging_config.configure_client_logging(role="hook")
        logging.getLogger("punt_vox.hooks").info("stop hook skipped")
        vox_log = tmp_path / "logs" / "vox.log"
        assert "client.hook.punt_vox.hooks: stop hook skipped" in vox_log.read_text(
            encoding="utf-8"
        )
        assert not (tmp_path / "logs" / "vox-fallback.log").exists()

    def test_verbose_raises_root_to_debug(self) -> None:
        logging_config.configure_client_logging(role="cli", verbose=True)
        assert logging.getLogger().level == logging.DEBUG

    def test_default_level_is_info(self) -> None:
        logging_config.configure_client_logging(role="cli")
        assert logging.getLogger().level == logging.INFO

    def test_reapply_picks_up_a_level_change(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A long-lived client re-reads the level and flips root + handler live."""
        _pin_level(monkeypatch, "info")
        logging_config.configure_client_logging(role="mcp")
        root = logging.getLogger()
        assert root.level == logging.INFO

        _pin_level(monkeypatch, "debug")  # e.g. `vox log debug` on another process
        logging_config.reapply_client_log_level()

        assert root.level == logging.DEBUG
        assert all(h.level == logging.DEBUG for h in root.handlers)

    def test_config_log_level_debug_applies(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A resolved ``debug`` level raises the client root to DEBUG."""
        _pin_level(monkeypatch, "debug")
        logging_config.configure_client_logging(role="mcp")
        assert logging.getLogger().level == logging.DEBUG

    def test_client_config_tightens_loose_log_dir(self, tmp_path: Path) -> None:
        """A pre-existing 0755 logs/ is re-tightened to 0700 on configure."""
        log_dir = tmp_path / "logs"
        log_dir.mkdir()
        log_dir.chmod(0o755)
        logging_config.configure_client_logging(role="hook")
        assert (log_dir.stat().st_mode & 0o077) == 0

    def test_client_config_survives_untightenable_dir(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A dir the client cannot chmod never crashes configure -- best effort."""
        log_dir = tmp_path / "logs"
        log_dir.mkdir()

        def _deny(_self: object, _mode: int) -> None:
            raise PermissionError("cannot chmod")

        monkeypatch.setattr("punt_vox.private_state.Path.chmod", _deny)
        logging_config.configure_client_logging(role="hook")  # no exception

    def test_mcp_framework_logger_suppressed(self) -> None:
        """The mcp framework request logger is pinned to WARNING (no INFO noise)."""
        logging_config.configure_client_logging(role="mcp")
        assert logging.getLogger("mcp.server.lowlevel").level == logging.WARNING
        assert logging.getLogger("mcp").level == logging.WARNING

    def test_httpx_loggers_suppressed(self) -> None:
        """Both httpx and httpx2 (openai>=3) are pinned to WARNING."""
        logging_config.configure_client_logging(role="mcp")
        assert logging.getLogger("httpx").level == logging.WARNING
        assert logging.getLogger("httpx2").level == logging.WARNING


class TestConfigureTurnTimerLogging:
    """vox call's turn-latency trace: always to vox.log, optionally to console too."""

    @pytest.fixture(autouse=True)
    def _isolate(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> Iterator[None]:
        yield from _redirect_log_tree(tmp_path, monkeypatch)
        # configure_turn_timer_logging mutates a specific named logger, not
        # just root -- the shared fixture above only saves/restores root, so
        # this logger needs its own save/restore or state leaks across tests.

    @pytest.fixture(autouse=True)
    def _isolate_turn_timer_logger(self) -> Iterator[None]:
        target = logging.getLogger(logging_config._TURN_TIMER_LOGGER_NAME)
        saved_handlers = target.handlers[:]
        saved_level = target.level
        saved_propagate = target.propagate
        yield
        for handler in target.handlers[:]:
            handler.close()
        target.handlers[:] = saved_handlers
        target.setLevel(saved_level)
        target.propagate = saved_propagate

    def test_debug_reaches_vox_log_without_verbose(self, tmp_path: Path) -> None:
        """The whole point: unconditional, not gated by echo_to_console."""
        logging_config.configure_turn_timer_logging(echo_to_console=False)
        logging.getLogger(logging_config._TURN_TIMER_LOGGER_NAME).debug(
            "turn_ended (+1.20s step, +3.45s turn)"
        )
        contents = (tmp_path / "logs" / "vox.log").read_text(encoding="utf-8")
        assert "turn_ended (+1.20s step, +3.45s turn)" in contents

    def test_echo_to_console_false_attaches_no_stream_handler(self) -> None:
        logging_config.configure_turn_timer_logging(echo_to_console=False)
        target = logging.getLogger(logging_config._TURN_TIMER_LOGGER_NAME)
        assert not any(
            isinstance(h, logging.StreamHandler) and not isinstance(h, AppendLogHandler)
            for h in target.handlers
        )

    def test_echo_to_console_true_attaches_a_stream_handler(self) -> None:
        logging_config.configure_turn_timer_logging(echo_to_console=True)
        target = logging.getLogger(logging_config._TURN_TIMER_LOGGER_NAME)
        assert any(
            isinstance(h, logging.StreamHandler) and not isinstance(h, AppendLogHandler)
            for h in target.handlers
        )

    def test_does_not_propagate_to_root(self) -> None:
        """Other modules' DEBUG noise must not become durable for the call's
        duration just because this logger was forced to DEBUG."""
        logging_config.configure_turn_timer_logging(echo_to_console=False)
        target = logging.getLogger(logging_config._TURN_TIMER_LOGGER_NAME)
        assert target.propagate is False

    def test_repeated_calls_do_not_accumulate_handlers(self) -> None:
        logging_config.configure_turn_timer_logging(echo_to_console=True)
        logging_config.configure_turn_timer_logging(echo_to_console=True)
        target = logging.getLogger(logging_config._TURN_TIMER_LOGGER_NAME)
        assert len(target.handlers) == 2  # one append + one console, not four


class TestLogHealth:
    """The unified sink's writability is client-observable (like vibe-trace)."""

    @pytest.fixture(autouse=True)
    def _isolate(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> Iterator[None]:
        yield from _redirect_log_tree(tmp_path, monkeypatch)

    def test_reports_relative_name_and_verdict(self, tmp_path: Path) -> None:
        _ = tmp_path  # in-jail via the autouse fixture's state-root patch
        health = logging_config.log_health()
        assert health["name"] == "logs/vox.log"  # relative, no absolute prefix
        assert not health["name"].startswith("/")
        assert health["status"] == "healthy"


class TestApplyLogLevel:
    """apply_log_level re-points the root logger + handlers to a concrete level."""

    def test_notset_clamped_level_still_emits_info(self) -> None:
        """A `notset` request, clamped to DEBUG, leaves a NAMED logger emitting INFO+.

        ``apply_log_level`` targets the root logger; a child logger defers to it,
        so after applying the ``AuditFloorLevel``-clamped ``notset`` (DEBUG) the
        audit trail (INFO and above) still emits -- the effective level is a
        concrete DEBUG, never NOTSET(0)/defer that could sit above INFO.
        """
        root = logging.getLogger()
        saved_level = root.level
        try:
            effective = AuditFloorLevel.from_name("notset").numeric
            logging_config.apply_log_level(effective)
            assert root.level == logging.DEBUG  # concrete, never NOTSET(0)
            assert logging.getLogger("voxd.audit.child").isEnabledFor(logging.INFO)
        finally:
            root.setLevel(saved_level)


class TestShipTransportIsGone:
    """The ship transport and fallback file no longer exist (forward integration)."""

    @pytest.mark.parametrize(
        "module",
        ["log_ship", "log_flush", "log_wire", "voxd.log_sink"],
    )
    def test_deleted_modules_do_not_import(self, module: str) -> None:
        import importlib

        with pytest.raises(ModuleNotFoundError):
            importlib.import_module(f"punt_vox.{module}")
