"""The production ``SessionAttach``: a headless ``claude -p --resume`` per turn.

Implements option D of ``docs/conversation-mode-session-attach-adr.md``,
ratified by the operator: for each human turn, spawn ``claude -p --resume
<id> --input-format stream-json --output-format stream-json
--include-partial-messages --bare``, write one JSON user-message object to
its stdin, and read the stream of JSON assistant-message-delta objects from
stdout. The reply is spoken as one block rather than one utterance per
delta -- FR-11's first-complete-portion requirement is satisfied by the
underlying subprocess still streaming; sentence-streamed synthesis (acting on
each delta as it arrives) is a distinct, larger change to this class's
contract with :class:`~.call_session.CallSession`, not attempted here.

**Auth model: ``--bare`` requires an API key, not OAuth.**
``--bare`` eliminates the ``SessionStart`` hook cascade every non-bare
``claude -p --resume`` pays on each spawn (measured empirically: 0 hooks
fire in bare mode versus 9-28 in normal mode), which is most of the 13-25s
median per-turn latency this call site exists to hide behind an ack quip
and a wait chime (see :mod:`~.wait_cue`). The cost: ``claude --help`` is
explicit that bare mode's auth is "strictly ``ANTHROPIC_API_KEY`` or
``apiKeyHelper`` via ``--settings``" -- no OAuth support at all. This is the
opposite requirement from every other ``claude``-spawn site in this
package: :class:`~.claude_subprocess_env.ClaudeSubprocessEnv` normally
*strips* ``ANTHROPIC_API_KEY`` so a resumed session uses the human's own
claude.ai login instead of a possibly-stale env-var key. This class alone
passes ``keep_api_key=True`` to keep it, and refuses to spawn at all
(:class:`~.session_attach.BareAuthMissingError`) when the key is absent --
a call placed against an OAuth-only setup (no ``ANTHROPIC_API_KEY``
configured) does not work with ``vox call start``.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from typing import TYPE_CHECKING, Self, cast, final

from punt_vox.voxd.conversation_mode.claude_subprocess_env import claude_subprocess_env
from punt_vox.voxd.conversation_mode.reply import ReplyChunk
from punt_vox.voxd.conversation_mode.session_attach import (
    BareAuthMissingError,
    SessionAttachError,
)

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

# asyncio.create_subprocess_exec's default StreamReader line-buffer limit is
# 64KB -- readline() (what `async for line in stdout` uses) raises ValueError
# ("Separator is not found, and chunk exceed the limit") if a single line
# exceeds it before finding a newline. --verbose mode emits richer, larger
# per-line JSON payloads than non-verbose stream-json did, and a single line
# carrying a substantial assistant reply can exceed 64KB on its own -- this
# raised before any real reply content was ever collected. 10MB is generous
# enough that no real single stream-json line should ever hit it, while still
# bounding memory against a pathological line.
_STDOUT_LINE_LIMIT = 10 * 1024 * 1024


@final
class ClaudeSessionAttach:
    """One call's session-attach mechanism, bound to one already-active session."""

    __slots__ = ("_claude_bin", "_session_id")
    _session_id: str
    _claude_bin: str

    def __new__(cls, session_id: str, *, claude_bin: str = "claude") -> Self:
        # Fail fast, before any subprocess is spawned, the same PY-CC-5
        # discipline this class already applies to a missing
        # ANTHROPIC_API_KEY (see :meth:`BareAuthMissingError.check`): an empty
        # session_id would otherwise pass through silently and only
        # surface 120 seconds later as an opaque "did not reply within
        # 120s" -- send_turn spawning `claude -p --resume ""` and hanging
        # on a reply that never comes, rather than the actionable error a
        # bad session id deserves right here.
        if not session_id:
            msg = "ClaudeSessionAttach requires a non-empty session_id"
            raise ValueError(msg)
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
        leak it. Checks :meth:`BareAuthMissingError.check` before that, so a
        missing ``ANTHROPIC_API_KEY`` never reaches a spawn attempt at all
        -- see this module's own docstring for why ``--bare`` requires it.
        Also checked as an actual startup pre-flight in
        :meth:`~.call_live_driver.LiveCallDriver.create`; kept here too as
        the last line of defense for any other caller.
        """
        BareAuthMissingError.check()
        try:
            process = await self._exec()
        except FileNotFoundError as exc:
            # Mirrors session_discovery.py's own conversion of the same
            # failure: without this, a missing ``claude`` binary raises
            # BEFORE send_turn's own try block is even reached, so it is
            # never a SessionAttachError and bypasses CallSession's
            # ``except SessionAttachError`` recovery entirely -- falling
            # through to the generic "call ended unexpectedly" boundary
            # handler instead of this actionable message.
            msg = f"{self._claude_bin} not found on PATH"
            raise SessionAttachError(msg) from exc
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

    async def _exec(self) -> asyncio.subprocess.Process:
        """Spawn the relay subprocess, letting a missing binary raise verbatim.

        Split out of :meth:`_spawn` so the ``FileNotFoundError`` conversion
        wraps only the exec call itself, not the stdin-write step below it
        (which raises its own, already-typed errors).
        """
        return await asyncio.create_subprocess_exec(
            self._claude_bin,
            "-p",
            "--resume",
            self._session_id,
            "--input-format",
            "stream-json",
            "--output-format",
            "stream-json",
            "--include-partial-messages",
            # claude now refuses to start at all without this combined with
            # -p/--output-format=stream-json ("requires --verbose"), exiting
            # 1 before a single reply frame is written. Verified against a
            # real `claude -p --verbose --output-format stream-json` run
            # that --verbose only adds `type: "system"` frames (hook
            # lifecycle, session init) -- none carry a top-level "message"
            # field shaped like an assistant delta, so _extract_text_delta's
            # existing narrowing already treats them as carrying no text.
            "--verbose",
            # Eliminates the SessionStart hook cascade every non-bare spawn
            # pays (measured: 0 hooks fire in bare mode versus 9-28 in
            # normal mode) -- most of the 13-25s median per-turn latency
            # this call site exists to hide behind an ack quip and wait
            # chime. See this module's own docstring for the auth-model
            # trade this requires.
            "--bare",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=self._subprocess_env(),
            # See _STDOUT_LINE_LIMIT: the default 64KB StreamReader line
            # buffer is too small for --verbose's larger per-line stream-json
            # payloads. Only stdout is read by line (readline() via `async
            # for line in stdout`); stderr.read() reads to EOF, which is not
            # limit-bounded (CPython's StreamReader.read() docstring: "not
            # limited with limit, configured at stream creation"). Both
            # streams share this one limit -- create_subprocess_exec applies
            # it to every pipe it creates, not per-stream -- but only stdout
            # can actually hit it.
            limit=_STDOUT_LINE_LIMIT,
        )

    @staticmethod
    def _subprocess_env() -> dict[str, str]:
        """Return the relay subprocess's environment, minus its auth traps.

        ``keep_api_key=True`` -- this call site's requirement is the
        opposite of :class:`~.claude_subprocess_env.ClaudeSubprocessEnv`'s
        default for every other ``claude``-spawn site
        (:class:`~.session_discovery.SessionDiscovery` still wants the
        strip; it doesn't pass ``--bare`` and still relies on OAuth). See
        this module's own docstring for the full auth-model rationale.
        """
        return claude_subprocess_env(extra={_RELAY_ENV_VAR: "1"}, keep_api_key=True)

    async def _exchange(
        self, stdout: asyncio.StreamReader, stderr: asyncio.StreamReader
    ) -> tuple[str, bytes]:
        """Read the reply and stderr concurrently, under a bounded timeout.

        Concurrently, not stdout-to-EOF then stderr: draining stdout first
        risks the classic pipe deadlock, leaving this coroutine waiting
        forever with the call's ``UserPromptSubmit`` lock still held. A
        timeout becomes :class:`SessionAttachError`; any other failure (a
        malformed line from :meth:`_collect_reply`) propagates unchanged --
        killing/reaping the subprocess is :meth:`send_turn`'s job. Explicit
        :class:`asyncio.Task` objects, not bare coroutines, so the
        ``finally`` below can cancel whichever one ``gather``'s
        ``return_exceptions=False`` left running when its sibling raised.
        """
        reply_task = asyncio.ensure_future(self._collect_reply(stdout))
        stderr_task = asyncio.ensure_future(stderr.read())
        try:
            return await asyncio.wait_for(
                asyncio.gather(reply_task, stderr_task),
                timeout=_REPLY_TIMEOUT_S,
            )
        except TimeoutError:
            msg = (
                f"{self._claude_bin} -p --resume {self._session_id} did not "
                f"reply within {_REPLY_TIMEOUT_S:.0f}s"
            )
            raise SessionAttachError(msg) from None
        finally:
            for task in (reply_task, stderr_task):
                if not task.done():
                    task.cancel()
            await asyncio.gather(reply_task, stderr_task, return_exceptions=True)

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
