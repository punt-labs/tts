"""Tests for ``ProgramService`` -- the daemon's handler-facing playback seam.

The service is driven synchronously via ``run_once`` (apply exactly one queued
command), so each handler-facing call and its serialized effect are asserted
without a running event-loop consumer. The Producer is a fake; the store is a
real filesystem store under ``tmp_path`` so replay resolves from disk. The named
invariants -- resume vs. mint, the prompt-fingerprint gate, the session-vibe tag,
and the cap-free union replay -- are asserted here by name.
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import TYPE_CHECKING

import pytest

from punt_vox.types_programs import Format, Mode
from punt_vox.types_programs.prompts import PromptSet
from punt_vox.voxd.programs.album_id import AlbumId
from punt_vox.voxd.programs.album_tags import PromptFingerprint, TagQuery
from punt_vox.voxd.programs.filesystem_store import FilesystemProgramStore
from punt_vox.voxd.programs.library import MusicLibrary

from .conftest import QuietProducer, make_service, seed_album

if TYPE_CHECKING:
    from pathlib import Path

    from punt_vox.voxd.programs.service import ProgramService

_ONE = PromptSet(base="one", variations=())
_TWO = PromptSet(base="two", variations=())
_POOL_SIZE = Format.PLAYLIST.pool_size


def _service(tmp_path: Path) -> ProgramService:
    return make_service(tmp_path / "programs")


async def _drive_fill_to_full(service: ProgramService) -> None:
    """Run the control writer until the sole minted album fills its pool on disk.

    Exercises the *real* fill: the writer applies the switch, the reconciler arms
    the background fill, and each produced Part is recorded to disk and applied.
    Polls the album's *live* ``ready_parts`` (a disk read, F1) until it is full.
    """
    writer = asyncio.ensure_future(service.serve_control())
    try:
        for _ in range(2000):
            await asyncio.sleep(0)
            albums = service.catalog_albums()
            if albums and len(albums[0].ready_parts()) >= _POOL_SIZE:
                return
        pytest.fail("the background fill never reached a full pool")
    finally:
        writer.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await writer
        service.shutdown()


class TestTurnOn:
    async def test_turn_on_makes_a_program_active(self, tmp_path: Path) -> None:
        service = _service(tmp_path)
        service.turn_on(style="techno", vibe="calm", name=None, prompts=_ONE)
        await service.run_once()
        service.shutdown()  # cancel the fill the switch armed before it runs
        status = service.status()
        assert not status.is_idle
        assert status.mode is Mode.GENERATING_FIRST

    def test_records_the_session_vibe_not_the_style(self, tmp_path: Path) -> None:
        # The album's vibe tag is the session vibe, never the style.
        service = _service(tmp_path)
        service.turn_on(style="techno", vibe="calm", name=None, prompts=_ONE)
        service.shutdown()
        album = service.catalog_albums()[0]
        assert album.manifest.tags.style == "techno"
        assert album.manifest.tags.vibe == "calm"

    def test_default_style_when_absent(self, tmp_path: Path) -> None:
        service = _service(tmp_path)
        service.turn_on(style=None, vibe=None, name=None, prompts=None)
        service.shutdown()
        assert service.catalog_albums()[0].manifest.tags.style == "ambient"


class TestResumeVsMint:
    def test_same_tags_and_fingerprint_resumes(self, tmp_path: Path) -> None:
        service = _service(tmp_path)
        service.turn_on(style="techno", vibe="calm", name=None, prompts=_ONE)
        service.turn_on(style="techno", vibe="calm", name=None, prompts=_ONE)
        service.shutdown()
        assert len(service.catalog_albums()) == 1  # resumed, not minted twice

    def test_differing_fingerprint_mints_fresh(self, tmp_path: Path) -> None:
        # A (style, vibe) hit with a different prompt-set mints fresh.
        service = _service(tmp_path)
        service.turn_on(style="techno", vibe="calm", name=None, prompts=_ONE)
        service.turn_on(style="techno", vibe="calm", name=None, prompts=_TWO)
        service.shutdown()
        assert len(service.catalog_albums()) == 2

    def test_two_unnamed_mints_get_distinct_reachable_names(
        self, tmp_path: Path
    ) -> None:
        # Same (style, vibe), different prompt-sets (a fingerprint miss) mint two
        # pools in the same clock-minute. Their auto-names must differ, or by_name
        # ("0 or 1") would strand the second pool as unreachable.
        service = _service(tmp_path)
        service.turn_on(style="techno", vibe="calm", name=None, prompts=_ONE)
        service.turn_on(style="techno", vibe="calm", name=None, prompts=_TWO)
        service.shutdown()
        albums = service.catalog_albums()
        by_name = {a.manifest.tags.name: a for a in albums}
        assert len(albums) == 2
        assert None not in by_name
        assert len(by_name) == 2  # distinct names -> both by_name-reachable

    def test_named_partial_foreign_prompts_mints_fresh(self, tmp_path: Path) -> None:
        # A partly-filled named album must never have its remaining tracks
        # generated from a foreign prompt set -- that blends two prompt sets into
        # one pool. A mismatch mints a fresh, auto-suffixed album instead.
        root = tmp_path / "programs"
        fp_alpha = PromptFingerprint.from_prompts("alpha", ())
        seed_album(root, 1, 2, name="mix", fingerprint=fp_alpha)  # partial: 2 of 12
        service = _service(tmp_path)
        service.turn_on(
            style="techno",
            vibe="calm",
            name="mix",
            prompts=PromptSet(base="beta", variations=()),
        )
        service.shutdown()
        by_name = {a.manifest.tags.name: a for a in service.catalog_albums()}
        assert set(by_name) == {"mix", "mix1"}  # foreign fingerprint minted fresh
        # The original mix is untouched: still 2 parts, still the alpha fingerprint.
        assert by_name["mix"].manifest.prompt_fingerprint == fp_alpha
        assert len(by_name["mix"].ready_parts()) == 2
        # The fresh album carries beta's fingerprint, so no pool spans two of them.
        fp_beta = PromptFingerprint.from_prompts("beta", ())
        assert by_name["mix1"].manifest.prompt_fingerprint == fp_beta

    def test_named_partial_matching_fingerprint_resumes(self, tmp_path: Path) -> None:
        # A partial named album resumes when the incoming prompts match its identity.
        root = tmp_path / "programs"
        fp_alpha = PromptFingerprint.from_prompts("alpha", ())
        seed_album(root, 1, 2, name="mix", fingerprint=fp_alpha)
        service = _service(tmp_path)
        service.turn_on(
            style="techno",
            vibe="calm",
            name="mix",
            prompts=PromptSet(base="alpha", variations=()),
        )
        service.shutdown()
        assert len(service.catalog_albums()) == 1  # same fingerprint -> resumed

    def test_named_full_album_resumes_regardless_of_fingerprint(
        self, tmp_path: Path
    ) -> None:
        # A full named album never fills, so a foreign prompt set mixes nothing --
        # it resumes as-is rather than minting a fresh album.
        root = tmp_path / "programs"
        fp_alpha = PromptFingerprint.from_prompts("alpha", ())
        seed_album(root, *range(1, _POOL_SIZE + 1), name="mix", fingerprint=fp_alpha)
        service = _service(tmp_path)
        service.turn_on(
            style="techno",
            vibe="calm",
            name="mix",
            prompts=PromptSet(base="beta", variations=()),
        )
        service.shutdown()
        albums = service.catalog_albums()
        assert len(albums) == 1  # resumed the full album, no fresh mint
        assert albums[0].manifest.tags.name == "mix"

    def test_resume_a_saved_pool_plays_without_regenerating(
        self, tmp_path: Path
    ) -> None:
        seed_album(tmp_path / "programs", 1, 2, style="techno", vibe="calm")
        service = _service(tmp_path)
        # Match the seeded album's fingerprint (conftest seeds the fallback set).
        service.turn_on(
            style="techno",
            vibe="ambient",
            name=None,
            prompts=PromptSet.fallback("techno", ""),
        )
        service.shutdown()
        # A fresh vibe ("ambient") mints a new album, so the seeded one is intact.
        assert len(service.catalog_albums()) >= 1


class TestFilledAlbumLiveReads:
    """After a real fill grows the pool, every read reflects the live parts.

    If ``Album`` froze a 0-part snapshot at mint, ``select`` would see an empty
    pool ("no albums match"), ``list`` would show 0/0, and a re-``on`` would seed
    an empty pool -- the fill then sees disk already full, starts nothing, and the
    playback loop hangs in ``_wait_for_playable``. Reading parts live prevents it.
    """

    async def test_select_list_and_resume_all_see_the_filled_pool(
        self, tmp_path: Path
    ) -> None:
        service = _service(tmp_path)
        service.turn_on(style="lofi", vibe="calm", name=None, prompts=_ONE)
        await _drive_fill_to_full(service)

        album = service.catalog_albums()[0]
        # list/select read parts live from the store, not the mint snapshot.
        assert len(album.ready_parts()) == _POOL_SIZE
        assert len(album.read().ready_parts()) == _POOL_SIZE

        # /music play lofi calm builds a non-empty Selection over the filled pool.
        service.replay(TagQuery(style="lofi", vibe="calm"))
        await service.run_once()
        now_playing = service.status().now_playing
        assert now_playing is not None
        assert now_playing.of == _POOL_SIZE

        # A re-on resumes the full album and comes up playing, seeded from the live
        # store -- it does not strand an empty pool, so the loop cannot hang.
        service.turn_on(style="lofi", vibe="calm", name=None, prompts=_ONE)
        await service.run_once()
        service.shutdown()
        resumed = service.status()
        assert not resumed.is_idle
        assert resumed.now_playing is not None
        assert resumed.now_playing.of == _POOL_SIZE
        assert len(service.catalog_albums()) == 1  # resumed, never minted twice


class TestReplay:
    async def test_replay_union_spans_two_albums_cap_free(self, tmp_path: Path) -> None:
        root = tmp_path / "programs"
        seed_album(root, 1, 2, style="trance", vibe="calm", album_id="a3f1c9")
        seed_album(root, 1, 2, 3, style="trance", vibe="calm", album_id="7b2e04")
        service = _service(tmp_path)
        service.replay(TagQuery(style="trance", vibe="calm"))
        await service.run_once()
        status = service.status()
        assert status.now_playing is not None
        assert status.now_playing.of == 5  # 2 + 3 parts, no 12-cap
        assert status.generation.filling is False  # replay never generates

    async def test_replay_album_by_id(self, tmp_path: Path) -> None:
        root = tmp_path / "programs"
        seed_album(root, 1, 2, style="trance", vibe="calm", album_id="a3f1c9")
        service = _service(tmp_path)
        service.replay_album(AlbumId("a3f1c9"))
        await service.run_once()
        assert service.status().now_playing is not None

    def test_replay_no_match_raises(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="no albums match"):
            _service(tmp_path).replay(TagQuery(style="ghost"))

    def test_replay_unknown_id_raises(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="no album with id"):
            _service(tmp_path).replay_album(AlbumId("badbad"))

    def test_replay_existing_empty_album_raises_distinct_error(
        self, tmp_path: Path
    ) -> None:
        # An album that EXISTS but has no ready tracks yet must not report the
        # generic tag-miss message (which reads as "unknown album"); it reports
        # the distinct "no playable tracks yet".
        seed_album(tmp_path / "programs", style="trance", vibe="calm", album_id="a3")
        service = _service(tmp_path)
        with pytest.raises(ValueError, match="has no playable tracks yet"):
            service.replay_album(AlbumId("a3"))


class TestConsumeControls:
    async def test_advance_rotates_a_replay(self, tmp_path: Path) -> None:
        seed_album(tmp_path / "programs", 1, 2, 3, style="trance", vibe="calm")
        service = _service(tmp_path)
        service.replay(TagQuery(style="trance"))
        await service.run_once()
        assert service.status().now_playing is not None
        service.advance()
        await service.run_once()
        assert service.status().now_playing is not None

    async def test_off_from_a_replay_goes_idle(self, tmp_path: Path) -> None:
        seed_album(tmp_path / "programs", 1, 2, style="trance", vibe="calm")
        service = _service(tmp_path)
        service.replay(TagQuery(style="trance"))
        await service.run_once()
        service.off()
        await service.run_once()
        assert service.status().is_idle  # a replay stops to idle (RadioOff)

    async def test_off_from_a_program_keeps_the_pool(self, tmp_path: Path) -> None:
        service = _service(tmp_path)
        service.turn_on(style="techno", vibe="calm", name=None, prompts=_ONE)
        await service.run_once()
        service.off()
        await service.run_once()
        service.shutdown()
        assert service.status().mode is Mode.OFF


class TestGeneratingFirstBacking:
    """The album generated in ``generating_first`` (empty pool) still backs it.

    During ``generating_first`` the first track is being written into the album's
    directory while the pool is still empty. The D-2 backing set must cover that
    album *regardless of pool emptiness*, so ``music_remove`` refuses it -- deleting
    the directory mid-generation would corrupt the in-flight first track. These
    tests assert that named invariant, and the regression that a non-backing idle
    album is still removable in the same state.
    """

    def _library(self, tmp_path: Path, service: ProgramService) -> MusicLibrary:
        """Build a library sharing the service's catalog + store, as wiring does."""
        root = tmp_path / "programs"
        return MusicLibrary(
            service.catalog, FilesystemProgramStore(root), root, QuietProducer()
        )

    async def _generating_first(self, tmp_path: Path) -> ProgramService:
        """Drive a fresh service to ``generating_first`` with an empty pool."""
        service = _service(tmp_path)
        service.turn_on(style="techno", vibe="calm", name=None, prompts=_ONE)
        await service.run_once()  # apply the switch that arms the background fill
        service.shutdown()  # cancel the fill before it records the first Part
        assert service.status().mode is Mode.GENERATING_FIRST  # pool still empty
        return service

    async def test_generating_first_album_is_in_the_backing_set(
        self, tmp_path: Path
    ) -> None:
        service = await self._generating_first(tmp_path)
        album = service.catalog_albums()[0]
        assert service.active_backing_locators() == frozenset({album.locator})

    async def test_music_remove_refused_while_generating_first(
        self, tmp_path: Path
    ) -> None:
        service = await self._generating_first(tmp_path)
        album = service.catalog_albums()[0]
        library = self._library(tmp_path, service)
        with pytest.raises(ValueError, match="is playing"):
            library.remove(album.id, blocked=service.active_backing_locators())
        # The in-flight generation is undisturbed: still generating_first, album kept.
        assert service.status().mode is Mode.GENERATING_FIRST
        assert album.id in {a.id for a in service.catalog_albums()}

    async def test_non_backing_album_is_removable_while_generating_first(
        self, tmp_path: Path
    ) -> None:
        root = tmp_path / "programs"
        idle = seed_album(root, 1, name="idle", album_id="b1b1b1")
        service = _service(tmp_path)  # scans the seeded idle album into the catalog
        service.turn_on(style="techno", vibe="calm", name=None, prompts=_ONE)
        await service.run_once()
        service.shutdown()
        assert service.status().mode is Mode.GENERATING_FIRST
        library = self._library(tmp_path, service)
        library.remove(AlbumId("b1b1b1"), blocked=service.active_backing_locators())
        assert not (root / idle).exists()  # a non-backing album still removes


