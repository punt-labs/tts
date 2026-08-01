"""Tests for ``MusicLibrary`` -- catalog authoring, manifest, remove, part resolve.

The library shares one live ``Catalog`` and ``ProgramStore`` with the
``ProgramService`` (a real filesystem store under ``tmp_path``; a fake Producer),
so these tests assert both the wire-op behaviour and the Z model's Catalog/System
delta by name: ``MusicNew`` frames the running Program unchanged;
``MusicNewBadPrompt`` never fails the Program; ``MusicRemove`` refuses an album
backing the active source; every live Part stays catalogued (F5).
"""

from __future__ import annotations

import asyncio
import shutil
from typing import TYPE_CHECKING, NamedTuple, Self, final

import pytest

from punt_vox.types_programs import Format
from punt_vox.types_programs.mode import Mode
from punt_vox.voxd.programs.album_id import AlbumId
from punt_vox.voxd.programs.album_tags import AlbumTags, PromptFingerprint
from punt_vox.voxd.programs.catalog import Catalog
from punt_vox.voxd.programs.filesystem_store import FilesystemProgramStore
from punt_vox.voxd.programs.library import MusicLibrary
from punt_vox.voxd.programs.manifest import ManifestDraft, PartEntry
from punt_vox.voxd.programs.part import Part, PartStatus
from punt_vox.voxd.programs.producer import (
    PartSpec,
    ProducerBadInputError,
    ProducerTransientError,
)
from punt_vox.voxd.programs.service import ProgramService

from .conftest import FakeSleeper, QuietProducer, seed_album

if TYPE_CHECKING:
    from pathlib import Path

    from punt_vox.voxd.programs.catalog import Album
    from punt_vox.voxd.programs.manifest import AlbumManifest
    from punt_vox.voxd.programs.producer import Producer
    from punt_vox.voxd.programs.store import PartStore, ProgramStore

_POOL_SIZE = Format.PLAYLIST.pool_size
_HOSTILE_PARTS = ["/etc/passwd", "../../../etc/x", "a/b.mp3", "..", "", "n\x00.mp3"]
_HOSTILE_IDENTITIES = ["../../../etc/passwd", "a/b.mp3", ".."]


@final
class _BadPromptProducer:
    """A Producer that always rejects permanently (a ``bad_prompt``/ToS refusal)."""

    __slots__ = ()

    async def produce(self, spec: PartSpec, target: Path) -> Part:
        del spec, target
        raise ProducerBadInputError("bad_prompt: rejected by provider")


@final
class _TransientProducer:
    """A Producer that always fails transiently (429/5xx)."""

    __slots__ = ()

    async def produce(self, spec: PartSpec, target: Path) -> Part:
        del spec, target
        raise ProducerTransientError("429 rate limited")


@final
class _GatedProducer:
    """A Producer whose ``produce`` blocks on a gate.

    Lets a test hold one ``new`` inside its generation await while a second
    concurrent ``new`` runs, exercising the reserve-before-await TOCTOU guard.
    """

    __slots__ = ("_gate", "entered")
    _gate: asyncio.Event
    entered: asyncio.Event

    def __new__(cls, gate: asyncio.Event) -> Self:
        self = super().__new__(cls)
        self._gate = gate
        self.entered = asyncio.Event()
        return self

    async def produce(self, spec: PartSpec, target: Path) -> Part:
        self.entered.set()
        await self._gate.wait()
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"audio")
        return Part(target.name, spec.index)


class _Fx(NamedTuple):
    """A service + a library sharing one catalog/store rooted under ``tmp_path``."""

    library: MusicLibrary
    service: ProgramService
    root: Path


def _fx(tmp_path: Path, producer: Producer | None = None) -> _Fx:
    root = tmp_path / "programs"
    store = FilesystemProgramStore(root)
    service = ProgramService(
        QuietProducer(), store, root, FakeSleeper(), root / "mpv.sock"
    )
    library = MusicLibrary(service.catalog, store, root, producer or QuietProducer())
    return _Fx(library, service, root)


