"""Wire-side types for the ``provider_status`` op (design §3.6).

The daemon-authoritative readiness view -- what ``mic:status`` reports and
what ``vox doctor`` prints -- crosses the WebSocket as JSON and lands
here as :class:`ProviderReadiness`, a frozen value object with typed
field access.  Kept in a dedicated types module (rather than beside the
daemon-side :class:`~punt_vox.providers.credentials.ProviderCredentials`)
because the wire shape has to be importable without any provider SDK on
the path -- the MCP server, the CLI, and the hooks decode a
``provider_status`` frame long before a provider factory is reached, and
``credentials.py`` pulls in boto3 for its AWS credential probe.

There is ONE :class:`ProviderReadiness`; the daemon builds it in
:meth:`ProviderCredentials.report`, the server sends it as JSON, and the
client reads it back through :meth:`ProviderReadiness.from_wire` -- the
same class travels the wire in both directions.  Status and behaviour
cannot drift because ``report()`` (the daemon proposal) and
``require()`` (the daemon gate) share one
:class:`~punt_vox.providers.credential_requirements.CredentialRequirement`
map (see :mod:`~punt_vox.providers.credentials`), and the wire
representation is a straight serialisation of that verdict.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Self, cast, final

from punt_vox.types_programs import JsonObject

__all__ = [
    "PROVIDER_STATUS_REASONS",
    "ProviderReadiness",
    "ProviderStatusPayload",
    "ProviderStatusReason",
]


# The closed set of reasons a ``ProviderReadiness`` can carry, and where
# each value comes from.  This is the enumeration lesson from PR 1 and
# PR 2: a boundary that looks complete because everything it names is
# correct is still defective when it never states which values it must
# handle.  Every reason on this type is produced by exactly one code
# path; the paired :data:`PROVIDER_STATUS_REASONS` frozenset is the
# runtime mirror, and :meth:`ProviderReadiness.__post_init__` refuses a
# value not in either.  A new reason must land at the same time in the
# Literal, the frozenset, the raise/assemble site that produces it, and
# every render branch in :mod:`punt_vox.doctor` and :mod:`punt_vox.server`.
# Each producer, named on one line so a grep finds all four together:
#     ok               ProviderCredentials.report when satisfied() True
#     unconfigured     server._provider_status_block when session.provider None
#     unknown_provider ProviderCredentials.report on a name with no requirement
#     no_credentials   ProviderCredentials.report when satisfied() False
#     voxd_unavailable server._provider_status_block on _DAEMON_ERRORS
type ProviderStatusReason = Literal[
    "ok",
    "unconfigured",
    "unknown_provider",
    "no_credentials",
    "voxd_unavailable",
]


# The runtime mirror of the Literal above -- kept next to the type on
# purpose, because a value that is not a member cannot be a legal
# ``ProviderReadiness.reason`` and the guard below enforces it at
# construction time.  Any new reason lands here in the same edit as the
# Literal; ``ProviderReadiness.__new__`` will refuse a mismatch.
PROVIDER_STATUS_REASONS: frozenset[str] = frozenset(
    {"ok", "unconfigured", "unknown_provider", "no_credentials", "voxd_unavailable"}
)


@dataclass(frozen=True, slots=True)
class ProviderReadiness:
    """One provider's readiness verdict, on the wire and inside the daemon.

    Built by :meth:`punt_vox.providers.credentials.ProviderCredentials.report`
    on the daemon side and rebuilt from JSON by :meth:`from_wire` on the
    client side.  Importable without any provider SDK on the path, so
    the MCP server, the CLI, and the hooks can carry it around without
    pulling in boto3 or the ElevenLabs client.

    ``ready`` and ``reason`` are the branch surface a caller programs
    against: ``ready is True`` iff ``reason == "ok"``, and any other
    value points at the failure class (see the comment above the
    :data:`ProviderStatusReason` Literal for the closed set).  ``detail``
    is the human sentence to print -- empty for ``ok`` and
    ``unknown_provider`` (nothing to explain in the first case, nothing
    a client can say better than the daemon's ``Available: ...`` list
    in the second), populated for ``no_credentials`` (the exact
    :class:`~punt_vox.providers.credential_requirements.CredentialRequirement.unmet_message`
    text) and ``voxd_unavailable`` (the transport failure message).
    """

    name: str
    ready: bool
    reason: ProviderStatusReason
    detail: str

    def __post_init__(self) -> None:
        """Refuse a ``reason`` that is not in :data:`PROVIDER_STATUS_REASONS`.

        A wire frame with a novel reason string would otherwise slip
        past ``mypy`` (which only knows the Literal) and land at a
        render branch that has no case for it -- exactly the F4 shape.
        The runtime guard closes the gap.
        """
        if self.reason not in PROVIDER_STATUS_REASONS:
            msg = (
                f"unknown provider_status reason {self.reason!r}; "
                f"expected one of {sorted(PROVIDER_STATUS_REASONS)}"
            )
            raise ValueError(msg)
        # ``ready`` and ``reason`` are two views of one fact; a wire
        # frame claiming ``ready=True`` with any reason other than
        # ``ok`` (or vice versa) is malformed.  A caller that trusts
        # ``ready`` alone must be safe.
        if self.ready != (self.reason == "ok"):
            msg = (
                f"provider_status ready={self.ready!r} disagrees with "
                f"reason={self.reason!r}"
            )
            raise ValueError(msg)

    @classmethod
    def from_wire(cls, obj: JsonObject) -> Self:
        """Build a readiness value from a wire object, raising on a bad frame.

        Each field access raises through :class:`JsonObject`, so a
        truncated or wrong-typed frame surfaces as one
        :class:`ValueError` with the offending field name, never as a
        ``KeyError`` far from the wire boundary.
        """
        return cls(
            name=obj.require_str("name"),
            ready=obj.require_bool("ready"),
            reason=_narrow_reason(obj.require_str("reason")),
            detail=obj.require_str("detail"),
        )

    def to_dict(self) -> dict[str, object]:
        """Return the JSON-serialisable form for a wire reply or a status block."""
        return {
            "name": self.name,
            "ready": self.ready,
            "reason": self.reason,
            "detail": self.detail,
        }


@final
class ProviderStatusPayload:
    """The ``provider_status`` wire reply, parsed once and read many ways.

    Two request shapes fold into one reply:  a request naming a single
    ``provider`` returns exactly one :class:`ProviderReadiness`; a
    request omitting it returns every registered provider and the
    daemon's ``preferred`` proposal.  Callers of the second shape
    (:mod:`punt_vox.doctor` for the readiness section,
    :mod:`punt_vox.enablement` for the preferred provider) read this
    object rather than juggling raw dicts.

    ``preferred`` is deliberately ``str | None`` -- absence is the
    documented contract for "no provider on this daemon is ready",
    which is a true report of an unusable host, not an error (§3.8).
    """

    __slots__ = ("_preferred", "_providers")
    _providers: tuple[ProviderReadiness, ...]
    _preferred: str | None

    def __new__(
        cls,
        providers: tuple[ProviderReadiness, ...],
        preferred: str | None,
    ) -> Self:
        self = super().__new__(cls)
        self._providers = providers
        self._preferred = preferred
        return self

    @property
    def providers(self) -> tuple[ProviderReadiness, ...]:
        """Return each provider's readiness in the daemon's preference order."""
        return self._providers

    @property
    def preferred(self) -> str | None:
        """Return the daemon's proposal for a fresh repo, or ``None`` if unusable."""
        return self._preferred

    def find(self, name: str) -> ProviderReadiness | None:
        """Return the readiness for *name*, or ``None`` if it is not reported."""
        for entry in self._providers:
            if entry.name == name:
                return entry
        return None

    @classmethod
    def from_wire(cls, obj: JsonObject) -> Self:
        """Rebuild the payload from a ``provider_status`` reply frame."""
        rows = tuple(
            ProviderReadiness.from_wire(JsonObject.coerce(row, "provider_status.row"))
            for row in obj.require_list("providers")
        )
        preferred = obj.opt_str("preferred")
        return cls(rows, preferred)

    def to_dict(self) -> dict[str, object]:
        """Return the JSON-serialisable reply, mirroring :meth:`from_wire`."""
        return {
            "providers": [row.to_dict() for row in self._providers],
            "preferred": self._preferred,
        }


def _narrow_reason(raw: str) -> ProviderStatusReason:
    """Return *raw* narrowed to :data:`ProviderStatusReason`, raising if not a member.

    ``mypy`` cannot verify the narrowing without a runtime check, so
    the check *is* the narrowing:  membership is asserted in
    :data:`PROVIDER_STATUS_REASONS`, then the value is returned with the
    ``Literal`` type.  Kept as a private module-level function rather
    than a method because :meth:`ProviderReadiness.from_wire` is the
    only call site.
    """
    if raw not in PROVIDER_STATUS_REASONS:
        msg = (
            f"unknown provider_status reason {raw!r}; "
            f"expected one of {sorted(PROVIDER_STATUS_REASONS)}"
        )
        raise ValueError(msg)
    # The guard above has already asserted membership; ``cast`` in
    # string form (PY-TS-12) is the honest narrow from ``str`` to the
    # closed Literal ``ProviderStatusReason``.  ``mypy`` cannot verify
    # a runtime-only set membership without help.
    return cast("ProviderStatusReason", raw)
