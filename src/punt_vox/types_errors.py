"""Custom exception types for punt-vox."""

from __future__ import annotations

from typing import Self

__all__ = [
    "ConfigValueError",
    "VoiceNotFoundError",
]


class ConfigValueError(ValueError):
    """Raised when a value cannot be stored in a config file's frontmatter.

    A ``ValueError`` subclass because the value is what is invalid, but its
    own type so a caller can tell it from the other ``ValueError`` its call
    might raise. That distinction is the difference between a caller's
    mistake -- an out-of-range index, an unroutable key -- and a real value
    that simply cannot be serialized: the first is a bug to log, the second
    is a failure the user asked for and must see answered.
    """


class VoiceNotFoundError(ValueError):
    """Raised when a voice name cannot be resolved by a provider.

    Renders as ``"<name> (available: <a>, <b>, ...)"`` via :meth:`__str__`.
    ``__str__`` is the load-bearing override: ``BaseException.__init__``
    runs after ``__new__`` and overwrites ``args`` with the constructor's
    original positional arguments, so a message stashed in
    ``super().__new__(cls, msg)`` would round-trip out as the tuple repr
    ``"('name', ['a', 'b'])"`` -- what every ``str(exc)`` caller on this
    branch would see. The structured fields stay on ``args`` (useful to
    programmatic callers) while ``__str__`` renders the sentence.
    """

    _voice_name: str
    _available: tuple[str, ...]

    def __new__(cls, name: str, available: list[str]) -> Self:  # pyright: ignore[reportInconsistentConstructor]
        self = super().__new__(cls, name, available)
        self._voice_name = name
        # Tuple-copy in __new__ so the stored field is immutable outright:
        # a caller that mutates its list after raising cannot change
        # ``str(exc)`` or ``exc.available``. The outbound ``list()`` copy
        # on the property is not enough on its own -- it defends the
        # caller who reads from the exception but not the exception
        # itself. Both halves matter: the frozen snapshot here plus the
        # defensive copy on read.
        self._available = tuple(available)
        return self

    def __str__(self) -> str:
        """Render the message so ``str(exc)`` is user-facing, not a tuple repr."""
        return f"{self._voice_name} (available: {', '.join(self._available)})"

    @property
    def voice_name(self) -> str:
        """Return the voice name that was not found."""
        return self._voice_name

    @property
    def available(self) -> list[str]:
        """Return a copy of the available voice names."""
        return list(self._available)
