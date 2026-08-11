"""Shared cascade helpers for the provider/model/voice switch rule (vox-awm9).

Setting the TTS provider or model on any surface (MCP ``mic:*``, CLI ``vox
*``, Lux Control Panel) writes dependent fields with deterministic first-
from-list defaults. The rule is one line per cascade:

- ``provider`` set: model = ``MODEL_TABLE.available(provider)[0]`` (empty
  for modelless providers); voice = ``client.voices(provider)[0]``.
- ``model`` set: voice = ``client.voices(current_provider)[0]``.
- ``voice`` set: nothing cascades.

The two computations -- "first from the list, empty otherwise" for both the
model default and the voice default -- and the "fetch the roster, wrap a
daemon fault as a typed sentinel" step used to live in six places (three
surfaces times two computations). Consolidated here so every surface calls
one function and the daemon-fault branch has one shape.

Each caller still translates :class:`RosterError` into its own error
envelope (``mic:*`` returns ``{"error": ...}``; ``CommandResult`` sets
``error=True``; the panel sets ``PanelNotice.voxd_unavailable()``). That
translation is per-surface protocol; the fetch and the "first or empty"
computation are not.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol, final, runtime_checkable

from websockets.exceptions import WebSocketException

from punt_vox.client_errors import VoxdConnectionError, VoxdProtocolError
from punt_vox.models import MODEL_TABLE

if TYPE_CHECKING:
    from collections.abc import Sequence

__all__ = ["Cascade", "RosterClient", "RosterError"]

logger = logging.getLogger(__name__)


# Non-connect faults on the voxd voices() wire funnel to the same typed
# sentinel. VoxdConnectionError (the daemon is down) is common and prosaic;
# the others (protocol errors, websocket faults, OS errors, ValueError from
# a malformed response) log with .exception() before wrapping so a real bug
# leaves a diagnostic trail. Matches how the retired ``mic:who`` tool split
# the two classes.
_VOICES_FAULT_ERRORS = (VoxdProtocolError, WebSocketException, OSError, ValueError)


@runtime_checkable
class RosterClient(Protocol):
    """The one method the cascade helpers need from a voxd client.

    Both :class:`~punt_vox.client_sync.VoxClientSync` and the panel's
    :class:`~punt_vox.panel.ports.PanelDaemonClient` protocol satisfy this
    structurally, so the cascade helpers accept either without a concrete
    import.
    """

    def voices(self, provider: str | None = None) -> list[str]:
        """Return the named provider's voice roster."""
        ...


@final
@dataclass(frozen=True, slots=True)
class RosterError:
    """A daemon fault on the roster fetch, wrapped as a typed sentinel.

    A plain ``str`` return would collide with a real voice name (empty
    string ``""`` is a valid modelless-roster answer). Wrapping the failure
    in a distinct type lets the caller ``isinstance``-branch cleanly
    instead of comparing against magic values.
    """

    message: str


@final
class Cascade:
    """Namespace for the switch-cascade helpers.

    A class rather than free functions because the OO ratchet
    (``method_ratio`` / ``class_to_func_ratio``) treats a module of free
    helpers as a procedural leak; grouping them on one type keeps the
    stance consistent while the members stay stateless.
    """

    __slots__ = ()

    @staticmethod
    def first_or_empty(seq: Sequence[str]) -> str:
        """Return the first element of *seq*, or ``""`` when *seq* is empty.

        Deterministic "first from list" default per the cascade contract.
        Used for both the model default
        (``MODEL_TABLE.available(provider)[0]``) and the voice default
        (``client.voices(provider)[0]``).
        """
        return seq[0] if seq else ""

    @staticmethod
    def default_model(provider: str) -> str:
        """Return the first model in *provider*'s list, ``""`` when modelless.

        A modelless provider (say, espeak, polly) has an empty
        :data:`MODEL_TABLE.available` list; the cascade rule writes ``""``
        in that case, which the frontmatter store treats as absent.
        """
        return Cascade.first_or_empty(MODEL_TABLE.available(provider))

    @staticmethod
    def fetch_roster(
        client: RosterClient, provider: str | None
    ) -> list[str] | RosterError:
        """Return *provider*'s voice roster, or a :class:`RosterError` on fault.

        Fetches ``client.voices(provider=provider)``. A
        :class:`VoxdConnectionError` (daemon down) is wrapped without
        logging; a :class:`VoxdProtocolError`, :class:`WebSocketException`,
        :class:`OSError`, or :class:`ValueError` is logged with
        ``.exception()`` before wrapping so a real bug leaves a diagnostic
        trail. Callers translate the wrapper into their own per-surface
        error envelope.
        """
        try:
            return client.voices(provider=provider)
        except VoxdConnectionError as exc:
            return RosterError(str(exc))
        except _VOICES_FAULT_ERRORS as exc:
            logger.exception("Voice roster fetch failed (provider=%r)", provider)
            return RosterError(str(exc))

    @staticmethod
    def fetch_first_voice(client: RosterClient, provider: str) -> str | RosterError:
        """Return the provider's first voice, or a :class:`RosterError` on fault.

        Thin wrapper: :meth:`fetch_roster` then :meth:`first_or_empty`. The
        empty-string return for an empty roster matches the cascade rule --
        the frontmatter store treats ``""`` as absent.
        """
        roster = Cascade.fetch_roster(client, provider)
        if isinstance(roster, RosterError):
            return roster
        return Cascade.first_or_empty(roster)
