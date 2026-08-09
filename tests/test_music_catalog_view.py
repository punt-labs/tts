"""Tests for ``SavedAlbums`` -- the saved crate as every music surface reports it.

A pure renderer, asserted directly on its three output shapes. The marker
argument is the only thing a surface varies: the ``music`` tool announces the
crate in its ``♪`` DJ voice, the ``vox music`` CLI plainly, and both must list
the same albums with the same fields.
"""

from __future__ import annotations

from punt_vox.music_catalog_view import SavedAlbums
from punt_vox.types_programs.control import ProgramSummary


def _summary(album_id: str, name: str | None = None) -> ProgramSummary:
    return ProgramSummary(
        id=album_id,
        style="klezmer",
        vibe="bright",
        format="music",
        ready=3,
        total=4,
        name=name,
    )


class TestSavedAlbumsAnnounced:
    """The crate as ``list`` announces it."""

    def test_empty_crate_says_so(self) -> None:
        assert SavedAlbums.marked([]).announced() == "♪ No saved albums."

    def test_counts_the_albums(self) -> None:
        listing = SavedAlbums.marked([_summary("a"), _summary("b")]).announced()
        assert listing.splitlines()[0] == "♪ 2 saved album(s):"

    def test_marks_every_album_line(self) -> None:
        listing = SavedAlbums.marked([_summary("a"), _summary("b")]).announced()
        assert [line.startswith("  ♪ ") for line in listing.splitlines()[1:]] == [
            True,
            True,
        ]

    def test_an_unmarked_surface_gets_the_same_listing_without_the_mark(self) -> None:
        """The CLI's plain voice differs only by the marker, never by content."""
        summaries = [_summary("a"), _summary("b")]
        plain = SavedAlbums(summaries).announced()
        assert plain == SavedAlbums.marked(summaries).announced().replace("♪ ", "")

    def test_an_unmarked_empty_crate_says_so_plainly(self) -> None:
        assert SavedAlbums([]).announced() == "No saved albums."


class TestSavedAlbumsAppendedTo:
    """The crate as a bare ``play`` with no history appends it to the reject."""

    def test_empty_crate_leaves_the_message_bare(self) -> None:
        assert SavedAlbums([]).appended_to("nothing played yet") == "nothing played yet"

    def test_keeps_the_message_as_the_first_line(self) -> None:
        text = SavedAlbums([_summary("a")]).appended_to("nothing played yet")
        assert text.splitlines()[0] == "nothing played yet"

    def test_lists_the_albums_under_a_heading(self) -> None:
        text = SavedAlbums([_summary("a")]).appended_to("nothing played yet")
        assert text.splitlines()[1] == "saved albums:"

    def test_album_lines_are_not_marquee_marked(self) -> None:
        """The reject is an error envelope, not a DJ announcement."""
        text = SavedAlbums([_summary("a")]).appended_to("nothing played yet")
        assert "♪" not in text


class TestSavedAlbumsToWire:
    """The ``programs`` records a reply carries."""

    def test_empty_crate_is_an_empty_list(self) -> None:
        assert SavedAlbums([]).to_wire() == []

    def test_carries_every_reported_field(self) -> None:
        assert SavedAlbums([_summary("a", name="Set")]).to_wire() == [
            {
                "id": "a",
                "style": "klezmer",
                "vibe": "bright",
                "name": "Set",
                "format": "music",
                "ready": 3,
                "total": 4,
            }
        ]

    def test_reports_an_untitled_album_with_a_null_name(self) -> None:
        assert SavedAlbums([_summary("a")]).to_wire()[0]["name"] is None
