"""Tests for :class:`MusicArgs` -- the bundled ``music`` tool arguments.

Each ``music`` call becomes one frozen :class:`MusicArgs`; the value object
canonicalises its own tag/title fields so a blank/whitespace value is absent
(``None``), never an explicit ``""`` the daemon would store. The four
``canonical_*`` properties and ``authored`` are asserted directly.
"""

from __future__ import annotations

import pytest

from punt_vox.music_args import MusicArgs


@pytest.mark.parametrize("blank", ["", "   "])
def test_blank_tags_canonicalise_to_none(blank: str) -> None:
    args = MusicArgs("on", style=blank, vibe=blank, name=blank, title=blank)
    assert args.canonical_style is None
    assert args.canonical_vibe is None
    assert args.canonical_name is None
    assert args.canonical_title is None


def test_canonical_tags_trim_surrounding_whitespace() -> None:
    args = MusicArgs("play", style="  trance  ", vibe=" calm ", name=" mix ")
    assert args.canonical_style == "trance"
    assert args.canonical_vibe == "calm"
    assert args.canonical_name == "mix"


def test_canonical_title_trims_surrounding_whitespace() -> None:
    # The authored title is the album's curated name; it is trimmed like a tag.
    args = MusicArgs("on", title="  Midnight Drive  ")
    assert args.canonical_title == "Midnight Drive"


def test_canonical_title_is_independent_of_name() -> None:
    # ``title`` (authoring) and ``name`` (replay handle) are separate fields.
    args = MusicArgs("on", name="old handle", title="New Title")
    assert args.canonical_name == "old handle"
    assert args.canonical_title == "New Title"


def test_absent_title_is_none() -> None:
    assert MusicArgs("on").canonical_title is None


def test_authored_reflects_a_supplied_variation_pool() -> None:
    assert MusicArgs("on", variations=["a", "b"]).authored is True
    assert MusicArgs("on").authored is False
