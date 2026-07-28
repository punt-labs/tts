"""Per-repo enablement: the ``enable`` / ``disable`` / ``purge`` state machine.

Enablement is not a boolean field but a small state machine over three presence
facts (``docs/vox-enable-disable.tex``): the tool-owned directory
``.punt-labs/vox/``, the ``enabled`` marker inside it, and the canonical
``@.punt-labs/vox/CLAUDE.md`` import in the repo ``CLAUDE.md``. The load-bearing
invariant is the § 2.11 biconditional -- the marker is present exactly when the
import is present -- which every transition preserves:

- ``enable``  deposits the guide, writes the marker, adds the import, registers
  settings; idempotent, and the upgrade path.
- ``disable`` removes the import, deletes the marker, deregisters settings, and
  leaves the directory exactly as found (a frame, never a create/remove) -- the
  dormant state.
- ``purge``   is ``disable`` (which removes the import, so no orphan) *then* the
  subtree removal; removing the subtree alone would strand a 404ing import.

:class:`RepoEnablement` is the facade; it composes the import writer
(:class:`~punt_vox.claude_md.ClaudeMdImport`), the :class:`VoxMarker`, the
:class:`DepositedGuide`, and the
:class:`~punt_vox.settings_registration.SettingsRegistration`.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Self, final

from punt_vox.claude_md import ClaudeMdImport
from punt_vox.dirs import find_repo_root
from punt_vox.settings_registration import SettingsRegistration
from punt_vox.tool_owned_file import ToolOwnedFile

__all__ = ["DepositedGuide", "RepoEnablement", "VoxMarker"]

# The exact canonical repo-scope import line (§ 2.4). Byte-identical across all
# tools and both surfaces; what ``enable`` writes and ``disable`` prunes.
_IMPORT_LINE = "@.punt-labs/vox/CLAUDE.md"

# The marker's bytes are irrelevant to enablement (presence is the signal), but
# both surfaces write this exact content, so a CLI marker and an MCP marker are
# byte-identical (§ 2.14).
_MARKER_TEXT = (
    "vox is enabled in this repository.\n"
    "\n"
    "Managed by `vox enable` / `vox disable` (and the `/enable` / `/disable`\n"
    "slash commands). Presence turns vox's per-repo guidance and hooks on;\n"
    "remove it with `vox disable`, not by hand.\n"
)


@final
class VoxMarker:
    """The ``.punt-labs/vox/enabled`` marker file -- vox's per-repo on signal."""

    __slots__ = ("_file",)

    _file: ToolOwnedFile

    def __new__(cls, path: Path) -> Self:
        self = super().__new__(cls)
        self._file = ToolOwnedFile(path)
        return self

    @property
    def path(self) -> Path:
        """Return the marker file path."""
        return self._file.path

    def is_present(self) -> bool:
        """Return whether the marker exists."""
        return self._file.is_present()

    def write(self) -> None:
        """Create the marker, making its directory if absent; refuse a symlink."""
        self._file.write(_MARKER_TEXT)

    def remove(self) -> None:
        """Delete the marker; an already-absent marker is a clean no-op."""
        self._file.remove()


@final
class DepositedGuide:
    """The surface-aware user guide vox deposits at ``.punt-labs/vox/CLAUDE.md``.

    The guide is static content shipped beside the package (§ 2.5); ``enable``
    overwrites it wholesale so it can never drift from the running vox version.
    """

    __slots__ = ("_file",)

    _file: ToolOwnedFile

    _ASSET_NAME = "global-guidance.md"

    def __new__(cls, path: Path) -> Self:
        self = super().__new__(cls)
        self._file = ToolOwnedFile(path)
        return self

    @property
    def path(self) -> Path:
        """Return the deposited guide path."""
        return self._file.path

    def is_present(self) -> bool:
        """Return whether the guide file exists."""
        return self._file.is_present()

    def deposit(self) -> None:
        """Write the guide wholesale, making the dir if absent; refuse a symlink."""
        self._file.write(self._asset_text())

    @classmethod
    def _asset_text(cls) -> str:
        """Read the guide bundled beside this package."""
        asset = Path(__file__).resolve().parent / "assets" / cls._ASSET_NAME
        return asset.read_text(encoding="utf-8")


