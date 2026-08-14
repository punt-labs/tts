"""Tests for :mod:`punt_vox.session_spec`."""

from __future__ import annotations

from dataclasses import dataclass
from typing import final

import pytest

from punt_vox.session_spec import SessionSpec, SessionState
from punt_vox.types_synthesis import SynthesisSpec
from punt_vox.types_synthesis_errors import (
    ModelNotAvailableError,
    ProviderNotConfiguredError,
)


@final
@dataclass(frozen=True, slots=True)
class _State:
    """A minimal :class:`SessionState` stand-in used by every test in this module."""

    provider: str | None = None
    voice: str | None = None
    model: str | None = None
    vibe_tags: str | None = None


def test_state_class_satisfies_the_protocol_structurally() -> None:
    """The Protocol is structural, not nominal -- ``_State`` must satisfy it."""
    state: SessionState = _State(provider="elevenlabs")
    assert state.provider == "elevenlabs"


def test_fill_uses_state_provider_when_override_is_absent() -> None:
    """A bare ``fill()`` inherits the state's provider verbatim."""
    spec = SessionSpec(_State(provider="elevenlabs")).fill()

    assert spec.provider == "elevenlabs"


def test_fill_uses_state_voice_and_vibe_tags_when_override_is_absent() -> None:
    """A bare ``fill()`` inherits voice and vibe_tags from state."""
    state = _State(provider="elevenlabs", voice="matilda", vibe_tags="[cheerful]")

    spec = SessionSpec(state).fill()

    assert spec.voice == "matilda"
    assert spec.vibe_tags == "[cheerful]"


def test_fill_uses_state_model_when_override_is_absent() -> None:
    """A bare ``fill()`` inherits the model from state."""
    state = _State(provider="elevenlabs", model="eleven_v3")

    spec = SessionSpec(state).fill()

    assert spec.model == "eleven_v3"


def test_override_provider_wins_over_state() -> None:
    """A per-call ``provider`` override wins over state."""
    state = _State(provider="elevenlabs")

    spec = SessionSpec(state).fill(SynthesisSpec(provider="openai"))

    assert spec.provider == "openai"


def test_override_voice_wins_over_state() -> None:
    """A per-call ``voice`` override wins over state."""
    state = _State(provider="elevenlabs", voice="matilda")

    spec = SessionSpec(state).fill(SynthesisSpec(voice="drew"))

    assert spec.voice == "drew"


def test_override_model_wins_over_state() -> None:
    """A per-call ``model`` override wins over state."""
    state = _State(provider="elevenlabs", model="eleven_v3")

    spec = SessionSpec(state).fill(SynthesisSpec(model="eleven_flash_v2_5"))

    assert spec.model == "eleven_flash_v2_5"


def test_override_preserves_per_call_fields_the_state_does_not_carry() -> None:
    """A per-call ``rate`` / ``api_key`` on the override survive filling.

    ``fill`` overlays state onto override for the fields state carries
    (provider, voice, model, vibe_tags); every other field is untouched, so
    a caller's per-call ``api_key`` or ``rate`` is not silently discarded.
    """
    state = _State(provider="elevenlabs")

    spec = SessionSpec(state).fill(
        SynthesisSpec(rate=120, api_key="sk-abc", stability=0.5)
    )

    assert spec.rate == 120
    assert spec.api_key == "sk-abc"
    assert spec.stability == 0.5


def test_unconfigured_provider_raises_provider_not_configured() -> None:
    """State declaring no provider is a refusal, not a licence to guess.

    The regression test for the whole subsystem: an unset provider is now a
    typed refusal at every synthesis surface (F1), not a daemon-side probe
    of the environment (the defect that produced the wrong-provider log
    line in the reported incident).
    """
    with pytest.raises(ProviderNotConfiguredError, match="no TTS provider"):
        SessionSpec(_State()).fill()


def test_empty_string_provider_state_raises_provider_not_configured() -> None:
    """``provider: ""`` (the shipped shape of a fresh ``vox.md``) also raises.

    The wire never crosses with an empty provider; the F1 refusal fires at
    the client-side boundary instead. This is the ``or None`` case
    ``hooks.py:168`` was papering over.
    """
    with pytest.raises(ProviderNotConfiguredError):
        SessionSpec(_State(provider="")).fill()


def test_empty_string_override_provider_falls_through_to_state() -> None:
    """A blank ``--provider ""`` is treated as absent so state fills in.

    Otherwise a stray CI-env expansion (``VOX_PROVIDER=""``) would refuse
    on a repo that is otherwise correctly configured; the empty override
    is an unset override, not a request for the F1 refusal.
    """
    spec = SessionSpec(_State(provider="elevenlabs")).fill(SynthesisSpec(provider=""))

    assert spec.provider == "elevenlabs"


