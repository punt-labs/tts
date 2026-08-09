"""TTS model enumeration per provider, plus shorthand resolution.

Models are provider-specific: ElevenLabs offers a small family of ``eleven_*``
names with shorthand aliases (``v3``, ``flash``, ``turbo``, ``multilingual``);
OpenAI offers ``tts-1``/``tts-1-hd``/``gpt-4o-mini-tts`` with no shorthands;
Polly, ``say``, and ``espeak`` have no user-selectable model.

The table lives here so the CLI (``vox model``) and the MCP tool
(``mic:model``) share one resolution function -- a shorthand that resolves on
one surface must resolve identically on the other. Retires the scattered
model-name constants at ``providers/elevenlabs.py:35, 39-43``.
"""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Final, Self, final

__all__ = [
    "MODEL_TABLE",
    "ModelTable",
    "ProviderModels",
    "resolve_model",
]


@final
@dataclass(frozen=True, slots=True)
class ProviderModels:
    """The model list and shorthand table for one provider.

    An empty ``full`` tuple means "no user-selectable model on this provider"
    (Polly/say/espeak). ``shorthands`` maps a caller alias (``v3``) to the
    full name (``eleven_v3``). The first entry of ``full`` is the default.
    """

    full: tuple[str, ...]
    shorthands: MappingProxyType[str, str]

    def resolve(self, name: str) -> str:
        """Return the full model name for *name* (a shorthand or a full name).

        Raises ``ValueError`` when the provider has no user-selectable model or
        when *name* is neither a shorthand nor a full name. The caller weaves
        the provider into the message; this raises a bare reason.
        """
        if not self.full:
            msg = "provider has no user-selectable model"
            raise ValueError(msg)
        if name in self.shorthands:
            return self.shorthands[name]
        if name in self.full:
            return name
        msg = f"unknown model {name!r}"
        raise ValueError(msg)


_EMPTY_SHORTHANDS: Final[MappingProxyType[str, str]] = MappingProxyType({})
_EMPTY: Final[ProviderModels] = ProviderModels(full=(), shorthands=_EMPTY_SHORTHANDS)


@final
class ModelTable:
    """The provider-scoped model table both the CLI and the MCP tool share.

    Held as one dict-of-:class:`ProviderModels` -- adding a provider is one
    entry in :data:`MODEL_TABLE` and a code change to the ``ProviderName``
    Literal in ``server_switches.py``, never a runtime discovery step (§3.1).
    All lookup paths route through :meth:`for_provider` so an unknown provider
    reads as modelless (an empty :class:`ProviderModels`) rather than raising,
    matching the "no user-selectable model" contract §3.1 states for
    Polly/say/espeak.
    """

    __slots__ = ("_table",)
    _table: dict[str, ProviderModels]

    def __new__(cls, table: dict[str, ProviderModels]) -> Self:
        self = super().__new__(cls)
        self._table = dict(table)
        return self

    def for_provider(self, provider: str) -> ProviderModels:
        """Return the provider's model list, empty for unknown or modelless."""
        return self._table.get(provider, _EMPTY)

    def available(self, provider: str) -> tuple[str, ...]:
        """Return the full model names for *provider*, empty when none."""
        return self.for_provider(provider).full

    def resolve(self, name: str, provider: str) -> str:
        """Resolve *name* against *provider*'s shorthand and full-name tables.

        Wraps :meth:`ProviderModels.resolve` so *provider* appears in the
        error message -- the caller sees ``provider 'openai' has no ...``
        rather than a bare ``no user-selectable model``.
        """
        try:
            return self.for_provider(provider).resolve(name)
        except ValueError as exc:
            if not self.available(provider):
                msg = f"provider {provider!r} has no user-selectable model"
            else:
                msg = f"unknown model {name!r} for provider {provider!r}"
            raise ValueError(msg) from exc

    def providers(self) -> tuple[str, ...]:
        """Return the provider names known to the table, in declaration order."""
        return tuple(self._table)


MODEL_TABLE: Final[ModelTable] = ModelTable(
    {
        "elevenlabs": ProviderModels(
            full=(
                "eleven_v3",
                "eleven_flash_v2_5",
                "eleven_turbo_v2_5",
                "eleven_turbo_v2",
                "eleven_multilingual_v2",
            ),
            shorthands=MappingProxyType(
                {
                    "v3": "eleven_v3",
                    "flash": "eleven_flash_v2_5",
                    "turbo": "eleven_turbo_v2_5",
                    "multilingual": "eleven_multilingual_v2",
                }
            ),
        ),
        "openai": ProviderModels(
            full=("tts-1", "tts-1-hd", "gpt-4o-mini-tts"),
            shorthands=_EMPTY_SHORTHANDS,
        ),
        "polly": ProviderModels(full=(), shorthands=_EMPTY_SHORTHANDS),
        "say": ProviderModels(full=(), shorthands=_EMPTY_SHORTHANDS),
        "espeak": ProviderModels(full=(), shorthands=_EMPTY_SHORTHANDS),
    }
)


def resolve_model(name: str, provider: str) -> str:
    """Resolve *name* against *provider*'s shorthand and full-name tables."""
    return MODEL_TABLE.resolve(name, provider)