@final
class RepoEnablement:
    """Turn vox on and off in one repo, preserving the marker-import biconditional.

    Bind the four collaborators at construction; :meth:`for_repo` wires the real
    per-repo paths and :meth:`for_cwd` discovers the repo from the working
    directory. Each transition writes one of the three legal states, so no
    sequence of :meth:`enable` / :meth:`disable` / :meth:`purge` can leave the
    marker and the import disagreeing.
    """

    __slots__ = ("_guide", "_import", "_marker", "_settings")

    _import: ClaudeMdImport
    _marker: VoxMarker
    _guide: DepositedGuide
    _settings: SettingsRegistration

    def __new__(
        cls,
        *,
        import_writer: ClaudeMdImport,
        marker: VoxMarker,
        guide: DepositedGuide,
        settings: SettingsRegistration,
    ) -> Self:
        self = super().__new__(cls)
        self._import = import_writer
        self._marker = marker
        self._guide = guide
        self._settings = settings
        return self

    @classmethod
    def for_repo(cls, repo_root: Path) -> Self:
        """Wire the real per-repo paths for *repo_root*."""
        vox_dir = repo_root / ".punt-labs" / "vox"
        return cls(
            import_writer=ClaudeMdImport(repo_root / "CLAUDE.md", _IMPORT_LINE),
            marker=VoxMarker(vox_dir / "enabled"),
            guide=DepositedGuide(vox_dir / "CLAUDE.md"),
            settings=SettingsRegistration(repo_root / ".claude" / "settings.json"),
        )

    @classmethod
    def for_cwd(cls) -> Self:
        """Wire the repo discovered from the working directory.

        Raises ``ValueError`` when the working directory is not inside a git
        repository -- ``enable`` / ``disable`` are repo-scoped verbs (§ 2.3), so a
        non-repo invocation is a clean boundary failure, not a silent no-op.
        """
        root = find_repo_root()
        if root is None:
            msg = "not inside a git repository"
            raise ValueError(msg)
        return cls.for_repo(root)

    @property
    def root(self) -> Path:
        """Return the repository root this instance operates on."""
        # marker path is <root>/.punt-labs/vox/enabled -> root is three parents up.
        return self._marker.path.parents[2]

    @property
    def marker_path(self) -> Path:
        """Return the ``enabled`` marker path."""
        return self._marker.path

    @property
    def import_line(self) -> str:
        """Return the canonical ``@``-import line enablement owns."""
        return self._import.import_line

    def is_enabled(self) -> bool:
        """Return whether the repo is enabled (the marker is present)."""
        return self._marker.is_present()

    def enable(self) -> None:
        """Reach the Enabled state from anywhere; idempotent (also the upgrade path).

        Order matters for crash-safety: guide first (so the import never points
        at a missing guide), then the import, then the settings, and the marker
        **last**. The marker is vox's on-signal -- the hooks gate on it -- so if
        any earlier step raises, the repo is left observably OFF (no marker)
        rather than half-on (a marker with no guidance behind it). Re-running
        rewrites the guide, leaves the single import in place, and adds no
        duplicate.
        """
        self._guide.deposit()
        self._import.register()
        self._settings.register()
        self._marker.write()

    def disable(self) -> None:
        """Reach the Dormant/Absent state non-destructively.

        Remove the import first (so the biconditional holds the moment the marker
        goes), delete the marker, and deregister the settings entries. The
        directory is left exactly as found -- ``disable`` never creates or removes
        it -- so it lands in Dormant when a directory was present and stays Absent
        when it was not.
        """
        self._import.prune()
        self._marker.remove()
        self._settings.deregister()

    def purge(self) -> None:
        """Reach the Absent state by removing the subtree, leaving no orphan import.

        ``purge`` is ``disable`` -- which removes the import line that lives in
        ``CLAUDE.md``, *outside* the subtree -- followed by the subtree removal.
        Removing the subtree alone would strand a 404ing ``@``-import and violate
        the § 2.11 biconditional.
        """
        self.disable()
        self._remove_subtree()

    def _remove_subtree(self) -> None:
        """Remove the ``.punt-labs/vox/`` directory if it is present."""
        vox_dir = self._marker.path.parent
        if vox_dir.is_dir():
            shutil.rmtree(vox_dir)
