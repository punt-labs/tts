"""Tests for punt_vox.frontmatter_block -- the YAML frontmatter grammar."""

from __future__ import annotations

import pytest

from punt_vox.frontmatter_block import FrontmatterBlock
from punt_vox.types_errors import ConfigValueError

_FENCED = '---\nvoice: "charlie"\nnotify: ""\n---\n'
# Frontmatter followed by prose that carries a markdown horizontal rule --
# the shape a deposited guide takes once a user writes notes under it.
_FENCED_WITH_PROSE = f"{_FENCED}\n# Notes\n\nabove the rule\n\n---\n\nbelow it\n"
# Prose with a horizontal rule and no frontmatter at all.
_PROSE_WITH_RULE = "# Notes\n\nabove the rule\n\n---\n\nbelow it\n"


class TestRendered:
    def test_builds_a_fenced_block(self) -> None:
        block = FrontmatterBlock.rendered({"voice": "fin", "notify": "y"})
        assert block.text == '---\nvoice: "fin"\nnotify: "y"\n---\n'

    def test_round_trips_through_fields(self) -> None:
        block = FrontmatterBlock.rendered({"voice": "fin"})
        assert block.fields() == {"voice": "fin"}


class TestFields:
    def test_skips_empty_values(self) -> None:
        assert FrontmatterBlock(_FENCED).fields() == {"voice": "charlie"}

    def test_a_field_reads_back(self) -> None:
        assert FrontmatterBlock(_FENCED).field("voice") == "charlie"

    def test_an_empty_field_is_unset(self) -> None:
        assert FrontmatterBlock(_FENCED).field("notify") is None

    def test_an_absent_field_is_unset(self) -> None:
        assert FrontmatterBlock(_FENCED).field("provider") is None


class TestAccepts:
    def test_a_fenced_block_takes_anything(self) -> None:
        assert FrontmatterBlock(_FENCED).accepts(["provider"]) is True

    def test_an_unfenced_block_takes_a_key_it_already_holds(self) -> None:
        # No fence to insert above, but the field is there to be replaced --
        # so the edit lands in place rather than rewriting the file whole.
        assert FrontmatterBlock('voice: "charlie"\n').accepts(["voice"]) is True

    def test_an_unfenced_block_refuses_a_key_it_does_not_hold(self) -> None:
        assert FrontmatterBlock('voice: "charlie"\n').accepts(["notify"]) is False

    def test_prose_refuses_everything(self) -> None:
        assert FrontmatterBlock("no frontmatter here\n").accepts(["voice"]) is False

    def test_a_horizontal_rule_in_prose_is_not_a_fence(self) -> None:
        """A ``---`` rule below prose opens nothing, so it closes nothing."""
        assert FrontmatterBlock(_PROSE_WITH_RULE).accepts(["voice"]) is False


class TestWithFields:
    def test_replaces_a_field_in_place(self) -> None:
        updated = FrontmatterBlock(_FENCED).with_fields({"voice": "roger"})
        assert updated.field("voice") == "roger"

    def test_inserts_an_absent_field_above_the_fence(self) -> None:
        updated = FrontmatterBlock(_FENCED).with_fields({"provider": "openai"})
        assert updated.field("provider") == "openai"
        assert updated.text == (
            '---\nvoice: "charlie"\nnotify: ""\nprovider: "openai"\n---\n'
        )

    def test_keeps_the_fields_it_was_not_asked_about(self) -> None:
        updated = FrontmatterBlock(_FENCED).with_fields({"provider": "openai"})
        assert updated.field("voice") == "charlie"

    def test_inserts_above_the_real_fence_not_a_rule_in_the_prose(self) -> None:
        """The insert lands in the frontmatter, leaving the body untouched."""
        updated = FrontmatterBlock(_FENCED_WITH_PROSE).with_fields(
            {"provider": "openai"}
        )
        assert updated.text == _FENCED_WITH_PROSE.replace(
            'notify: ""\n---\n', 'notify: ""\nprovider: "openai"\n---\n'
        )

    def test_replaces_a_field_without_disturbing_the_prose(self) -> None:
        updated = FrontmatterBlock(_FENCED_WITH_PROSE).with_fields({"voice": "roger"})
        assert updated.field("voice") == "roger"
        assert updated.text.endswith("\nbelow it\n")

    def test_leaves_the_original_untouched(self) -> None:
        """Immutable: the edit is a new block, so a failure partway through a
        batch cannot leave a half-applied one behind."""
        held = FrontmatterBlock(_FENCED)
        held.with_fields({"voice": "roger"})
        assert held.field("voice") == "charlie"


class TestValidateValue:
    @pytest.mark.parametrize("bad", ["a\nb", "a\rb"])
    def test_rejects_newlines(self, bad: str) -> None:
        with pytest.raises(ConfigValueError, match="must not contain newlines"):
            FrontmatterBlock.validate_value(bad)

    @pytest.mark.parametrize("bad", ['I"m tired', 'say "hi"', '"'])
    def test_rejects_double_quotes(self, bad: str) -> None:
        with pytest.raises(ConfigValueError, match="must not contain double-quotes"):
            FrontmatterBlock.validate_value(bad)

    def test_accepts_an_apostrophe(self) -> None:
        FrontmatterBlock.validate_value("I'm tired")  # no raise
