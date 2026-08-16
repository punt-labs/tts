"""Tests for :mod:`punt_vox.session_state`."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Self, final

from punt_vox.session_state import SessionState


@final
@dataclass(frozen=True, slots=True)
class _AttrState:
    """A dataclass with all four attributes -- the ``VoxConfig`` shape."""

    provider: str | None = None
    voice: str | None = None
    model: str | None = None
    vibe_tags: str | None = None


@final
class _PropertyState:
    """A class exposing the fields as read-only properties.

    Mirrors the ``SessionConfig`` shape from the MCP server. Structural
    conformance means both attribute-based and property-based
    implementations satisfy the protocol without either inheriting from it.
    """

    __slots__ = ("_model", "_provider", "_vibe_tags", "_voice")
    _provider: str | None
    _voice: str | None
    _model: str | None
    _vibe_tags: str | None

    def __new__(
        cls,
        provider: str | None,
        voice: str | None,
        model: str | None,
        vibe_tags: str | None,
    ) -> Self:
        self = super().__new__(cls)
        self._provider = provider
        self._voice = voice
        self._model = model
        self._vibe_tags = vibe_tags
        return self

    @property
    def provider(self) -> str | None:
        return self._provider

    @property
    def voice(self) -> str | None:
        return self._voice

    @property
    def model(self) -> str | None:
        return self._model

    @property
    def vibe_tags(self) -> str | None:
        return self._vibe_tags


def test_dataclass_state_satisfies_protocol_structurally() -> None:
    """A frozen dataclass with the four fields is a :class:`SessionState`.

    The ``VoxConfig`` on-disk snapshot is a dataclass; structural conformance
    means it does not inherit from ``SessionState`` -- the fields alone make
    it a member of the family (PY-TS-6).
    """
    state: SessionState = _AttrState(provider="elevenlabs", voice="matilda")

    assert isinstance(state, SessionState)
    assert state.provider == "elevenlabs"
    assert state.voice == "matilda"
    assert state.model is None
    assert state.vibe_tags is None


def test_property_state_satisfies_protocol_structurally() -> None:
    """A class exposing the four fields as ``@property`` is a :class:`SessionState`.

    ``SessionConfig`` in the MCP server exposes its fields as properties
    over private attributes; the protocol must accept that shape too so
    the same :class:`SessionSpec` reads either the on-disk snapshot or
    the in-memory session without a common base class.
    """
    state: SessionState = _PropertyState(
        provider="openai", voice=None, model="tts-1", vibe_tags="[calm]"
    )

    assert isinstance(state, SessionState)
    assert state.provider == "openai"
    assert state.model == "tts-1"
    assert state.vibe_tags == "[calm]"


def test_missing_field_fails_runtime_isinstance() -> None:
    """A class that omits any of the four fields is not a :class:`SessionState`.

    ``@runtime_checkable`` protocols verify attribute presence, so an
    incomplete implementation fails :func:`isinstance` at call time rather
    than silently satisfying the type and crashing later on
    ``AttributeError``.
    """

    @final
    class _Incomplete:
        __slots__ = ("provider",)
        provider: str

        def __new__(cls) -> Self:
            self = super().__new__(cls)
            self.provider = "elevenlabs"
            return self

    assert not isinstance(_Incomplete(), SessionState)
