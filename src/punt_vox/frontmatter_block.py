"""The YAML frontmatter grammar: parsing a block, and editing one in place."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Self, final

from punt_vox.types_errors import ConfigValueError

if TYPE_CHECKING:
    from collections.abc import Iterable

__all__ = ["FrontmatterBlock"]

_FIELD_RE = re.compile(r'^([a-z_]+):\s*"?([^"\n]*)"?\s*$', re.MULTILINE)
# A fence is a `---` line; it only opens a block on the file's very first line,
# and only then does a later one close it. Without the opening anchor a
# markdown horizontal rule buried in prose reads as a closing fence, and an
# inserted field lands above that rule instead of in the frontmatter.
_OPENING_FENCE_RE = re.compile(r"\A---[ \t]*\n")
_FENCE_LINE_RE = re.compile(r"\n---[ \t]*$", re.MULTILINE)


@final
class FrontmatterBlock:
    """One config file's text, and every question about its frontmatter.

    Immutable: an edit answers a new block rather than changing this one, so
    the text that gets written is always a value someone chose, never a
    half-applied edit left behind by a failure partway through a batch.

    Holding the grammar here leaves :class:`~punt_vox.frontmatter.Frontmatter`
    with the file -- where it lives, whether it exists, what to log about it --
    and neither has to know the other's job.
    """

    __slots__ = ("_text",)

    _text: str

    def __new__(cls, text: str) -> Self:
        self = super().__new__(cls)
        self._text = text
        return self

    @classmethod
    def rendered(cls, updates: dict[str, str]) -> Self:
        """Return a complete block holding exactly *updates* and nothing else."""
        lines = [f'{k}: "{v}"' for k, v in updates.items()]
        return cls("---\n" + "\n".join(lines) + "\n---\n")

    @staticmethod
    def validate_value(value: str) -> None:
        """Reject values that would corrupt the ``key: "<value>"`` round-trip.

        The parser reads up to the first quote or newline, so either would
        truncate the field. Apostrophes are safe, so ``I'm tired`` survives.

        Raises :class:`~punt_vox.types_errors.ConfigValueError` -- its own
        type, not a bare ``ValueError``, because a caller answering this has
        a real value it cannot store, not a malformed request to discard.
        """
        if "\n" in value or "\r" in value:
            msg = f"config values must not contain newlines, got: {value!r}"
            raise ConfigValueError(msg)
        if '"' in value:
            msg = f"config values must not contain double-quotes, got: {value!r}"
            raise ConfigValueError(msg)

    @property
    def text(self) -> str:
        """Return the block's whole text, ready to write."""
        return self._text

    def fields(self) -> dict[str, str]:
        """Return every non-empty frontmatter field."""
        found: dict[str, str] = {}
        for match in _FIELD_RE.finditer(self._text):
            val = match.group(2).strip()
            if val:
                found[match.group(1)] = val
        return found

    def field(self, name: str) -> str | None:
        """Return one frontmatter field, or ``None`` when it is absent or empty.

        ``None`` is the documented contract here, not a giving-up: a config
        field the user never set has no value to answer with, and every
        caller reads it as "unset" and falls back to its own default.
        """
        match = self._field_re(name).search(self._text)
        if match and match.group(1).strip():
            return match.group(1).strip()
        return None

    def accepts(self, keys: Iterable[str]) -> bool:
        """Return whether every key in *keys* has somewhere to go in this block.

        A key already present is replaced where it sits; one that is absent
        needs the closing fence to sit above. Text with neither is not
        frontmatter this can edit, and the caller rebuilds it whole rather
        than appending fields below prose where no reader would find them.
        """
        if self._closing_fence(self._text) is not None:
            return True
        return all(self._field_re(key).search(self._text) for key in keys)

    def with_fields(self, updates: dict[str, str]) -> Self:
        """Return a block with *updates* applied, each replaced or inserted.

        Assumes :meth:`accepts` already said yes for these keys.
        """
        text = self._text
        for key, value in updates.items():
            line = f'{key}: "{value}"'
            field_re = self._field_re(key)
            # Each insert shifts the fence, so it is found afresh per key.
            if field_re.search(text):
                text = field_re.sub(line, text)
            else:
                text = self._inserted(text, line)
        return type(self)(text)

    @classmethod
    def _inserted(cls, text: str, line: str) -> str:
        """Return *text* with *line* written above its closing fence."""
        fence = cls._closing_fence(text)
        if fence is None:
            msg = f"no closing fence to insert {line!r} above"
            raise ValueError(msg)
        cut = fence.start() + 1  # just past the newline that opens the fence line
        return f"{text[:cut]}{line}\n{text[cut:]}"

    @staticmethod
    def _closing_fence(text: str) -> re.Match[str] | None:
        """Return the fence closing *text*'s frontmatter, if it has one.

        ``None`` is the documented contract for "this is not a fenced block":
        a fence closes only what an opening fence on the first line began, so
        a ``---`` horizontal rule sitting in prose answers nothing. The search
        starts on the opening fence's own newline, so an empty block --
        ``---\\n---\\n`` -- still finds its closing line.
        """
        opening = _OPENING_FENCE_RE.match(text)
        if opening is None:
            return None
        return _FENCE_LINE_RE.search(text, opening.end() - 1)

    @staticmethod
    def _field_re(key: str) -> re.Pattern[str]:
        """Return the pattern matching *key*'s whole line, quoted or bare."""
        return re.compile(rf"^{re.escape(key)}:\s*\"?([^\"\n]*)\"?\s*$", re.MULTILINE)
