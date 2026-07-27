"""Strip host paths from free-form diagnostic text bound for the wire.

Player stderr, an ``OSError`` string, and a generation reason all embed absolute
host paths -- the home directory and the username inside it. :class:`SafeText`
scans such a string for absolute-path tokens and rewrites each in place: an
in-jail path to its labeled relative form (``recordings/foo.mp3``) via
:func:`relativize_to_data_root`, an out-of-jail path to ``<path>``. The result is
length-capped so a runaway blob cannot bloat a reply. The raw text stays with the
caller for the host-local ``vox.log``.

A path token is an absolute POSIX path of two or more segments
(``/Users/name/foo``); a single-segment ``a/b`` word (``and/or``) is not a host
path and passes through untouched. Erring toward stripping is the safe direction
at a trust boundary: an unrecognized absolute path is redacted, never emitted.
"""

from __future__ import annotations

import re
from typing import Self, final

from punt_vox.voxd.data_root_boundary import relativize_to_data_root

__all__ = ["SafeText"]

# An absolute path of >= 2 segments: a leading "/", a first segment of non-space,
# non-quote, non-slash chars, then one or more "/segment" runs. The two-segment
# floor protects readable "a/b" words (a username leak is always multi-segment).
_ABSOLUTE_PATH = re.compile(r"/[^\s'\"/]+(?:/[^\s'\"]+)+")

_REDACTED = "<path>"

# The relativized text is capped at this length -- matching the playback stderr
# ceiling -- so an unbounded exception message cannot bloat a status reply.
_MAX_LEN = 2000


@final
class SafeText:
    """Free-form diagnostic text with every host path removed, safe for the wire.

    Built through :meth:`of`, which rewrites each absolute-path token and caps the
    length. The constructor takes the already-safe text; :meth:`of` is the only
    way to produce it from a raw string.
    """

    __slots__ = ("_text",)

    _text: str

    def __new__(cls, text: str) -> Self:
        self = super().__new__(cls)
        self._text = text
        return self

    @classmethod
    def of(cls, raw: str, *, cap: int = _MAX_LEN) -> Self:
        """Return *raw* with host paths relativized/stripped and length capped.

        Cap the *input* before the regex, not only the output: ``_ABSOLUTE_PATH``
        backtracks super-linearly, so scanning an uncapped 200k-char many-segment
        string would hang the daemon's event loop on the fault path. Bounding the
        scan to *cap* chars keeps the cost self-contained, independent of caller
        input discipline.
        """
        return cls(_ABSOLUTE_PATH.sub(cls._rewrite_token, cls._capped(raw, cap)))

    @property
    def text(self) -> str:
        """Return the prefix-free, length-capped text safe to send to a client."""
        return self._text

    def __str__(self) -> str:
        return self._text

    @staticmethod
    def _rewrite_token(match: re.Match[str]) -> str:
        """Return an in-jail token's relative form, else the redaction marker."""
        rel = relativize_to_data_root(match.group())
        return _REDACTED if rel is None else str(rel.path)

    @staticmethod
    def _capped(text: str, cap: int) -> str:
        """Return *text* clipped to *cap* chars, noting how many were dropped."""
        if len(text) <= cap:
            return text
        dropped = len(text) - cap
        return f"{text[:cap]}… [+{dropped} chars]"