def test_whitespace_only_provider_state_raises_provider_not_configured() -> None:
    """``provider: "   "`` (hand-edited padding) raises F1, not the F4 unknown-provider.

    Without the strip, ``"   "`` is truthy: the F1 guard passes and the
    whitespace name reaches ``ProviderRegistry.get`` where it raises
    ``Unknown provider '   '`` (design F4). That is diagnosable but wrong
    -- the caller needs the F1 message that names the fix
    (``mic:provider <name>``). Same shape as the lux
    ``AppletIdentity.for_session`` fix: the walrus-strip both rejects the
    whitespace-only case and normalises what flows onward.
    """
    with pytest.raises(ProviderNotConfiguredError, match="no TTS provider"):
        SessionSpec(_State(provider="   ")).fill()


def test_whitespace_only_override_provider_raises_provider_not_configured() -> None:
    """A whitespace-only ``--provider "   "`` on the CLI is also F1, not F4.

    Same rationale as the state case: without the strip the whitespace
    would flow through to the daemon as an unknown-provider error.
    """
    with pytest.raises(ProviderNotConfiguredError):
        SessionSpec(_State(provider="elevenlabs")).fill(SynthesisSpec(provider="  "))


def test_provider_stripped_before_flowing_downstream() -> None:
    """``provider: "elevenlabs "`` reaches the wire as ``"elevenlabs"``.

    A hand-edited trailing space would otherwise send ``"elevenlabs "``
    to ``ProviderRegistry.get`` and fail with an unknown-provider error.
    Normalising here means no padded name crosses the SessionSpec boundary.
    """
    spec = SessionSpec(_State(provider="  elevenlabs  ")).fill()

    assert spec.provider == "elevenlabs"


def test_whitespace_only_model_state_normalises_to_empty_on_the_wire() -> None:
    """``model: "   "`` becomes ``""`` in the spec, not ``"   "``.

    Without the strip AND the ``return candidate`` the whitespace either
    (a) reaches ``MODEL_TABLE.available()`` as ``"   "`` and fails with
    ``ModelNotAvailableError`` naming a whitespace model, or (b) skips
    validation but still crosses the wire as ``"   "`` -- a truthy value
    a provider constructor could mistake for a real name (``self._model
    = model or DEFAULT`` would accept ``"   "`` and lock in a model
    nobody chose). Normalising to ``""`` here means the empty case
    round-trips (``""`` in stays ``""`` out) and the whitespace case
    collapses to the same empty (``"   "`` in becomes ``""`` out), so
    the downstream contract holds on both.
    """
    spec = SessionSpec(_State(provider="elevenlabs", model="   ")).fill()

    assert spec.provider == "elevenlabs"
    assert spec.model == ""


def test_model_stripped_before_validation() -> None:
    """``model: " eleven_v3 "`` validates as ``eleven_v3`` and flows unpadded.

    Without the strip the trailing/leading space would fail list-membership
    against the provider's models and raise ``ModelNotAvailableError`` for
    a name the user meant correctly. Normalising here means the padded
    form is accepted and the clean form is what reaches downstream.
    """
    spec = SessionSpec(_State(provider="elevenlabs", model=" eleven_v3 ")).fill()

    assert spec.model == "eleven_v3"


def test_provider_alien_model_is_rejected() -> None:
    """``provider: openai`` paired with ``model: eleven_v3`` refuses, never falls back.

    Silently substituting the OpenAI default for the model state asked for
    would be the same class of substitution as the provider defect this
    subsystem exists to prevent (F7).
    """
    state = _State(provider="openai", model="eleven_v3")

    with pytest.raises(ModelNotAvailableError) as excinfo:
        SessionSpec(state).fill()

    err = excinfo.value
    assert err.model == "eleven_v3"
    assert err.provider == "openai"
    assert "tts-1" in err.available


def test_alien_model_error_snapshots_available_at_construction() -> None:
    """Regression: mutating the caller's list after raising does not change the error.

    Two halves of one invariant: the ``available`` property returns an
    outbound copy AND the internal field is stored as a copy at
    construction (tuple in ``__new__``). Without the second, a caller that
    mutates its list after raising changes ``str(exc)`` and every subsequent
    ``exc.available`` read. This test guards the whole invariant, not one
    half -- the previous ``available``-returns-a-copy test passes even with
    the inbound-copy defect present.
    """
    available = ["tts-1", "tts-1-hd"]
    exc = ModelNotAvailableError("eleven_v3", "openai", available)

    available.append("added-after-raise")
    available[0] = "MUTATED"

    assert (
        str(exc) == "model 'eleven_v3' is not available for provider "
        "'openai' (available: tts-1, tts-1-hd)"
    )
    assert exc.available == ["tts-1", "tts-1-hd"]


