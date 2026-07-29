"""The voxd music player: project the active source onto a lux scene, and back.

The package is a headless app inside ``voxd`` that mirrors the saved-album catalog
and the current now-playing onto the ``vox.music`` lux scene. :class:`MusicPlayer`
is the facade (PY-DP-10); the daemon builds it through
:class:`MusicPlayerSubsystem`.
"""

from __future__ import annotations

from punt_vox.voxd.music_player.composition import MusicPlayerSubsystem
from punt_vox.voxd.music_player.player import MusicPlayer

__all__ = ["MusicPlayer", "MusicPlayerSubsystem"]
