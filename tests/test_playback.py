"""Tests for punt_vox.playback."""

from __future__ import annotations

import logging
import subprocess
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import MagicMock, patch

from punt_vox.playback import PLAYBACK_TIMEOUT, enqueue, play_audio

if TYPE_CHECKING:
    import pytest

_MOD = "punt_vox.playback"


class TestResolvePlayer:
    def test_prefers_afplay(self) -> None:
        from punt_vox.playback import resolve_player

        with patch(f"{_MOD}.shutil.which", side_effect=lambda cmd: cmd):  # pyright: ignore[reportUnknownLambdaType]
            result = resolve_player()
        assert result == ["afplay"]

    def test_falls_back_to_ffplay(self) -> None:
        from punt_vox.playback import resolve_player

        def which(cmd: str) -> str | None:  # pyright: ignore[reportUnknownParameterType]
            return cmd if cmd == "ffplay" else None

        with patch(f"{_MOD}.shutil.which", side_effect=which):
            result = resolve_player()
        assert result[0] == "ffplay"
        assert "-nodisp" in result

    def test_raises_when_no_player(self) -> None:
        import pytest

        from punt_vox.playback import resolve_player

        with (
            patch(f"{_MOD}.shutil.which", return_value=None),
            pytest.raises(FileNotFoundError),
        ):
            resolve_player()


