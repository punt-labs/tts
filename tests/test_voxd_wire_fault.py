"""Tests for punt_vox.voxd.wire_fault -- the prefix-free fault message policy.

``SafeFault`` splits a server-side fault into a wire message that never carries
an absolute prefix and a raw log detail that keeps the full text for the operator.
These tests pin: an in-jail OSError relativizes to ``"path: reason"``; an
out-of-jail, filename-less, or non-OSError fault falls back to the generic
``"operation failed"``; the log detail always retains the raw ``str(exc)``; and no
wire message ever leaks an absolute prefix or an ``Errno``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from punt_vox import dirs, paths
from punt_vox.voxd.wire_fault import SafeFault


@pytest.fixture
def state_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the state data root at an isolated, resolved tmp directory."""
    state = (tmp_path / "state").resolve()
    output = (tmp_path / "output").resolve()
    state.mkdir()
    output.mkdir()
    monkeypatch.setattr(paths, "user_state_dir", lambda: state)
    monkeypatch.setattr(dirs, "default_output_dir", lambda: output)
    return state


def _perm_error(path: Path) -> OSError:
    """A realistic EACCES ``OSError`` naming *path* -- errno, strerror, filename."""
    return OSError(13, "Permission denied", str(path))


class TestFromExceptionInJail:
    """An in-jail OSError crosses as its relativized path plus the cause."""

    def test_relativizes_path_and_lowercases_reason(self, state_root: Path) -> None:
        exc = _perm_error(state_root / "recordings" / "foo.mp3")
        fault = SafeFault.from_exception(exc)
        assert fault.wire_message == "recordings/foo.mp3: permission denied"

    def test_log_detail_keeps_the_raw_absolute_path(self, state_root: Path) -> None:
        exc = _perm_error(state_root / "recordings" / "foo.mp3")
        fault = SafeFault.from_exception(exc)
        # The operator's host-local log keeps the full detail the wire must not.
        assert str(state_root) in fault.log_detail
        assert "Errno 13" in fault.log_detail


class TestFromExceptionGeneric:
    """Anything not an in-jail OSError falls back to the generic verdict."""

    def test_out_of_jail_oserror_is_generic(self, state_root: Path) -> None:
        exc = _perm_error(Path("/etc/passwd"))
        fault = SafeFault.from_exception(exc)
        assert fault.wire_message == "operation failed"
        # ...but the raw path is still available in the log.
        assert "/etc/passwd" in fault.log_detail

    def test_oserror_without_filename_is_generic(self, state_root: Path) -> None:
        fault = SafeFault.from_exception(OSError("file changed mid-fetch"))
        assert fault.wire_message == "operation failed"

    def test_oserror_without_strerror_is_generic(self, state_root: Path) -> None:
        # A synthetic OSError(msg) has filename=None and strerror=None.
        fault = SafeFault.from_exception(OSError("boom"))
        assert fault.wire_message == "operation failed"

    def test_non_oserror_is_generic(self, state_root: Path) -> None:
        fault = SafeFault.from_exception(RuntimeError("provider 500"))
        assert fault.wire_message == "operation failed"
        assert "provider 500" in fault.log_detail


class TestOpaque:
    """An opaque fault sends the generic verdict and logs its detail alone."""

    def test_wire_is_generic_and_detail_is_logged(self) -> None:
        fault = SafeFault.opaque("play_directly failed with rc=3")
        assert fault.wire_message == "operation failed"
        assert fault.log_detail == "play_directly failed with rc=3"


class TestNoPrefixLeak:
    """No wire message ever carries an absolute prefix or an Errno."""

    @pytest.mark.parametrize(
        "exc",
        [
            OSError(13, "Permission denied", "/Users/someone/.punt-labs/vox/x.mp3"),
            OSError(2, "No such file or directory", "/Users/someone/secret"),
            RuntimeError("/Users/someone/leaked/path in a message"),
        ],
    )
    def test_wire_message_has_no_absolute_prefix(
        self, state_root: Path, exc: BaseException
    ) -> None:
        wire = SafeFault.from_exception(exc).wire_message
        assert "/Users/" not in wire
        assert not wire.startswith("/")
        assert "Errno" not in wire


class TestResolveFailureDoesNotEscape:
    """A filename whose resolve() raises yields the generic verdict, never a crash.

    SafeFault runs on the fault path, so a symlink-loop filename must not raise
    while a fault is being built (that would fault the fault handler).
    """

    def test_unresolvable_filename_is_generic_and_does_not_raise(
        self, state_root: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def boom(_self: Path, *_args: object, **_kwargs: object) -> Path:
            raise OSError(40, "Too many levels of symbolic links")  # ELOOP

        monkeypatch.setattr(Path, "resolve", boom)
        exc = OSError(40, "Too many levels of symbolic links", "/some/looping/path.mp3")
        # Building the fault must not propagate the ELOOP from relativize.
        fault = SafeFault.from_exception(exc)
        assert fault.wire_message == "operation failed"
