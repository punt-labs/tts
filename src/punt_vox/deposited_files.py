"""The two tool-owned files ``enable`` writes into ``.punt-labs/vox/``.

Both are thin :class:`~punt_vox.tool_owned_file.ToolOwnedFile` wrappers: the
:class:`VoxMarker` is vox's per-repo on-signal (presence is the whole signal),
and the :class:`DepositedGuide` is the surface-aware agent guide overwritten
wholesale on every ``enable`` so it can never drift from the running vox version.
:class:`~punt_vox.enablement.RepoEnablement` composes both.
"""

from __future__ import annotations

from pathlib import Path
from typing import Self, final

from punt_vox.tool_owned_file import ToolOwnedFile

__all__ = ["DepositedGuide", "VoxMarker"]

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

    def __new__(cls, path: Path, base: Path) -> Self:
        self = super().__new__(cls)
        self._file = ToolOwnedFile(path, base)
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

    def __new__(cls, path: Path, base: Path) -> Self:
        self = super().__new__(cls)
        self._file = ToolOwnedFile(path, base)
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
