"""Tests for MusicPlayer: notify_changed projects fresh state and submits once.

Two properties matter beyond the projection content itself. The first
(vox-h777): an install shows and raises, a change refreshes and never installs.
The second is this bug's own fix: neither ``notify_changed`` nor the failure
presenters may block on disk for a track count -- they read
:class:`~punt_vox.voxd.music_player.track_count_cache.TrackCountCache` instead,
which is refreshed off the hot path via ``asyncio.to_thread`` and (for the
control-channel path) resubmits once the refresh lands, so a render fired the
instant a state change arrives can be a render or two behind the true disk
count, converging shortly after. ``install`` gets a fresh read awaited inline
instead, because it already runs on the lux listener's event loop, never the
control-channel writer.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, final

from punt_vox.types_programs.status import ProgramStatus
from punt_vox.voxd.music_player.player import MusicPlayer
from punt_vox.voxd.programs.album_id import AlbumId

if TYPE_CHECKING:
    from collections.abc import Callable

    import pytest
    from punt_lux import RenderRequest

    from punt_vox.voxd.programs.catalog import Album

    type AlbumFactory = Callable[..., Album]
    type PlayingFactory = Callable[[Album, int, int], ProgramStatus]

# Ample time for a scheduled background refresh (an in-memory fake store, no
# real disk) to land and, where one is expected, resubmit.
_SETTLE_SECONDS = 0.05


def _by_id(elements: list[dict[str, object]], elem_id: str) -> dict[str, object]:
    """Return the scene element with ``elem_id`` (the scene is a flat id-keyed list)."""
    return next(element for element in elements if element["id"] == elem_id)


def _position_text(elements: list[dict[str, object]]) -> str:
    """Return the ``N of M`` cell from the now-playing position line."""
    return str(_by_id(elements, "music.now.position")["content"])


async def _settle() -> None:
    """Yield the event loop long enough for a scheduled refresh to complete."""
    await asyncio.sleep(_SETTLE_SECONDS)


@final
class _FakeService:
    """A PlayerService double returning a fixed status and catalog."""

    def __init__(self, status: ProgramStatus, albums: tuple[Album, ...]) -> None:
        self._status = status
        self._albums = albums

    def status(self) -> ProgramStatus:
        return self._status

    def catalog_albums(self) -> tuple[Album, ...]:
        return self._albums


@final
class _CapturingPublisher:
    """A ScenePublisher double recording refreshes and installs separately.

    They are recorded apart because they mean different things on screen: an
    install raises the frame, a refresh must leave it alone.
    """

    def __init__(self) -> None:
        self.submitted: list[RenderRequest] = []
        self.installed: list[RenderRequest] = []

    def submit(self, request: RenderRequest) -> None:
        self.submitted.append(request)

    def reinstall(self, request: RenderRequest) -> None:
        self.installed.append(request)


async def test_notify_changed_projects_the_playing_scene_and_submits_once(
    album_of: AlbumFactory, playing_of: PlayingFactory
) -> None:
    album = album_of("aa11bb", name="Techno Mix")
    service = _FakeService(playing_of(album, 1, 3), (album,))
    publisher = _CapturingPublisher()

    MusicPlayer(service, publisher).notify_changed()

    assert len(publisher.submitted) == 1
    elements = publisher.submitted[0].elements
    assert publisher.submitted[0].scene_id == "vox.music"
    assert "Techno Mix" in str(_by_id(elements, "music.now.album")["content"])
    assert _position_text(elements) == "1 of 3"
    await _settle()  # let the background refresh this scheduled drain cleanly


async def test_notify_changed_projects_the_idle_scene(album_of: AlbumFactory) -> None:
    service = _FakeService(ProgramStatus.idle(), (album_of("aa11bb"),))
    publisher = _CapturingPublisher()

    MusicPlayer(service, publisher).notify_changed()

    elements = publisher.submitted[0].elements
    assert _by_id(elements, "music.now.album")["content"] == "### Nothing playing"
    assert _position_text(elements) == ""
    await _settle()  # let the background refresh this scheduled drain cleanly


async def test_present_play_failure_surfaces_the_warning_then_a_change_clears_it(
    album_of: AlbumFactory,
) -> None:
    # A play that could not run shows its warning in the status slot; the failure
    # left the daemon idle, so the now-playing line still reads idle (I2 holds). The
    # next legitimate change repaints silently and clears the warning in place.
    album = album_of("aa11bb", name="Techno Mix")
    service = _FakeService(ProgramStatus.idle(), (album,))
    publisher = _CapturingPublisher()
    player = MusicPlayer(service, publisher)

    player.present_play_failure(AlbumId("aa11bb"))

    failed = publisher.submitted[-1].elements
    assert _by_id(failed, "music.now.album")["content"] == "### Nothing playing"  # I2
    assert _by_id(failed, "music.status") == {
        "kind": "text",
        "id": "music.status",
        "content": "⚠ couldn't play Techno Mix — it has no tracks yet",
    }

    player.notify_changed()
    cleared = publisher.submitted[-1].elements
    assert _by_id(cleared, "music.status")["content"] == ""  # cleared in place
    await _settle()  # let the background refresh this scheduled drain cleanly


async def test_present_play_failure_keeps_now_playing_when_a_source_plays(
    album_of: AlbumFactory, playing_of: PlayingFactory
) -> None:
    # A failed switch to a vanished album leaves the current source playing, so the
    # warning and a live now-playing coexist -- I2 (now-playing present iff playing).
    playing = album_of("aa11bb", name="Techno Mix")
    service = _FakeService(playing_of(playing, 2, 3), (playing,))
    publisher = _CapturingPublisher()

    MusicPlayer(service, publisher).present_play_failure(AlbumId("ff99ee"))

    elements = publisher.submitted[-1].elements
    assert "Techno Mix" in str(_by_id(elements, "music.now.album")["content"])  # I2
    assert _position_text(elements) == "2 of 3"
    assert (
        _by_id(elements, "music.status")["content"]
        == "⚠ couldn't play ff99ee — no longer in the crate"
    )
    await _settle()  # let the background refresh this scheduled drain cleanly


class TestInstallVersusRefresh:
    async def test_a_change_refreshes_and_never_installs(
        self, album_of: AlbumFactory
    ) -> None:
        publisher = _CapturingPublisher()
        service = _FakeService(ProgramStatus.idle(), (album_of("aa11bb"),))

        MusicPlayer(service, publisher).notify_changed()

        assert len(publisher.submitted) == 1
        assert publisher.installed == []  # no frame raise on a state change
        await _settle()  # let the background refresh this scheduled drain cleanly

    async def test_install_installs_and_never_refreshes(
        self, album_of: AlbumFactory
    ) -> None:
        publisher = _CapturingPublisher()
        service = _FakeService(ProgramStatus.idle(), (album_of("aa11bb"),))

        await MusicPlayer(service, publisher).install()

        assert len(publisher.installed) == 1
        assert publisher.submitted == []

    async def test_a_refused_click_refreshes_because_the_user_is_already_here(
        self, album_of: AlbumFactory
    ) -> None:
        # The user clicked *inside* this window, so it is already in front of
        # them; the warning belongs in place, not behind a frame raise.
        publisher = _CapturingPublisher()
        service = _FakeService(ProgramStatus.idle(), (album_of("aa11bb"),))
        player = MusicPlayer(service, publisher)

        player.present_play_failure(AlbumId("aa11bb"))
        player.present_stop_failure()
        player.present_resolve_failure("Ghost")
        player.present_transport_failure()

        assert len(publisher.submitted) == 4
        assert publisher.installed == []
        await _settle()  # let the background refresh this scheduled drain cleanly


class TestInstallSurvivesARefreshFailure:
    """The bug this fix closes: a failed cache refresh must never sink the
    menu click, and a warning install() clears must not come back."""

    async def test_install_still_shows_the_window_when_the_refresh_fails(
        self, album_of: AlbumFactory
    ) -> None:
        # TrackCountCache._refresh propagates any non-LookupError fault. Before
        # this fix, install() had no guard around it -- an OSError (disk
        # pressure, fd exhaustion) would propagate out of install() entirely,
        # caught only by the outer lux boundary's log-and-swallow, so the menu
        # click that asked to see the window would produce nothing visible.
        album = album_of("aa11bb", name="Techno Mix", fails_with=OSError("EMFILE"))
        publisher = _CapturingPublisher()
        player = MusicPlayer(_FakeService(ProgramStatus.idle(), (album,)), publisher)

        await player.install()  # must not raise

        assert len(publisher.installed) == 1

    async def test_a_warning_install_clears_is_not_resurrected_by_a_refresh_in_flight(
        self, album_of: AlbumFactory
    ) -> None:
        # notify_changed schedules a background refresh; before it lands, a
        # failed stop raises a warning. install() (the menu click) then clears
        # it in the scene -- and must update _latest_notice too, or the
        # background refresh's resubmit (which reads _latest_notice fresh at
        # its own execution time, not the notice captured at schedule time)
        # brings the warning back right after the click just cleared it.
        album = album_of("aa11bb", name="Techno Mix", tracks=0, on_disk=9)
        publisher = _CapturingPublisher()
        player = MusicPlayer(_FakeService(ProgramStatus.idle(), (album,)), publisher)

        player.notify_changed()  # schedules a background refresh (not yet run)
        player.present_stop_failure()  # raises a warning before it lands
        warned = _by_id(publisher.submitted[-1].elements, "music.status")["content"]
        assert warned == "⚠ couldn't stop the music"

        await player.install()
        cleared = _by_id(publisher.installed[-1].elements, "music.status")["content"]
        assert cleared == ""  # the click clears it

        await _settle()  # let the in-flight background refresh resubmit
        resubmitted = _by_id(publisher.submitted[-1].elements, "music.status")[
            "content"
        ]
        assert resubmitted == ""  # never resurrected


class TestTrackCountsNeverBlockTheHotPath:
    """The bug this fix closes: a disk read must never run inline on a render
    fired from the control-channel single-writer or the lux listener's loop."""

    async def test_notify_changed_never_blocks_on_the_live_read(
        self, album_of: AlbumFactory
    ) -> None:
        # The immediate render -- before any background refresh has had a
        # chance to run -- reads the cache's default (zero), not the disk.
        # That is the whole point: notify_changed returns without waiting on
        # anything the store's disk access could stall on.
        album = album_of("aa11bb", name="Techno Mix", tracks=0, on_disk=9)
        publisher = _CapturingPublisher()
        player = MusicPlayer(_FakeService(ProgramStatus.idle(), (album,)), publisher)

        player.notify_changed()

        assert _by_id(publisher.submitted[0].elements, "music.albums")["rows"] == [
            ["Techno Mix", "techno", 0]
        ]
        await _settle()  # let the background refresh this scheduled drain cleanly

    async def test_notify_changed_schedules_a_background_refresh_via_to_thread(
        self, album_of: AlbumFactory, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls: list[object] = []
        real_to_thread = asyncio.to_thread

        async def _spying_to_thread(func: object, *args: object) -> object:
            calls.append(func)
            return await real_to_thread(func, *args)  # type: ignore[arg-type]

        monkeypatch.setattr(asyncio, "to_thread", _spying_to_thread)
        album = album_of("aa11bb", name="Techno Mix", tracks=0, on_disk=9)
        publisher = _CapturingPublisher()
        player = MusicPlayer(_FakeService(ProgramStatus.idle(), (album,)), publisher)

        player.notify_changed()
        await _settle()

        assert calls == [player._cache._refresh]

    async def test_the_background_refresh_resubmits_with_the_live_count(
        self, album_of: AlbumFactory
    ) -> None:
        album = album_of("aa11bb", name="Techno Mix", tracks=0, on_disk=9)
        publisher = _CapturingPublisher()
        player = MusicPlayer(_FakeService(ProgramStatus.idle(), (album,)), publisher)

        player.notify_changed()
        await _settle()

        assert _by_id(publisher.submitted[-1].elements, "music.albums")["rows"] == [
            ["Techno Mix", "techno", 9]
        ]

    async def test_a_background_refresh_never_clobbers_a_warning_raised_after_it(
        self, album_of: AlbumFactory
    ) -> None:
        # notify_changed schedules a refresh; before it lands, a click fails
        # and raises a warning. The refresh's resubmit must carry that warning
        # forward, not silently wipe it back to the silent notice it started
        # with.
        album = album_of("aa11bb", name="Techno Mix", tracks=0, on_disk=9)
        publisher = _CapturingPublisher()
        player = MusicPlayer(_FakeService(ProgramStatus.idle(), (album,)), publisher)

        player.notify_changed()
        player.present_stop_failure()
        await _settle()

        elements = publisher.submitted[-1].elements
        status = _by_id(elements, "music.status")["content"]
        assert status == "⚠ couldn't stop the music"
        # The count still converged even though the refresh in flight was
        # scheduled by notify_changed, not by the failure that followed it.
        assert _by_id(elements, "music.albums")["rows"] == [["Techno Mix", "techno", 9]]

    async def test_a_burst_of_changes_schedules_only_one_refresh(
        self, album_of: AlbumFactory
    ) -> None:
        # One per completed Part could mean many notify_changed calls in a
        # row; they must coalesce onto whichever refresh is already in
        # flight rather than queueing an unbounded pile of disk reads.
        album = album_of("aa11bb", tracks=0, on_disk=9)
        publisher = _CapturingPublisher()
        player = MusicPlayer(_FakeService(ProgramStatus.idle(), (album,)), publisher)

        player.notify_changed()
        player.notify_changed()
        player.notify_changed()
        assert player._refresher.running is True
        await _settle()

        assert player._refresher.running is False

    async def test_install_awaits_a_fresh_read_via_to_thread(
        self, album_of: AlbumFactory, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls: list[object] = []
        real_to_thread = asyncio.to_thread

        async def _spying_to_thread(func: object, *args: object) -> object:
            calls.append(func)
            return await real_to_thread(func, *args)  # type: ignore[arg-type]

        monkeypatch.setattr(asyncio, "to_thread", _spying_to_thread)
        album = album_of("aa11bb", name="Techno Mix", tracks=0, on_disk=4)
        publisher = _CapturingPublisher()
        player = MusicPlayer(_FakeService(ProgramStatus.idle(), (album,)), publisher)

        await player.install()

        assert calls == [player._cache._refresh]
        assert _by_id(publisher.installed[0].elements, "music.albums")["rows"] == [
            ["Techno Mix", "techno", 4]
        ]

    async def test_a_menu_click_shows_the_live_count_at_once_no_settling_needed(
        self, album_of: AlbumFactory
    ) -> None:
        # Unlike notify_changed, install already runs on the lux listener's
        # event loop, so it can afford to await the fresh read inline -- the
        # very click that asks to see the window shows accurate counts,
        # with no background convergence needed.
        album = album_of("aa11bb", name="Techno Mix", tracks=0, on_disk=4)
        publisher = _CapturingPublisher()
        player = MusicPlayer(_FakeService(ProgramStatus.idle(), (album,)), publisher)

        await player.install()

        assert _by_id(publisher.installed[0].elements, "music.albums")["rows"] == [
            ["Techno Mix", "techno", 4]
        ]


async def test_an_album_the_disk_read_fails_for_keeps_its_row_at_zero(
    album_of: AlbumFactory,
) -> None:
    # A LookupError during the background refresh (the store's documented
    # delete contract) drops the album from the CACHE, not from the render:
    # the catalog decides which albums exist at all now (it already excludes a
    # deleted album synchronously, in the same call that deletes its
    # directory -- see Library.remove), so this only means the row keeps its
    # default zero count instead of the whole row vanishing or the background
    # refresh crashing.
    kept = album_of("aa11bb", name="Kept", tracks=0, on_disk=3)
    gone = album_of("cc22dd", name="Gone", fails_with=LookupError("deleted"))
    publisher = _CapturingPublisher()
    player = MusicPlayer(_FakeService(ProgramStatus.idle(), (kept, gone)), publisher)

    player.notify_changed()
    await _settle()

    elements = publisher.submitted[-1].elements
    assert _by_id(elements, "music.albums")["rows"] == [
        ["Kept", "techno", 3],
        ["Gone", "techno", 0],
    ]
    assert _by_id(elements, "music.albums.label")["content"] == "Albums · 2 albums"


async def test_present_stop_failure_surfaces_the_stop_warning(
    album_of: AlbumFactory,
) -> None:
    service = _FakeService(ProgramStatus.idle(), (album_of("aa11bb"),))
    publisher = _CapturingPublisher()

    MusicPlayer(service, publisher).present_stop_failure()

    elements = publisher.submitted[-1].elements
    assert _by_id(elements, "music.status")["content"] == "⚠ couldn't stop the music"
    await _settle()  # let the background refresh this scheduled drain cleanly
