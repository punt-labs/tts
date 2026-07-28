"""Find and remove a bare ``@``-import line among a Markdown doc's top-level lines."""

from __future__ import annotations

from typing import TYPE_CHECKING, Self, final

if TYPE_CHECKING:
    from collections.abc import Iterator

__all__ = ["MarkdownDoc"]


@final
class MarkdownDoc:
    """A Markdown document that knows which of its physical lines are top-level.

    A line is *top-level* when Claude Code would resolve an ``@``-import written
    on it -- outside every code block. Fenced blocks are **balanced pairs**: a
    run of *N* backticks or tildes opens a block, and only a later delimiter of
    the **same** marker whose run is **at least** *N* closes it; a shorter or
    mismatched delimiter inside the block is content, and blocks never nest. An
    **unterminated opener delimits nothing** -- a dangling fence in the user's
    prose does not swallow the lines below it, so the tool's own column-0 import
    line stays matchable regardless of a stray fence above it. An **indented**
    line (a tab or four or more leading spaces) is an inert indented-code line,
    never top-level and never a fence delimiter.

    The classification is computed once at construction; :meth:`contains` and
    :meth:`without` are the two queries the import writer needs.
    """

    __slots__ = ("_lines", "_top_level")

    _lines: tuple[str, ...]
    _top_level: tuple[bool, ...]

    _MARKERS = "`~"
    _MIN_RUN = 3
    _INDENT = 4

    def __new__(cls, text: str) -> Self:
        self = super().__new__(cls)
        lines = text.splitlines(keepends=True)
        self._lines = tuple(lines)
        self._top_level = cls._classify(lines)
        return self

    def contains(self, import_line: str) -> bool:
        """Return whether a top-level line equals *import_line* net of terminator.

        The comparison strips each physical line's trailing ``\\r``, ``\\n``, or
        ``\\r\\n`` before matching, so a CRLF or lone-CR host still matches the
        terminator-free canonical string (\\S2.4). A copy inside a code block is
        not top-level and never matches.
        """
        return any(
            top and self._matches(line, import_line) for line, top in self._pairs()
        )

    def without(self, import_line: str) -> str:
        """Return the document with every top-level *import_line* occurrence removed.

        Removes **every** top-level match, so a file that somehow carried two
        collapses to zero (\\S2.4 duplicate heal). Lines that are not removed are
        rejoined byte-for-byte, and an inert copy inside a code block is left
        untouched.
        """
        return "".join(
            line
            for line, top in self._pairs()
            if not (top and self._matches(line, import_line))
        )

    def with_appended(self, import_line: str) -> str:
        """Return the document with *import_line* appended as one bare top-level line.

        Ensures a separating terminator first, so the import is never glued to
        the user's last line, and uses the document's own EOL convention for both
        the separator and the appended line -- so the tool's line matches the
        surrounding endings and stays terminator-insensitively matchable on
        re-run (\\S2.4).
        """
        text = "".join(self._lines)
        eol = self._eol()
        if text and not text.endswith(("\n", "\r")):
            text += eol
        return f"{text}{import_line}{eol}"

    def _eol(self) -> str:
        """Return the document's line-ending convention, defaulting to ``\\n``.

        Each line kept by ``splitlines(keepends=True)`` carries its own
        terminator, so the first terminated line fixes the host convention;
        ``\\r\\n`` is tested before ``\\n`` because a CRLF line ends with both.
        """
        for line in self._lines:
            if line.endswith("\r\n"):
                return "\r\n"
            if line.endswith("\n"):
                return "\n"
            if line.endswith("\r"):
                return "\r"
        return "\n"

    def _pairs(self) -> Iterator[tuple[str, bool]]:
        """Yield each physical line paired with whether it is top-level."""
        return zip(self._lines, self._top_level, strict=True)

    @staticmethod
    def _matches(line: str, import_line: str) -> bool:
        """Match a physical line against *import_line*, net of its terminator."""
        return line.rstrip("\r\n") == import_line

    @classmethod
    def _classify(cls, lines: list[str]) -> tuple[bool, ...]:
        """Flag each line ``True`` when it is top-level (outside every code block)."""
        inside: set[int] = set()
        for open_idx, close_idx in cls._fenced_ranges(lines):
            # The content and the closing delimiter are inside; the opener is not.
            inside.update(range(open_idx + 1, close_idx + 1))
        return tuple(
            i not in inside and not cls._is_indented(line)
            for i, line in enumerate(lines)
        )

    @classmethod
    def _fenced_ranges(cls, lines: list[str]) -> list[tuple[int, int]]:
        """Return ``(open_idx, close_idx)`` pairs of matched fenced blocks.

        An opener of run length *N* closes only on a later same-marker delimiter
        of run ``>= N``; a shorter or mismatched delimiter inside the block is
        content. An unterminated opener yields no pair, so a dangling fence never
        swallows the rest of the file.
        """
        ranges: list[tuple[int, int]] = []
        open_at: int | None = None
        open_marker = ""
        open_len = 0
        for i, line in enumerate(lines):
            fence = cls._parse_fence(line)
            if open_at is None:
                if fence is not None:
                    open_at, (open_marker, open_len) = i, fence
            elif fence is not None and fence[0] == open_marker and fence[1] >= open_len:
                ranges.append((open_at, i))
                open_at = None
        return ranges

    @classmethod
    def _parse_fence(cls, line: str) -> tuple[str, int] | None:
        """Return ``(marker, run_length)`` if *line* is a fence delimiter, else None.

        ``None`` is the documented "not a fence line" contract (an absence, like
        ``dict.get``), not a failure to produce a value. A fence delimiter is a
        run of three or more of a single marker character after at most three
        leading spaces; a tab or four or more leading spaces makes the line inert
        indented code, never a delimiter (\\S2.4).
        """
        bare = line.rstrip("\r\n")
        if bare.startswith("\t"):
            return None
        stripped = bare.lstrip(" ")
        if len(bare) - len(stripped) >= cls._INDENT:
            return None
        if not stripped or stripped[0] not in cls._MARKERS:
            return None
        marker = stripped[0]
        run = len(stripped) - len(stripped.lstrip(marker))
        return (marker, run) if run >= cls._MIN_RUN else None

    @classmethod
    def _is_indented(cls, line: str) -> bool:
        """Return whether *line* is an indented-code line (tab or 4+ spaces)."""
        return line.startswith("\t") or len(line) - len(line.lstrip(" ")) >= cls._INDENT