class TestMusicNew:
    """Authoring one track files a fresh single-track album (``MusicNew``)."""

    async def test_new_files_a_single_track_album(self, tmp_path: Path) -> None:
        fx = _fx(tmp_path)
        album_id = await fx.library.new("warm analog pads, D minor", name=None)
        album = fx.service.catalog.by_id(album_id)
        assert album is not None
        assert len(album.ready_parts()) == 1
        assert (fx.root / album.locator / "001.mp3").is_file()

    async def test_new_mints_a_six_hex_id(self, tmp_path: Path) -> None:
        fx = _fx(tmp_path)
        album_id = await fx.library.new("a prompt", name=None)
        assert len(album_id.value) == 6
        assert all(c in "0123456789abcdef" for c in album_id.value)

    async def test_new_honours_a_curated_name(self, tmp_path: Path) -> None:
        fx = _fx(tmp_path)
        album_id = await fx.library.new("a prompt", name="my-track")
        album = fx.service.catalog.by_id(album_id)
        assert album is not None
        assert album.manifest.tags.name == "my-track"

    async def test_empty_prompt_is_rejected(self, tmp_path: Path) -> None:
        fx = _fx(tmp_path)
        with pytest.raises(ValueError, match="empty prompt"):
            await fx.library.new("   ", name=None)
        assert fx.service.catalog_albums() == ()

    async def test_bad_prompt_leaves_catalog_unchanged(self, tmp_path: Path) -> None:
        fx = _fx(tmp_path, _BadPromptProducer())
        with pytest.raises(ValueError, match="bad_prompt"):
            await fx.library.new("rejected", name=None)
        assert fx.service.catalog_albums() == ()
        # The half-authored directory was discarded, not left behind.
        assert list(fx.root.glob("*")) == []

    async def test_transient_failure_leaves_catalog_unchanged(
        self, tmp_path: Path
    ) -> None:
        fx = _fx(tmp_path, _TransientProducer())
        with pytest.raises(ValueError, match="rate limited"):
            await fx.library.new("later", name=None)
        assert fx.service.catalog_albums() == ()


class TestMusicNewReservation:
    """The curated-name reservation guards the generation await (D-1 TOCTOU)."""

    async def test_overlapping_same_name_new_reserves(self, tmp_path: Path) -> None:
        """Two concurrent same-name ``new`` calls: exactly one album is created."""
        gate = asyncio.Event()
        producer = _GatedProducer(gate)
        fx = _fx(tmp_path, producer)

        first = asyncio.create_task(fx.library.new("first prompt", name="dup"))
        await producer.entered.wait()  # first has reserved and is mid-generation

        # The second reserves synchronously, sees the reservation, and rejects --
        # neither album is catalogued yet, so this is the TOCTOU the guard closes.
        with pytest.raises(ValueError, match="already exists"):
            await fx.library.new("second prompt", name="dup")

        gate.set()
        album_id = await first
        assert fx.service.catalog.by_id(album_id) is not None
        assert len(fx.service.catalog_albums()) == 1

    async def test_reservation_released_after_failure(self, tmp_path: Path) -> None:
        """A failed ``new`` releases its name, so a retry is not blocked as a dup."""
        fx = _fx(tmp_path, _BadPromptProducer())
        with pytest.raises(ValueError, match="bad_prompt"):
            await fx.library.new("rejected", name="retry")
        # A leaked reservation would raise "already exists"; instead the name is
        # free and the retry reaches the producer (bad_prompt again).
        with pytest.raises(ValueError, match="bad_prompt"):
            await fx.library.new("rejected", name="retry")


class TestMaterialiseDiscard:
    """``_materialise`` discards only the directory *this* call created (3.2)."""

    async def test_locator_collision_preserves_existing_album(
        self, tmp_path: Path
    ) -> None:
        """A locator collision leaves the pre-existing album on disk untouched."""
        fx = _fx(tmp_path)
        draft = ManifestDraft(
            album_id=AlbumId("a3f1c9"),
            tags=AlbumTags(style="custom", vibe="custom", name="dup"),
            fingerprint=PromptFingerprint.from_prompts("p", ()),
            taken_names=frozenset(),
        )
        existing = fx.root / draft.locator
        existing.mkdir(parents=True)
        (existing / "keep.txt").write_bytes(b"precious")

        with pytest.raises(ValueError, match="already exists"):
            await fx.library._materialise(draft, "prompt")  # pyright: ignore[reportPrivateUsage]

        assert (existing / "keep.txt").read_bytes() == b"precious"


