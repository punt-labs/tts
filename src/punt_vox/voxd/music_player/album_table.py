"""``AlbumTable`` -- the click-to-play album grid of the scene.

A one-line count label (``Albums · 18 albums``) above one lux ``table`` of three
sortable columns -- **Album · Genre · Tracks** -- one row per catalogued album.
The table renders directly, not inside a ``collapsing_header``: collapsing it only
hid the grid while the lux frame stayed full-size, leaving an empty void. There is
no id column and no now-playing marker *in the name cell* -- a ``▶`` prefix would
change the cell's sort order and its identity as the click key. Instead the
now-playing album is shown by *selecting its row*: the table's authoritative
selection is set to the playing album's ``key_column`` cell, so lux renders that
row highlighted -- the natural media-player indicator, and a row highlight, not a
cell value, so it leaves column sort and the click-to-play key intact. The
``key_column`` is the Album (name) column, so a row selection publishes the clicked
album's *friendly name*, which voxd resolves back to its id against its own catalog
(:meth:`AlbumNames.resolve`). Friendly names are made catalogue-unique (a collision
suffix on later albums), so the name is an unambiguous key.

**Now-playing highlight.** The playing album (``playing``, an :class:`AlbumId` or
``None`` when idle) maps to its row's key cell via :class:`AlbumNames`, and that
name goes onto the wire as ``selected_row_ids`` -- lux seats it into the table's
single-select model and highlights the row. When idle, or when the playing source
is a multi-album radio the catalog cannot name to one row, no row is selected.

**Click-to-play.** The table is ``selection_mode="single"`` with a ``publish``
decorator on its row-selection event: selecting a row publishes ``music.play``, and
lux delivers the selected row's key cell to voxd as ``payload['anchor']``. A user
click both selects the row (lux's built-in selection sync) and plays the album; the
next scene re-push then re-seats the selection onto the now-playing album. Today
lux's publish path emits an empty payload, so the click is inert; ``lux-r4pp``
(branch ``feat/publish-event-payload``) adds the passthrough, after which ``anchor``
arrives and click-to-play works with no change on this side. The wire shape is the
final one either way -- only the payload content changes.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Final, final

from punt_lux import TextElement

from punt_vox.voxd.music_player.album_names import AlbumNames
from punt_vox.voxd.music_player.album_roster import AlbumRoster
from punt_vox.voxd.music_player.wire import MusicTopic

if TYPE_CHECKING:
    from punt_vox.voxd.music_player.album_display import AlbumDisplay
    from punt_vox.voxd.programs.album_id import AlbumId

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
    """Project the catalog onto the labelled, single-select, sortable album table.

    ``playing`` is the active album's id (or ``None`` when idle / an uncatalogued
    radio): its row is pre-selected so lux highlights it as the now-playing cue.
    """

    roster: AlbumRoster
    playing: AlbumId | None = None

    def elements(self) -> list[dict[str, object]]:
        """Return the count label followed by the album table (two flat elements)."""
        return [self._label_element(), self._table()]

    def _label_element(self) -> dict[str, object]:
        """Return the one-line ``Albums · N albums`` count label above the table."""
        return TextElement(id=_LABEL_ID, content=self._label()).to_dict()

    def _label(self) -> str:
        """Return the count label carrying the album count (``Albums · 18 albums``)."""
        count = len(self.roster.displays)
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
        names = AlbumNames(self.roster.albums)
        return {
            "kind": "table",
            "id": _TABLE_ID,
            "columns": list(_COLUMNS),
            # ``rows`` precedes ``selected_row_ids`` and must keep doing so: on a
            # patch, lux's setters run in this order and the selection is
            # intersected against the rows *as they stand at setter time*, so a
            # selection written first would name ids the old row set lacks and be
            # silently dropped.
            "rows": [self._row(display, names) for display in self.roster.displays],
            "key_column": _KEY_COLUMN,
            "selection_mode": "single",
            "flags": list(_FLAGS),
            "handlers": [{"event": _ROW_EVENT, "publish": [MusicTopic.PLAY.value]}],
            **self._selection(names),
        }

    def _selection(self, names: AlbumNames) -> dict[str, object]:
        """Return the now-playing selection wire fragment: one row id, or none.

        Selecting the playing album's row is how the scene marks now-playing --
        lux highlights the selected row. The list is empty when idle or when the
        active source is a radio the catalog names to no single row
        (``friendly_for_id`` returns ``None``), so no row is highlighted.

        The key is always present, empty list and all. A field that appears in one
        render and vanishes from the next is a shape change no patch can express:
        the differ would emit nothing and the stale highlight would survive on a
        row that stopped playing.
        """
        selected = names.friendly_for_id(self.playing)
        return {"selected_row_ids": [] if selected is None else [selected]}

    @staticmethod
    def _row(display: AlbumDisplay, names: AlbumNames) -> list[object]:
        """Return one album's row: its friendly name, its genre, its track count.

        The name cell comes from the catalogue-wide ``names`` map (unique, no
        marker); the genre and the live track count come from the display the
        roster already read.
        """
        return [names.friendly(display.album), display.genre, display.tracks]
