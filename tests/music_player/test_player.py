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
    """A ScenePublisher double that records every submitted scene."""

    def __init__(self) -> None:
        self.submitted: list[RenderRequest] = []

    def submit(self, request: RenderRequest) -> None:
        self.submitted.append(request)


def test_notify_changed_projects_the_playing_scene_and_submits_once(
    album_of: AlbumFactory, playing_of: PlayingFactory
) -> None:
    album = album_of("aa11bb", name="Techno Mix")
    service = _FakeService(playing_of(album, 1, 3), (album,))
    publisher = _CapturingPublisher()

    MusicPlayer(service, publisher).notify_changed()

    assert len(publisher.submitted) == 1
    request = publisher.submitted[0]
    assert request.scene_id == "vox.music"
    assert "Techno Mix" in str(request.elements[1]["content"])
    assert "1 of 3" in str(request.elements[1]["content"])


def test_notify_changed_projects_the_idle_scene(album_of: AlbumFactory) -> None:
    service = _FakeService(ProgramStatus.idle(), (album_of("aa11bb"),))
    publisher = _CapturingPublisher()

    MusicPlayer(service, publisher).notify_changed()

    assert publisher.submitted[0].elements[1]["content"] == "Nothing playing"


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
    assert failed[1]["content"] == "Nothing playing"  # I2: idle stays idle
    assert failed[2] == {
        "kind": "text",
        "id": "music.status",
        "content": "⚠ couldn't play Techno Mix — it has no tracks yet",
    }

    player.notify_changed()
    assert publisher.submitted[-1].elements[2]["content"] == ""  # cleared in place


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
    assert "Techno Mix" in str(elements[1]["content"])  # I2: the source still shows
    assert "2 of 3" in str(elements[1]["content"])
    assert elements[2]["content"] == "⚠ couldn't play ff99ee — no longer in the crate"


def test_present_stop_failure_surfaces_the_stop_warning(album_of: AlbumFactory) -> None:
    service = _FakeService(ProgramStatus.idle(), (album_of("aa11bb"),))
    publisher = _CapturingPublisher()

    MusicPlayer(service, publisher).present_stop_failure()

    assert publisher.submitted[-1].elements[2]["content"] == "⚠ couldn't stop the music"
