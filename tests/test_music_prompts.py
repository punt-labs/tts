"""Tests for PromptSet: agent-authored and fallback generation prompts."""

from __future__ import annotations

import pytest

from punt_vox.types_programs.prompts import POOL_SIZE, PromptSet


def _variations(n: int = POOL_SIZE) -> list[str]:
    """Return ``n`` distinct variation strings."""
    return [f"variation {i}" for i in range(n)]


class TestFromAgent:
    """The agent path validates shape and stores exactly what it was given."""

    def test_accepts_base_and_pool_size_variations(self) -> None:
        ps = PromptSet.from_agent("Klezmer, clarinet lead", _variations())
        assert ps.base == "Klezmer, clarinet lead"
        assert len(ps.variations) == POOL_SIZE

    def test_strips_whitespace(self) -> None:
        ps = PromptSet.from_agent("  base  ", ["  a  ", *_variations(POOL_SIZE - 1)])
        assert ps.base == "base"
        assert ps.variations[0] == "a"

    def test_empty_base_rejected(self) -> None:
        with pytest.raises(ValueError, match="base_prompt must be a non-empty"):
            PromptSet.from_agent("   ", _variations())

    def test_wrong_variation_count_rejected(self) -> None:
        with pytest.raises(ValueError, match=f"exactly {POOL_SIZE} entries"):
            PromptSet.from_agent("base", _variations(POOL_SIZE - 1))

    def test_blank_variation_rejected(self) -> None:
        variations = _variations()
        variations[3] = "   "
        with pytest.raises(ValueError, match="non-empty string"):
            PromptSet.from_agent("base", variations)


class TestSingle:
    """The single path wraps one verbatim prompt as a base-only set."""

    def test_wraps_prompt_as_base_with_no_variations(self) -> None:
        ps = PromptSet.single("warm analog pads, slow, D minor")
        assert ps.base == "warm analog pads, slow, D minor"
        assert ps.variations == ()

    def test_strips_surrounding_whitespace(self) -> None:
        ps = PromptSet.single("  ambient drone  ")
        assert ps.base == "ambient drone"

    def test_blank_prompt_rejected(self) -> None:
        with pytest.raises(ValueError, match="prompt must be a non-empty string"):
            PromptSet.single("   ")

    def test_empty_prompt_rejected(self) -> None:
        with pytest.raises(ValueError, match="prompt must be a non-empty string"):
            PromptSet.single("")

    def test_prompt_for_every_track_is_the_verbatim_base(self) -> None:
        """A single set is undecorated: no `. instrumental, loopable.` tail."""
        ps = PromptSet.single("copyrighted-safe descriptive prompt")
        assert ps.prompt_for(0) == "copyrighted-safe descriptive prompt"
        assert ps.prompt_for(5) == ps.prompt_for(0)


class TestPromptForAgentSet:
    """Track i draws variation i, composed onto the shared base."""

    def test_track_index_selects_variation(self) -> None:
        ps = PromptSet.from_agent("BASE", _variations())
        assert ps.prompt_for(0) == "BASE variation 0"
        assert ps.prompt_for(5) == "BASE variation 5"

    def test_twelve_indices_are_distinct(self) -> None:
        ps = PromptSet.from_agent("BASE", _variations())
        prompts = {ps.prompt_for(i) for i in range(POOL_SIZE)}
        assert len(prompts) == POOL_SIZE

    def test_index_wraps_past_pool_size(self) -> None:
        ps = PromptSet.from_agent("BASE", _variations())
        assert ps.prompt_for(POOL_SIZE) == ps.prompt_for(0)


class TestFallback:
    """The fallback is a minimal literal prompt, free of deep-work boilerplate."""

    _BANNED = (
        "background music for deep work",
        "smooth ambient texture",
        "driving beat",
        "afternoon focus",
        "steady working pace",
    )

    def test_shape(self) -> None:
        ps = PromptSet.fallback("Klezmer", "sad")
        assert ps.prompt_for(0) == "Klezmer music, sad. instrumental, loopable."

    def test_every_track_uses_the_same_minimal_prompt(self) -> None:
        ps = PromptSet.fallback("techno", "focused")
        assert ps.prompt_for(0) == ps.prompt_for(7)

    def test_empty_style_becomes_ambient(self) -> None:
        ps = PromptSet.fallback("", "calm")
        assert ps.prompt_for(0) == "ambient music, calm. instrumental, loopable."

    def test_empty_mood_dropped(self) -> None:
        ps = PromptSet.fallback("jazz", "")
        assert ps.prompt_for(0) == "jazz music. instrumental, loopable."

    def test_no_boilerplate(self) -> None:
        prompt = PromptSet.fallback("Klezmer", "sad").prompt_for(0)
        for banned in self._BANNED:
            assert banned not in prompt


class TestFromWire:
    """from_wire parses a wire message into a PromptSet or None."""

    def test_full_message_builds_agent_set(self) -> None:
        msg: dict[str, object] = {
            "base_prompt": "Klezmer, clarinet lead",
            "variations": _variations(),
        }
        ps = PromptSet.from_wire(msg)
        assert ps is not None
        assert ps.base == "Klezmer, clarinet lead"
        assert len(ps.variations) == POOL_SIZE

    def test_rejects_a_non_object_payload(self) -> None:
        # Valid JSON that is a list, string, or number is not a pool object;
        # reject it at the wire boundary rather than fail .get with AttributeError.
        for payload in ([1, 2], "a bare string", 42):
            with pytest.raises(ValueError, match="must be a JSON object"):
                PromptSet.from_wire(payload)

    def test_no_prompt_fields_returns_none(self) -> None:
        assert PromptSet.from_wire({"style": "techno"}) is None

    def test_rejects_a_non_string_variation_entry(self) -> None:
        # voxd's wire rule rejects a non-string field; no str() coercion.
        msg: dict[str, object] = {
            "base_prompt": "base",
            "variations": [*_variations(POOL_SIZE - 1), 42],
        }
        with pytest.raises(ValueError, match="every variation must be a string"):
            PromptSet.from_wire(msg)

    def test_rejects_a_numeric_base_prompt(self) -> None:
        # A present non-string base_prompt is rejected, not str()-coerced.
        with pytest.raises(ValueError, match="base_prompt must be a string"):
            PromptSet.from_wire({"base_prompt": 42, "variations": _variations()})

    def test_base_without_variations_raises(self) -> None:
        with pytest.raises(ValueError, match=f"exactly {POOL_SIZE}"):
            PromptSet.from_wire({"base_prompt": "base"})

    def test_variations_without_base_raises(self) -> None:
        with pytest.raises(ValueError, match="base_prompt must be a non-empty"):
            PromptSet.from_wire({"variations": _variations()})

    def test_non_list_variations_treated_as_absent(self) -> None:
        # A malformed non-list variations with no base is "no agent prompts".
        assert PromptSet.from_wire({"variations": "not-a-list"}) is None

    def test_null_base_prompt_treated_as_absent(self) -> None:
        # JSON null (Python None) must not become the literal string "None".
        assert PromptSet.from_wire({"base_prompt": None}) is None

    def test_null_base_with_variations_raises(self) -> None:
        # A null base alongside variations is malformed, not a silent fallback.
        with pytest.raises(ValueError, match="base_prompt must be a non-empty"):
            PromptSet.from_wire({"base_prompt": None, "variations": _variations()})