class TestModelAlignment:
    """The audio-programs Catalog/System delta, asserted by name."""

    async def test_music_new_leaves_full_pool_playing(self, tmp_path: Path) -> None:
        """``MusicNew`` frames ``ΞProgram``: a full playing pool is unchanged."""
        seed_album(
            tmp_path / "programs",
            *range(1, _POOL_SIZE + 1),
            name="live",
            album_id="a3f1c9",
        )
        fx = _fx(tmp_path)
        fx.service.turn_on(style="techno", vibe="calm", name="live", prompts=None)
        await fx.service.run_once()
        fx.service.shutdown()
        before = fx.service.status().to_dict()
        assert fx.service.status().mode is Mode.PLAYING_ROTATING

        await fx.library.new("a parked track", name=None)

        assert fx.service.status().to_dict() == before
        assert len(fx.service.catalog_albums()) == 2  # the parked track was added

    async def test_music_new_bad_prompt_does_not_fail_program(
        self, tmp_path: Path
    ) -> None:
        """``MusicNewBadPrompt``: the Program's mode/lastError are untouched."""
        fx = _fx(tmp_path, _BadPromptProducer())
        fx.service.turn_on(style="techno", vibe="calm", name=None, prompts=None)
        await fx.service.run_once()
        fx.service.shutdown()
        before = fx.service.status().to_dict()

        with pytest.raises(ValueError, match="bad_prompt"):
            await fx.library.new("rejected", name=None)

        after = fx.service.status()
        assert after.mode is not Mode.FAILED
        assert after.to_dict() == before

    async def test_music_remove_refuses_playing_album(self, tmp_path: Path) -> None:
        """``MusicRemove``: an album backing the active radio is refused (D-2)."""
        root = tmp_path / "programs"
        locator = seed_album(root, 1, 2, name="live", album_id="a3f1c9")
        fx = _fx(tmp_path)
        fx.service.replay_album(AlbumId("a3f1c9"))
        await fx.service.run_once()
        fx.service.shutdown()

        with pytest.raises(ValueError, match="is playing; stop it first"):
            fx.library.remove(
                AlbumId("a3f1c9"), blocked=fx.service.active_backing_locators()
            )
        assert (fx.root / locator).is_dir()  # nothing deleted

    async def test_live_parts_stay_catalogued(self, tmp_path: Path) -> None:
        """F5: after an accepted new + remove, every live Part is still catalogued."""
        root = tmp_path / "programs"
        seed_album(root, 1, 2, name="live", album_id="a3f1c9")
        idle = seed_album(root, 1, name="idle", album_id="bbbbbb")
        fx = _fx(tmp_path)
        fx.service.replay_album(AlbumId("a3f1c9"))
        await fx.service.run_once()
        fx.service.shutdown()

        await fx.library.new("a parked track", name=None)
        blocked = fx.service.active_backing_locators()
        fx.library.remove(AlbumId("bbbbbb"), blocked=blocked)

        # The active radio's album is still catalogued; the idle one is gone.
        assert fx.service.catalog.by_id(AlbumId("a3f1c9")) is not None
        assert fx.service.catalog.by_id(AlbumId("bbbbbb")) is None
        assert not (fx.root / idle).exists()


