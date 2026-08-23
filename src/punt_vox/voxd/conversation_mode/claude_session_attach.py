"""The production ``SessionAttach``: a headless ``claude -p --resume`` per turn.

Implements option D of ``docs/conversation-mode-session-attach-adr.md``,
ratified by the operator: for each human turn, spawn ``claude -p --resume
<id> --input-format stream-json --output-format stream-json
--include-partial-messages``, write one JSON user-message object to its
stdin, and read the stream of JSON assistant-message-delta objects from
stdout. The reply is spoken as one block rather than one utterance per
delta -- FR-11's first-complete-portion requirement is satisfied by the
underlying subprocess still streaming; sentence-streamed synthesis (acting on
each delta as it arrives) is a distinct, larger change to this class's
contract with :class:`~.call_session.CallSession`, not attempted here.
"""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import AsyncIterator
from typing import TYPE_CHECKING, Self, cast, final

from punt_vox.voxd.conversation_mode.reply import ReplyChunk
from punt_vox.voxd.conversation_mode.session_attach import SessionAttachError

if TYPE_CHECKING:
    from punt_vox.voxd.conversation_mode.turn import TranscribedTurn

__all__ = ["ClaudeSessionAttach"]

# plugin/hooks/call-lock.sh reads this to bypass the UserPromptSubmit lock
# it otherwise enforces while a call is active. Set only on the relay
# subprocess this class spawns -- if that subprocess itself fires
# UserPromptSubmit hooks, its own turn must never be blocked by the lock
# the outer call already holds for the *human's* interactive input.
_RELAY_ENV_VAR = "VOX_CALL_RELAY"

# A ceiling on how long one turn's reply may take. Without one, a stalled
# subprocess (network hang, a tool call that never returns) leaves the call
# waiting forever with no signal that anything is wrong -- distinct from a
# clean exit with a nonzero return code, which already raises. Generous
# because a genuine agent turn can involve tool calls and real work, not
# just a language-model round trip.
_REPLY_TIMEOUT_S = 120.0


