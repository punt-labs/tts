"""Relativize an in-jail daemon path to its labeled data root for the wire.

The daemon is conceptually chrooted to two data roots: the per-user *state* dir
(``~/.punt-labs/vox``) holding recordings, cache, and logs, and the *output* dir
(``$VOX_OUTPUT_DIR`` or ``~/Music/vox``) holding saved music albums. A path may
cross the wire only *relative* to whichever root contains it; the absolute prefix
-- the home directory and the username inside it -- never does.

:func:`relativize_to_data_root` is the boundary helper every client-facing reply
shares. It resolves both roots and the candidate before comparing -- the same
``.resolve()``-then-contain discipline ``ContainmentRoot`` applies to inbound
names -- so a symlink or ``..``-bearing path cannot appear in-jail when it is
not. An in-jail path returns its labeled relative form; a path under neither root
returns ``None``, and the caller drops it or falls back to a generic verdict.
"""

from __future__ import annotations

from pathlib import Path
from typing import Self, final

from punt_vox import dirs, paths

__all__ = ["DataRootRelative", "relativize_to_data_root"]


@final
class DataRootRelative:
    """An in-jail path expressed relative to one labeled daemon data root.

    ``label`` is ``"state"`` or ``"output"`` -- it disambiguates the two roots
    when the same relative shape could sit under either -- and ``path`` is the
    natural subdir path a client sees, e.g. ``recordings/foo.mp3``, with the
    absolute prefix stripped.
    """

    __slots__ = ("_label", "_path")

    _label: str
    _path: Path

    def __new__(cls, label: str, path: Path) -> Self:
        self = super().__new__(cls)
        self._label = label
        self._path = path
        return self

    @property
    def label(self) -> str:
        """Return the data-root label -- ``"state"`` or ``"output"``."""
        return self._label

    @property
    def path(self) -> Path:
        """Return the path relative to its root -- e.g. ``recordings/foo.mp3``."""
        return self._path

    @classmethod
    def of(cls, candidate: str | Path | None) -> Self | None:
        """Return the in-jail relative form of *candidate*, or ``None`` out of jail.

        *candidate* is an absolute daemon path -- typically ``exc.filename`` -- or
        ``None`` when the fault named no file. Both roots and the candidate are
        resolved before comparing, mirroring the ``ContainmentRoot`` discipline, so
        a symlink or ``..`` cannot make an out-of-jail path look contained. The
        state root is tested first, so a state dir nested inside the output root
        still labels ``state``.

        Returns ``None`` -- a documented state, not a failure -- when *candidate*
        is absent, cannot be built into a ``Path`` (a ``bytes`` filename), cannot
        be resolved (a symlink loop), or resolves under neither root; the caller
        then drops the path or sends a generic verdict.
        """
        if candidate is None:
            return None
        resolved = cls._resolved(candidate)
        if resolved is None:
            return None
        labeled_roots = (
            ("state", paths.user_state_dir()),
            ("output", dirs.default_output_dir()),
        )
        for label, root in labeled_roots:
            root_resolved = cls._resolved(root)
            if root_resolved is not None and resolved.is_relative_to(root_resolved):
                return cls(label, resolved.relative_to(root_resolved))
        return None

    @staticmethod
    def _resolved(candidate: str | Path) -> Path | None:
        """Return ``Path(candidate).resolve(strict=False)``, or ``None`` on failure.

        Both the construction and the resolution can fail on a hostile filename:
        ``Path(candidate)`` raises ``TypeError`` for a non-str/os.PathLike value
        (an ``OSError.filename`` may be ``bytes``, which the ``str | Path`` type
        does not admit but a runtime fault can still deliver), and ``resolve()``
        raises ``OSError`` (``ELOOP``)/``RuntimeError`` on a symlink loop or
        ``ValueError`` on a malformed path. This runs on the fault path --
        ``SafeFault`` relativizes ``exc.filename`` -- so an unguarded
        build-or-resolve would raise *while a fault is being built*, faulting the
        fault handler and tearing down the socket. Fail closed: any such failure
        means out of jail (``None``), so the filename yields the generic
        ``"operation failed"`` rather than a crash.
        """
        # None here is the documented fail-closed contract (out of jail), not a
        # give-up -- see the docstring and PY-TS-14.
        try:
            # strict=False is explicit: the fail-closed contract depends on
            # best-effort resolution (no raise on a missing path), and pinning it
            # guards against a future change to the resolve() default.
            return Path(candidate).resolve(strict=False)
        except (OSError, RuntimeError, ValueError, TypeError):
            return None


def relativize_to_data_root(path: str | Path | None) -> DataRootRelative | None:
    """Return *path* relative to its labeled data root, or ``None`` out of jail.

    The shared wire-boundary helper of the trust-boundary work: a reply calls it
    to strip the absolute prefix (home + username) from an in-jail path before the
    path crosses the wire. A thin facade over :meth:`DataRootRelative.of`, kept as
    a module function because it is the name every sibling reply depends on.

    Returns ``None`` when *path* is absent or resolves under neither root --
    absence is the documented contract, so the caller branches on it rather than
    handling an exception.
    """
    return DataRootRelative.of(path)