class TestManifestAndResolve:
    """``music get`` manifest + per-part resolution (catalog id, bare part name)."""

    async def test_manifest_lists_parts_with_bytes(self, tmp_path: Path) -> None:
        fx = _fx(tmp_path)
        album_id = await fx.library.new("a prompt", name="pads")
        contents = fx.library.manifest(album_id)
        assert contents.name.endswith(album_id.value)
        assert [p.name for p in contents.parts] == ["001.mp3"]
        assert contents.parts[0].byte_count > 0

    def test_unknown_album_id_is_a_clean_error(self, tmp_path: Path) -> None:
        fx = _fx(tmp_path)
        with pytest.raises(ValueError, match="no album named"):
            fx.library.manifest(AlbumId("abcdef"))

    async def test_resolve_part_contains_within_album(self, tmp_path: Path) -> None:
        fx = _fx(tmp_path)
        album_id = await fx.library.new("a prompt", name=None)
        resolved = fx.library.resolve_part(album_id, "001.mp3")
        album = fx.service.catalog.by_id(album_id)
        assert album is not None
        # Resolved as an immediate child (no ``.resolve()`` follow), so a symlink
        # part is never dereferenced -- the path is the direct child itself.
        assert resolved == fx.root / album.locator / "001.mp3"
        assert resolved.read_bytes()  # a real regular file, streamable

    async def test_resolve_part_rejects_symlink_without_following(
        self, tmp_path: Path
    ) -> None:
        """A part that is a symlink is refused -- its target is never served.

        Mirrors ``PartFile.measured`` rejecting a symlink: ``resolve_part`` must
        not follow a symlinked part to whatever it points at, even an in-root
        file, because the store's no-symlink invariant forbids symlink parts.
        """
        fx = _fx(tmp_path)
        album_id = await fx.library.new("a prompt", name=None)
        album = fx.service.catalog.by_id(album_id)
        assert album is not None
        secret = tmp_path / "secret.bin"
        secret.write_bytes(b"x" * 4096)
        (fx.root / album.locator / "link.mp3").symlink_to(secret)
        with pytest.raises(ValueError, match="no part name"):
            fx.library.resolve_part(album_id, "link.mp3")

    async def test_resolve_part_missing_is_a_clean_error(self, tmp_path: Path) -> None:
        """A well-formed but absent part is the clean not-found, not a probe."""
        fx = _fx(tmp_path)
        album_id = await fx.library.new("a prompt", name=None)
        with pytest.raises(ValueError, match="no part name"):
            fx.library.resolve_part(album_id, "999.mp3")

    @pytest.mark.parametrize("hostile", _HOSTILE_PARTS)
    async def test_resolve_part_rejects_hostile_names(
        self, tmp_path: Path, hostile: str
    ) -> None:
        """A part-name escape is refused inside the resolved album dir (F2)."""
        fx = _fx(tmp_path)
        album_id = await fx.library.new("a prompt", name=None)
        with pytest.raises(ValueError):
            fx.library.resolve_part(album_id, hostile)

    def test_resolve_part_unknown_album_rejected(self, tmp_path: Path) -> None:
        """An id with no catalog entry is a clean error, never a filesystem probe."""
        fx = _fx(tmp_path)
        with pytest.raises(ValueError, match="no album named"):
            fx.library.resolve_part(AlbumId("abcdef"), "001.mp3")


class TestRemove:
    """``music_remove`` deletes an idle album and forgets it."""

    async def test_remove_deletes_idle_album(self, tmp_path: Path) -> None:
        fx = _fx(tmp_path)
        locator = seed_album(fx.root, 1, 2, name="idle", album_id="a3f1c9")
        # Rebuild the fixture's catalog to see the seeded album (scan at construct).
        fx2 = _fx(tmp_path)
        fx2.library.remove(AlbumId("a3f1c9"), blocked=frozenset())
        assert fx2.service.catalog.by_id(AlbumId("a3f1c9")) is None
        assert not (fx.root / locator).exists()

    def test_remove_unknown_album_rejected(self, tmp_path: Path) -> None:
        fx = _fx(tmp_path)
        with pytest.raises(ValueError, match="no album named"):
            fx.library.remove(AlbumId("abcdef"), blocked=frozenset())

    def test_remove_forgets_entry_when_directory_already_gone(
        self, tmp_path: Path
    ) -> None:
        # A catalogued album whose on-disk directory has already vanished must not
        # leave a ghost id: remove forgets the catalog entry cleanly rather than
        # raising on the missing directory and failing every retry until a rescan.
        locator = seed_album(tmp_path / "programs", 1, name="idle", album_id="a3f1c9")
        fx = _fx(tmp_path)  # scans the seeded album into the shared catalog
        shutil.rmtree(fx.root / locator)  # the directory disappears out from under it

        fx.library.remove(AlbumId("a3f1c9"), blocked=frozenset())

        assert fx.service.catalog.by_id(AlbumId("a3f1c9")) is None  # no ghost id
        # A second remove sees no entry -- the clean not-found, not a repeat failure.
        with pytest.raises(ValueError, match="no album named"):
            fx.library.remove(AlbumId("a3f1c9"), blocked=frozenset())


