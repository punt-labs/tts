"""Tests for MusicPlayer: notify_changed projects fresh state and submits once."""

from __future__ import annotations

from typing import TYPE_CHECKING, final

from punt_vox.types_programs.status import ProgramStatus
from punt_vox.voxd.music_player.player import MusicPlayer
from punt_vox.voxd.programs.album_id import AlbumId

if TYPE_CHECKING:
    from collections.abc import Callable

    from punt_lux import RenderRequest

    from punt_vox.voxd.programs.catalog import Album

    type AlbumFactory = Callable[..., Album]
    type PlayingFactory = Callable[[Album, int, int], ProgramStatus]


def _by_id(elements: list[dict[str, object]], elem_id: str) -> dict[str, object]:
    """Return the scene element with ``elem_id`` (the scene is a flat id-keyed list)."""
    return next(element for element in elements if element["id"] == elem_id)


def _position_text(elements: list[dict[str, object]]) -> str:
    """Return the ``N of M`` cell from the now-playing position line."""
    return str(_by_id(elements, "music.now.position")["content"])


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


def test_notify_changed_projects_the_playing_scene_and_submits_once(
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


def test_notify_changed_projects_the_idle_scene(album_of: AlbumFactory) -> None:
    service = _FakeService(ProgramStatus.idle(), (album_of("aa11bb"),))
    publisher = _CapturingPublisher()

    MusicPlayer(service, publisher).notify_changed()

    elements = publisher.submitted[0].elements
    assert _by_id(elements, "music.now.album")["content"] == "### Nothing playing"
    assert _position_text(elements) == ""


def test_present_play_failure_surfaces_the_warning_then_a_change_clears_it(
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


def test_present_play_failure_keeps_now_playing_when_a_source_plays(
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


class TestInstallVersusRefresh:
    def test_a_change_refreshes_and_never_installs(
        self, album_of: AlbumFactory
    ) -> None:
        publisher = _CapturingPublisher()
        service = _FakeService(ProgramStatus.idle(), (album_of("aa11bb"),))

        MusicPlayer(service, publisher).notify_changed()

        assert len(publisher.submitted) == 1
        assert publisher.installed == []  # no frame raise on a state change

    def test_install_installs_and_never_refreshes(self, album_of: AlbumFactory) -> None:
        publisher = _CapturingPublisher()
        service = _FakeService(ProgramStatus.idle(), (album_of("aa11bb"),))

        MusicPlayer(service, publisher).install()

        assert len(publisher.installed) == 1
        assert publisher.submitted == []

    def test_a_refused_click_refreshes_because_the_user_is_already_here(
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


def test_the_submitted_scene_carries_live_track_counts(
    album_of: AlbumFactory,
) -> None:
    # The player's seam is where the live read happens, so a scene it submits
    # already knows the on-disk count -- not the empty creation-time snapshot.
    album = album_of("aa11bb", name="Techno Mix", tracks=0, on_disk=9)
    publisher = _CapturingPublisher()

    MusicPlayer(
        _FakeService(ProgramStatus.idle(), (album,)), publisher
    ).notify_changed()

    assert _by_id(publisher.submitted[0].elements, "music.albums")["rows"] == [
        ["Techno Mix", "techno", 9]
    ]


def test_an_album_deleted_since_the_catalog_read_drops_out_coherently(
    album_of: AlbumFactory,
) -> None:
    # The catalog is in memory and the album is on disk, so the two can disagree.
    # A row whose album has been deleted disappears from the table AND from the
    # count label -- one render, one coherent view of the catalog.
    kept = album_of("aa11bb", name="Kept")
    gone = album_of("cc22dd", name="Gone", fails_with=LookupError("deleted"))
    publisher = _CapturingPublisher()

    MusicPlayer(
        _FakeService(ProgramStatus.idle(), (kept, gone)), publisher
    ).notify_changed()

    elements = publisher.submitted[0].elements
    assert _by_id(elements, "music.albums")["rows"] == [["Kept", "techno", 3]]
    assert _by_id(elements, "music.albums.label")["content"] == "Albums · 1 album"


def test_present_stop_failure_surfaces_the_stop_warning(album_of: AlbumFactory) -> None:
    service = _FakeService(ProgramStatus.idle(), (album_of("aa11bb"),))
    publisher = _CapturingPublisher()

    MusicPlayer(service, publisher).present_stop_failure()

    elements = publisher.submitted[-1].elements
    assert _by_id(elements, "music.status")["content"] == "⚠ couldn't stop the music"
