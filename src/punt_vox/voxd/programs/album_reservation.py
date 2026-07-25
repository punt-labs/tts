"""The curated-name reservation guard and the pending-album ticket it mints.

``NameReservations`` closes the D-1 TOCTOU: two overlapping ``music new`` calls
for the same curated name cannot both pass the duplicate check while neither is
yet catalogued, because the name is held synchronously -- before the multi-second
generation await -- and freed once the album is catalogued or the generation
fails. An ``AlbumReservation`` is the ticket it hands back: a prompt-validated,
name-held pending album that releases its held name exactly once when its context
exits, on every path -- generation succeeded, generation failed, or an ack the
caller meant to interpose was never sent because the peer had gone.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Self, final

if TYPE_CHECKING:
    from collections.abc import Callable, Container
    from types import TracebackType

    from punt_vox.voxd.programs.album_tags import AlbumTags

__all__ = ["AlbumReservation", "NameReservations"]


@final
class NameReservations:
    """A synchronous curated-name guard over the in-flight ``music new`` set (D-1).

    Holds the curated names an in-flight ``new`` has claimed but not yet
    catalogued, and refuses a name already catalogued (``taken``) or already held.
    ``None`` -- an unnamed, tag-addressed pool auto-named at stamp time -- reserves
    nothing and never collides.
    """

    __slots__ = ("_held", "_taken")
    _held: set[str]
    _taken: Callable[[], Container[str]]

    def __new__(cls, taken: Callable[[], Container[str]]) -> Self:
        self = super().__new__(cls)
        self._held = set()
        self._taken = taken
        return self

    def hold(self, prompt: str, tags: AlbumTags) -> AlbumReservation:
        """Reserve ``tags.name`` and mint a ticket, refusing a taken/held name.

        The reservation is synchronous, before any generation await, so two
        overlapping same-name ``new`` calls cannot both pass this duplicate check
        while neither is yet catalogued. Raises ``ValueError`` when the curated
        name is already catalogued or held by another in-flight ``new``.
        """
        name = tags.name
        if name is not None and (name in self._taken() or name in self._held):
            raise ValueError(f"album named {name!r} already exists")
        if name is not None:
            self._held.add(name)
        return AlbumReservation(prompt, tags, self._release)

    def held_names(self) -> frozenset[str]:
        """Return the curated names in-flight ``new`` calls hold but have not filed."""
        return frozenset(self._held)

    def _release(self, name: str | None) -> None:
        """Free a held name once its album is catalogued or its generation failed.

        Idempotent: an unnamed pool (``None``) held nothing, and a double release
        of the same name is a harmless no-op.
        """
        if name is not None:
            self._held.discard(name)


@final
class AlbumReservation:
    """A prompt-validated, name-held pending album, released once on context exit.

    :meth:`NameReservations.hold` mints one after every pre-generation input
    rejection (empty prompt, duplicate or taken curated name) has already passed.
    It is an idempotent context manager: exiting it -- whether generation
    succeeded, failed, or never started because the ack could not be sent --
    releases the held name exactly once.
    """

    __slots__ = ("_held", "_prompt", "_release", "_tags")
    _prompt: str
    _tags: AlbumTags
    _release: Callable[[str | None], None]
    _held: bool

    def __new__(
        cls, prompt: str, tags: AlbumTags, release: Callable[[str | None], None]
    ) -> Self:
        self = super().__new__(cls)
        self._prompt = prompt
        self._tags = tags
        self._release = release
        self._held = True
        return self

    @property
    def prompt(self) -> str:
        """Return the validated, whitespace-stripped generation prompt."""
        return self._prompt

    @property
    def tags(self) -> AlbumTags:
        """Return the album tags carrying the reserved curated name."""
        return self._tags

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        del exc_type, exc, tb
        if self._held:
            self._held = False
            self._release(self._tags.name)
