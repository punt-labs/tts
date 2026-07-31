"""Behavioral-parity snapshot for the background fill (ports test_filler.py).

Every test drives the REAL fill task against a filesystem PartStore and a fake
Producer. The scenarios mirror the old PoolFiller suite -- fill-to-full, partial
resume, full-pool-no-task, cancel-freezes-count-no-orphan, retarget-cancels-old,
single-in-flight-across-retarget-discards-orphan, transient-backoff-recovers --
plus the new permanent-failure-per-part path. The single-flight
``asyncio.shield`` / orphan-discard machinery is proven byte-for-byte equivalent
before slice 5 deletes the original.
"""
# pyright: reportPrivateUsage=false

from __future__ import annotations

import asyncio
import contextlib
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Self, final

from punt_vox.types_programs import MAX_RETRY, Format
from punt_vox.types_programs.prompts import PromptSet
from punt_vox.voxd.programs import (
    Part,
    PartStatus,
    PlaybackPolicy,
    Program,
    ProgramState,
)
from punt_vox.voxd.programs.album_id import AlbumId
from punt_vox.voxd.programs.album_tags import AlbumTags, PromptFingerprint
from punt_vox.voxd.programs.control_channel import ControlChannel
from punt_vox.voxd.programs.filesystem_store import FilesystemProgramStore
from punt_vox.voxd.programs.filler import Filler, FillPlan
from punt_vox.voxd.programs.manifest import ManifestDraft, PartEntry
from punt_vox.voxd.programs.producer import (
    PartSpec,
    ProducerBadInputError,
    ProducerTransientError,
)
from punt_vox.voxd.programs.sleeper import Sleeper
from punt_vox.voxd.programs.store import PartStore

if TYPE_CHECKING:
    import pytest

_FULL = Format.PLAYLIST.pool_size


@final
class WritingProducer:
    """Write a byte to the target and record each produced index."""

    __slots__ = ("calls",)
    calls: list[int]

    def __new__(cls) -> Self:
        self = super().__new__(cls)
        self.calls = []
        return self

    async def produce(self, spec: PartSpec, target: Path) -> Part:
        self.calls.append(spec.index)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"audio")
        return Part(target.name, spec.index)


def _seeded_store(tmp_path: Path, count: int, album_id: str = "a3f1c9") -> PartStore:
    """Build a filesystem PartStore seeded with ``count`` ready Parts."""
    draft = ManifestDraft(
        album_id=AlbumId(album_id),
        tags=AlbumTags(style="jazz", vibe="calm"),
        fingerprint=PromptFingerprint("deadbeef"),
        parts=tuple(
            PartEntry(index=i, file=f"{i:03d}.mp3", status=PartStatus.READY)
            for i in range(1, count + 1)
        ),
    )
    return FilesystemProgramStore(tmp_path).create(draft)


def _plan(store: PartStore, vibe: str = "calm", style: str = "jazz") -> FillPlan:
    prompts = PromptSet(base="pad", variations=("p1", "p2", "p3"))
    return FillPlan(store, AlbumTags(style=style, vibe=vibe), prompts)


def _channel(policy: PlaybackPolicy) -> ControlChannel:
    return ControlChannel(Program(ProgramState.initial(), policy))


async def _drain(filler: Filler) -> None:
    task = filler._task  # test drives the real fill task
    assert task is not None
    await task


def _titled_store(tmp_path: Path, name: str | None) -> PartStore:
    """Build an empty store whose manifest carries ``name`` (the authored title)."""
    draft = ManifestDraft(
        album_id=AlbumId("a3f1c9"),
        tags=AlbumTags(style="synthwave", vibe="calm", name=name),
        fingerprint=PromptFingerprint("deadbeef"),
    )
    return FilesystemProgramStore(tmp_path).create(draft)


