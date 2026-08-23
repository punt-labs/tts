"""Resolve the :class:`SynthesisSpec` a ``vox call`` needs before it can speak.

A bare ``client.synthesize(text)`` sends no provider on the wire -- voxd's
``speech_handlers.py`` requires one (``parse_required_str``) and does not
guess, so an unresolved spec rejects with ``Unknown provider ''`` on the
very first utterance. :class:`~punt_vox.session_spec.SessionSpec` is the
resolution every other synthesis surface (``vox say``, ``vox rec new``)
already goes through: state (``vox.md``) is the authority, never the
daemon.

Extracted out of :mod:`punt_vox.commands.call`, alongside its sibling
extraction :mod:`punt_vox.commands.call_live_driver`, for the same reason:
the resolve-and-translate-errors logic is one coherent operation, not a
pile of lines inline in ``_run``.
"""

from __future__ import annotations

from typing import Self, final

import typer

from punt_vox.session_spec import SessionSpec
from punt_vox.types_synthesis import SynthesisSpec
from punt_vox.types_synthesis_errors import (
    ModelNotAvailableError,
    ProviderNotConfiguredError,
)

__all__ = ["resolve_call_spec"]


@final
class _CallSpecResolver:
    """Resolve a call's :class:`SynthesisSpec`, translating errors for the CLI."""

    __slots__ = ()

    def __new__(cls) -> Self:
        return super().__new__(cls)

    def __call__(self) -> SynthesisSpec:
        """Return the resolved spec, or raise ``typer.BadParameter``.

        A call that cannot speak must refuse to start, not begin and then
        have every ``speak()`` call fail silently on the wire -- resolved
        once, before the call state machine starts.

        :class:`~punt_vox.session_spec.SessionSpec`'s own
        :class:`ProviderNotConfiguredError` message names ``mic:provider``
        -- the MCP tool, not a command a CLI user can run.
        :mod:`punt_vox.commands.model` and :mod:`punt_vox.commands.voice`
        hit the same "no provider configured" condition and write their
        own CLI-appropriate hint rather than surfacing the shared
        exception's text verbatim; matched here.
        :class:`ModelNotAvailableError`'s message carries no such
        MCP-flavored text, so it passes through unchanged.
        """
        try:
            return SessionSpec.for_repo().fill()
        except ProviderNotConfiguredError as exc:
            msg = (
                "no TTS provider is configured for this repo; "
                "set one with vox provider <name>"
            )
            raise typer.BadParameter(msg) from exc
        except ModelNotAvailableError as exc:
            raise typer.BadParameter(str(exc)) from exc


resolve_call_spec: _CallSpecResolver = _CallSpecResolver()
