"""Own a single bare ``@``-import line inside a host ``CLAUDE.md``.

Claude Code loads a ``CLAUDE.md`` and resolves any top-level ``@path`` line as
an included file. A punt tool registers exactly one *bare* import line pointing
at a file it owns entirely -- ``@.punt-labs/vox/CLAUDE.md`` in a repo's
``CLAUDE.md`` (repo scope) or ``@~/.punt-labs/vox/CLAUDE.md`` in
``~/.claude/CLAUDE.md`` (user scope) -- so the guide loads with no per-project
edit. Composition happens at read time, when Claude Code resolves the import;
this module never merges, marks, or manages a section inside the user's file
(``tool-enable-disable.md`` § 2.1). Every byte outside the single import line is
preserved verbatim.

:class:`ClaudeMdImport` implements the full § 2.4 write contract. It composes an
:class:`~punt_vox.atomic_file.AtomicFile` for the atomic, symlink-resolving,
byte-preserving, mode-preserving read/write, and a
:class:`~punt_vox.markdown_doc.MarkdownDoc` for the top-level match
(terminator-insensitive, code-block-aware with balanced-pair fences and the
unterminated-opener guard), and a :class:`~punt_vox.sibling_lock.SiblingLock` to
serialize the read-modify-write against parallel invocations.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Self, final

from punt_vox.atomic_file import AtomicFile
from punt_vox.markdown_doc import MarkdownDoc
from punt_vox.sibling_lock import SiblingLock

if TYPE_CHECKING:
    from pathlib import Path

__all__ = ["ClaudeMdImport"]


@final
class ClaudeMdImport:
    """Registers or prunes one bare ``@``-import line in a host ``CLAUDE.md``.

    Bind the host file and the exact canonical import line at construction; the
    line is validated once at that boundary (:meth:`_validate`) because it is
    spliced into the host verbatim. :meth:`register` appends it if absent and
    :meth:`prune` removes every top-level match, both under an exclusive sibling
    lock so parallel invocations never lose an update.
    """

    __slots__ = ("_file", "_import_line", "_lock")

    _file: AtomicFile
    _import_line: str
    _lock: SiblingLock

    def __new__(cls, host_path: Path, import_line: str) -> Self:
        cls._validate(import_line)
        self = super().__new__(cls)
        self._file = AtomicFile(host_path)
        self._import_line = import_line
        self._lock = SiblingLock(host_path)
        return self

    @property
    def path(self) -> Path:
        """Return the host ``CLAUDE.md`` path (the symlink itself when it is one)."""
        return self._file.path

    @property
    def import_line(self) -> str:
        """Return the canonical ``@``-import line this instance owns."""
        return self._import_line

    def is_registered(self) -> bool:
        """Return whether the import line is present at top level.

        A pure read -- no lock needed, since every write lands atomically and a
        read therefore never observes a torn file.
        """
        return MarkdownDoc(self._file.read()).contains(self._import_line)

    def register(self) -> bool:
        """Append the import line if absent. Return ``True`` if the file changed.

        Idempotent by exact match net of terminator, top-level only: a line
        already present is a no-op, so re-running ``enable`` never duplicates it
        (``AppendImport`` 0->1->1).
        """
        with self._lock.held():
            doc = MarkdownDoc(self._file.read())
            if doc.contains(self._import_line):
                return False
            self._file.replace(doc.with_appended(self._import_line))
            return True

    def prune(self) -> bool:
        """Remove every top-level occurrence. Return ``True`` if the file changed.

        Collapses an accidental duplicate to zero (``RemoveImport`` 2->0) and
        leaves any inert copy inside a code block untouched.
        """
        with self._lock.held():
            text = self._file.read()
            new_text = MarkdownDoc(text).without(self._import_line)
            if new_text == text:
                return False
            self._file.replace(new_text)
            return True

    @staticmethod
    def _validate(import_line: str) -> None:
        """Raise ``ValueError`` unless *import_line* is a lone top-level ``@`` line.

        Validated at the construction boundary (PY-EH-1): the line is spliced
        into the host file verbatim, so a padded, multi-line, or non-``@`` value
        would inject a duplicate, a blank line, or inert markdown.
        """
        if not import_line or import_line.isspace():
            raise ValueError("import line must be non-empty")
        if "\n" in import_line or "\r" in import_line:
            raise ValueError(f"import line must be a single line: {import_line!r}")
        if import_line != import_line.strip():
            raise ValueError(
                f"import line must have no leading/trailing whitespace: {import_line!r}"
            )
        if not import_line.startswith("@"):
            raise ValueError(f"import line must begin with '@': {import_line!r}")
