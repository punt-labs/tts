# /// script
# requires-python = ">=3.13"
# dependencies = ["websockets>=14"]
# ///
"""Stub voxd context store: a loopback WebSocket sink for `mcp-proxy --hook`.

Speaks the exact wire contract the real mcp-proxy hook relay uses (JSON-RPC
2.0 over WebSocket, method ``hook/<Event>``, params = the raw hook payload):
sync requests get a result response; async notifications get none. Every
accepted payload is stamped by ``SequenceStamper`` and durably appended to a
``HookLedger`` JSONL file before the response is sent, so a SIGKILL of this
process -- the survival test -- loses nothing already acknowledged.

Run:  uv run hook_store.py --port 8931 --ledger results/run/hook_ledger.jsonl
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
from pathlib import Path
from typing import Self, final

from websockets.asyncio.server import serve

from stamp import HookLedger, Sanitizer, SequenceStamper

_HOOK_PREFIX = "hook/"
_PARSE_ERROR = -32700
_METHOD_NOT_FOUND = -32601


@final
class JsonRpcFrame:
    """One inbound JSON-RPC message, already shape-checked."""

    __slots__ = ("_id", "_method", "_params")

    # JSON-RPC ids are opaque (int, str, or absent for notifications); the
    # store echoes whatever arrived, so the field stays a wire object.
    _id: object
    _method: str
    _params: dict[str, object]

    def __new__(cls, raw: str) -> Self:
        parsed = json.loads(raw)
        if not isinstance(parsed, dict):
            msg = "JSON-RPC frame is not an object"
            raise ValueError(msg)
        method = parsed.get("method")
        if not isinstance(method, str) or not method:
            msg = "JSON-RPC frame has no method"
            raise ValueError(msg)
        params = parsed.get("params")
        self = super().__new__(cls)
        self._id = parsed.get("id")
        self._method = method
        self._params = params if isinstance(params, dict) else {}
        return self

    @property
    def method(self) -> str:
        """The JSON-RPC method name."""
        return self._method

    @property
    def params(self) -> dict[str, object]:
        """The params object; empty when absent or non-object."""
        return self._params

    @property
    def is_notification(self) -> bool:
        """True when no response is expected."""
        return self._id is None

    def result(self, payload: dict[str, object]) -> str:
        """A success response echoing this frame's id."""
        return json.dumps({"jsonrpc": "2.0", "id": self._id, "result": payload})

    def error(self, code: int, message: str) -> str:
        """An error response echoing this frame's id."""
        return json.dumps(
            {
                "jsonrpc": "2.0",
                "id": self._id,
                "error": {"code": code, "message": message},
            }
        )


@final
class HookStore:
    """Dispatches JSON-RPC frames into the stamped, durable ledger."""

    __slots__ = ("_ledger", "_stamper")

    _ledger: HookLedger
    _stamper: SequenceStamper

    def __new__(cls, ledger: HookLedger, sanitizer: Sanitizer | None = None) -> Self:
        # sanitizer optional: tests of the wire contract need no host rules.
        self = super().__new__(cls)
        self._ledger = ledger
        self._stamper = SequenceStamper(sanitizer)
        return self

    def process(self, raw: str) -> str | None:
        """Handle one wire message.

        Returns the response to send, or None for notifications (which
        expect no reply). An unparseable frame gets a JSON-RPC parse-error
        response with a null id -- the sender's id is unknowable.
        """
        try:
            frame = JsonRpcFrame(raw)
        except (json.JSONDecodeError, ValueError) as exc:
            return json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": None,
                    "error": {"code": _PARSE_ERROR, "message": str(exc)},
                }
            )
        if frame.method.startswith(_HOOK_PREFIX):
            return self._accept_hook(frame)
        if frame.method == "store/health":
            return frame.result({"status": "ok", "records": self._count()})
        if frame.is_notification:
            return None
        return frame.error(_METHOD_NOT_FOUND, f"unknown method {frame.method}")

    def _accept_hook(self, frame: JsonRpcFrame) -> str | None:
        event = frame.method.removeprefix(_HOOK_PREFIX)
        record = self._stamper.stamp(event, frame.params)
        self._ledger.append(record)
        if frame.is_notification:
            return None
        return frame.result({"ok": True, "recv_seq": record.recv_seq})

    def _count(self) -> int:
        return len(self._ledger.records())


async def _serve(port: int, store: HookStore) -> None:
    async def handler(connection: object) -> None:
        # websockets' ServerConnection is async-iterable; typed loosely here
        # because the spike pins no stubs for the library.
        async for message in connection:  # type: ignore[attr-defined]
            text = message if isinstance(message, str) else bytes(message).decode()
            response = store.process(text)
            if response is not None:
                await connection.send(response)  # type: ignore[attr-defined]

    async with serve(handler, "127.0.0.1", port, max_size=2**20) as server:
        print(f"hook store listening on ws://127.0.0.1:{port}", flush=True)
        await server.serve_forever()


def main() -> None:
    """CLI entry: serve until killed."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--ledger", type=Path, required=True)
    parser.add_argument(
        "--scratch-root",
        type=Path,
        default=None,
        help="harness scratch namespace; paths under it persist as <scratch>",
    )
    args = parser.parse_args()
    store = HookStore(HookLedger(args.ledger), Sanitizer.for_host(args.scratch_root))
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(_serve(args.port, store))


if __name__ == "__main__":
    main()
