"""``AlbumTable`` -- the collapsible, click-to-play album grid of the scene.

A ``collapsing_header`` (``▼ Albums``, with the album count) wrapping one lux
``table`` of three columns -- **Album · Genre · Tracks** -- one row per catalogued
album, the playing album's name cell marked with ``▶``. There is no id column: the
table's ``key_column`` is the Album (name) column, so a row selection publishes the
clicked album's *name*, which voxd resolves back to its id against its own catalog
(:meth:`AlbumDisplay.resolve`). Album names are catalogue-unique, so the name is an
unambiguous key.

**Click-to-play.** The table is ``selection_mode="single"`` with a ``publish``
decorator on its row-selection event: selecting a row publishes ``music.play``, and
lux delivers the selected row's key cell to voxd as ``payload['anchor']``. Today
lux's publish path emits an empty payload, so the click is inert; ``lux-r4pp``
(branch ``feat/publish-event-payload``) adds the passthrough, after which ``anchor``
arrives and click-to-play works with no change on this side. The wire shape is the
final one either way -- only the payload content changes.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final, final

from punt_vox.voxd.music_player.album_display import AlbumDisplay
from punt_vox.voxd.music_player.player_view import PlayerView
from punt_vox.voxd.music_player.wire import MusicTopic
from punt_vox.voxd.programs.catalog import Album

__all__ = ["AlbumTable"]

_HEADER_ID: Final = "music.albums.header"
_TABLE_ID: Final = "music.albums"
_COLUMNS: Final = ("Album", "Genre", "Tracks")
_KEY_COLUMN: Final = 0  # the Album (name) column is the row-id source
_ROW_EVENT: Final = "row_selection_changed"


@final
@dataclass(frozen=True, slots=True)
class AlbumTable:
    """Project the catalog onto the collapsible, single-select album table."""

    albums: tuple[Album, ...]
    view: PlayerView

    def to_dict(self) -> dict[str, object]:
        """Return the ``▼ Albums`` collapsing header wrapping the album table."""
        return {
            "kind": "collapsing_header",
            "id": _HEADER_ID,
            "label": self._label(),
            "open": True,
            "children": [self._table()],
        }

    def _label(self) -> str:
        """Return the header label carrying the album count (``Albums · 18 albums``)."""
        count = len(self.albums)
        noun = "album" if count == 1 else "albums"
        return f"Albums · {count} {noun}"

    def _table(self) -> dict[str, object]:
        """Return the single-select album table wire dict, with the play publish.

        The ``handlers`` entry attaches a ``publish`` decorator to the row-selection
        event: a selection publishes ``music.play``, carrying the selected row's key
        cell (the album name) once lux's publish-payload passthrough lands
        (``lux-r4pp``). ``key_column`` is the Album column, so ``anchor`` is that
        name; voxd resolves it to an id catalogue-side.
        """
        return {
            "kind": "table",
            "id": _TABLE_ID,
            "columns": list(_COLUMNS),
            "rows": [self._row(album) for album in self.albums],
            "key_column": _KEY_COLUMN,
            "selection_mode": "single",
            "handlers": [{"event": _ROW_EVENT, "publish": [MusicTopic.PLAY.value]}],
        }

    def _row(self, album: Album) -> list[object]:
        """Return one album's row: its marked name, its genre, its track count."""
        display = AlbumDisplay(album)
        return [display.marked_name(self.view), display.genre, display.track_count]