def test_alien_model_error_renders_the_full_sentence() -> None:
    """``str(exc)`` returns the crafted F7 sentence, not the args tuple repr.

    A substring match on ``"tts-1"`` would pass whether ``str(exc)`` returned
    ``"model 'eleven_v3' is not available for provider 'openai' (available:
    tts-1, tts-1-hd, ...)"`` OR the tuple repr ``"('eleven_v3', 'openai',
    ['tts-1', ...])"`` -- the second is what a subclass of ``BaseException``
    with a multi-arg constructor produces when ``__str__`` is not overridden
    (``BaseException.__init__`` runs after ``__new__`` and overwrites
    ``args``). Assert on ``"is not available for provider"`` -- a phrase only
    the crafted sentence can contain -- so the test fails loudly if the
    override goes missing.
    """
    with pytest.raises(ModelNotAvailableError) as excinfo:
        SessionSpec(_State(provider="openai", model="eleven_v3")).fill()

    rendered = str(excinfo.value)
    assert (
        rendered == "model 'eleven_v3' is not available for provider "
        "'openai' (available: tts-1, tts-1-hd, gpt-4o-mini-tts)"
    )
    assert "is not available for provider" in rendered


def test_empty_model_uses_the_provider_default() -> None:
    """``model: ""`` with elevenlabs synthesizes with the provider's default.

    The absent-model case falls through to the provider constructor, which
    supplies its own documented default constant -- no substitution takes
    place at ``session_spec`` because no substitution is possible: the
    caller declared no preference.
    """
    spec = SessionSpec(_State(provider="elevenlabs", model="")).fill()

    assert spec.model == ""


def test_absent_model_state_stays_absent() -> None:
    """``model: None`` on state passes through unchanged."""
    spec = SessionSpec(_State(provider="elevenlabs", model=None)).fill()

    assert spec.model is None


@pytest.mark.parametrize("provider", ["polly", "say", "espeak"])
def test_modelless_provider_needs_no_model(provider: str) -> None:
    """Polly, say, and espeak have no model -- an empty model passes cleanly.

    Refusing on an empty model against these providers would make three of
    the five providers unusable; the asymmetry with the provider refusal
    is deliberate and rests on the "no user-selectable model" contract.
    """
    spec = SessionSpec(_State(provider=provider, model="")).fill()

    assert spec.provider == provider
    assert spec.model == ""


@pytest.mark.parametrize("provider", ["polly", "say", "espeak"])
def test_non_empty_model_on_modelless_provider_is_rejected(provider: str) -> None:
    """A non-empty model against a modelless provider is a hand-edit refusal.

    Symmetric with F7 for the model-bearing providers -- the caller sees
    the incompatible pair state actually holds, never a silent drop.
    """
    with pytest.raises(ModelNotAvailableError):
        SessionSpec(_State(provider=provider, model="eleven_v3")).fill()


def test_error_type_carries_the_pair_and_the_available_list() -> None:
    """The typed error exposes model, provider, and available as read-only fields."""
    err = ModelNotAvailableError("eleven_v3", "openai", ["tts-1", "tts-1-hd"])

    assert err.model == "eleven_v3"
    assert err.provider == "openai"
    assert err.available == ["tts-1", "tts-1-hd"]


def test_valid_openai_model_passes_through() -> None:
    """A model that appears in the provider's list survives validation."""
    spec = SessionSpec(_State(provider="openai", model="tts-1")).fill()

    assert spec.provider == "openai"
    assert spec.model == "tts-1"


def test_valid_elevenlabs_model_passes_through() -> None:
    """An ElevenLabs full-name model survives validation."""
    spec = SessionSpec(_State(provider="elevenlabs", model="eleven_flash_v2_5")).fill()

    assert spec.model == "eleven_flash_v2_5"


def test_override_model_validated_against_override_provider() -> None:
    """When override changes provider, model is validated against the new provider.

    The override wins for both fields; ``fill`` must validate the pair the
    caller is actually sending, not the pair state carried -- otherwise the
    check would rubber-stamp incompatible per-call combinations.
    """
    state = _State(provider="elevenlabs", model="eleven_v3")

    with pytest.raises(ModelNotAvailableError):
        SessionSpec(state).fill(SynthesisSpec(provider="openai", model="eleven_v3"))


def test_override_model_only_validates_against_state_provider() -> None:
    """Overriding only the model, without touching provider, validates against state."""
    state = _State(provider="openai")

    with pytest.raises(ModelNotAvailableError):
        SessionSpec(state).fill(SynthesisSpec(model="eleven_v3"))
