"""Tests for ``punt_vox.models`` -- the model enum table and shorthand resolver.

The table is provider-scoped, so an ``elevenlabs`` shorthand never resolves
against an ``openai`` call, and a provider with no user-selectable model
(``polly``/``say``/``espeak``) refuses any ``resolve_model`` call with a
"no user-selectable model" message. Both the CLI and the MCP tool share this
one function -- a regression here breaks both surfaces at once.
"""

from __future__ import annotations

from types import MappingProxyType

import pytest

from punt_vox.models import MODEL_TABLE, ModelTable, ProviderModels, resolve_model

_EMPTY: MappingProxyType[str, str] = MappingProxyType({})

# ---------------------------------------------------------------------------
# ModelTable.available (the list surface both switch tools drive)
# ---------------------------------------------------------------------------


def test_elevenlabs_lists_all_five_models() -> None:
    assert MODEL_TABLE.available("elevenlabs") == (
        "eleven_v3",
        "eleven_flash_v2_5",
        "eleven_turbo_v2_5",
        "eleven_turbo_v2",
        "eleven_multilingual_v2",
    )


def test_openai_lists_its_three_models() -> None:
    assert MODEL_TABLE.available("openai") == ("tts-1", "tts-1-hd", "gpt-4o-mini-tts")


@pytest.mark.parametrize("provider", ["polly", "say", "espeak"])
def test_modelless_providers_expose_an_empty_list(provider: str) -> None:
    """Polly/say/espeak have no user-selectable model -- the list is empty."""
    assert MODEL_TABLE.available(provider) == ()


def test_unknown_provider_returns_empty_tuple() -> None:
    """An unknown provider reads as modelless -- callers surface the same message."""
    assert MODEL_TABLE.available("nonesuch") == ()


# ---------------------------------------------------------------------------
# resolve_model -- shorthand resolution
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("shorthand", "full"),
    [
        ("v3", "eleven_v3"),
        ("flash", "eleven_flash_v2_5"),
        ("turbo", "eleven_turbo_v2_5"),
        ("multilingual", "eleven_multilingual_v2"),
    ],
)
def test_elevenlabs_shorthand_resolves_to_full_name(shorthand: str, full: str) -> None:
    assert resolve_model(shorthand, "elevenlabs") == full


def test_elevenlabs_full_name_is_returned_unchanged() -> None:
    assert resolve_model("eleven_v3", "elevenlabs") == "eleven_v3"


def test_openai_full_name_is_returned_unchanged() -> None:
    assert resolve_model("tts-1", "openai") == "tts-1"


def test_openai_has_no_shorthand_so_only_full_names_resolve() -> None:
    """OpenAI's table advertises no shorthand -- ``v3`` is unknown here."""
    with pytest.raises(ValueError, match="unknown model 'v3'"):
        resolve_model("v3", "openai")


def test_shorthand_is_provider_scoped() -> None:
    """``v3`` is an elevenlabs shorthand; passing it to openai must not resolve."""
    with pytest.raises(ValueError, match="openai"):
        resolve_model("v3", "openai")


def test_unknown_shorthand_names_the_offending_input() -> None:
    with pytest.raises(ValueError, match="unknown model 'made-up'"):
        resolve_model("made-up", "elevenlabs")


@pytest.mark.parametrize("provider", ["polly", "say", "espeak"])
def test_modelless_provider_refuses_any_name(provider: str) -> None:
    """A modelless provider never accepts a model -- the message names it."""
    with pytest.raises(ValueError, match=f"provider {provider!r}"):
        resolve_model("anything", provider)


def test_unknown_provider_refuses_any_name() -> None:
    with pytest.raises(ValueError, match="no user-selectable model"):
        resolve_model("eleven_v3", "nonesuch")


# ---------------------------------------------------------------------------
# ProviderModels + ModelTable directly
# ---------------------------------------------------------------------------


def test_provider_models_is_frozen() -> None:
    """A frozen dataclass -- mutating an attribute raises ``FrozenInstanceError``."""
    entry = ProviderModels(full=("a",), shorthands=MappingProxyType({"x": "a"}))
    with pytest.raises(Exception, match="cannot assign"):
        entry.full = ("b",)  # type: ignore[misc]


def test_provider_models_resolve_returns_full_name_for_shorthand() -> None:
    entry = ProviderModels(
        full=("eleven_v3",), shorthands=MappingProxyType({"v3": "eleven_v3"})
    )
    assert entry.resolve("v3") == "eleven_v3"


def test_provider_models_resolve_returns_full_name_unchanged() -> None:
    entry = ProviderModels(full=("eleven_v3",), shorthands=_EMPTY)
    assert entry.resolve("eleven_v3") == "eleven_v3"


def test_provider_models_resolve_on_empty_table_refuses() -> None:
    with pytest.raises(ValueError, match="no user-selectable model"):
        ProviderModels(full=(), shorthands=_EMPTY).resolve("anything")


def test_model_table_lists_the_declared_providers() -> None:
    assert set(MODEL_TABLE.providers()) == {
        "elevenlabs",
        "openai",
        "polly",
        "say",
        "espeak",
    }


def test_model_table_for_provider_returns_empty_for_unknown() -> None:
    entry = MODEL_TABLE.for_provider("nonesuch")
    assert entry.full == ()
    assert dict(entry.shorthands) == {}


def test_ad_hoc_model_table_composes_from_provider_models() -> None:
    """Building a table by hand exercises ``ModelTable.__new__`` end-to-end."""
    table = ModelTable(
        {
            "custom": ProviderModels(
                full=("m1", "m2"), shorthands=MappingProxyType({"m": "m1"})
            )
        }
    )
    assert table.available("custom") == ("m1", "m2")
    assert table.resolve("m", "custom") == "m1"
