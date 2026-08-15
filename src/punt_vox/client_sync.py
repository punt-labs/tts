"""Synchronous facade over :class:`~punt_vox.client.VoxClient` for hooks and CLI.

``VoxClientSync`` creates a fresh connection per call and drives the async client
to completion -- simple and correct, because hooks and CLI commands are
short-lived so connection pooling adds no value. It is a thin humble object: each
method delegates to the matching :class:`VoxClient` coroutine through
:meth:`VoxClientSync._drive`, which takes a Callable factory (``lambda c:
c.synthesize(...)``), opens a connection, awaits the operation, and closes.
The Callable-factory shape preserves each ``VoxClient`` method's precise return
type through the bridge; a ``getattr``-based dispatch would have forced every
method to carry a ``# type: ignore[no-any-return]``. The event-loop plumbing
lives in a composed :class:`_SyncRunner`, generic over the coroutine return
type, so the facade owns only the "which async op, on which connection" concern.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
from collections.abc import Callable, Coroutine
from pathlib import Path
from typing import Any, Self

from punt_vox.client import (
    CacheStatus,
    RecordingSummary,
    RecordResult,
    SynthesizeResult,
    VoxClient,
)
from punt_vox.client_env import DaemonEnv
from punt_vox.types_programs import (
    CommandOutcome,
    HealthStatus,
    ProgramStatus,
    ProgramSummary,
    PromptSet,
)
from punt_vox.types_synthesis import SynthesisSpec

__all__ = ["VoxClientSync"]


class _SyncRunner:
    """Drive an async coroutine to completion from synchronous code.

    When the caller is already inside a running event loop (e.g. the MCP
    server), ``asyncio.run`` would raise, so the coroutine is driven on a
    fresh loop in a worker thread instead.

    Generic over the coroutine's return type ``T`` so a typed
    ``Coroutine[..., SynthesizeResult]`` passed in emerges as a
    ``SynthesizeResult`` at the call site -- the untyped bridge that
    forced every :class:`VoxClientSync` method to carry a
    ``# type: ignore[no-any-return]`` is retired.
    """

    __slots__ = ()

    def run[T](self, coro: Coroutine[Any, Any, T]) -> T:
        """Run *coro* to completion, on this loop or a worker-thread loop."""
        if self._loop_is_running():
            return self._run_in_thread(coro)
        return asyncio.run(coro)

    @staticmethod
    def _loop_is_running() -> bool:
        """Return True when called from within a running event loop."""
        try:
            return asyncio.get_running_loop().is_running()
        except RuntimeError:
            return False

    @staticmethod
    def _run_in_thread[T](coro: Coroutine[Any, Any, T]) -> T:
        """Drive *coro* to completion on a fresh loop in a worker thread."""
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            return pool.submit(asyncio.run, coro).result()


class VoxClientSync:
    """Synchronous client for the voxd audio daemon.

    Exposes the same RPC surface as :class:`VoxClient` -- synthesize, chime,
    record, voices, health, and the program_* controls -- as plain blocking
    methods, for callers not running an event loop (hooks, CLI commands,
    one-off scripts).

    Lifecycle: there is nothing to open or close. Each call opens a fresh
    connection, drives it to completion, and closes it, so a caller just
    constructs the client and invokes methods::

        vox = VoxClientSync()
        vox.synthesize("build finished")

    The per-call connection is deliberate: sync callers are short-lived, so
    pooling would add complexity for no gain. Every failure raises a
    :class:`~punt_vox.VoxError`. With no arguments, host, port, and token
    resolve from the ``VOXD_*`` environment variables and the daemon's
    run-directory files.
    """

    __slots__ = ("_host", "_port", "_runner", "_token")

    _host: str
    _port: int | None
    _token: str | None
    _runner: _SyncRunner

    def __new__(
        cls,
        host: str | None = None,
        port: int | None = None,
        token: str | None = None,
    ) -> Self:
        self = super().__new__(cls)
        self._host = host if host is not None else DaemonEnv.host()
        self._port = port
        self._token = token
        self._runner = _SyncRunner()
        return self

    def _make_client(self) -> VoxClient:
        return VoxClient(host=self._host, port=self._port, token=self._token)

    def _drive[T](self, op: Callable[[VoxClient], Coroutine[Any, Any, T]]) -> T:
        """Open a connection, drive *op* on it, close, return the value.

        The old ``_call(method_name, *args)`` used ``getattr(client,
        method)`` which loses the method's precise return type -- every
        caller then carried a ``# type: ignore[no-any-return]`` to
        reconcile a ``-> ExactType`` annotation with the ``Any`` that
        came back. This helper takes a Callable factory instead, so
        the caller's ``lambda c: c.synthesize(...)`` preserves the
        return type through the bridge and the ignore is unnecessary.

        Same connection lifecycle as before: one fresh connection per
        call, opened, awaited, closed in a ``finally``. Sync callers
        are short-lived so pooling would add complexity for no gain.
        """

        async def _op() -> T:
            client = self._make_client()
            await client.connect()
            try:
                return await op(client)
            finally:
                await client.close()

        return self._runner.run(_op())

    def synthesize(
        self, text: str, spec: SynthesisSpec | None = None, *, once: int | None = None
    ) -> SynthesizeResult:
        """Send synthesize request. Audio plays on server.

        *spec* bundles the voice/provider/rate parameters; *once* is the dedup
        TTL. See :class:`SynthesizeResult` for the returned fields -- in
        particular the ``deduped`` flag that surfaces when ``once=<ttl>`` matches
        an identical text already played within the window.
        """
        return self._drive(lambda c: c.synthesize(text, spec, once=once))

    def chime(self, signal: str) -> None:
        """Play a bundled chime asset."""
        self._drive(lambda c: c.chime(signal))

    def record(
        self,
        text: str,
        spec: SynthesisSpec | None = None,
        *,
        name: str | None = None,
    ) -> RecordResult:
        """Synthesize into the daemon's store; return a locator, not a path.

        Returns a :class:`RecordResult` locator (store id/name, store path,
        byte count). The daemon owns the file; no audio crosses the wire and the
        client names no daemon path.
        """
        return self._drive(lambda c: c.record(text, spec, name=name))

    def play(self, ref: str) -> None:
        """Play a stored recording on the daemon host by its store reference."""
        self._drive(lambda c: c.play(ref))

    def fetch(self, ref: str) -> bytes:
        """Return a stored recording's bytes by its store reference."""
        return self._drive(lambda c: c.fetch(ref))

    def voices(self, provider: str) -> list[str]:
        """List *provider*'s voice roster; the wire always carries a provider."""
        return self._drive(lambda c: c.voices(provider))

    def health(self) -> HealthStatus:
        """Return the daemon's health snapshot (liveness, port, version)."""
        return self._drive(lambda c: c.health())

    # -- program surface (session-free; the daemon-facing wire, design section 4)

    def program_status(self) -> ProgramStatus:
        """Return the daemon's authoritative Program status."""
        return self._drive(lambda c: c.program_status())

    def program_on(
        self,
        *,
        style: str | None = None,
        vibe: str | None = None,
        name: str | None = None,
        prompts: PromptSet | None = None,
    ) -> CommandOutcome:
        """Turn a Program on from the session vibe and authored prompts."""
        return self._drive(
            lambda c: c.program_on(style=style, vibe=vibe, name=name, prompts=prompts)
        )

    def program_stop(self) -> CommandOutcome:
        """Halt the active Program."""
        return self._drive(lambda c: c.program_stop())

    def program_next(self) -> CommandOutcome:
        """User transport next: step the replay cursor forward, or skip a Program."""
        return self._drive(lambda c: c.program_next())

    def program_prev(self) -> CommandOutcome:
        """User transport prev: step the replay cursor back one part."""
        return self._drive(lambda c: c.program_prev())

    def program_pause(self) -> CommandOutcome:
        """Suspend the active source in place (transport pause)."""
        return self._drive(lambda c: c.program_pause())

    def program_resume(self) -> CommandOutcome:
        """Continue a suspended source (transport resume)."""
        return self._drive(lambda c: c.program_resume())

    def program_select(
        self,
        *,
        style: str | None = None,
        vibe: str | None = None,
        name: str | None = None,
        album_id: str | None = None,
    ) -> CommandOutcome:
        """Replay a Selection resolved by album id (direct) or by tags."""
        return self._drive(
            lambda c: c.program_select(
                style=style, vibe=vibe, name=name, album_id=album_id
            )
        )

    def program_list(self) -> tuple[ProgramSummary, ...]:
        """Return every album as a catalogue summary."""
        return self._drive(lambda c: c.program_list())

    # -- recordings store (rec group) ---------------------------------------

    def rec_list(self) -> tuple[RecordingSummary, ...]:
        """Return the store's recordings (name + bytes)."""
        return self._drive(lambda c: c.rec_list())

    def rec_remove(self, ref: str) -> None:
        """Delete recording *ref* from the store."""
        self._drive(lambda c: c.rec_remove(ref))

    # -- cache (daemon-owned MP3 quip cache) --------------------------------

    def cache_status(self) -> CacheStatus:
        """Return the daemon cache's entry count, size, and path."""
        return self._drive(lambda c: c.cache_status())

    def cache_clear(self) -> int:
        """Delete every entry in the daemon cache; return the count deleted."""
        return self._drive(lambda c: c.cache_clear())

    def set_log_level(self, level: str) -> str:
        """Set the daemon's log level; return the effective level it applied.

        The daemon clamps *level* to the INFO audit floor, so a stricter request
        comes back as ``info`` -- the audit trail is never blinded.
        """
        return self._drive(lambda c: c.set_log_level(level))

    # -- music catalog (music group) ----------------------------------------

    def music_new(self, prompts: PromptSet, name: str | None = None) -> str:
        """Author one track into a fresh catalog album; return its bare album id.

        Both surfaces build *prompts* as ``PromptSet.single(prompt)`` and send it,
        so the daemon receives the authored-input object (its ``base`` as the wire
        ``base_prompt``), never a bare string.
        """
        return self._drive(lambda c: c.music_new(prompts, name))

    def music_get(self, album_id: str, dest_dir: Path) -> Path:
        """Copy an album into *dest_dir* as a directory of its parts."""
        return self._drive(lambda c: c.music_get(album_id, dest_dir))

    def music_remove(self, album_id: str) -> None:
        """Delete a catalog album by id (a playing album is refused)."""
        self._drive(lambda c: c.music_remove(album_id))
