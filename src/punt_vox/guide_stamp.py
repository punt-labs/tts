"""Content-hash stamp for the deposited vox agent guide.

The deposited guide -- ``~/.punt-labs/vox/CLAUDE.md`` for the per-user
:class:`~punt_vox.guidance.VoxGuidance` install, and the per-repo
``.punt-labs/vox/CLAUDE.md`` for :class:`~punt_vox.deposited_files.DepositedGuide`
-- is a copy of the packaged asset ``src/punt_vox/assets/global-guidance.md``.
Nothing on disk previously told a reader which version of the packaged asset the
deposited copy was written from, so the deposited copy could rot silently every
time the packaged asset changed without ``enable`` being re-run. Agents then
acted on documentation for tools that no longer existed.

:class:`GuideStamp` closes that gap. A stamp is the hex SHA-256 digest of the
packaged asset bytes, embedded as an HTML comment at the tail of the deposited
file -- invisible to a Markdown reader, greppable in a plain-text tool. The
deposit path calls :meth:`stamped` to append the current digest; a health check
calls :meth:`verify` to compare the embedded digest against a fresh hash of the
packaged asset. Divergence surfaces as a concrete verdict instead of as broken
agent behaviour weeks later.

A content hash is preferred over a version string because the packaged asset
can and does change *within* a release; a string that only bumps on release
would go stale mid-cycle in exactly the way this guards against.
"""

from __future__ import annotations

import hashlib
import re
from enum import Enum, unique
from pathlib import Path
from typing import Self, final

__all__ = ["GuideStamp", "GuideStampVerdict"]


@unique
class GuideStampVerdict(Enum):
    """Outcome of comparing a deposited guide's stamp to the packaged asset.

    ``ABSENT_STAMP`` is a distinct third case, not a false pass and not a fail:
    every deposit written before this stamping existed carries no marker, so the
    honest reading is "unknown, re-run ``vox enable``" -- callers surface it as
    its own line, not as agreement.
    """

    AGREE = "agree"
    DIVERGE = "diverge"
    ABSENT_STAMP = "absent_stamp"


@final
class GuideStamp:
    """Read, write, and verify the content-hash stamp on the deposited guide.

    Bind the packaged asset path at construction; :meth:`stamped` returns the
    text that should be written to the deposited copy, and :meth:`verify`
    inspects a deposited copy and returns which of the three
    :class:`GuideStampVerdict` states holds.
    """

    __slots__ = ("_packaged",)

    _packaged: Path

    _STAMP_TAG = "vox-guide-source-sha256"
    # A dedicated regex is easier to reason about than string slicing when the
    # packaged asset itself later grows a trailing HTML comment for some other
    # reason -- the search anchors on the tag, not on file position.
    _STAMP_RE = re.compile(
        rf"<!--\s*{re.escape(_STAMP_TAG)}:\s*([0-9a-f]{{64}})\s*-->",
    )

    def __new__(cls, packaged: Path) -> Self:
        self = super().__new__(cls)
        self._packaged = packaged
        return self

    @classmethod
    def for_packaged_asset(cls) -> Self:
        """Wire the real packaged-asset path bundled beside this package."""
        asset = Path(__file__).resolve().parent / "assets" / "global-guidance.md"
        return cls(asset)

    def packaged_hash(self) -> str:
        """Return the hex SHA-256 of the packaged asset bytes."""
        return hashlib.sha256(self._packaged.read_bytes()).hexdigest()

    def stamped(self, asset_text: str) -> str:
        """Return *asset_text* with the source-hash stamp appended.

        The stamp is one HTML-comment line at the tail so a Markdown renderer
        shows nothing and a diff against the packaged asset differs only in the
        trailing lines. A missing final newline in *asset_text* is padded so the
        stamp always sits on its own line -- required by :meth:`read`, which
        anchors on the tag rather than on position but reads cleaner this way.
        """
        digest = self.packaged_hash()
        suffix = "" if asset_text.endswith("\n") else "\n"
        return f"{asset_text}{suffix}<!-- {self._STAMP_TAG}: {digest} -->\n"

    def read(self, deposited: Path) -> str | None:
        """Return the embedded digest from *deposited*, or ``None`` if unstamped.

        ``None`` is the documented contract for a deposited guide whose stamp
        cannot be recovered -- a copy written before this stamping existed, a
        hand-edited copy, one with a garbled tail, one whose bytes are not
        valid UTF-8, or one the process cannot read at all (permission-denied,
        vanished mid-check). Callers surface this as the ``absent stamp``
        verdict; it is reported state, not an error to swallow. A tool whose
        job is to say a file is in a bad state must never be the thing that
        crashes on that same bad state -- so a broken deposit lands on the
        same code path a merely unstamped one does.
        """
        try:
            text = deposited.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return None
        match = self._STAMP_RE.search(text)
        return None if match is None else match.group(1)

    def verify(self, deposited: Path) -> GuideStampVerdict:
        """Compare *deposited*'s embedded stamp to a fresh packaged hash."""
        stamped_digest = self.read(deposited)
        if stamped_digest is None:
            return GuideStampVerdict.ABSENT_STAMP
        if stamped_digest == self.packaged_hash():
            return GuideStampVerdict.AGREE
        return GuideStampVerdict.DIVERGE