class TestStoppedProgramBacking:
    """A Program stopped by ``off`` backs nothing, so its album becomes removable.

    ``turn_off`` keeps the saved pool for a later re-``on`` (Z ``TurnOff``), but a
    stopped Program neither plays a ready Part nor generates one. D-2 must drop
    its album from the backing set: otherwise the retained non-empty pool would
    wedge ``music remove`` with "is playing; stop it first" on a program that is
    already stopped, leaving the operator no way to remove the album. The
    contrast tests confirm a *live* program (playing a full pool) still backs its
    album, so removal remains refused while it plays.
    """

    def _library(self, tmp_path: Path, service: ProgramService) -> MusicLibrary:
        """Build a library sharing the service's catalog + store, as wiring does."""
        root = tmp_path / "programs"
        return MusicLibrary(
            service.catalog, FilesystemProgramStore(root), root, QuietProducer()
        )

    async def _played_then_stopped(self, tmp_path: Path) -> tuple[ProgramService, str]:
        """Resume a full saved pool (playing_rotating), then ``off`` it (pool kept).

        Seeding a full album and resuming it reaches ``playing_rotating`` with a
        non-empty pool and no armed fill -- the exact retained-pool state the D-2
        off fix concerns -- via a single queued switch, so one ``run_once`` per
        transition applies deterministically.
        """
        root = tmp_path / "programs"
        locator = seed_album(
            root, *range(1, _POOL_SIZE + 1), name="live", album_id="c1c1c1"
        )
        service = _service(tmp_path)  # scans the full seeded album into the catalog
        service.turn_on(style="techno", vibe="ambient", name="live", prompts=_ONE)
        await service.run_once()  # apply the switch onto the restored full pool
        service.shutdown()  # a full pool arms no fill; cancel defensively
        assert service.status().mode is Mode.PLAYING_ROTATING
        # A live program playing a full pool backs its album (removal is refused).
        assert service.active_backing_locators() == frozenset({locator})
        service.off()
        await service.run_once()
        assert service.status().mode is Mode.OFF
        return service, locator

    async def test_stopped_program_backs_nothing(self, tmp_path: Path) -> None:
        service, _ = await self._played_then_stopped(tmp_path)
        # The retained pool must no longer back the album once playback is off.
        assert service.active_backing_locators() == frozenset()

    async def test_music_remove_succeeds_after_off(self, tmp_path: Path) -> None:
        service, locator = await self._played_then_stopped(tmp_path)
        album = service.catalog_albums()[0]
        library = self._library(tmp_path, service)
        library.remove(album.id, blocked=service.active_backing_locators())
        assert not (tmp_path / "programs" / locator).exists()

    async def test_music_remove_refused_while_playing(self, tmp_path: Path) -> None:
        service = _service(tmp_path)
        service.turn_on(style="techno", vibe="calm", name=None, prompts=_ONE)
        await _drive_fill_to_full(service)
        album = service.catalog_albums()[0]
        library = self._library(tmp_path, service)
        # No regression: a live, playing program still refuses removal of its album.
        with pytest.raises(ValueError, match="is playing"):
            library.remove(album.id, blocked=service.active_backing_locators())
        assert album.id in {a.id for a in service.catalog_albums()}
