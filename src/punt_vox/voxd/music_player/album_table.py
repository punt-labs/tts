"""``AlbumTable`` -- the click-to-play album grid of the scene.

A one-line count label (``Albums · 18 albums``) above one lux ``table`` of three
sortable columns -- **Album · Genre · Tracks** -- one row per catalogued album,
the playing album's name cell marked with ``▶``. The table renders directly, not
inside a ``collapsing_header``: collapsing it only hid the grid while the lux frame
stayed full-size, leaving an empty void. There is no id column: the table's
``key_column`` is the Album (name) column, so a row selection publishes the clicked
album's *friendly name*, which voxd resolves back to its id against its own catalog
(:meth:`AlbumDisplay.resolve`). Friendly names are made catalogue-unique (a
collision suffix on later albums), so the name is an unambiguous key.

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

from punt_lux import TextElement

from punt_vox.voxd.music_player.album_display import AlbumDisplay
from punt_vox.voxd.music_player.album_names import AlbumNames
from punt_vox.voxd.music_player.player_view import PlayerView
from punt_vox.voxd.music_player.wire import MusicTopic
from punt_vox.voxd.programs.catalog import Album

__all__ = ["AlbumTable"]

_LABEL_ID: Final = "music.albums.label"
_TABLE_ID: Final = "music.albums"
_COLUMNS: Final = ("Album", "Genre", "Tracks")
_KEY_COLUMN: Final = 0  # the Album (name) column is the row-id source
_ROW_EVENT: Final = "row_selection_changed"
# Keep the default grid chrome (borders + row backgrounds) and add the
# Display-local column sort; the wire form is the list of *enabled* flag names.
_FLAGS: Final = ["borders", "row_bg", "sortable"]


@final
@dataclass(frozen=True, slots=True)
class AlbumTable:
    """Project the catalog onto the labelled, single-select, sortable album table."""

    albums: tuple[Album, ...]
    view: PlayerView

    def elements(self) -> list[dict[str, object]]:
        """Return the count label followed by the album table (two flat elements)."""
        return [self._label_element(), self._table()]

    def _label_element(self) -> dict[str, object]:
        """Return the one-line ``Albums · N albums`` count label above the table."""
        return TextElement(id=_LABEL_ID, content=self._label()).to_dict()

    def _label(self) -> str:
        """Return the count label carrying the album count (``Albums · 18 albums``)."""
        count = len(self.albums)
        noun = "album" if count == 1 else "albums"
        return f"Albums · {count} {noun}"

    def _table(self) -> dict[str, object]:
        """Return the single-select, sortable album table wire dict, with the publish.

        The ``handlers`` entry attaches a ``publish`` decorator to the row-selection
        event: a selection publishes ``music.play``, carrying the selected row's key
        cell (the friendly album name) once lux's publish-payload passthrough lands
        (``lux-r4pp``). ``key_column`` is the Album column, so ``anchor`` is that
        name; voxd resolves it to an id catalogue-side. ``flags`` turns on the
        Display-local column sort while keeping the default borders/row-backgrounds.
        """
        names = AlbumNames(self.albums)
        return {
            "kind": "table",
            "id": _TABLE_ID,
            "columns": list(_COLUMNS),
            "rows": [self._row(album, names) for album in self.albums],
            "key_column": _KEY_COLUMN,
            "selection_mode": "single",
            "flags": list(_FLAGS),
            "handlers": [{"event": _ROW_EVENT, "publish": [MusicTopic.PLAY.value]}],
        }

    def _row(self, album: Album, names: AlbumNames) -> list[object]:
        """Return one album's row: its marked friendly name, its genre, its tracks.

        The name cell comes from the catalogue-wide ``names`` map (unique, marked
        with ``▶`` when this album plays), the genre and track count from the
        album's own :class:`AlbumDisplay`.
        """
        display = AlbumDisplay(album)
        return [names.marked_name(album, self.view), display.genre, display.track_count]
