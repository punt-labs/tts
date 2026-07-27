"""Tests for punt_vox.voxd.data_root_boundary -- the wire-boundary relativizer.

The daemon is conceptually chrooted to two data roots: the state dir
(``~/.punt-labs/vox``) and the output dir (``$VOX_OUTPUT_DIR`` or ``~/Music/vox``).
These tests pin the one helper every wire reply shares: an in-jail path crosses
relativized to its labeled root, a path under neither root returns ``None``, and
a symlink or ``..`` cannot make an out-of-jail path look contained.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from punt_vox import dirs, paths
from punt_vox.voxd.data_root_boundary import (
    DataRootRelative,
    relativize_to_data_root,
)


@pytest.fixture
def roots(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    """Point both data roots at isolated, resolved tmp directories.

    Returns ``(state, output)``. Both are ``.resolve()``d so a macOS
    ``/var`` -> ``/private/var`` symlink in ``tmp_path`` does not defeat the
    equality the helper relies on.
    """
    state = (tmp_path / "state").resolve()
    output = (tmp_path / "output").resolve()
    state.mkdir()
    output.mkdir()
    monkeypatch.setattr(paths, "user_state_dir", lambda: state)
    monkeypatch.setattr(dirs, "default_output_dir", lambda: output)
    return state, output


class TestInJail:
    """An in-jail path crosses relativized to its labeled root."""

    def test_state_path_is_relative_to_state(self, roots: tuple[Path, Path]) -> None:
        state, _output = roots
        candidate = state / "recordings" / "foo.mp3"
        rel = relativize_to_data_root(candidate)
        assert rel is not None
        assert rel.label == "state"
        assert rel.path == Path("recordings/foo.mp3")

    def test_output_path_is_relative_to_output(self, roots: tuple[Path, Path]) -> None:
        _state, output = roots
        candidate = output / "album-1" / "part-2.mp3"
        rel = relativize_to_data_root(candidate)
        assert rel is not None
        assert rel.label == "output"
        assert rel.path == Path("album-1/part-2.mp3")

    def test_relative_str_carries_no_absolute_prefix(
        self, roots: tuple[Path, Path]
    ) -> None:
        state, _output = roots
        rel = relativize_to_data_root(state / "cache" / "ab" / "cd.mp3")
        assert rel is not None
        assert not str(rel.path).startswith("/")
        assert str(rel.path) == "cache/ab/cd.mp3"

    def test_accepts_a_string_filename(self, roots: tuple[Path, Path]) -> None:
        """``exc.filename`` is a ``str``; the helper accepts it, not just ``Path``."""
        state, _output = roots
        rel = relativize_to_data_root(str(state / "logs" / "vox.log"))
        assert rel is not None
        assert rel.path == Path("logs/vox.log")

    def test_state_wins_when_nested_under_output(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When the state root sits inside the output root, ``state`` labels first."""
        output = (tmp_path / "out").resolve()
        state = output / "vox-state"
        state.mkdir(parents=True)
        monkeypatch.setattr(paths, "user_state_dir", lambda: state)
        monkeypatch.setattr(dirs, "default_output_dir", lambda: output)
        rel = relativize_to_data_root(state / "recordings" / "x.mp3")
        assert rel is not None
        assert rel.label == "state"


class TestOutOfJail:
    """A path under neither root -- or no path at all -- returns ``None``."""

    def test_unrelated_absolute_path_is_none(self, roots: tuple[Path, Path]) -> None:
        assert relativize_to_data_root(Path("/etc/passwd")) is None

    def test_none_filename_is_none(self, roots: tuple[Path, Path]) -> None:
        """``OSError.filename`` is ``None`` for a fault with no path."""
        assert relativize_to_data_root(None) is None

    def test_sibling_of_root_is_none(self, roots: tuple[Path, Path]) -> None:
        state, _output = roots
        sibling = state.parent / "state-elsewhere" / "foo.mp3"
        assert relativize_to_data_root(sibling) is None


class TestTraversalSafety:
    """A symlink or ``..`` cannot make an out-of-jail path look contained."""

    def test_dotdot_escaping_root_is_none(self, roots: tuple[Path, Path]) -> None:
        state, _output = roots
        escaping = state / ".." / "outside.mp3"
        assert relativize_to_data_root(escaping) is None

    def test_dotdot_landing_back_in_root_relativizes(
        self, roots: tuple[Path, Path]
    ) -> None:
        """A ``..`` that resolves back inside the root is in-jail, relativized."""
        state, _output = roots
        (state / "recordings").mkdir()
        winding = state / "recordings" / ".." / "recordings" / "foo.mp3"
        rel = relativize_to_data_root(winding)
        assert rel is not None
        assert rel.path == Path("recordings/foo.mp3")

    def test_symlink_pointing_outside_root_is_none(
        self, roots: tuple[Path, Path]
    ) -> None:
        state, _output = roots
        outside = state.parent / "outside"
        outside.mkdir()
        link = state / "escape"
        link.symlink_to(outside)
        # The candidate lexically sits under state, but resolves outside it.
        assert relativize_to_data_root(link / "secret.mp3") is None


class TestResolveFailsClosed:
    """An unresolvable candidate fails closed to ``None`` -- never propagates.

    The helper runs on the fault path (``SafeFault`` relativizes ``exc.filename``),
    so a ``resolve()`` that raises must not escape and fault the fault handler.
    """

    @pytest.mark.parametrize(
        "error",
        [
            OSError(40, "Too many levels of symbolic links"),  # ELOOP
            RuntimeError("Symlink loop detected"),
        ],
    )
    def test_resolve_failure_returns_none(
        self,
        roots: tuple[Path, Path],
        monkeypatch: pytest.MonkeyPatch,
        error: OSError | RuntimeError,
    ) -> None:
        """A ``resolve()`` that raises fails closed to ``None``, never propagates.

        ``Path.resolve(strict=False)`` is best-effort and does not raise on a loop
        on every platform, so the guard is proven by forcing the raise: an ELOOP
        ``OSError`` and a ``RuntimeError`` must both be swallowed.
        """

        def boom(_self: Path, *_args: object, **_kwargs: object) -> Path:
            raise error

        monkeypatch.setattr(Path, "resolve", boom)
        # Must return None, not propagate the exception.
        assert relativize_to_data_root("/anything/at/all.mp3") is None


class TestDataRootRelative:
    """The value object is an immutable label + relative path."""

    def test_exposes_label_and_path(self) -> None:
        rel = DataRootRelative("state", Path("recordings/foo.mp3"))
        assert rel.label == "state"
        assert rel.path == Path("recordings/foo.mp3")
