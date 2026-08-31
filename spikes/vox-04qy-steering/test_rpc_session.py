"""Pins for the RPC session driver, against a fake peer — no pi, no spend.

The fake peer speaks the confirmed wire shape: an ack ``response`` per
command, a canned event burst per ``prompt``. What these tests pin is the
driver's own behavior — argv construction, stamped send/recv ordering,
predicate waits with timeouts, and a close that never leaves the child
running.
"""

from __future__ import annotations

import os
import sys
import textwrap
from pathlib import Path

import pytest

from rpc_protocol import RpcCommand
from rpc_session import PiRpcSession, PiSpec

_FAKE_PEER = textwrap.dedent(
    """\
    import json, sys

    print(json.dumps({"type": "hello"}), flush=True)
    for line in sys.stdin:
        command = json.loads(line)
        kind = command["type"]
        print(
            json.dumps({"type": "response", "command": kind, "success": True}),
            flush=True,
        )
        if kind == "prompt":
            print(json.dumps({"type": "agent_start"}), flush=True)
            print(
                json.dumps({"type": "message_update", "text": "MARKER-ONE"}),
                flush=True,
            )
            print(json.dumps({"type": "agent_end"}), flush=True)
    """
)


@pytest.fixture
def peer_argv(tmp_path: Path) -> list[str]:
    script = tmp_path / "fake_peer.py"
    script.write_text(_FAKE_PEER, encoding="utf-8")
    return [sys.executable, str(script)]


def _spawn(peer_argv: list[str], tmp_path: Path) -> PiRpcSession:
    return PiRpcSession.spawn(
        peer_argv, cwd=tmp_path, stderr_path=tmp_path / "peer_stderr.log"
    )


class TestPiSpec:
    """The exact pi invocation, pure and assertable."""

    def test_argv_carries_rpc_isolation_and_model_flags(self) -> None:
        spec = PiSpec(
            binary=Path("/opt/pi"),
            provider="anthropic",
            model="claude-haiku-4-5",
            tools=("read", "grep", "find", "ls"),
        )
        assert spec.to_argv() == [
            "/opt/pi",
            "--mode",
            "rpc",
            "--no-session",
            "--no-extensions",
            "--provider",
            "anthropic",
            "--model",
            "claude-haiku-4-5",
            "--tools",
            "read,grep,find,ls",
        ]

    def test_empty_tools_means_no_tools_flag(self) -> None:
        spec = PiSpec(binary=Path("/opt/pi"), provider="anthropic", model="m", tools=())
        assert "--tools" not in spec.to_argv()


class TestPiRpcSession:
    """Spawn, converse, and tear down against the fake peer."""

    def test_send_then_wait_for_sees_the_ack(
        self, peer_argv: list[str], tmp_path: Path
    ) -> None:
        session = _spawn(peer_argv, tmp_path)
        try:
            session.send(RpcCommand.prompt("go"))
            ack = session.wait_for(
                lambda event: event.is_response_to("prompt"),
                timeout_s=10,
                description="prompt ack",
            )
            assert ack.data["success"] is True
            end = session.wait_for(
                lambda event: event.type == "agent_end",
                timeout_s=10,
                description="agent_end",
            )
            assert end.recv_ns > ack.recv_ns
        finally:
            session.close()

    def test_transcript_orders_send_before_its_ack(
        self, peer_argv: list[str], tmp_path: Path
    ) -> None:
        session = _spawn(peer_argv, tmp_path)
        try:
            session.send(RpcCommand.steer("turn left"))
            session.wait_for(
                lambda event: event.is_response_to("steer"),
                timeout_s=10,
                description="steer ack",
            )
        finally:
            session.close()
        entries = session.transcript.entries()
        send_index = next(
            index for index, entry in enumerate(entries) if entry.direction == "send"
        )
        ack_index = next(
            index
            for index, entry in enumerate(entries)
            if entry.direction == "recv" and '"steer"' in entry.text
        )
        assert send_index < ack_index

    def test_wait_for_times_out_loudly(
        self, peer_argv: list[str], tmp_path: Path
    ) -> None:
        session = _spawn(peer_argv, tmp_path)
        try:
            with pytest.raises(TimeoutError, match="never_happens"):
                session.wait_for(
                    lambda event: event.type == "never_happens",
                    timeout_s=0.3,
                    description="never_happens",
                )
        finally:
            session.close()

    def test_close_reaps_the_child(self, peer_argv: list[str], tmp_path: Path) -> None:
        session = _spawn(peer_argv, tmp_path)
        pid = session.pid
        session.close()
        # A reaped child is gone: signal 0 must fail (or the child is a
        # zombie already collected by wait()).
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)

    def test_close_twice_is_a_no_op(self, peer_argv: list[str], tmp_path: Path) -> None:
        session = _spawn(peer_argv, tmp_path)
        session.close()
        session.close()

    def test_spawn_env_reaches_the_child(self, tmp_path: Path) -> None:
        echo_env = tmp_path / "echo_env.py"
        echo_env.write_text(
            "import json, os\n"
            'print(json.dumps({"type": "hello",'
            ' "marker": os.environ.get("SPIKE_MARKER", "")}), flush=True)\n',
            encoding="utf-8",
        )
        env = dict(os.environ)
        env["SPIKE_MARKER"] = "vox04qy"
        session = PiRpcSession.spawn(
            [sys.executable, str(echo_env)],
            cwd=tmp_path,
            stderr_path=tmp_path / "stderr.log",
            env=env,
        )
        try:
            hello = session.wait_for(
                lambda event: event.type == "hello", timeout_s=10, description="hello"
            )
            assert hello.data["marker"] == "vox04qy"
        finally:
            session.close()
