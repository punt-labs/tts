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

The type of the state snapshot is a structural :class:`SessionState`
protocol so both :class:`~punt_vox.config.VoxConfig` (the on-disk snapshot
hooks and the CLI read) and :class:`~punt_vox.server.SessionConfig` (the
in-memory MCP session that a tool may have mutated) satisfy it without a
common base class.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Protocol, Self, final, runtime_checkable

from punt_vox.models import MODEL_TABLE
from punt_vox.types_synthesis import SynthesisSpec
from punt_vox.types_synthesis_errors import (
    ModelNotAvailableError,
    ProviderNotConfiguredError,
)

__all__ = ["SessionSpec", "SessionState"]


@runtime_checkable
class SessionState(Protocol):
    """The state fields a :class:`SessionSpec` reads.

    Structural so :class:`~punt_vox.config.VoxConfig` (dataclass attributes)
    and :class:`~punt_vox.server.SessionConfig` (properties) both satisfy it
    without an inheritance relationship neither of them wants.
    """

    @property
    def provider(self) -> str | None:
        """Return the state's provider name, or ``None`` when unset."""

    @property
    def voice(self) -> str | None:
        """Return the state's voice name, or ``None`` when unset."""

    @property
    def model(self) -> str | None:
        """Return the state's model name, or ``None`` / ``""`` when unset."""

    @property
    def vibe_tags(self) -> str | None:
        """Return the state's ElevenLabs expressive tags, or ``None`` when unset."""


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

    def fill(self, override: SynthesisSpec | None = None) -> SynthesisSpec:
        """Return *override* filled from state, provider and model both authoritative.

        A per-call ``override`` field wins when it is set; an unset field
        (``None`` for the ``str | None`` fields, missing for the rest) inherits
        from state. The provider must resolve to a non-empty name; a
        non-empty model must appear in the resolved provider's model list,
        or one of the two typed errors is raised.

        Empty state (``model: ""``) or unset state (``model: None``) is a
        permanent, legitimate state for the modelless providers (polly, say,
        espeak) and, for the model-bearing providers, means "use the provider
        default" -- no validation runs in that case, so the daemon still sees
        an absent model and its constructor supplies its own default.
        """
        base = override or SynthesisSpec()
        provider = self._resolve_provider(base.provider)
        model = self._resolve_model(base.model, provider, self._state.model)
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

    @staticmethod
    def _resolve_model(
        override: str | None, provider: str, state_model: str | None
    ) -> str | None:
        """Return the model to send, validated against *provider* when non-empty.

        The override wins over state's model; either falls through to
        :data:`None` when both are unset. An empty or whitespace-only string
        is treated as "no model" (the permanent state for polly, say, and
        espeak, and the "use the provider's default constant" case for
        elevenlabs and openai) -- a modelless request never fails
        validation. A non-empty model must appear in the provider's own
        list, or the caller sees the pair state actually declared, not a
        substitution to the provider's default.

        The strip mirrors :meth:`_resolve_provider`'s: a hand-edited
        ``model: "   "`` would otherwise reach ``MODEL_TABLE.available()``
        as ``"   "`` and fail with a list-membership error naming a
        whitespace model, when the documented meaning of empty-for-those-
        two-providers is "use the default". Normalising here also means
        no downstream surface sees a padded model name.
        """
        raw = override if override is not None else state_model
        if raw is None:
            return None
        # An empty (or whitespace-only) model means "no user-selectable
        # model" and skips validation, so a modelless provider (polly /
        # say / espeak) and an elevenlabs/openai session that has not
        # chosen a model both pass through to the provider constructor's
        # own default constant. The empty string is preserved verbatim
        # so ``config.get("model") == ""`` on the wire round-trips.
        if not (candidate := raw.strip()):
            return raw
        available = MODEL_TABLE.available(provider)
        # Modelless providers report ``available == ()``; a non-empty model
        # requested against one of them is the pair state must not hold,
        # so it is rejected here rather than at synthesis.
        if not available or candidate not in available:
            raise ModelNotAvailableError(candidate, provider, list(available))
        return candidate
