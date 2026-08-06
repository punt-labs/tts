"""Tests for the ``SelectionPlayback`` consume-only replay source.

Asserts the modeled Radio invariants by name: ``playing in selection``, no
immediate repeat when ``#selection >= 2`` (reusing ``RotatePolicy``), a singleton
replays, a Selection never generates, and an empty Selection is a caught boundary.
"""

from __future__ import annotations

from punt_vox.voxd.programs.part import Part
from punt_vox.voxd.programs.playback_source import PlaybackSource
from punt_vox.voxd.programs.rotate_policy import RotatePolicy
from punt_vox.voxd.programs.selection import Selection
from punt_vox.voxd.programs.selection_playback import SelectionPlayback


def _union() -> Selection:
    return Selection.from_albums(
        [
            ("album-a", (Part("001.mp3", 1), Part("002.mp3", 2))),
            ("album-b", (Part("001.mp3", 1), Part("002.mp3", 2))),
        ]
    )


class TestConstruction:
    def test_begins_at_first_track(self) -> None:
        selection = _union()
        source = SelectionPlayback(selection, RotatePolicy())
        assert source.playing == selection.playable_pool()[0]
        assert source.last_played is None

    def test_conforms_to_playback_source(self) -> None:
        source = SelectionPlayback(_union(), RotatePolicy())
        assert isinstance(source, PlaybackSource)


class TestRotate:
    def test_playing_stays_in_selection(self) -> None:
        selection = _union()
        source = SelectionPlayback(selection, RotatePolicy())
        pool = set(selection.playable_pool())
        for _ in range(10):
            source.rotate()
            assert source.playing in pool

    def test_no_immediate_repeat_when_two_or_more(self) -> None:
        source = SelectionPlayback(_union(), RotatePolicy())
        for _ in range(10):
            before = source.playing
            source.rotate()
            assert source.playing != before
            assert source.last_played == before

    def test_singleton_selection_replays(self) -> None:
        selection = Selection.from_albums([("album-a", (Part("001.mp3", 1),))])
        source = SelectionPlayback(selection, RotatePolicy())
        only = source.playing
        source.rotate()
        assert source.playing == only


class TestPosition:
    def test_position_begins_at_one(self) -> None:
        source = SelectionPlayback(_union(), RotatePolicy())
        assert source.position == 1

    def test_position_agrees_with_the_scanned_index_across_rotations(self) -> None:
        selection = _union()
        source = SelectionPlayback(selection, RotatePolicy())
        pool = selection.playable_pool()
        for _ in range(20):
            source.rotate()
            playing = source.playing
            position = source.position
            assert playing is not None
            assert position is not None
            # The O(1) cursor matches the O(n) scan it replaces, and is 1-based.
            assert position == pool.index(playing) + 1
            assert 1 <= position <= len(pool)

    def test_position_is_none_when_empty(self) -> None:
        source = SelectionPlayback(Selection.from_albums([]), RotatePolicy())
        assert source.position is None


class TestNeverGenerates:
    def test_wants_generation_is_false(self) -> None:
        assert SelectionPlayback(_union(), RotatePolicy()).wants_generation is False

    def test_advances_on_end_tracks_the_cursor(self) -> None:
        assert SelectionPlayback(_union(), RotatePolicy()).advances_on_end is True


class TestEmptyBoundary:
    def test_empty_selection_holds_no_cursor(self) -> None:
        source = SelectionPlayback(Selection.from_albums([]), RotatePolicy())
        assert source.playing is None
        assert source.advances_on_end is False

    def test_rotate_on_empty_is_a_no_op(self) -> None:
        source = SelectionPlayback(Selection.from_albums([]), RotatePolicy())
        source.rotate()
        assert source.playing is None


def _single_album() -> Selection:
    return Selection.from_albums(
        [("album-a", (Part("001.mp3", 1), Part("002.mp3", 2), Part("003.mp3", 3)))]
    )


class TestDeterministicStep:
    """The user transport step (Z ``Next``/``Prev``): deterministic, stalls at bounds.

    Distinct from :meth:`rotate` (the loop's shuffle auto-advance, Fork C): the
    step walks the ordered pool by one and is a no-op at the boundary (``T5``).
    """

    def test_step_forward_advances_by_one(self) -> None:
        source = SelectionPlayback(_single_album(), RotatePolicy())
        assert source.position == 1
        source.step_forward()
        assert source.position == 2
        source.step_forward()
        assert source.position == 3

    def test_step_back_retreats_by_one(self) -> None:
        source = SelectionPlayback(_single_album(), RotatePolicy())
        source.step_forward()
        source.step_forward()  # at part 3
        source.step_back()
        assert source.position == 2

    def test_t5_next_at_last_part_is_a_no_op(self) -> None:
        source = SelectionPlayback(_single_album(), RotatePolicy())
        source.step_forward()
        source.step_forward()  # at part 3 (the last)
        held = source.playing
        source.step_forward()  # next at M stalls -- unlike rotate, it does not wrap
        assert source.position == 3
        assert source.playing == held

    def test_t5_prev_at_first_part_is_a_no_op(self) -> None:
        source = SelectionPlayback(_single_album(), RotatePolicy())
        held = source.playing
        source.step_back()  # prev at 1 stalls
        assert source.position == 1
        assert source.playing == held

    def test_step_on_empty_is_a_no_op(self) -> None:
        source = SelectionPlayback(Selection.from_albums([]), RotatePolicy())
        source.step_forward()
        source.step_back()
        assert source.playing is None