class TestSpecTagsCarryTheTitle:
    """The pool fill tags each Part's ID3 album with the authored album title."""

    def test_titled_album_tags_the_part_with_the_title(self, tmp_path: Path) -> None:
        store = _titled_store(tmp_path, "Midnight Drive")
        plan = FillPlan(
            store, store.manifest().tags, PromptSet(base="pad", variations=("p1",))
        )
        assert plan.spec_for(1).tags.album == "Midnight Drive"

    def test_untitled_album_tags_the_part_with_the_minted_name(
        self, tmp_path: Path
    ) -> None:
        # With no authored title the manifest mints a timestamp name at creation,
        # so the Part's ID3 album carries that minted name, never a bare filename.
        store = _titled_store(tmp_path, None)
        minted = store.manifest().tags.name
        assert minted is not None
        plan = FillPlan(
            store, store.manifest().tags, PromptSet(base="pad", variations=("p1",))
        )
        assert plan.spec_for(1).tags.album == minted


class TestFillToPoolSize:
    async def test_fills_to_full_then_stops(
        self, tmp_path: Path, policy: PlaybackPolicy, sleeper: Sleeper
    ) -> None:
        store = _seeded_store(tmp_path, 3)
        producer = WritingProducer()
        filler = Filler(producer, _channel(policy), sleeper)
        filler.ensure_running(_plan(store))
        await _drain(filler)
        assert len(store.ready_parts()) == _FULL
        assert len(producer.calls) == _FULL - 3  # only the shortfall
        assert filler._task is not None and filler._task.done()
        assert not filler._task.cancelled()

    async def test_partial_pool_resumes_from_disk_count(
        self, tmp_path: Path, policy: PlaybackPolicy, sleeper: Sleeper
    ) -> None:
        store = _seeded_store(tmp_path, 5)
        producer = WritingProducer()
        filler = Filler(producer, _channel(policy), sleeper)
        filler.ensure_running(_plan(store))
        await _drain(filler)
        assert len(producer.calls) == _FULL - 5

    async def test_full_pool_starts_no_task(
        self, tmp_path: Path, policy: PlaybackPolicy, sleeper: Sleeper
    ) -> None:
        store = _seeded_store(tmp_path, _FULL)
        producer = WritingProducer()
        filler = Filler(producer, _channel(policy), sleeper)
        filler.ensure_running(_plan(store))
        await asyncio.sleep(0)
        assert not filler.is_running
        assert producer.calls == []

    async def test_same_plan_is_idempotent(
        self, tmp_path: Path, policy: PlaybackPolicy, sleeper: Sleeper
    ) -> None:
        store = _seeded_store(tmp_path, 0)
        hold = asyncio.Event()

        @final
        class HoldingProducer:
            async def produce(self, spec: PartSpec, target: Path) -> Part:
                await hold.wait()  # keep the fill task live
                target.write_bytes(b"x")
                return Part(target.name, spec.index)

        filler = Filler(HoldingProducer(), _channel(policy), sleeper)
        plan = _plan(store)
        filler.ensure_running(plan)
        await asyncio.sleep(0)
        task = filler._task
        filler.ensure_running(plan)  # same plan, still running -> no-op
        assert filler._task is task
        filler.cancel()


class TestCancelStopsGeneration:
    async def test_cancel_freezes_count_no_orphan(
        self, tmp_path: Path, policy: PlaybackPolicy, sleeper: Sleeper
    ) -> None:
        store = _seeded_store(tmp_path, 1)
        started = asyncio.Event()
        release = asyncio.Event()
        calls = 0

        @final
        class BlockingProducer:
            async def produce(self, spec: PartSpec, target: Path) -> Part:
                nonlocal calls
                calls += 1
                started.set()
                await release.wait()  # never released -> cancelled mid-flight
                target.write_bytes(b"x")
                return Part(target.name, spec.index)

        filler = Filler(BlockingProducer(), _channel(policy), sleeper)
        filler.ensure_running(_plan(store))
        await asyncio.wait_for(started.wait(), timeout=2.0)
        task = filler._task
        filler.cancel()
        await asyncio.sleep(0)
        assert task is not None
        with contextlib.suppress(asyncio.CancelledError):
            await task
        assert task.cancelled()
        assert calls == 1  # the in-flight generation; no Part was added
        assert not filler.is_running
        assert len(store.ready_parts()) == 1


