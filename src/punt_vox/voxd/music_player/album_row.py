"""``AlbumRow`` -- one album's scene row: its name cell beside a Play button.

The row is a columns group of a name :class:`TextElement` and a
:class:`PublishButton`. It is built by hand rather than as a ``GroupElement`` because
the pinned group only nests ABC elements, and the Play button must carry a ``publish``
attribute the pinned ``ButtonElement`` has no field for -- so the row owns its own
wire dict and nests the button's.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import final

from punt_lux import TextElement

from punt_vox.voxd.music_player.publish_button import PublishButton

__all__ = ["AlbumRow"]


@final
@dataclass(frozen=True, slots=True)
class AlbumRow:
    """One album's row: a name cell and a Play button, laid out in columns."""

    album_id: str
    label: str

    def to_dict(self) -> dict[str, object]:
        """Return the row's wire dict: a columns group of name cell and Play button."""
        name = TextElement(id=f"music.name.{self.album_id}", content=self.label)
        return {
            "kind": "group",
            "id": f"music.row.{self.album_id}",
            "layout": "columns",
            "children": [name.to_dict(), PublishButton.play(self.album_id).to_dict()],
        }
