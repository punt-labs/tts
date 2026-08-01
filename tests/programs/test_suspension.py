"""Tests for ``PlaybackSuspension`` -- the click-free pause/resume seam.

With mpv, pause is a single ``set_property pause`` on the one persistent process
and resume is its inverse; there is no teardown and no seek to reconstruct. The
suspension holds just the authoritative paused flag and delegates the click-free
IPC to the injected player, so these tests assert the flag transitions and the
delegated control calls, and that ``reset`` (a switch or off) clears the flag
without an IPC of its own.
"""

from __future__ import annotations

from punt_vox.voxd.programs.suspension import PlaybackSuspension

from ._mpv_fakes import FakePlayer


def test_new_suspension_is_not_paused() -> None:
    suspension = PlaybackSuspension(FakePlayer())
    assert suspension.is_paused is False


def test_pause_sets_the_flag_and_delegates_to_the_player() -> None:
    player = FakePlayer()
    suspension = PlaybackSuspension(player)

    suspension.pause()

    assert suspension.is_paused is True
    assert player.pauses == 1  # the click-free set_property pause went to the player


def test_resume_clears_the_flag_and_delegates_to_the_player() -> None:
    player = FakePlayer()
    suspension = PlaybackSuspension(player)
    suspension.pause()

    suspension.resume()

    assert suspension.is_paused is False
    assert player.resumes == 1  # the click-free un-pause went to the player


def test_pause_is_idempotent() -> None:
    player = FakePlayer()
    suspension = PlaybackSuspension(player)
    suspension.pause()
    suspension.pause()  # already paused -> no second IPC
    assert player.pauses == 1


def test_resume_is_idempotent() -> None:
    player = FakePlayer()
    suspension = PlaybackSuspension(player)
    suspension.resume()  # not paused -> a no-op
    assert suspension.is_paused is False
    assert player.resumes == 0


def test_reset_clears_the_flag_without_an_ipc() -> None:
    # A stop/switch resets: the flag clears and no control command is delegated --
    # the loop's stop (off) or next loadfile (switch) drives the mpv side.
    player = FakePlayer()
    suspension = PlaybackSuspension(player)
    suspension.pause()

    suspension.reset()

    assert suspension.is_paused is False
    assert player.pauses == 1  # only the earlier pause; reset added no IPC
    assert player.resumes == 0
    assert player.stops == 0