class TestRetargetCancelsOldTask:
    async def test_retarget_switches_store(
        self, tmp_path: Path, policy: PlaybackPolicy, sleeper: Sleeper
    ) -> None:
        store_a = _seeded_store(tmp_path, 0, "a")
        store_b = _seeded_store(tmp_path, 0, "b")
        started_a = asyncio.Event()
        release = asyncio.Event()

        @final
        class GatedProducer:
            async def produce(self, spec: PartSpec, target: Path) -> Part:
                if not started_a.is_set():
                    started_a.set()
                    await release.wait()  # hold plan A's first generation
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(b"x")
                return Part(target.name, spec.index)

        filler = Filler(GatedProducer(), _channel(policy), sleeper)
        filler.ensure_running(_plan(store_a))
        await asyncio.wait_for(started_a.wait(), timeout=2.0)
        old_task = filler._task
        assert old_task is not None
        filler.ensure_running(_plan(store_b, "bright", "techno"))  # retarget
        await asyncio.sleep(0)
        release.set()  # single-flight: B waits for A's in-flight to drain
        await _drain(filler)
        assert old_task.cancelled()
        assert len(store_b.ready_parts()) == _FULL


class TestSingleInFlightAcrossRetarget:
    async def test_retarget_bounds_concurrency_and_discards_orphan(
        self, tmp_path: Path, policy: PlaybackPolicy, sleeper: Sleeper
    ) -> None:
        store_a = _seeded_store(tmp_path, 0, "a")
        store_b = _seeded_store(tmp_path, 0, "b")
        started = asyncio.Event()
        release_a = asyncio.Event()
        concurrency = 0
        max_concurrency = 0

        async def _core(target: Path) -> None:
            nonlocal concurrency, max_concurrency
            concurrency += 1
            max_concurrency = max(max_concurrency, concurrency)
            if not started.is_set():
                started.set()
                await release_a.wait()  # hold pool A's first generation open
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(b"x")  # the write completes despite the cancel
            concurrency -= 1

        @final
        class UncancellableProducer:
            async def produce(self, spec: PartSpec, target: Path) -> Part:
                # shield models asyncio.to_thread: the work is not cancellable.
                await asyncio.shield(_core(target))
                return Part(target.name, spec.index)

        orphan = store_a.write_target(1)
        filler = Filler(UncancellableProducer(), _channel(policy), sleeper)
        filler.ensure_running(_plan(store_a))
        await asyncio.wait_for(started.wait(), timeout=2.0)
        filler.ensure_running(_plan(store_b, "bright", "techno"))  # retarget -> B
        await asyncio.sleep(0)
        assert concurrency == 1  # B is blocked; only A is in flight
        release_a.set()
        await _drain(filler)
        for _ in range(5):
            await asyncio.sleep(0)  # let the discard callback settle
        assert max_concurrency == 1  # A and B never ran concurrently
        assert not orphan.exists()  # the orphan was discarded
        assert len(store_b.ready_parts()) == _FULL


