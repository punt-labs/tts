"""Install vox's usage guide and register it as a global CLAUDE.md import.

:class:`VoxGuidance` owns the vox-side artifact: it writes the usage guide to
``~/.punt-labs/vox/CLAUDE.md`` and registers the line
``@~/.punt-labs/vox/CLAUDE.md`` in the user's ``~/.claude/CLAUDE.md`` via
:class:`~punt_vox.claude_md.ClaudeMdImport`, so the guide loads in every
Claude Code session without a per-project edit. The installer rewrites the
guide every run, so it is the single source of truth and can never drift from
the running vox version; uninstall deletes the guide and prunes its import.
"""

from __future__ import annotations

from contextlib import suppress
from pathlib import Path
from typing import Self, final

from punt_vox.claude_md import ClaudeMdImport
from punt_vox.paths import user_state_dir

__all__ = ["VoxGuidance"]


@final
class VoxGuidance:
    """Owns vox's usage guide and its registration in ``~/.claude/CLAUDE.md``.

    The guide is written to ``~/.punt-labs/vox/CLAUDE.md`` -- distinct from the
    repo-local ``.punt-labs/vox/vox.md`` config, so there is no collision.
    """

    __slots__ = ("_doc_path", "_import")

    _doc_path: Path
    _import: ClaudeMdImport

    _ASSET_NAME = "global-guidance.md"

    def __new__(cls, doc_path: Path, import_writer: ClaudeMdImport) -> Self:
        self = super().__new__(cls)
        self._doc_path = doc_path
        self._import = import_writer
        return self

    @classmethod
    def for_current_user(cls) -> Self:
        """Wire the real per-user paths for the running install."""
        home = Path.home()
        doc_path = user_state_dir() / "CLAUDE.md"
        import_line = "@~/" + doc_path.relative_to(home).as_posix()
        global_path = home / ".claude" / "CLAUDE.md"
        return cls(doc_path, ClaudeMdImport(global_path, import_line))

    @property
    def doc_path(self) -> Path:
        """Return the path of the vox usage guide."""
        return self._doc_path

    @property
    def import_line(self) -> str:
        """Return the ``@``-import line registered in the global CLAUDE.md."""
        return self._import.import_line

    @property
    def global_path(self) -> Path:
        """Return the ``~/.claude/CLAUDE.md`` the ``@``-import is written into."""
        return self._import.path

    def install(self) -> str:
        """Write the guide and register its import. Return a status message.

        The guide must exist before the ``@``-import points at it, so the write
        precedes the register. If ``register`` then fails (e.g. ``~/.claude`` is
        not writable), the just-written guide would be orphaned -- present on
        disk with nothing importing it. Unlink it best-effort and re-raise the
        original error, so a failed install leaves no partial state behind.
        """
        self._doc_path.parent.mkdir(parents=True, exist_ok=True)
        self._doc_path.write_text(self._load_doc(), encoding="utf-8")
        try:
            wrote = self._import.register()
        except OSError:
            self._unlink_quietly()
            raise
        state = "registered" if wrote else "already registered"
        return (
            f"vox usage guide written to {self._doc_path}; "
            f"import {state} in {self._import.path}"
        )

    def _unlink_quietly(self) -> None:
        """Remove the guide, suppressing cleanup errors so the original raises.

        Used to roll back a failed :meth:`install`: a failure to remove the
        orphaned guide must not mask the register error that triggered cleanup.
        """
        with suppress(OSError):
            self._doc_path.unlink(missing_ok=True)

    def uninstall(self) -> str:
        """Delete the guide and prune its import. Return a status message.

        The two teardown steps run independently: a failing ``unlink`` (a
        permissions error, or a race that already removed the doc) must not
        skip the prune, or the managed ``@``-import would be orphaned --
        pointing at a now-deleted guide. Both are attempted before any error
        is re-raised. When both fail neither is lost: the prune (orphaned-import)
        failure is raised with the ``unlink`` failure chained as its ``__cause__``.
        """
        doc_error: OSError | None = None  # a prior unlink failure, chained below
        try:
            # ``missing_ok=True`` makes an already-gone guide a clean no-op
            # atomically -- an ``is_file()`` guard would leave a TOCTOU window
            # where a concurrent removal (or a re-run after a partial teardown)
            # between the check and the unlink raises ``FileNotFoundError``.
            # A real failure (a permissions error) still surfaces below.
            self._doc_path.unlink(missing_ok=True)
        except OSError as exc:
            doc_error = exc
        try:
            self._import.prune()
        except OSError as exc:
            # ``from doc_error`` chains the unlink failure when both failed, and
            # is a plain ``raise exc`` (cause None) when the unlink succeeded.
            raise exc from doc_error
        if doc_error is not None:
            raise doc_error
        return (
            f"vox usage guide removed ({self._doc_path}); "
            f"import pruned from {self._import.path}"
        )

    def _load_doc(self) -> str:
        """Read the usage guide bundled beside this package."""
        asset = Path(__file__).resolve().parent / "assets" / self._ASSET_NAME
        return asset.read_text(encoding="utf-8")
