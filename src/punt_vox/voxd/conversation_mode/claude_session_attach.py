"""The production ``SessionAttach``: a headless ``claude -p --resume`` per turn.

Implements option D of ``docs/conversation-mode-session-attach-adr.md``,
ratified by the operator: for each human turn, spawn ``claude -p --resume
<id> --input-format stream-json --output-format stream-json
--include-partial-messages``, write one JSON user-message object to its
stdin, and read the stream of JSON assistant-message-delta objects from
stdout. This slice speaks the reply as one block (sentence-streamed
synthesis is Slice 2a+ territory per the epic's context), so this
implementation collects every delta and yields exactly one final
:class:`~.reply.ReplyChunk` rather than one per delta -- FR-11's
first-complete-portion requirement is satisfied by the underlying subprocess
still streaming; this class simply does not yet act on each delta as it
arrives.
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
        if process.stdin is None or process.stdout is None:
            msg = f"{self._claude_bin} subprocess has no stdin/stdout pipe"
            raise SessionAttachError(msg)

        user_message = {
            "type": "user",
            "message": {"role": "user", "content": turn.text},
        }
        process.stdin.write(json.dumps(user_message).encode() + b"\n")
        await process.stdin.drain()
        process.stdin.close()

        text = await self._collect_reply(process.stdout)
        _, stderr = await process.communicate()
        if process.returncode != 0:
            msg = (
                f"{self._claude_bin} -p --resume {self._session_id} exited "
                f"{process.returncode}: {stderr.decode(errors='replace').strip()}"
            )
            raise SessionAttachError(msg)
        yield ReplyChunk(text=text, is_final=True)

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
