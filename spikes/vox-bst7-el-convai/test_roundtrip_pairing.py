"""Offline sanity tests for client_tool_call -> client_tool_result pairing.

The p95 verdict is only as good as the pairing that produces each round-trip
sample. These tests drive the real ConvAISession against a scripted localhost
WebSocket server speaking the EL Conv AI event shapes -- no real API, no
credits -- and pin: correct pairing under interleaved calls, orphan behavior
(call with no follow-up agent event), and error-result completion.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
from typing import TYPE_CHECKING

import pytest
from websockets.asyncio.server import serve

from convai import ConvAISession, EventTrace, SessionMetrics, ToolInvocation
from spike_tools import ToolBelt

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from pathlib import Path

    from websockets.asyncio.server import ServerConnection

    type ServerScript = Callable[
        [ServerConnection, list[dict[str, object]]], Awaitable[None]
    ]
    type Settled = Callable[[SessionMetrics], bool]

_INIT_EVENT: dict[str, object] = {
    "type": "conversation_initiation_metadata",
    "conversation_initiation_metadata_event": {"conversation_id": "conv-test"},
}
_AGENT_PROGRESS: dict[str, object] = {
    "type": "agent_response",
    "agent_response_event": {"agent_response": "done."},
}


class TestToolInvocationMath:
    """Pure timing arithmetic on one invocation record."""

    def test_handling_total_overhead_from_known_timestamps(self) -> None:
        inv = ToolInvocation(
            tool_name="clock",
            tool_call_id="c1",
            t_call=100.0,
            exec_ms=250.0,
            t_result=100.4,
            t_next_event=101.0,
        )
        assert inv.handling_ms == pytest.approx(400.0)  # (100.4 - 100.0) s
        assert inv.total_ms == pytest.approx(1000.0)  # (101.0 - 100.0) s
        assert inv.overhead_ms == pytest.approx(750.0)  # total - exec
        assert inv.is_complete

    def test_orphan_without_result_raises(self) -> None:
        inv = ToolInvocation(tool_name="clock", tool_call_id="c2", t_call=1.0)
        with pytest.raises(ValueError, match="has no result yet"):
            _ = inv.handling_ms
        with pytest.raises(ValueError, match="saw no follow-up event"):
            _ = inv.total_ms
        with pytest.raises(ValueError, match="never executed"):
            _ = inv.overhead_ms
        assert not inv.is_complete

    def test_result_without_next_event_is_incomplete(self) -> None:
        inv = ToolInvocation(
            tool_name="clock",
            tool_call_id="c3",
            t_call=1.0,
            exec_ms=5.0,
            t_result=1.1,
        )
        assert inv.handling_ms == pytest.approx(100.0)
        assert not inv.is_complete
        with pytest.raises(ValueError, match="saw no follow-up event"):
            _ = inv.total_ms

    def test_completed_invocations_excludes_orphans(self) -> None:
        done = ToolInvocation(
            tool_name="clock",
            tool_call_id="d1",
            t_call=1.0,
            exec_ms=1.0,
            t_result=1.1,
            t_next_event=1.2,
        )
        orphan = ToolInvocation(tool_name="clock", tool_call_id="d2", t_call=2.0)
        metrics = SessionMetrics(invocations=[done, orphan])
        assert metrics.completed_invocations == [done]


def _tool_call(name: str, call_id: str, params: dict[str, object]) -> str:
    return json.dumps(
        {
            "type": "client_tool_call",
            "client_tool_call": {
                "tool_name": name,
                "tool_call_id": call_id,
                "parameters": params,
            },
        }
    )


async def _wait_until(predicate: Callable[[], bool], timeout_s: float = 5.0) -> None:
    deadline = time.monotonic() + timeout_s
    while not predicate():
        if time.monotonic() > deadline:
            msg = "session did not reach the expected state within timeout"
            raise TimeoutError(msg)
        await asyncio.sleep(0.01)


async def _run_session(
    tmp_path: Path, script: ServerScript, settled: Settled
) -> tuple[SessionMetrics, list[dict[str, object]]]:
    """Serve one scripted conversation on localhost; return metrics + results.

    ``script`` plays the EL server side after initiation; ``results``
    accumulates the client_tool_result payloads the session sends back.
    """
    results: list[dict[str, object]] = []

    async def handler(ws: ServerConnection) -> None:
        await ws.recv()  # conversation_initiation_client_data
        await ws.send(json.dumps(_INIT_EVENT))
        await script(ws, results)

    async with serve(handler, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        session = ConvAISession(
            url=f"ws://127.0.0.1:{port}",
            toolbelt=ToolBelt(tmp_path / "notes.md"),
            trace=EventTrace(tmp_path / "trace.jsonl"),
            overrides={},
        )
        await session.open(timeout_s=5.0)
        try:
            await _wait_until(lambda: settled(session.metrics))
        finally:
            with contextlib.suppress(Exception):  # teardown must not mask asserts
                await session.close()
    return session.metrics, results


class TestRoundTripPairing:
    """The session pairs results to calls by tool_call_id, not by order."""

    async def test_interleaved_calls_pair_by_id(self, tmp_path: Path) -> None:
        async def script(
            ws: ServerConnection, results: list[dict[str, object]]
        ) -> None:
            # Two calls in flight at once, different tools, distinct ids.
            await ws.send(_tool_call("clock", "call-a", {}))
            await ws.send(_tool_call("write_note", "call-b", {"text": "pairing probe"}))
            results.extend(
                [
                    dict(json.loads(await ws.recv())),
                    dict(json.loads(await ws.recv())),
                ]
            )
            await ws.send(json.dumps(_AGENT_PROGRESS))

        def settled(metrics: SessionMetrics) -> bool:
            return len(metrics.invocations) == 2 and all(
                inv.is_complete for inv in metrics.invocations
            )

        metrics, results = await _run_session(tmp_path, script, settled)

        # Session-side pairing: each invocation carries its own id and name.
        by_id = {inv.tool_call_id: inv for inv in metrics.invocations}
        assert set(by_id) == {"call-a", "call-b"}
        assert by_id["call-a"].tool_name == "clock"
        assert by_id["call-b"].tool_name == "write_note"

        # Wire-side pairing: each result rode back under the matching id and
        # carries the matching tool's output, regardless of arrival order.
        result_by_id = {str(r["tool_call_id"]): r for r in results}
        assert set(result_by_id) == {"call-a", "call-b"}
        assert str(result_by_id["call-a"]["result"]).startswith("Current time:")
        assert str(result_by_id["call-b"]["result"]).startswith("Note saved")
        assert all(r["is_error"] is False for r in results)

        # Co-scheduling flag: with two tools in flight, the first result is
        # sent while the other still executes (not clean), the second is not.
        assert sorted(inv.is_clean for inv in metrics.invocations) == [False, True]

        # Timing invariants every sample in the verdict depends on.
        for inv in metrics.invocations:
            exec_ms = inv.exec_ms
            assert exec_ms is not None
            assert inv.handling_ms >= exec_ms  # handling wraps execution
            assert inv.total_ms >= inv.handling_ms  # close event after result
            assert inv.overhead_ms == pytest.approx(inv.total_ms - exec_ms)

        note = (tmp_path / "notes.md").read_text(encoding="utf-8")
        assert "pairing probe" in note

    async def test_orphaned_call_stays_incomplete(self, tmp_path: Path) -> None:
        async def script(
            ws: ServerConnection, results: list[dict[str, object]]
        ) -> None:
            await ws.send(_tool_call("clock", "orphan-1", {}))
            results.append(dict(json.loads(await ws.recv())))
            # No agent progress event follows: the round trip never closes.

        def settled(metrics: SessionMetrics) -> bool:
            return bool(metrics.invocations) and (
                metrics.invocations[0].t_result is not None
            )

        metrics, results = await _run_session(tmp_path, script, settled)

        assert len(results) == 1  # the result was sent...
        inv = metrics.invocations[0]
        assert not inv.is_complete  # ...but the sample never completed
        assert metrics.completed_invocations == []  # and cannot reach the stats
        with pytest.raises(ValueError, match="saw no follow-up event"):
            _ = inv.total_ms

    async def test_duplicate_tool_call_id_does_not_corrupt_the_samples(
        self, tmp_path: Path
    ) -> None:
        async def script(
            ws: ServerConnection, results: list[dict[str, object]]
        ) -> None:
            # EL re-delivers the same call id, then the conversation moves on.
            await ws.send(_tool_call("clock", "dup-1", {}))
            await ws.send(_tool_call("clock", "dup-1", {}))
            results.append(dict(json.loads(await ws.recv())))
            # A second result may or may not arrive depending on how the
            # session deduplicates; drain briefly without insisting.
            with contextlib.suppress(TimeoutError):
                async with asyncio.timeout(0.5):
                    results.append(dict(json.loads(await ws.recv())))
            await ws.send(_tool_call("clock", "after-dup", {}))
            results.append(dict(json.loads(await ws.recv())))
            await ws.send(json.dumps(_AGENT_PROGRESS))

        def settled(metrics: SessionMetrics) -> bool:
            done = {inv.tool_call_id for inv in metrics.completed_invocations}
            return {"dup-1", "after-dup"} <= done

        metrics, results = await _run_session(tmp_path, script, settled)

        # The session survived the duplicate and kept measuring.
        completed_ids = [inv.tool_call_id for inv in metrics.completed_invocations]
        assert completed_ids.count("after-dup") == 1
        assert completed_ids.count("dup-1") == 1
        assert {str(r["tool_call_id"]) for r in results} == {"dup-1", "after-dup"}
        # No phantom half-finished record may linger: a dead fire-and-forget
        # task would leave an invocation that never completes, silently
        # understating the sample set in the run record.
        assert len(metrics.invocations) == len(metrics.completed_invocations)

    async def test_error_results_still_complete_and_are_flagged(
        self, tmp_path: Path
    ) -> None:
        async def script(
            ws: ServerConnection, results: list[dict[str, object]]
        ) -> None:
            # Invalid params (empty note) and an unknown tool name.
            await ws.send(_tool_call("write_note", "err-1", {"text": "  "}))
            await ws.send(_tool_call("no_such_tool", "err-2", {}))
            results.extend(
                [
                    dict(json.loads(await ws.recv())),
                    dict(json.loads(await ws.recv())),
                ]
            )
            await ws.send(json.dumps(_AGENT_PROGRESS))

        def settled(metrics: SessionMetrics) -> bool:
            return len(metrics.completed_invocations) == 2

        metrics, results = await _run_session(tmp_path, script, settled)

        assert all(r["is_error"] is True for r in results)
        result_by_id = {str(r["tool_call_id"]): r for r in results}
        assert "non-empty" in str(result_by_id["err-1"]["result"])
        assert "unknown client tool" in str(result_by_id["err-2"]["result"])
        # Errored invocations complete and therefore count in the latency
        # stats -- an error is a measured round trip, not a dropped sample.
        assert all(inv.is_error for inv in metrics.invocations)
        assert len(metrics.completed_invocations) == 2
