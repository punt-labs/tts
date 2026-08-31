"""Pins for the sentinel stubs that keep the real vox surface unreachable.

The h7k8 rule: a spawned session must find recording stand-ins for `vox`
and `vox-panel` first on PATH, so no real panel and no live Lux hub is
reachable, and any attempt is evidence rather than a side effect.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from stubs import SentinelStubs


def _run_stub(stubs: SentinelStubs, name: str, *args: str) -> int:
    env = dict(os.environ)
    env["PATH"] = stubs.path_env(env["PATH"])
    probe = subprocess.run(
        [name, *args], env=env, check=False, capture_output=True, text=True
    )
    return probe.returncode


class TestSentinelStubs:
    """Create, intercept, record."""

    def test_stubbed_names_resolve_first_on_path(self, tmp_path: Path) -> None:
        stubs = SentinelStubs(tmp_path / "stubs")
        stubs.create()
        env_path = stubs.path_env("/usr/bin")
        assert env_path.startswith(str(stubs.bin_dir))
        for name in ("vox", "vox-panel"):
            assert (stubs.bin_dir / name).exists()

    def test_invocation_is_recorded_and_harmless(self, tmp_path: Path) -> None:
        stubs = SentinelStubs(tmp_path / "stubs")
        stubs.create()
        code = _run_stub(stubs, "vox-panel", "--session-pid", "123")
        assert code == 0
        assert any("vox-panel" in line for line in stubs.invocations())
        assert any("--session-pid 123" in line for line in stubs.invocations())

    def test_no_invocations_means_empty_evidence(self, tmp_path: Path) -> None:
        stubs = SentinelStubs(tmp_path / "stubs")
        stubs.create()
        assert stubs.invocations() == ()

    def test_missing_log_is_loud_not_zero_hits(self, tmp_path: Path) -> None:
        # A removed (or never-created) log must never read as "zero hits":
        # that is exactly how a teardown-before-harvest bug fabricates a
        # clean isolation claim.
        stubs = SentinelStubs(tmp_path / "stubs")
        with pytest.raises(FileNotFoundError, match="invocation log"):
            stubs.invocations()
