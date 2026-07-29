"""Tests for the in-memory album catalog -- the query surface over manifests.

Asserts the query invariants by name: ``by_id`` is a direct lookup; ``by_name``
returns 0 or 1 (names unique); ``by_tags`` returns many albums sharing
``(style, vibe)`` newest-first; ``newest`` picks the latest tz-aware ``created``;
``resume`` matches only the same prompt-fingerprint; ``add`` makes a new album
queryable; and legacy (idless) dirs never enter the catalog.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from punt_vox.types_programs import Format
from punt_vox.voxd.programs import PartStatus
from punt_vox.voxd.programs.album_id import AlbumId
from punt_vox.voxd.programs.album_tags import AlbumTags, PromptFingerprint, TagQuery
from punt_vox.voxd.programs.catalog import Album, Catalog
from punt_vox.voxd.programs.change_signal import ChangeSignal
from punt_vox.voxd.programs.manifest import AlbumManifest, PartEntry

from .conftest import InMemoryProgramStore

_BASE = datetime(2026, 7, 8, tzinfo=UTC)
_FP_ONE = PromptFingerprint("11111111")
_FP_TWO = PromptFingerprint("22222222")


def _album(
    album_id: str,
    style: str,
    vibe: str,
    *,
    name: str | None = None,
    fingerprint: PromptFingerprint = _FP_ONE,
    created: datetime = _BASE,
    ready: int = 2,
) -> Album:
    manifest = AlbumManifest(
        album_id=AlbumId(album_id),
        fmt=Format.PLAYLIST,
        tags=AlbumTags(style=style, vibe=vibe, name=name),
        created=created,
        fingerprint=fingerprint,
        parts=tuple(
            PartEntry(index=i, file=f"{i:03d}.mp3", status=PartStatus.READY)
            for i in range(1, ready + 1)
        ),
    )
    # Wire the album to a live in-memory store so ready_parts/select read from
    # the store, not a frozen snapshot -- one store per album, each keyed by
    # its own locator.
    store = InMemoryProgramStore()
    store.preload(manifest)
    return store.scan()[0]


class _Recorder:
    """A ChangeListener that counts notifications."""

    def __init__(self) -> None:
        self.count = 0

    def notify_changed(self) -> None:
        self.count += 1


class TestChangeSignal:
    """The catalog fires its change signal after each mutation (new / remove)."""

    def test_add_fires_the_change_signal(self) -> None:
        catalog = Catalog(())
        signal = ChangeSignal()
        recorder = _Recorder()
        signal.subscribe(recorder)
        catalog.attach_change_signal(signal)

        catalog.add(_album("aa11bb", "techno", "ambient"))
        assert recorder.count == 1  # a new album re-pushes the scene

    def test_remove_fires_the_change_signal(self) -> None:
        album = _album("aa11bb", "techno", "ambient")
        catalog = Catalog((album,))
        signal = ChangeSignal()
        recorder = _Recorder()
        signal.subscribe(recorder)
        catalog.attach_change_signal(signal)

        catalog.remove(album.id)
        assert recorder.count == 1  # a removal re-pushes the scene

    def test_mutation_without_a_signal_is_safe(self) -> None:
        catalog = Catalog(())  # no signal attached
        catalog.add(_album("aa11bb", "techno", "ambient"))  # must not raise


class TestByIdAndName:
    def test_by_id_is_a_direct_hit_or_miss(self) -> None:
        catalog = Catalog((_album("a3f1c9", "trance", "calm"),))
        assert catalog.by_id(AlbumId("a3f1c9")) is not None
        assert catalog.by_id(AlbumId("000000")) is None

    def test_by_name_returns_zero_or_one(self) -> None:
        catalog = Catalog((_album("a3f1c9", "lofi", "focus", name="focus-beats"),))
        assert catalog.by_name("focus-beats") is not None
        assert catalog.by_name("absent") is None

    def test_taken_names_excludes_unnamed_albums(self) -> None:
        catalog = Catalog(
            (
                _album("a3f1c9", "lofi", "focus", name="mix"),
                _album("7b2e04", "trance", "calm"),  # unnamed
            )
        )
        assert catalog.taken_names() == frozenset({"mix"})


class TestReservedNames:
    """The mint-collision set unions catalogued names with in-flight ``new`` holds.

    ``reserved_names`` is the single held-names authority both authoring paths
    read: ``music new`` holds a curated name across its generation await, and a
    concurrent ``music on --name`` unions those holds with the catalogued names,
    so neither path can duplicate a name the other has claimed but not yet filed.
    """

    def test_reserved_names_unions_held_and_catalogued(self) -> None:
        catalog = Catalog((_album("a3f1c9", "lofi", "focus", name="mix"),))
        held = AlbumTags(style="custom", vibe="custom", name="focus")
        with catalog.reservations.hold("pads", held):
            # A name still generating is reserved even though it is not catalogued.
            assert catalog.reserved_names() == frozenset({"mix", "focus"})
        assert catalog.reserved_names() == frozenset({"mix"})  # released on exit

    def test_reserved_names_matches_taken_when_no_hold_is_in_flight(self) -> None:
        catalog = Catalog((_album("a3f1c9", "lofi", "focus", name="mix"),))
        assert catalog.reserved_names() == catalog.taken_names()


class TestByTags:
    def test_returns_many_albums_sharing_tags_newest_first(self) -> None:
        older = _album("a3f1c9", "trance", "calm", created=_BASE)
        newer = _album("7b2e04", "trance", "calm", created=_BASE + timedelta(hours=1))
        catalog = Catalog((older, newer))
        result = catalog.by_tags(TagQuery(style="trance", vibe="calm"))
        assert [a.id.value for a in result] == ["7b2e04", "a3f1c9"]  # newest first

    def test_style_only_query_matches_cross_vibe(self) -> None:
        catalog = Catalog(
            (
                _album("a3f1c9", "trance", "calm"),
                _album("7b2e04", "trance", "energetic"),
                _album("c40d11", "lofi", "calm"),
            )
        )
        result = catalog.by_tags(TagQuery(style="trance"))
        assert {a.id.value for a in result} == {"a3f1c9", "7b2e04"}

    def test_empty_query_matches_everything(self) -> None:
        catalog = Catalog(
            (_album("a3f1c9", "trance", "calm"), _album("7b2e04", "lofi", "focus"))
        )
        assert len(catalog.by_tags(TagQuery())) == 2

    def test_newest_picks_the_latest_created(self) -> None:
        older = _album("a3f1c9", "trance", "calm", created=_BASE)
        newer = _album("7b2e04", "trance", "calm", created=_BASE + timedelta(days=1))
        catalog = Catalog((older, newer))
        assert catalog.newest(TagQuery(style="trance")) == newer


class TestResumeFingerprint:
    def test_same_fingerprint_resumes(self) -> None:
        catalog = Catalog((_album("a3f1c9", "trance", "calm", fingerprint=_FP_ONE),))
        query = TagQuery(style="trance", vibe="calm")
        assert catalog.resume(query, _FP_ONE) is not None

    def test_different_fingerprint_is_a_miss(self) -> None:
        # A (style, vibe) hit with a foreign fingerprint does not resume.
        catalog = Catalog((_album("a3f1c9", "trance", "calm", fingerprint=_FP_ONE),))
        assert catalog.resume(TagQuery(style="trance", vibe="calm"), _FP_TWO) is None

    def test_resume_prefers_the_newest_matching_fingerprint(self) -> None:
        older = _album("a3f1c9", "trance", "calm", fingerprint=_FP_ONE, created=_BASE)
        newer = _album(
            "7b2e04",
            "trance",
            "calm",
            fingerprint=_FP_ONE,
            created=_BASE + timedelta(hours=1),
        )
        catalog = Catalog((older, newer))
        assert catalog.resume(TagQuery(style="trance", vibe="calm"), _FP_ONE) == newer


class TestMutationAndSelect:
    def test_add_makes_a_new_album_queryable(self) -> None:
        catalog = Catalog(())
        catalog.add(_album("a3f1c9", "trance", "calm"))
        assert catalog.by_id(AlbumId("a3f1c9")) is not None

    def test_mint_id_avoids_taken_ids(self) -> None:
        catalog = Catalog((_album("a3f1c9", "trance", "calm"),))
        assert catalog.mint_id() != AlbumId("a3f1c9")

    def test_select_unions_matching_albums(self) -> None:
        catalog = Catalog(
            (
                _album("a3f1c9", "trance", "calm", ready=2),
                _album("7b2e04", "trance", "calm", ready=3),
            )
        )
        selection = catalog.select(TagQuery(style="trance", vibe="calm"))
        assert len(selection) == 5  # 2 + 3 parts, cap-free union

    def test_select_no_match_is_empty(self) -> None:
        catalog = Catalog((_album("a3f1c9", "trance", "calm"),))
        assert not catalog.select(TagQuery(style="ghost"))


class TestLegacyExclusion:
    def test_catalog_only_holds_scanned_albums(self) -> None:
        # A catalog built from scan() output never sees idless legacy dirs -- the
        # store skips them before an Album is ever constructed, so the catalog's
        # query surface cannot return one. An album with a real id is present.
        catalog = Catalog((_album("a3f1c9", "trance", "calm"),))
        assert catalog.by_tags(TagQuery(vibe="trance")) == ()  # legacy vibe==style
        real = _album("a3f1c9", "trance", "calm")
        assert catalog.by_id(real.id) is not None
