"""Tests for the replay request value objects (types_programs/control.py)."""

from __future__ import annotations

from punt_vox.types_programs.control import ProgramSummary, SelectionRequest


def _album(
    style: str, vibe: str, album_id: str = "a1", name: str | None = None
) -> ProgramSummary:
    return ProgramSummary(
        id=album_id, style=style, vibe=vibe, format="music", ready=3, name=name
    )


class TestResolvedStyle:
    """resolved_style names the one genre a replay lands on, else None."""

    def test_vibe_needing_normalization_matches_catalog_album(self) -> None:
        # The catalog stores the vibe bounded through VibeLabel; a raw request
        # vibe with collapsible whitespace and tag-hostile punctuation must be
        # bounded the same way so it matches its album instead of clearing.
        catalog = [_album("trance", "calm focus")]
        request = SelectionRequest(vibe="calm   focus!!")
        assert request.resolved_style(catalog) == "trance"

    def test_nonmatching_vibe_returns_none(self) -> None:
        catalog = [_album("trance", "calm focus")]
        request = SelectionRequest(vibe="anxious energy")
        assert request.resolved_style(catalog) is None

    def test_id_lookup_ignores_the_vibe_axis(self) -> None:
        catalog = [_album("trance", "calm focus", album_id="a1")]
        request = SelectionRequest(id="a1", vibe="anything")
        assert request.resolved_style(catalog) == "trance"

    def test_explicit_style_short_circuits_the_catalog(self) -> None:
        request = SelectionRequest(style="techno", vibe="wired")
        assert request.resolved_style([]) == "techno"

    def test_curated_name_in_the_id_slot_resolves_its_style(self) -> None:
        # The positional play path can carry a curated NAME in the id slot. That
        # must resolve the named album and name its style, not match nothing and
        # clear the register as a style-less union radio would.
        catalog = [_album("trance", "calm", album_id="a1", name="focus-beats")]
        request = SelectionRequest(id="focus-beats")
        assert request.resolved_style(catalog) == "trance"

    def test_id_in_the_id_slot_still_resolves_its_style(self) -> None:
        catalog = [_album("techno", "wired", album_id="a1", name="focus-beats")]
        request = SelectionRequest(id="a1")
        assert request.resolved_style(catalog) == "techno"

    def test_unknown_specific_ref_clears_to_none(self) -> None:
        # A ref matching no album (neither id nor name) has no album to name: None,
        # so the caller clears rather than naming a wrong genre.
        catalog = [_album("trance", "calm", album_id="a1", name="focus-beats")]
        request = SelectionRequest(id="does-not-exist")
        assert request.resolved_style(catalog) is None
