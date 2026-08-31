"""One live ``pi --mode rpc`` process held over direct subprocess pipes.

DES-066's own recommendation for a daemon-held agent process: no tmux, no
``keep`` — ``subprocess.Popen`` with line-buffered pipes. A daemon reader
thread stamps every stdout line at receipt and feeds both the transcript
(the committed evidence) and a queue the scenario driver blocks on. The
child runs in its own session (process group) so teardown can kill the
whole group after reading its state — evidence before destruction.
"""

from __future__ import annotations

import queue
import signal
import subprocess
import threading
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Self, final

from rpc_protocol import RpcEvent, Transcript

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from rpc_protocol import RpcCommand

# Seconds to allow a clean exit after stdin closes before the group is
# killed. pi exits promptly once its stdin drains; anything slower is hung.
_CLOSE_GRACE_S = 10


@final
@dataclass(frozen=True, slots=True)
class PiSpec:
    """The exact pi invocation: RPC mode, isolated, pinned model and tools."""

    binary: Path
    provider: str
    model: str
    tools: tuple[str, ...]

    def to_argv(self) -> list[str]:
        """The argv the session spawns; pure so tests can assert on it."""
        argv = [
            str(self.binary),
            "--mode",
            "rpc",
            "--no-session",
            "--no-extensions",
            "--provider",
            self.provider,
            "--model",
            self.model,
        ]
        if self.tools:
            argv.extend(["--tools", ",".join(self.tools)])
        return argv


@final
class PiRpcSession:
    """Spawn, converse with, and reap one RPC-mode child process."""

    __slots__ = ("_closed", "_pending", "_process", "_reader", "_transcript")

    _closed: bool
    _pending: queue.Queue[RpcEvent]
    _process: subprocess.Popen[str]
    _reader: threading.Thread
    _transcript: Transcript

    def __new__(cls, process: subprocess.Popen[str]) -> Self:
        self = super().__new__(cls)
        self._process = process
        self._transcript = Transcript()
        self._pending = queue.Queue()
        self._closed = False
        self._reader = threading.Thread(target=self._read_stdout, daemon=True)
        self._reader.start()
        return self

    @classmethod
    def spawn(
        cls,
        argv: list[str],
        cwd: Path,
        stderr_path: Path,
        env: dict[str, str] | None = None,  # None inherits; harness passes stub PATH
    ) -> Self:
        """Start the child in its own process group, pipes attached.

        stderr goes to a file, not a pipe: a second reader thread for a
        stream that only matters post-mortem is complexity for nothing,
        and the file survives a crash for the report to quote.
        """
        stderr_path.parent.mkdir(parents=True, exist_ok=True)
        with stderr_path.open("w", encoding="utf-8") as stderr_handle:
            process = subprocess.Popen(
                argv,
                cwd=cwd,
                env=env,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=stderr_handle,
                text=True,
                start_new_session=True,
            )
        return cls(process)

    @property
    def transcript(self) -> Transcript:
        """The stamped in/out log accumulated so far."""
        return self._transcript

    @property
    def pid(self) -> int:
        """The child's pid (also its process-group id)."""
        return self._process.pid

    def send(self, command: RpcCommand) -> int:
        """Write one command line; return the send-nanosecond stamp."""
        stdin = self._process.stdin
        if stdin is None:  # pragma: no cover - spawn always pipes stdin
            msg = "session has no stdin pipe"
            raise RuntimeError(msg)
        wire = command.to_wire()
        sent_ns = time.time_ns()
        self._transcript.note_send(wire, ns=sent_ns)
        stdin.write(wire + "\n")
        stdin.flush()
        return sent_ns

    def wait_for(
        self, predicate: Callable[[RpcEvent], bool], timeout_s: float, description: str
    ) -> RpcEvent:
        """Block until an event satisfies ``predicate``; raise on timeout.

        Non-matching events are consumed, not lost — they are already in
        the transcript, which is the evidence of record. ``description``
        names the awaited condition; a lambda's ``__name__`` cannot.
        """
        deadline = time.monotonic() + timeout_s
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                msg = f"no event matched within {timeout_s}s: {description}"
                raise TimeoutError(msg)
            try:
                event = self._pending.get(timeout=remaining)
            except queue.Empty:
                msg = f"no event matched within {timeout_s}s: {description}"
                raise TimeoutError(msg) from None
            if predicate(event):
                return event

    def settle(self, quiet_s: float) -> None:
        """Consume events until the stream is quiet for ``quiet_s``."""
        while True:
            try:
                self._pending.get(timeout=quiet_s)
            except queue.Empty:
                return

    def close(self) -> None:
        """Close stdin, wait for a clean exit, kill the group if it hangs."""
        if self._closed:
            return
        self._closed = True
        stdin = self._process.stdin
        if stdin is not None and not stdin.closed:
            stdin.close()
        try:
            self._process.wait(timeout=_CLOSE_GRACE_S)
        except subprocess.TimeoutExpired:
            # Evidence before destruction: the transcript already holds
            # everything received; the group kill is the last resort for
            # a child that ignored stdin EOF.
            self._process.send_signal(signal.SIGTERM)
            try:
                self._process.wait(timeout=_CLOSE_GRACE_S)
            except subprocess.TimeoutExpired:
                self._process.kill()
                self._process.wait()
        self._reader.join(timeout=_CLOSE_GRACE_S)

    def _read_stdout(self) -> None:
        stdout = self._process.stdout
        if stdout is None:  # pragma: no cover - spawn always pipes stdout
            return
        for raw_line in stdout:
            received_ns = time.time_ns()
            line = raw_line.rstrip("\n")
            if not line:
                continue
            self._transcript.note_recv(line, ns=received_ns)
            try:
                event = RpcEvent(line, recv_ns=received_ns)
            except ValueError:
                # A non-JSON stdout line is itself evidence; it is in the
                # transcript, it just cannot be waited on as an event.
                continue
            self._pending.put(event)
