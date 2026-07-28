"""Per-repo enablement: the ``enable`` / ``disable`` / ``purge`` state machine.

Enablement is not a boolean field but a small state machine over three presence
facts (``docs/vox-enable-disable.tex``): the tool-owned directory
``.punt-labs/vox/``, the ``enabled`` marker inside it, and the canonical
``@.punt-labs/vox/CLAUDE.md`` import in the repo ``CLAUDE.md``. The load-bearing
invariant is the § 2.11 biconditional -- the marker is present exactly when the
import is present -- which every transition preserves:

- ``enable``  deposits the guide, adds the import, registers settings, sets an
  audible notify default, then writes the marker; idempotent, and the upgrade
  path. An enabled repo is audible by default -- silence is ``disable``.
- ``disable`` removes the import, deletes the marker, deregisters settings, and
  leaves the directory exactly as found (a frame, never a create/remove) -- the
  dormant state.
- ``purge``   is ``disable`` (which removes the import, so no orphan) *then* the
  subtree removal; removing the subtree alone would strand a 404ing import.

:class:`RepoEnablement` is the facade; it composes the import writer
(:class:`~punt_vox.claude_md.ClaudeMdImport`), the
:class:`~punt_vox.deposited_files.VoxMarker`, the
:class:`~punt_vox.deposited_files.DepositedGuide`, the
:class:`~punt_vox.settings_registration.SettingsRegistration`, and the
:class:`~punt_vox.audible_notify.AudibleNotify` default.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Self, final

from punt_vox.audible_notify import AudibleNotify
from punt_vox.claude_md import ClaudeMdImport
from punt_vox.deposited_files import DepositedGuide, VoxMarker
from punt_vox.dirs import find_repo_root
from punt_vox.settings_registration import SettingsRegistration

__all__ = ["RepoEnablement"]

# The exact canonical repo-scope import line (§ 2.4). Byte-identical across all
# tools and both surfaces; what ``enable`` writes and ``disable`` prunes.
_IMPORT_LINE = "@.punt-labs/vox/CLAUDE.md"


@final
class RepoEnablement:
    """Turn vox on and off in one repo, preserving the marker-import biconditional.

    Bind the five collaborators at construction; :meth:`for_repo` wires the real
    per-repo paths and :meth:`for_cwd` discovers the repo from the working
    directory. Each transition writes one of the three legal states, so no
    sequence of :meth:`enable` / :meth:`disable` / :meth:`purge` can leave the
    marker and the import disagreeing.
    """

    __slots__ = ("_audible", "_guide", "_import", "_marker", "_settings")

    _import: ClaudeMdImport
    _marker: VoxMarker
    _guide: DepositedGuide
    _settings: SettingsRegistration
    _audible: AudibleNotify

    def __new__(
        cls,
        *,
        import_writer: ClaudeMdImport,
        marker: VoxMarker,
        guide: DepositedGuide,
        settings: SettingsRegistration,
        audible: AudibleNotify,
    ) -> Self:
        self = super().__new__(cls)
        self._import = import_writer
        self._marker = marker
        self._guide = guide
        self._settings = settings
        self._audible = audible
        return self

    @classmethod
    def for_repo(cls, repo_root: Path) -> Self:
        """Wire the real per-repo paths for *repo_root*."""
        vox_dir = repo_root / ".punt-labs" / "vox"
        return cls(
            import_writer=ClaudeMdImport(repo_root / "CLAUDE.md", _IMPORT_LINE),
            marker=VoxMarker(vox_dir / "enabled", repo_root),
            guide=DepositedGuide(vox_dir / "CLAUDE.md", repo_root),
            settings=SettingsRegistration(repo_root / ".claude" / "settings.json"),
            audible=AudibleNotify(vox_dir),
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
        at a missing guide), then the import, the settings, the audible notify
        default, and the marker **last**. The marker is vox's on-signal -- the
        hooks gate on it -- so if any earlier step raises, the repo is left
        observably OFF (no marker) rather than half-on (a marker with no guidance
        behind it). The audible default lands before the marker so a completed
        ``enable`` is audible: silence is ``disable`` (marker gone), never an
        enabled repo left at ``notify=n``. Re-running rewrites the guide, leaves
        the single import in place, preserves an existing audible level, and adds
        no duplicate.
        """
        self._guide.deposit()
        self._import.register()
        self._settings.register()
        self._audible.ensure_audible()
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
