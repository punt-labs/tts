"""Offline pin for AlsaAudio's flush ordering invariant.

flush() must reassign ``self._playback`` BEFORE killing the old process:
_write_chunk disambiguates a barge-in flush from aplay genuinely dying
by checking whether the shared reference moved on, so a kill that lands
before the reassignment turns every normal barge-in into a false
``aplay_died`` note plus an orphaned respawn. No real ALSA processes:
the spawn seam is monkeypatched.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, final

from convai import EventTrace
from run_live import AlsaAudio

if TYPE_CHECKING:
    from pathlib import Path

    import pytest


class TestFlushOrdering:
    """The kill-induced pipe error is only observable after reassignment."""

    async def test_reassign_happens_before_kill(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("run_live.shutil.which", lambda _binary: "/bin/true")
        audio = AlsaAudio(EventTrace(tmp_path / "trace.jsonl"))
        reassigned_first: list[bool] = []
        reap_timeouts: list[float] = []

        @final
        class _Probe:
            """Stands in for the aplay Popen; records ordering at kill time."""

            def kill(self) -> None:
                # The invariant under test: by the time the old process is
                # killed, the shared reference already points elsewhere.
                # (Reaching into _playback is the point -- the invariant is
                # about this attribute's ordering vs the kill.)
                reassigned_first.append(audio._playback is not self)

            def wait(self, timeout: float) -> int:
                reap_timeouts.append(timeout)  # the kill was also reaped
                return 0

        spawned = iter((_Probe(), _Probe()))
        monkeypatch.setattr(
            AlsaAudio, "_spawn_playback", staticmethod(lambda: next(spawned))
        )
        await audio.flush()  # no old process yet: installs the first probe
        await audio.flush()  # must reassign to the second BEFORE killing the first
        assert reassigned_first == [True]
        assert len(reap_timeouts) == 1  # the killed probe was reaped, no zombie
