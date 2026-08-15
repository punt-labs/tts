"""The ``provider_status`` wire op: report readiness, propose a starter provider.

Design §3.6.  Two request shapes fold through one handler:

* With a ``provider`` field, the reply carries that provider's
  :class:`~punt_vox.types_provider.ProviderReadiness` alone.
* Without, the reply carries every registered provider's verdict and
  the daemon's ``preferred`` proposal -- the name a fresh repo should
  adopt, per :meth:`ProviderCredentials.preferred`.

Both shapes read the same
:class:`~punt_vox.providers.credentials.ProviderCredentials` the daemon
gate calls, so status and behaviour cannot drift (§3.4): ``require`` and
``report`` are one function called two ways.

Kept in its own module rather than added to
:mod:`punt_vox.voxd.system_handlers` because that module already holds
three classes (``ChimeHandler``, ``VoicesHandler``, ``HealthHandler``)
and PY-OO-2 caps a module at three.  Adding a fourth would breach the
cap for a class that has one clear domain concern of its own.
"""
# pyright: reportPrivateUsage=false
# Internal module within the voxd package -- cross-module private access is expected.

from __future__ import annotations

from typing import TYPE_CHECKING, Self, final

from punt_vox.providers.credentials import ProviderCredentials
from punt_vox.voxd._parse import parse_optional_str
from punt_vox.voxd.wire_reply import WireReply

if TYPE_CHECKING:
    from starlette.websockets import WebSocket

    from punt_vox.types_provider import ProviderReadiness

__all__ = ["ProviderStatusHandler"]


@final
class ProviderStatusHandler:
    """Answer the ``provider_status`` op from one shared readiness source.

    The credentials source is injected so tests bind a bespoke
    dispatch (``ProviderCredentials(requirements={...})``) rather than
    monkey-patching the module.  Production wiring in
    :class:`~punt_vox.voxd.handler_registry.HandlerRegistry` passes a
    freshly constructed default (:func:`_default_requirements` inside
    the credentials module), so the handler reads the same environment
    on every request and the answer is current rather than cached.
    """

    __slots__ = ("_credentials",)
    _credentials: ProviderCredentials

    def __new__(cls, credentials: ProviderCredentials | None = None) -> Self:
        self = super().__new__(cls)
        self._credentials = (
            credentials if credentials is not None else ProviderCredentials()
        )
        return self

    async def __call__(self, msg: dict[str, object], websocket: WebSocket) -> None:
        """Reply with a per-provider verdict or the full set, per the request shape.

        The wire schema is:

        Request::

            {"type": "provider_status", "id": "<hex>",
             "provider": "<name>" | absent}

        Reply, always through :class:`WireReply` (so a gone peer is a
        quiet no-op rather than an uncorrelated router traceback)::

            {"type": "provider_status", "id": "<hex>",
             "providers": [ {name, ready, reason, detail}, ... ],
             "preferred": "<name>" | null}

        A single-provider request still reports as a one-row
        ``providers`` list so the reply's shape does not fork on the
        request shape -- the client's ``ProviderStatusPayload.find``
        picks the row.  ``preferred`` rides on both shapes because it
        is answered from the same requirement map and costs nothing:
        making the client omit it on the per-provider branch would
        force ``enable`` to send a second request to learn what the
        first one already knew.

        The op never raises across the boundary: a
        :class:`ValueError` from ``parse_optional_str`` on a
        wrong-typed ``provider`` field routes through
        :meth:`WireReply.error` (rejected client request, WARNING),
        and every other exception is caught by the router's broad
        boundary guard as usual.  ``ProviderCredentials.report`` /
        :meth:`ProviderCredentials.preferred` do not raise --
        :class:`~punt_vox.providers.credential_requirements.CredentialRequirement.satisfied`
        is contractually side-effect-free and swallow-its-own -- so
        the handler needs no try/except of its own.
        """
        reply = WireReply(websocket, str(msg.get("id", "")))
        try:
            requested = parse_optional_str(msg, "provider")
        except ValueError as exc:
            await reply.error(str(exc))
            return

        rows = self._rows(requested)
        payload: dict[str, object] = {
            "type": "provider_status",
            "providers": [row.to_dict() for row in rows],
            "preferred": self._credentials.preferred(),
        }
        await reply.send(payload)

    def _rows(self, requested: str | None) -> tuple[ProviderReadiness, ...]:
        """Return the readiness rows the reply should carry.

        A named provider yields a one-row tuple (still an
        :class:`~punt_vox.types_provider.ProviderReadiness`, so the
        reply shape does not fork); an omitted ``provider`` returns
        every registered verdict in the daemon's fixed preference
        order.  Extracted as a method so the ``__call__`` body reads
        as one wire transaction rather than two.
        """
        if requested is None:
            return self._credentials.report_all()
        return (self._credentials.report(requested),)