class TestPlayAudio:
    def test_acquires_lock_and_calls_player(self, tmp_path: Path) -> None:
        audio = tmp_path / "test.mp3"
        audio.write_bytes(b"fake")

        with (
            patch(f"{_MOD}.resolve_player", return_value=["afplay"]),
            patch(f"{_MOD}.subprocess.run") as mock_run,
            patch(f"{_MOD}.LOCK_FILE", tmp_path / "playback.lock"),
        ):
            play_audio(audio)

        mock_run.assert_called_once_with(
            ["afplay", str(audio)],
            check=False,
            timeout=PLAYBACK_TIMEOUT,
            stderr=subprocess.PIPE,
        )

    def test_returns_failure_detail_on_nonzero_exit(self, tmp_path: Path) -> None:
        """A non-zero player exit is reported so vox play can exit non-zero."""
        audio = tmp_path / "test.mp3"
        audio.write_bytes(b"fake")
        proc = MagicMock()
        proc.returncode = 1
        proc.stderr = b"AudioFileOpen failed ('typ?')"

        with (
            patch(f"{_MOD}.resolve_player", return_value=["afplay"]),
            patch(f"{_MOD}.subprocess.run", return_value=proc),
            patch(f"{_MOD}.LOCK_FILE", tmp_path / "playback.lock"),
        ):
            result = play_audio(audio)

        assert result is not None
        assert "rc=1" in result
        assert "AudioFileOpen failed" in result

    def test_returns_none_on_clean_play(self, tmp_path: Path) -> None:
        """A clean, real-duration play returns None (success)."""
        audio = tmp_path / "test.mp3"
        audio.write_bytes(b"fake")
        proc = MagicMock()
        proc.returncode = 0
        proc.stderr = b""

        with (
            patch(f"{_MOD}.resolve_player", return_value=["afplay"]),
            patch(f"{_MOD}.subprocess.run", return_value=proc),
            patch(f"{_MOD}.time.monotonic", side_effect=[0.0, 1.0]),
            patch(f"{_MOD}.LOCK_FILE", tmp_path / "playback.lock"),
        ):
            assert play_audio(audio) is None

    def test_failure_detail_is_logged(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A failure detail is logged (WARNING) so detached playback is diagnosable."""
        audio = tmp_path / "test.mp3"
        audio.write_bytes(b"fake")
        proc = MagicMock()
        proc.returncode = 1
        proc.stderr = b"AudioFileOpen failed"

        with (
            patch(f"{_MOD}.resolve_player", return_value=["afplay"]),
            patch(f"{_MOD}.subprocess.run", return_value=proc),
            patch(f"{_MOD}.LOCK_FILE", tmp_path / "playback.lock"),
            caplog.at_level(logging.WARNING, logger=_MOD),
        ):
            detail = play_audio(audio)

        assert detail is not None
        assert "Playback failed" in caplog.text
        assert "AudioFileOpen failed" in caplog.text

    def test_returns_failure_when_played_nothing(self, tmp_path: Path) -> None:
        """A clean exit under the suspicious-elapsed floor is a failure."""
        audio = tmp_path / "test.mp3"
        audio.write_bytes(b"fake")
        proc = MagicMock()
        proc.returncode = 0
        proc.stderr = b""

        with (
            patch(f"{_MOD}.resolve_player", return_value=["afplay"]),
            patch(f"{_MOD}.subprocess.run", return_value=proc),
            patch(f"{_MOD}.time.monotonic", side_effect=[0.0, 0.001]),
            patch(f"{_MOD}.LOCK_FILE", tmp_path / "playback.lock"),
        ):
            result = play_audio(audio)

        assert result is not None
        assert "played nothing" in result

    def test_creates_lock_parent_directory(self, tmp_path: Path) -> None:
        lock_file = tmp_path / "nested" / "dir" / "playback.lock"
        audio = tmp_path / "test.mp3"
        audio.write_bytes(b"fake")

        with (
            patch(f"{_MOD}.resolve_player", return_value=["afplay"]),
            patch(f"{_MOD}.subprocess.run"),
            patch(f"{_MOD}.LOCK_FILE", lock_file),
        ):
            play_audio(audio)

        assert lock_file.parent.exists()

    def test_handles_no_player(self, tmp_path: Path) -> None:
        audio = tmp_path / "test.mp3"
        audio.write_bytes(b"fake")

        with (
            patch(
                f"{_MOD}.resolve_player",
                side_effect=FileNotFoundError("no player"),
            ),
            patch(f"{_MOD}.LOCK_FILE", tmp_path / "playback.lock"),
        ):
            play_audio(audio)  # should not raise

    def test_handles_timeout(self, tmp_path: Path) -> None:
        audio = tmp_path / "test.mp3"
        audio.write_bytes(b"fake")

        with (
            patch(f"{_MOD}.resolve_player", return_value=["afplay"]),
            patch(
                f"{_MOD}.subprocess.run",
                side_effect=subprocess.TimeoutExpired("afplay", PLAYBACK_TIMEOUT),
            ),
            patch(f"{_MOD}.LOCK_FILE", tmp_path / "playback.lock"),
        ):
            play_audio(audio)  # should not raise

    def test_concurrent_calls_serialize(self, tmp_path: Path) -> None:
        """Two concurrent play_audio calls should not overlap.

        Uses an Event to ensure thread 2 is started and contending
        for the lock while thread 1 holds it, preventing false
        positives from scheduler timing.
        """
        lock_file = tmp_path / "playback.lock"
        audio = tmp_path / "test.mp3"
        audio.write_bytes(b"fake")

        order: list[str] = []
        t1_holding_lock = threading.Event()

        def fake_play(*args: object, **kwargs: object) -> MagicMock:
            order.append("start")
            t1_holding_lock.set()
            time.sleep(0.1)
            order.append("end")
            return MagicMock(returncode=0, stderr=b"")

        with (
            patch(f"{_MOD}.resolve_player", return_value=["afplay"]),
            patch(f"{_MOD}.subprocess.run", side_effect=fake_play),
            patch(f"{_MOD}.LOCK_FILE", lock_file),
        ):
            t1 = threading.Thread(target=play_audio, args=(audio,))
            t2 = threading.Thread(target=play_audio, args=(audio,))
            t1.start()
            t1_holding_lock.wait()  # t1 is inside fake_play (lock held)
            t2.start()  # t2 will block on flock until t1 releases
            t1.join()
            t2.join()

        # Serialized: start-end-start-end, not overlapping start-start-end-end.
        assert order == ["start", "end", "start", "end"]

    def test_uses_ffplay_when_no_afplay(self, tmp_path: Path) -> None:
        audio = tmp_path / "test.mp3"
        audio.write_bytes(b"fake")

        def which(cmd: str) -> str | None:
            return cmd if cmd == "ffplay" else None

        with (
            patch(f"{_MOD}.shutil.which", side_effect=which),
            patch(f"{_MOD}.subprocess.run") as mock_run,
            patch(f"{_MOD}.LOCK_FILE", tmp_path / "playback.lock"),
        ):
            play_audio(audio)

        cmd = mock_run.call_args[0][0]
        assert cmd[0] == "ffplay"
        assert str(audio) in cmd


class TestEnqueue:
    def test_spawns_detached_subprocess(self, tmp_path: Path) -> None:
        audio = tmp_path / "test.mp3"
        audio.write_bytes(b"fake")
        pending_dir = tmp_path / "pending"

        mock_popen = MagicMock()
        with (
            patch(f"{_MOD}.subprocess.Popen", return_value=mock_popen) as popen_cls,
            patch(f"{_MOD}._PENDING_DIR", pending_dir),
        ):
            enqueue(audio)

        call_args = popen_cls.call_args
        assert call_args is not None
        assert call_args[1]["start_new_session"] is True
        assert pending_dir.exists()

    def test_handles_spawn_failure(self, tmp_path: Path) -> None:
        audio = tmp_path / "test.mp3"
        audio.write_bytes(b"fake")
        pending_dir = tmp_path / "pending"

        with (
            patch(f"{_MOD}.subprocess.Popen", side_effect=OSError("spawn failed")),
            patch(f"{_MOD}._PENDING_DIR", pending_dir),
        ):
            enqueue(audio)  # should not raise

    def test_copies_file_to_pending_dir(self, tmp_path: Path) -> None:
        """Enqueue copies the file so the original can be safely deleted."""
        audio = tmp_path / "test.mp3"
        audio.write_bytes(b"fake-audio-data")
        pending_dir = tmp_path / "pending"

        mock_popen = MagicMock()
        with (
            patch(f"{_MOD}.subprocess.Popen", return_value=mock_popen),
            patch(f"{_MOD}._PENDING_DIR", pending_dir),
        ):
            enqueue(audio)

        pending_files = list(pending_dir.iterdir())
        assert len(pending_files) == 1
        assert pending_files[0].read_bytes() == b"fake-audio-data"