class TestFailureHandling:
    async def test_transient_backoff_recovers(
        self, tmp_path: Path, policy: PlaybackPolicy, sleeper: Sleeper
    ) -> None:
        store = _seeded_store(tmp_path, _FULL - 1)  # one short of full
        attempts = 0

        @final
        class FailOnceProducer:
            async def produce(self, spec: PartSpec, target: Path) -> Part:
                nonlocal attempts
                attempts += 1
                if attempts == 1:
                    raise ProducerTransientError("429")
                target.write_bytes(b"x")
                return Part(target.name, spec.index)

        filler = Filler(FailOnceProducer(), _channel(policy), sleeper)
        filler.ensure_running(_plan(store))
        await _drain(filler)
        assert attempts == 2  # one failure, one success
        assert len(store.ready_parts()) == _FULL

    async def test_permanent_failure_records_failed_and_continues(
        self, tmp_path: Path, policy: PlaybackPolicy, sleeper: Sleeper
    ) -> None:
        store = _seeded_store(tmp_path, _FULL - 1)
        seen: list[int] = []

        @final
        class BadFirstProducer:
            async def produce(self, spec: PartSpec, target: Path) -> Part:
                seen.append(spec.index)
                if len(seen) == 1:
                    raise ProducerBadInputError("bad_prompt")
                target.write_bytes(b"x")
                return Part(target.name, spec.index)

        filler = Filler(BadFirstProducer(), _channel(policy), sleeper)
        filler.ensure_running(_plan(store))
        await _drain(filler)
        # The failed Part is recorded (not ready); the fill advances to the next
        # index and completes the ready pool.
        assert len(store.ready_parts()) == _FULL
        failed = [e for e in store.manifest().parts if not e.is_ready]
        assert len(failed) == 1

    async def test_permanent_failure_every_call_stops_at_cap(
        self, tmp_path: Path, policy: PlaybackPolicy, sleeper: Sleeper
    ) -> None:
        # A producer that fails permanently on EVERY call (a bad prompt, a missing
        # key) must not loop forever hammering the provider. With a non-empty pool
        # the fill records failed Parts but never a ready one, so the MAX_RETRY
        # failure cap stops it -- and, because the cap is durable in the manifest,
        # a restart cannot resume the runaway.
        store = _seeded_store(tmp_path, 1)  # one ready Part -> playing_filling path
        calls = 0

        @final
        class AlwaysBadProducer:
            async def produce(self, spec: PartSpec, target: Path) -> Part:
                nonlocal calls
                calls += 1
                raise ProducerBadInputError("bad_prompt")

        filler = Filler(AlwaysBadProducer(), _channel(policy), sleeper)
        filler.ensure_running(_plan(store))
        await _drain(filler)
        assert calls == MAX_RETRY  # bounded, not an infinite hammer
        failed = [e for e in store.manifest().parts if not e.is_ready]
        assert len(failed) == MAX_RETRY
        assert len(store.ready_parts()) == 1  # the seed remains; none added
        filler.ensure_running(_plan(store))  # a restart cannot resume the runaway
        assert not filler.is_running

    async def test_unexpected_error_records_failed_and_continues(
        self, tmp_path: Path, policy: PlaybackPolicy, sleeper: Sleeper
    ) -> None:
        # An error that is NEITHER a ProducerBadInputError NOR a
        # ProducerTransientError (a bug, an OSError writing the target) must not
        # kill the fill task silently: _produce_one records the Part failed with
        # an "unexpected:" reason, posts a permanent outcome, and the loop goes on.
        store = _seeded_store(tmp_path, _FULL - 1)
        seen: list[int] = []

        @final
        class BoomOnceProducer:
            async def produce(self, spec: PartSpec, target: Path) -> Part:
                seen.append(spec.index)
                if len(seen) == 1:
                    msg = "disk gone"
                    raise OSError(msg)  # not a Producer* error -- unexpected
                target.write_bytes(b"x")
                return Part(target.name, spec.index)

        filler = Filler(BoomOnceProducer(), _channel(policy), sleeper)
        filler.ensure_running(_plan(store))
        await _drain(filler)
        assert len(store.ready_parts()) == _FULL
        failed = [e for e in store.manifest().parts if not e.is_ready]
        assert len(failed) == 1
        assert failed[0].reason is not None
        assert failed[0].reason.startswith("unexpected:")

    async def test_done_callback_logs_an_escaped_error(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        # A fill task should only ever finish cleanly or by cancellation, but if
        # an error escapes it (a store read, a bug), the done-callback surfaces
        # it at ERROR instead of letting the task die with no trace.
        async def _boom() -> None:
            msg = "upstream boom"
            raise RuntimeError(msg)

        task: asyncio.Task[None] = asyncio.ensure_future(_boom())
        with contextlib.suppress(RuntimeError):
            await task
        with caplog.at_level(logging.ERROR):
            Filler._on_fill_done(task)
        assert any(
            "fill task died" in r.getMessage() and r.levelno == logging.ERROR
            for r in caplog.records
        )

    async def test_done_callback_stays_quiet_on_cancellation(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        async def _hang() -> None:
            await asyncio.Event().wait()

        task: asyncio.Task[None] = asyncio.ensure_future(_hang())
        await asyncio.sleep(0)
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        with caplog.at_level(logging.ERROR):
            Filler._on_fill_done(task)  # a normal cancel is not an error
        assert not any(r.levelno == logging.ERROR for r in caplog.records)
