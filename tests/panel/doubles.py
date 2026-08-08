"""The doubles the panel's leg and runner tests both drive.

A regular module rather than ``conftest`` on purpose: these are types the
tests annotate and construct, and ``conftest`` is imported by pytest under a
name of its own, so importing from it would give one file two module names.
Fixtures stay in ``conftest``; the types they hand out live here.

Imported as ``panel.doubles``, not ``tests.panel.doubles``: ``tests`` carries
no ``__init__``, so ``tests/`` is the import root both pytest and mypy work
from -- and spelling it the other way makes this one file resolvable under
two module names, which mypy rejects outright.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from typing import TYPE_CHECKING, Literal, cast, final

from punt_lux import HubUnavailableError
from punt_lux.operations import Ok

from punt_vox.client_errors import VoxdProtocolError

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    import pytest
    from punt_lux import RenderRequest, SceneShown
    from punt_lux.hub_client import CallbackHandler, ConnectHandler, EventHandler
    from punt_lux.operations import OpError

    from punt_vox.panel.ports import HubListener

__all__ = [
    "FAILURE_TEXT",
    "PANEL_LOGGER",
    "FailPoint",
    "Failure",
    "FakeListener",
    "FakeRest",
    "FakeService",
    "panel_records",
    "wait_until",
]

# The logger every panel collaborator writes to: the leg's, injected into the
# guard and the runner so one connection's story reads under one name.
PANEL_LOGGER = logging.getLogger("punt_vox.panel.leg")

# What voxd says when it refuses -- the text a notice has to carry through.
FAILURE_TEXT = "unknown voice 'nope'"

# How long a blocked fake waits before giving up: finite so a gate left shut
# fails its test instead of hanging the suite on an unreachable worker thread.
_GATE_SECONDS = 5.0

# Where a fake is asked to fail: the connection-setup calls the leg makes in
# order, each of which luxd can drop between. "register" is the last of them
# and the odd one out -- it runs as on_connect, after the handshake, on the
# far side of the hub client's own blanket handler.
type FailPoint = Literal["listener", "subscribe", "listen", "register"]

# Which call a service double fails, and how: one selector rather than a flag
# per call, so a test cannot ask for two failures the panel never sees at once.
# "unexpected" is the odd one out -- not a failure the panel names and guards,
# but a bug, raised from every call the runner's work makes so each piece can
# be shown ending inside its own boundary rather than in a task nobody reads.
type Failure = Literal[
    "", "prefetch", "service", "apply", "write", "preview", "unexpected"
]

# What a bug looks like: an exception type no guard and no handler names.
_BUG = "a failure the panel never planned for"


def panel_records(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    """Return only the records the panel emitted, dropping punt_lux's."""
    return [r for r in caplog.records if r.name == PANEL_LOGGER.name]


async def wait_until(predicate: Callable[[], bool], *, timeout: float = 2.0) -> None:
    """Poll *predicate* until it is true -- a ``to_thread`` worker needs real
    wall-clock time to run, not just an event-loop tick."""
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() > deadline:
            msg = "timed out waiting for the background worker to finish"
            raise AssertionError(msg)
        await asyncio.sleep(0.01)


@final
class FakeListener:
    """A ``HubListener`` double that records subscriptions and never blocks."""

    _fail_at: FailPoint | None

    def __init__(
        self, *, fail_at: FailPoint | None = None, error: Exception | None = None
    ) -> None:
        self.subscribed: tuple[str, ...] = ()
        self.listened = False
        self._fail_at = fail_at
        self._error = error if error is not None else HubUnavailableError("down")

    def subscribe(self, *topics: str) -> None:
        if self._fail_at == "subscribe":
            raise self._error
        self.subscribed = topics

    async def listen(self) -> None:
        if self._fail_at == "listen":
            raise self._error
        self.listened = True


@final
class FakeRest:
    """A ``PanelRestClient`` double: canned register result, records listeners."""

    _fail_at: FailPoint | None

    def __init__(
        self,
        *,
        register_result: Ok | OpError | None = None,
        fail_at: FailPoint | None = None,
        error: Exception | None = None,
    ) -> None:
        self.register_result = register_result if register_result is not None else Ok()
        self.registered: list[tuple[str, str]] = []
        self.rendered_count = 0
        self.listener_built: FakeListener | None = None
        self._fail_at = fail_at
        self._error = error if error is not None else HubUnavailableError("down")

    def render(self, request: RenderRequest) -> SceneShown | OpError:
        self.rendered_count += 1
        return cast("SceneShown", Ok())

    def register_callback(self, callback_id: str, label: str) -> Ok | OpError:
        if self._fail_at == "register":
            raise self._error
        self.registered.append((callback_id, label))
        return self.register_result

    def listener(
        self,
        *,
        on_callback: CallbackHandler,
        on_event: EventHandler,
        on_connect: ConnectHandler | None = None,
    ) -> HubListener:
        if self._fail_at == "listener":
            raise self._error
        self.listener_built = FakeListener(fail_at=self._fail_at, error=self._error)
        return self.listener_built


@final
class FakeService:
    """A ``VoxPanelService`` double recording its lifecycle calls."""

    _raise_on: Failure

    def __init__(self, *, raise_on: Failure = "") -> None:
        self.callback_id = "vox-panel"
        self.label = "Vox"
        self.refusal = FAILURE_TEXT
        # Open unless a test closes it to stand in for a voxd slow to answer.
        self.prefetch_gate = threading.Event()
        self.prefetch_gate.set()
        self.prefetch_called = False
        self.acknowledged = 0
        self.serviced = 0
        self.applied: list[tuple[str, Mapping[str, object]]] = []
        self.apply_returns = True
        self.pushed = 0
        self.recovered: list[str] = []
        self.rejections: list[str] = []
        self._raise_on = raise_on

    def prefetch(self) -> None:
        if self._raise_on == "prefetch":
            raise VoxdProtocolError(self.refusal)
        if self._raise_on == "unexpected":
            raise RuntimeError(_BUG)
        # The timeout is a backstop: a gate left shut must fail its test, never
        # hang the suite on a worker thread nobody can reach.
        self.prefetch_gate.wait(_GATE_SECONDS)
        self.prefetch_called = True

    def acknowledge(self, client: object, latency: object) -> None:
        self.acknowledged += 1

    def service(self, client: object, latency: object) -> None:
        if self._raise_on == "service":
            raise VoxdProtocolError(self.refusal)
        if self._raise_on == "unexpected":
            raise RuntimeError(_BUG)
        self.serviced += 1

    def apply_event(self, topic: str, payload: Mapping[str, object]) -> bool:
        if self._raise_on == "apply":
            msg = "bad payload"
            raise TypeError(msg)
        if self._raise_on == "write":
            msg = "disk full"
            raise OSError(msg)
        if self._raise_on == "preview":
            raise VoxdProtocolError(self.refusal)
        if self._raise_on == "unexpected":
            raise RuntimeError(_BUG)
        self.applied.append((topic, payload))
        return self.apply_returns

    def push_scene(self, client: object) -> None:
        self.pushed += 1

    def recover_from_write_failure(self, field: str) -> None:
        self.recovered.append(field)

    def note_rejection(self, detail: str) -> None:
        self.rejections.append(detail)
