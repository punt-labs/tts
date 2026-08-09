"""Tests for the shared music fault contract (music_faults)."""

from __future__ import annotations

import json

import pytest

from punt_vox.client_errors import VoxdConnectionError, VoxdProtocolError
from punt_vox.music_faults import DAEMON_ERRORS, MusicFault


class TestDaemonErrors:
    """The catch set every music verb shares."""

    def test_covers_both_client_failures(self) -> None:
        assert {VoxdConnectionError, VoxdProtocolError} <= set(DAEMON_ERRORS)

    def test_covers_os_level_transport_failure(self) -> None:
        assert OSError in DAEMON_ERRORS

    @pytest.mark.parametrize("exc_type", DAEMON_ERRORS)
    def test_every_member_is_catchable(self, exc_type: type[BaseException]) -> None:
        assert issubclass(exc_type, BaseException)


class TestMusicFault:
    """The one shape every music failure reaches the caller in."""

    def test_of_reports_the_exception_message(self) -> None:
        payload = json.loads(MusicFault.of(VoxdConnectionError("voxd is down")))
        assert payload == {"error": "voxd is down"}

    def test_rejecting_reports_the_verbs_own_message(self) -> None:
        payload = json.loads(MusicFault.rejecting("music get requires album_id"))
        assert payload == {"error": "music get requires album_id"}

    def test_renders_only_the_error_key(self) -> None:
        payload = json.loads(MusicFault.rejecting("nope"))
        assert list(payload) == ["error"]

    def test_multi_line_message_survives_the_envelope(self) -> None:
        """A no-history reject carries its saved-album list through the JSON."""
        payload = json.loads(MusicFault.rejecting("no history\nsaved albums:\n  a"))
        assert payload["error"].splitlines() == ["no history", "saved albums:", "  a"]

    def test_of_and_rejecting_agree_on_shape(self) -> None:
        """A daemon fault and a verb reject are indistinguishable to a client."""
        assert MusicFault.of(ValueError("x")) == MusicFault.rejecting("x")

    def test_render_is_the_shape_the_classmethods_return(self) -> None:
        assert MusicFault("boom").render() == MusicFault.rejecting("boom")
