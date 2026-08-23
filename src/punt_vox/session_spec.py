"""``SessionSpec`` -- the one state-to-spec constructor every synthesis surface uses.

State on disk (or the in-memory session it seeded) is the authority on which
provider voxd should synthesize with; voxd itself owns no session and no repo
context and must be told, per request, which provider to run. Five surfaces
build a :class:`~punt_vox.types_synthesis.SynthesisSpec` from state today --
the MCP tool's ``fill_defaults``, ``rec new``, the hook speech helper, the
CLI ``vox say``/``vox record``, and the panel voice preview -- and three
already do it wrong (§1.3 of ``docs/provider-authority.md``). Fixing that
five times guarantees a sixth surface reintroduces the same bug.

This module owns the fix: one class turns a state snapshot plus an optional
per-call override into a :class:`SynthesisSpec` whose ``provider`` is
guaranteed non-empty, whose ``model`` is validated against that provider,
and whose ``voice`` / ``vibe_tags`` inherit from state when the override
leaves them unset. A caller with no authoritative provider -- neither in
state nor in the override -- gets :class:`ProviderNotConfiguredError` before any
wire message is built. A state that pairs a provider with an alien model
(a hand-edited ``vox.md``) gets :class:`ModelNotAvailableError` in the same
place, never a silent substitution to the provider's default.

The type of the state snapshot is the structural
:class:`~punt_vox.session_state.SessionState` protocol -- see that module
for its shape and rationale.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Self, final

from punt_vox.config import ConfigStore
from punt_vox.dirs import DEFAULT_CONFIG_DIR, find_config_dir
from punt_vox.models import MODEL_TABLE
from punt_vox.session_state import SessionState
from punt_vox.types_synthesis import SynthesisSpec
from punt_vox.types_synthesis_errors import ProviderNotConfiguredError

__all__ = ["SessionSpec"]


@final
class SessionSpec:
    """Build a :class:`SynthesisSpec` from a :class:`SessionState`.

    Every synthesis surface constructs a ``SessionSpec`` for the current
    state, then calls :meth:`fill` with the caller's per-call override (or
    :data:`None` for a bare read). The returned spec is guaranteed to name
    a provider and, when its ``model`` is non-empty, to name one the provider
    offers.
    """

    __slots__ = ("_state",)
    _state: SessionState

    def __new__(cls, state: SessionState) -> Self:
        self = super().__new__(cls)
        self._state = state
        return self

    @classmethod
    def for_repo(cls) -> Self:
        """Build a :class:`SessionSpec` from this repo's ``vox.md``.

        The one config-lookup path every synthesis surface shares --
        ``vox say``, ``vox rec new``, and ``vox call`` all read the same
        ``ConfigStore`` at the same repo-scoped directory, so this is the
        single place that lookup is written rather than copied at each call
        site. A surface that skipped this (built a bare
        :class:`~punt_vox.types_synthesis.SynthesisSpec` and called
        :meth:`fill` on it without ever consulting state) would be back to
        the exact bug this whole module exists to prevent.
        """
        config = ConfigStore(find_config_dir() or DEFAULT_CONFIG_DIR).read()
        return cls(config)

    def fill(self, override: SynthesisSpec | None = None) -> SynthesisSpec:
        """Return *override* filled from state, provider and model both authoritative.

        A per-call ``override`` field wins when it is set; an unset field
        (``None`` for the ``str | None`` fields, missing for the rest) inherits
        from state. The provider must resolve to a non-empty name; a
        non-empty model must appear in the resolved provider's model list
        (see :meth:`_state_model_for` for how state's model interacts with
        a provider override), or one of the two typed errors is raised.
        """
        base = override or SynthesisSpec()
        provider = self._resolve_provider(base.provider)
        model = self._resolve_model(base.model, provider)
        voice = base.voice if base.voice is not None else self._state.voice
        vibe_tags = (
            base.vibe_tags if base.vibe_tags is not None else self._state.vibe_tags
        )
        return replace(
            base, provider=provider, model=model, voice=voice, vibe_tags=vibe_tags
        )

    def _resolve_provider(self, override: str | None) -> str:
        """Return the authoritative provider name, or raise if state has none.

        The override wins over state; an empty or whitespace-only string is
        treated as absent so that ``provider: ""`` in a fresh ``vox.md``
        (:class:`DESIGN.md`:69), an omitted ``--provider`` flag, and a
        hand-edited ``provider: "   "`` all fall through to state and,
        failing that, refuse.

        The strip is what closes the whitespace-bypass hazard. Without it
        ``"   "`` is truthy, would satisfy the guard, and would flow on to
        ``ProviderRegistry.get`` where it raises the less-informative
        ``Unknown provider '   '`` (design F4) instead of the F1
        "configure one" message the caller needs. Assigning through the
        walrus both rejects the whitespace-only case AND normalises what
        downstream surfaces see, so no padded name crosses the boundary.
        """
        if not (candidate := (override or self._state.provider or "").strip()):
            msg = (
                "no TTS provider is configured for this repo; "
                "set one with mic:provider <name>"
            )
            raise ProviderNotConfiguredError(msg)
        return candidate

    def _state_model_for(self, provider: str) -> str | None:
        """Return state's model when it belongs to *provider*, else :data:`None`.

        State's model is scoped to state's provider -- a hand-edited
        ``vox.md`` naming elevenlabs and ``eleven_v3`` says nothing about
        what to send when the caller overrides to polly. The comparison
        strips whitespace to mirror :meth:`_resolve_provider`'s
        normalisation, so ``provider: "  elevenlabs  "`` in state still
        counts as matching a bare ``elevenlabs`` override.
        """
        state_provider = (self._state.provider or "").strip()
        return self._state.model if state_provider == provider else None

    def _resolve_model(self, override: str | None, provider: str) -> str | None:
        """Return the model to send, validated against *provider* when non-empty.

        The override wins over state's model; either falls through to
        :data:`None` when both are unset. State's model is only consulted
        when it belongs to *provider* -- see :meth:`_state_model_for`.

        An empty or whitespace-only string is treated as "no model" (the
        permanent state for polly, say, and espeak, and the "use the
        provider's default constant" case for elevenlabs and openai) --
        a modelless request never fails validation. A non-empty model
        must appear in the provider's own list, or the caller sees the
        pair state actually declared, not a substitution to the provider's
        default.

        The strip mirrors :meth:`_resolve_provider`'s: a hand-edited
        ``model: "   "`` would otherwise reach ``MODEL_TABLE.available()``
        as ``"   "`` and fail with a list-membership error naming a
        whitespace model, when the documented meaning of empty-for-those-
        two-providers is "use the default". Normalising here also means
        no downstream surface sees a padded model name.
        """
        raw = override if override is not None else self._state_model_for(provider)
        if raw is None:
            return None
        # An empty (or whitespace-only) model means "no user-selectable
        # model" and skips validation, so a modelless provider (polly /
        # say / espeak) and an elevenlabs/openai session that has not
        # chosen a model both pass through to the provider constructor's
        # own default constant. The strip normalises whitespace to ``""``
        # here so ``"   "`` never crosses the wire as a truthy value that
        # a provider constructor could mistake for a real model name --
        # if the provider read ``self._model = model or DEFAULT``, a
        # padded string would satisfy the truthiness check and lock in a
        # value nobody chose. The empty case round-trips: ``""`` in
        # stays ``""`` out; ``"   "`` in becomes ``""`` out.
        if not (candidate := raw.strip()):
            return candidate
        MODEL_TABLE.validate(candidate, provider)
        return candidate