@final
class _RecordFailsPartStore:
    """Wrap a real PartStore but raise ``OSError`` on ``record`` (a write fault)."""

    __slots__ = ("_inner",)
    _inner: PartStore

    def __new__(cls, inner: PartStore) -> Self:
        self = super().__new__(cls)
        self._inner = inner
        return self

    def ready_parts(self) -> tuple[Part, ...]:
        return self._inner.ready_parts()

    def next_index(self) -> int:
        return self._inner.next_index()

    def write_target(self, index: int) -> Path:
        return self._inner.write_target(index)

    def record(self, entry: PartEntry) -> None:
        del entry
        raise OSError("disk full")

    def manifest(self) -> AlbumManifest:
        return self._inner.manifest()

    def prepare(self) -> None:
        self._inner.prepare()


@final
class _RecordFailsStore:
    """A ProgramStore delegating to a real one whose created Part write faults."""

    __slots__ = ("_inner",)
    _inner: ProgramStore

    def __new__(cls, inner: ProgramStore) -> Self:
        self = super().__new__(cls)
        self._inner = inner
        return self

    def scan(self) -> tuple[Album, ...]:
        return self._inner.scan()

    def open(self, directory: str) -> PartStore:
        return self._inner.open(directory)

    def create(self, draft: ManifestDraft) -> PartStore:
        return _RecordFailsPartStore(self._inner.create(draft))

    def delete(self, directory: str) -> None:
        self._inner.delete(directory)


def _seed_hostile_manifest(root: Path, identity: str, album_id: str = "cccccc") -> None:
    """Persist an album whose manifest names a hostile Part file (as a corrupt disk)."""
    draft = ManifestDraft(
        album_id=AlbumId(album_id),
        tags=AlbumTags(style="techno", vibe="ambient", name="hostile"),
        fingerprint=PromptFingerprint("deadbeef"),
        parts=(
            PartEntry(index=1, file=identity, status=PartStatus.READY, duration_ms=1),
        ),
    )
    FilesystemProgramStore(root).create(draft)


class TestDuplicateName:
    """``music_new`` refuses a curated name already in the catalog."""

    async def test_duplicate_curated_name_is_rejected(self, tmp_path: Path) -> None:
        fx = _fx(tmp_path)
        await fx.library.new("first", name="dup")
        with pytest.raises(ValueError, match="already exists"):
            await fx.library.new("second", name="dup")
        # The colliding second authoring created no album.
        assert len(fx.service.catalog_albums()) == 1

    async def test_unique_curated_name_still_succeeds(self, tmp_path: Path) -> None:
        fx = _fx(tmp_path)
        await fx.library.new("first", name="alpha")
        await fx.library.new("second", name="beta")
        assert len(fx.service.catalog_albums()) == 2

    async def test_unnamed_pools_never_collide(self, tmp_path: Path) -> None:
        """Two unnamed authorings get distinct auto-names -- no false rejection."""
        fx = _fx(tmp_path)
        await fx.library.new("first", name=None)
        await fx.library.new("second", name=None)
        assert len(fx.service.catalog_albums()) == 2


class TestNewOrphanCleanup:
    """A write fault during ``music_new`` leaves no orphan album directory."""

    async def test_oserror_during_write_leaves_no_orphan(self, tmp_path: Path) -> None:
        root = tmp_path / "programs"
        inner = FilesystemProgramStore(root)
        catalog = Catalog(inner.scan())
        library = MusicLibrary(catalog, _RecordFailsStore(inner), root, QuietProducer())

        with pytest.raises(OSError, match="disk full"):
            await library.new("a prompt", name=None)

        # The just-created directory was discarded: no orphan on disk or in scan.
        assert list(root.glob("*")) == []
        assert inner.scan() == ()


class TestManifestContainment:
    """``manifest`` refuses a hostile on-disk Part identity rather than stat outside."""

    @pytest.mark.parametrize("identity", _HOSTILE_IDENTITIES)
    async def test_manifest_rejects_hostile_part_identity(
        self, tmp_path: Path, identity: str
    ) -> None:
        _seed_hostile_manifest(tmp_path / "programs", identity)
        fx = _fx(tmp_path)  # scans the seeded album into the shared catalog
        with pytest.raises(ValueError):
            fx.library.manifest(AlbumId("cccccc"))

    async def test_manifest_lists_a_normal_part(self, tmp_path: Path) -> None:
        """A well-formed manifest still lists its parts with byte counts."""
        fx = _fx(tmp_path)
        album_id = await fx.library.new("a prompt", name="clean")
        contents = fx.library.manifest(album_id)
        assert [p.name for p in contents.parts] == ["001.mp3"]
        assert contents.parts[0].byte_count > 0
