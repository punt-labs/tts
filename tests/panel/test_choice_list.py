"""Tests for :mod:`punt_vox.panel.choice_list`."""

from __future__ import annotations

import pytest

from punt_vox.panel.choice_list import ChoiceList

_ITEMS = ("alpha", "beta", "gamma")


def _choices(current: str | None) -> ChoiceList:
    return ChoiceList(items=_ITEMS, current=current, empty_label="(nothing)")


class TestSomethingChosen:
    def test_the_sentinel_still_leads_the_list(self) -> None:
        assert _choices("beta").wire_items() == ["(none)", "alpha", "beta", "gamma"]

    def test_selected_points_at_the_chosen_entry(self) -> None:
        assert _choices("gamma").selected_index() == 3

    def test_an_index_names_the_entry_at_that_position(self) -> None:
        assert _choices("beta").name_for_index(2, noun="things") == "beta"


class TestTheSentinelIsUnconditional:
    """The offset must not depend on state a click cannot see.

    An index is picked from the list as rendered and resolved against
    state read later. Showing the sentinel only while unchosen made the
    two disagree whenever a refresh landed a value in between: the entry
    would retire, every index would shift down one, and a click on the
    third entry would commit the second. Silently wrong, not refused.
    """

    def test_the_index_of_an_entry_does_not_move_when_a_value_lands(self) -> None:
        rendered = _choices(None)
        # A refresh lands "alpha" between the render and the click.
        resolved = _choices("alpha")
        for index in range(1, len(rendered.wire_items())):
            before = rendered.name_for_index(index, noun="things")
            after = resolved.name_for_index(index, noun="things")
            assert before == after

    def test_the_offered_list_is_the_same_either_way(self) -> None:
        assert _choices(None).wire_items() == _choices("beta").wire_items()


class TestNothingChosen:
    def test_the_list_leads_with_a_sentinel(self) -> None:
        assert _choices(None).wire_items() == ["(none)", "alpha", "beta", "gamma"]

    def test_selected_points_at_the_sentinel(self) -> None:
        """Zero now names a real entry saying nothing is chosen.

        A Lux combo's ``selected`` is a total index, so "nothing chosen"
        has to be an item. Answering 0 without one made the display assert
        the first genuine choice was in force while the daemon held none.
        """
        assert _choices(None).selected_index() == 0

    def test_indices_are_shifted_past_the_sentinel(self) -> None:
        choices = _choices(None)
        assert choices.name_for_index(1, noun="things") == "alpha"
        assert choices.name_for_index(3, noun="things") == "gamma"

    def test_the_sentinel_itself_names_nothing(self) -> None:
        with pytest.raises(ValueError, match="out of range"):
            _choices(None).name_for_index(0, noun="things")

    def test_the_shifted_list_still_ends_where_the_items_do(self) -> None:
        with pytest.raises(ValueError, match="out of range"):
            _choices(None).name_for_index(4, noun="things")


class TestUnknownCurrent:
    def test_a_value_outside_the_list_counts_as_nothing_chosen(self) -> None:
        """A typo in config must not read as a selection.

        Falling back to index 0 rendered the first entry as chosen while
        the daemon held the typo, and nothing said the two differed.
        """
        choices = _choices("not-a-thing")
        assert choices.wire_items()[0] == "(none)"
        assert choices.selected_index() == 0
        assert choices.name_for_index(1, noun="things") == "alpha"


class TestNothingToChooseFrom:
    def test_an_empty_list_offers_only_its_empty_label(self) -> None:
        empty = ChoiceList(items=(), current=None, empty_label="(nothing)")
        assert empty.wire_items() == ["(nothing)"]
        assert empty.selected_index() == 0
        assert empty.is_empty

    def test_an_empty_list_names_nothing_at_any_index(self) -> None:
        empty = ChoiceList(items=(), current=None, empty_label="(nothing)")
        for index in (-1, 0, 1):
            with pytest.raises(ValueError, match="out of range"):
                empty.name_for_index(index, noun="things")

    def test_a_populated_list_is_not_empty_just_because_none_is_chosen(self) -> None:
        assert not _choices(None).is_empty


class TestRefusalMessage:
    def test_the_refusal_speaks_the_caller_s_noun(self) -> None:
        with pytest.raises(ValueError, match="voice combo"):
            _choices("beta").name_for_index(9, noun="voice")

    def test_the_count_agrees_with_its_noun(self) -> None:
        """A list of one reads "1 voice", not "1 voices"."""
        one = ChoiceList(items=("only",), current=None, empty_label="(nothing)")
        with pytest.raises(ValueError, match=r"\b1 voice\b"):
            one.name_for_index(7, noun="voice")

    @pytest.mark.parametrize("count", [0, 2, 3])
    def test_every_other_count_takes_the_plural(self, count: int) -> None:
        many = ChoiceList(
            items=tuple(f"item{n}" for n in range(count)),
            current=None,
            empty_label="(nothing)",
        )
        with pytest.raises(ValueError, match=rf"\b{count} voices\b"):
            many.name_for_index(99, noun="voice")


class TestRenderAndResolveAgree:
    """Every index the combo offers must resolve, and no others.

    This is the property the type exists for: the offered list and the
    click resolver were separate open-coded copies before, and they
    disagreed -- a model combo once offered five entries its own resolver
    refused categorically.
    """

    @pytest.mark.parametrize("current", [None, "alpha", "gamma", "not-a-thing"])
    def test_every_offered_position_resolves_except_a_sentinel(
        self, current: str | None
    ) -> None:
        choices = _choices(current)
        offered = choices.wire_items()
        for index, item in enumerate(offered):
            if item == "(none)":
                with pytest.raises(ValueError):
                    choices.name_for_index(index, noun="things")
            else:
                assert choices.name_for_index(index, noun="things") == item

    @pytest.mark.parametrize("current", [None, "alpha", "not-a-thing"])
    def test_one_past_the_offered_list_never_resolves(
        self, current: str | None
    ) -> None:
        choices = _choices(current)
        with pytest.raises(ValueError, match="out of range"):
            choices.name_for_index(len(choices.wire_items()), noun="things")
