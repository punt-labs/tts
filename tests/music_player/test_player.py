"""Tests for MusicPlayer: notify_changed projects fresh state and submits once."""

from __future__ import annotations

from typing import TYPE_CHECKING, final

from punt_vox.types_programs.status import ProgramStatus
from punt_vox.voxd.music_player.player import MusicPlayer

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
