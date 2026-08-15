"""Tests for the deposited-guide source-hash stamp (``GuideStamp``)."""

from __future__ import annotations

import hashlib
from pathlib import Path

from punt_vox.guide_stamp import GuideStamp, GuideStampVerdict

# ---------------------------------------------------------------------------
# packaged_hash
# ---------------------------------------------------------------------------


def test_packaged_hash_is_sha256_of_bytes(tmp_path: Path) -> None:
    asset = tmp_path / "asset.md"
    asset.write_bytes(b"hello vox\n")
    stamp = GuideStamp(asset)
    assert stamp.packaged_hash() == hashlib.sha256(b"hello vox\n").hexdigest()


# ---------------------------------------------------------------------------
# stamped
# ---------------------------------------------------------------------------


def test_stamped_appends_tag_line_at_tail(tmp_path: Path) -> None:
    asset = tmp_path / "asset.md"
    asset.write_text("body text\n", encoding="utf-8")
    stamp = GuideStamp(asset)
    stamped = stamp.stamped("body text\n")
    # The whole asset lands verbatim first, then the stamp on its own line.
    assert stamped.startswith("body text\n")
    tail = stamped.removeprefix("body text\n")
    assert tail == f"<!-- vox-guide-source-sha256: {stamp.packaged_hash()} -->\n"


def test_stamped_pads_missing_final_newline(tmp_path: Path) -> None:
    # A packaged asset without a trailing newline must still stamp cleanly --
    # the stamp comment sits on its own line either way.
    asset = tmp_path / "asset.md"
    asset.write_text("no trailing newline", encoding="utf-8")
    stamp = GuideStamp(asset)
    stamped = stamp.stamped("no trailing newline")
    assert stamped == (
        "no trailing newline\n"
        f"<!-- vox-guide-source-sha256: {stamp.packaged_hash()} -->\n"
    )


# ---------------------------------------------------------------------------
# read
# ---------------------------------------------------------------------------


def test_read_returns_none_when_unstamped(tmp_path: Path) -> None:
    asset = tmp_path / "asset.md"
    asset.write_text("body\n", encoding="utf-8")
    deposited = tmp_path / "deposited.md"
    deposited.write_text("plain content, no stamp\n", encoding="utf-8")
    assert GuideStamp(asset).read(deposited) is None


def test_read_recovers_embedded_digest(tmp_path: Path) -> None:
    asset = tmp_path / "asset.md"
    asset.write_text("body\n", encoding="utf-8")
    stamp = GuideStamp(asset)
    deposited = tmp_path / "deposited.md"
    deposited.write_text(stamp.stamped("body\n"), encoding="utf-8")
    assert stamp.read(deposited) == stamp.packaged_hash()


def test_read_ignores_garbled_stamp(tmp_path: Path) -> None:
    # A tag with a non-hex payload does not match -- treated as absent-stamp.
    asset = tmp_path / "asset.md"
    asset.write_text("body\n", encoding="utf-8")
    deposited = tmp_path / "deposited.md"
    deposited.write_text(
        "body\n<!-- vox-guide-source-sha256: notahash -->\n",
        encoding="utf-8",
    )
    assert GuideStamp(asset).read(deposited) is None


# ---------------------------------------------------------------------------
# verify
# ---------------------------------------------------------------------------


def test_verify_agrees_on_fresh_deposit(tmp_path: Path) -> None:
    asset = tmp_path / "asset.md"
    asset.write_text("body\n", encoding="utf-8")
    stamp = GuideStamp(asset)
    deposited = tmp_path / "deposited.md"
    deposited.write_text(stamp.stamped("body\n"), encoding="utf-8")
    assert stamp.verify(deposited) is GuideStampVerdict.AGREE


def test_verify_diverges_after_asset_changes(tmp_path: Path) -> None:
    asset = tmp_path / "asset.md"
    asset.write_text("original body\n", encoding="utf-8")
    stamp = GuideStamp(asset)
    deposited = tmp_path / "deposited.md"
    deposited.write_text(stamp.stamped("original body\n"), encoding="utf-8")
    # Packaged asset drifts after the deposit was stamped.
    asset.write_text("new body\n", encoding="utf-8")
    assert stamp.verify(deposited) is GuideStampVerdict.DIVERGE


def test_verify_reports_absent_stamp(tmp_path: Path) -> None:
    asset = tmp_path / "asset.md"
    asset.write_text("body\n", encoding="utf-8")
    deposited = tmp_path / "deposited.md"
    deposited.write_text("unstamped body\n", encoding="utf-8")
    assert GuideStamp(asset).verify(deposited) is GuideStampVerdict.ABSENT_STAMP


# ---------------------------------------------------------------------------
# for_packaged_asset
# ---------------------------------------------------------------------------


def test_for_packaged_asset_resolves_shipped_guide() -> None:
    stamp = GuideStamp.for_packaged_asset()
    # A fresh hash of the shipped guide is 64 hex chars -- proves the path
    # under `assets/` is real and readable, without pinning a value that
    # changes every time the asset does.
    digest = stamp.packaged_hash()
    assert len(digest) == 64
    assert all(c in "0123456789abcdef" for c in digest)
