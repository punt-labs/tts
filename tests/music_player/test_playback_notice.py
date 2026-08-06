"""Tests for PlaybackNotice: the transient status the scene projection carries.

The named constructors own every warning phrase, so these pin the exact user-facing
text and the catalogued-vs-vanished distinction the receive leg surfaces on a refused
play.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from punt_vox.voxd.music_player.playback_notice import PlaybackNotice
from punt_vox.voxd.programs.album_id import AlbumId

if TYPE_CHECKING:
    from collections.abc import Callable

    from punt_vox.voxd.programs.catalog import Album

    type AlbumFactory = Callable[..., Album]


def test_silent_notice_is_empty_and_absent() -> None:
    notice = PlaybackNotice.silent()
    assert notice.message == ""
    assert notice.is_present is False


def test_warning_notice_carries_its_message() -> None:
    notice = PlaybackNotice.warning("⚠ boom")
    assert notice.message == "⚠ boom"
    assert notice.is_present is True


def test_play_failed_names_a_catalogued_empty_album(album_of: AlbumFactory) -> None:
    # Still in the crate but unplayable -> "no tracks yet", named by its curated name.
    album = album_of("aa11bb", name="Techno Mix")
    notice = PlaybackNotice.play_failed(AlbumId("aa11bb"), (album,))
    assert notice.message == "⚠ couldn't play Techno Mix — it has no tracks yet"


def test_play_failed_reports_a_vanished_album_by_id(album_of: AlbumFactory) -> None:
    # No lookup finds it -> "no longer in the crate", falling back to the bare id.
    other = album_of("cc22dd", name="Ambient Drift")
    notice = PlaybackNotice.play_failed(AlbumId("aa11bb"), (other,))
    assert notice.message == "⚠ couldn't play aa11bb — no longer in the crate"


def test_play_failed_uses_the_album_id_when_unnamed(album_of: AlbumFactory) -> None:
    album = album_of("aa11bb", name=None)
    notice = PlaybackNotice.play_failed(AlbumId("aa11bb"), (album,))
    assert notice.message == "⚠ couldn't play album aa11bb — it has no tracks yet"


def test_stop_failed_message() -> None:
    assert PlaybackNotice.stop_failed().message == "⚠ couldn't stop the music"