@final
class ClaudeSessionAttach:
    """One call's session-attach mechanism, bound to one already-active session."""

    __slots__ = ("_claude_bin", "_session_id")
    _session_id: str
    _claude_bin: str

    def __new__(cls, session_id: str, *, claude_bin: str = "claude") -> Self:
        self = super().__new__(cls)
        self._session_id = session_id
        self._claude_bin = claude_bin
        return self

    async def send_turn(self, turn: TranscribedTurn) -> AsyncIterator[ReplyChunk]:
        process = await self._spawn(turn)
        try:
            if process.stdout is None or process.stderr is None:
                msg = f"{self._claude_bin} subprocess has no stdout/stderr pipe"
                raise SessionAttachError(msg)
            text, stderr = await self._exchange(process.stdout, process.stderr)
        except BaseException:
            # ANY failure once the process is running -- not just a timeout
            # or an ``Exception`` -- must not leak the subprocess. Ctrl-C
            # during a call (the obvious way a human ends one) raises
            # KeyboardInterrupt or CancelledError, both BaseException and
            # neither Exception, since Python 3.8; a bare ``except
            # Exception`` here would let either escape without killing or
            # reaping the child.
            process.kill()
            await process.wait()
            raise
        await process.wait()
        if process.returncode != 0:
            msg = (
                f"{self._claude_bin} -p --resume {self._session_id} exited "
                f"{process.returncode}: {stderr.decode(errors='replace').strip()}"
            )
            raise SessionAttachError(msg)
        yield ReplyChunk(text=text, is_final=True)

    async def _spawn(self, turn: TranscribedTurn) -> asyncio.subprocess.Process:
        """Start the relay subprocess and write *turn* to its stdin.

        Kills and reaps the process before raising if anything after
        creation fails -- a missing stdin pipe, or a
        ``BrokenPipeError``/``ConnectionResetError`` writing to it (the
        likely real case: ``claude`` can exit immediately on an invalid
        session id). ``create_subprocess_exec`` succeeding only means the
        process started, not that it is usable; a failure here must not
        leak it.
        """
        process = await asyncio.create_subprocess_exec(
            self._claude_bin,
            "-p",
            "--resume",
            self._session_id,
            "--input-format",
            "stream-json",
            "--output-format",
            "stream-json",
            "--include-partial-messages",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env={**os.environ, _RELAY_ENV_VAR: "1"},
        )
        try:
            if process.stdin is None:
                msg = f"{self._claude_bin} subprocess has no stdin pipe"
                raise SessionAttachError(msg)

            user_message = {
                "type": "user",
                "message": {"role": "user", "content": turn.text},
            }
            process.stdin.write(json.dumps(user_message).encode() + b"\n")
            await process.stdin.drain()
            process.stdin.close()
        except BaseException:
            process.kill()
            await process.wait()
            raise
        return process

    async def _exchange(
        self, stdout: asyncio.StreamReader, stderr: asyncio.StreamReader
    ) -> tuple[str, bytes]:
        """Read the reply and stderr concurrently, under a bounded timeout.

        stdout and stderr are read concurrently, not stdout-to-EOF then
        stderr: ``claude -p`` is not quiet on stderr, and reading stdout to
        completion first risks the classic pipe deadlock -- if the child
        writes more than the OS pipe buffer to stderr before it finishes
        stdout, the child blocks on that write, stdout stalls waiting for
        the child, and this coroutine waits on stdout forever with the
        call's ``UserPromptSubmit`` lock still held. A bounded timeout
        guards the whole exchange: a stalled subprocess (a hung tool call,
        a network wedge) otherwise leaves the call waiting with no signal
        anything is wrong.

        Translates a timeout into :class:`SessionAttachError`; any other
        failure (a malformed stream-json line raising
        :class:`SessionAttachError` from :meth:`_collect_reply`, for
        instance) propagates unchanged. Killing and reaping the subprocess
        on failure is :meth:`send_turn`'s job, not this method's -- it owns
        the process object, this method only owns the two streams.
        """
        try:
            return await asyncio.wait_for(
                asyncio.gather(self._collect_reply(stdout), stderr.read()),
                timeout=_REPLY_TIMEOUT_S,
            )
        except TimeoutError:
            msg = (
                f"{self._claude_bin} -p --resume {self._session_id} did not "
                f"reply within {_REPLY_TIMEOUT_S:.0f}s"
            )
            raise SessionAttachError(msg) from None

    async def _collect_reply(self, stdout: asyncio.StreamReader) -> str:
        """Read stream-json lines from *stdout*, concatenating assistant text deltas.

        Raises :class:`SessionAttachError` on a line that cannot be parsed as
        JSON -- a malformed frame here means the wire contract with
        ``claude`` has drifted, not a condition to silently skip past.
        """
        pieces: list[str] = []
        async for raw_line in stdout:
            line = raw_line.decode(errors="replace").strip()
            if not line:
                continue
            try:
                frame = json.loads(line)
            except json.JSONDecodeError as exc:
                msg = f"malformed stream-json line from {self._claude_bin}: {line!r}"
                raise SessionAttachError(msg) from exc
            piece = self._extract_text_delta(frame)
            if piece:
                pieces.append(piece)
        return "".join(pieces)

    @staticmethod
    def _extract_text_delta(frame: object) -> str:
        """Return the text delta in *frame*, or ``""`` if this frame carries none.

        ``claude --output-format stream-json``'s exact frame shapes are not
        exhaustively documented; this narrows only the one shape needed here
        (an assistant message delta carrying text content) and treats every
        other frame type (tool calls, system events, the final result) as
        carrying no text -- never as an error, since those frames are a
        normal and expected part of the stream.
        """
        if not isinstance(frame, dict):
            return ""
        fields = cast("dict[str, object]", frame)
        message = fields.get("message")
        if not isinstance(message, dict):
            return ""
        message_fields = cast("dict[str, object]", message)
        content = message_fields.get("content")
        if isinstance(content, str):
            return content
        if not isinstance(content, list):
            return ""
        blocks = cast("list[object]", content)
        pieces: list[str] = []
        for block in blocks:
            if not isinstance(block, dict):
                continue
            block_fields = cast("dict[str, object]", block)
            text = block_fields.get("text")
            if isinstance(text, str):
                pieces.append(text)
        return "".join(pieces)
