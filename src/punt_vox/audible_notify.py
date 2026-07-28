"""Establish an audible notify level as part of the ``enable`` transition.

In the enablement model, silence is ``disable`` (no marker, hook gate closed),
not ``notify=n``. So an *enabled* repo must be audible: both ``vox notify
normal`` and ``vox notify continuous`` are audible levels, and ``enable`` is the
replacement for the retired ``/vox y`` (voice on). A fresh repo has no ``notify``
field, and :class:`~punt_vox.config.VoxConfig` defaults an absent-or-invalid
value to the silent ``"n"`` -- a leftover of the old model where ``n`` was a
level. Without this step, ``enable`` would pass the marker gate yet leave every
notify-gated hook skipping, so a freshly-enabled repo would be audibly identical
to a disabled one.

:class:`AudibleNotify` writes the audible default through the config layer,
never a raw ``vox.md`` write, and preserves a level the user already chose: a
re-run of ``enable`` (the upgrade path) over a ``continuous`` repo leaves it
continuous.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Self, final

from punt_vox.config import ConfigStore

if TYPE_CHECKING:
    from pathlib import Path

__all__ = ["AudibleNotify"]

# The notify values that already speak: normal ("y") and continuous ("c"). Only
# these are preserved on enable; an absent field or the silent "n" becomes "y".
_AUDIBLE_LEVELS: frozenset[str] = frozenset({"y", "c"})

# The audible default enable establishes -- normal notifications, the retired
# ``/vox y`` behavior (task completion + permission prompts).
_DEFAULT_LEVEL = "y"


@final
class AudibleNotify:
    """Ensure an enabled repo has an audible notify level, preserving any set one."""

    __slots__ = ("_store",)

    _store: ConfigStore

    def __new__(cls, config_dir: Path) -> Self:
        self = super().__new__(cls)
        self._store = ConfigStore(config_dir)
        return self

    def ensure_audible(self) -> None:
        """Set notify to the audible default unless an audible level is already set.

        Reads the stored ``notify`` through the config layer: an absent field or
        the silent ``"n"`` is raised to ``"y"``; an already-audible ``"y"`` or
        ``"c"`` is left untouched so a re-enable never downgrades a user's
        ``continuous`` choice.
        """
        if self._store.read_field("notify") not in _AUDIBLE_LEVELS:
            self._store.write_field("notify", _DEFAULT_LEVEL)
