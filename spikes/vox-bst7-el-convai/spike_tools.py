"""The three client tools the spike registers on the Conv AI agent."""

from __future__ import annotations

import time
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol, Self, final

# JSON-schema fragments cross the EL wire verbatim -- a wire boundary,
# so the values are schema objects, not a domain type.
type JsonSchema = dict[str, object]
type ToolParams = Mapping[str, object]

# Deterministic exec durations for the slow tool, cycled per call.
# A known schedule lets the report subtract exec time from the measured
# round trip exactly, isolating the EL-attributable overhead.
_SLOW_SCHEDULE_S: tuple[float, ...] = (2.2, 3.1, 4.3, 2.7, 3.8)

_FAKE_MATCHES: tuple[str, ...] = (
    "src/punt_vox/voxd/daemon.py:214: async def _dispatch(self, frame) -> None:",
    "src/punt_vox/voxd/playback.py:88: self._queue.append(request)",
    "src/punt_vox/providers/__init__.py:41: def registry() -> ProviderMap:",
)


@dataclass(frozen=True, slots=True)
class ToolSpec:
    """Declaration of one client tool, as the EL control plane wants it."""

    name: str
    description: str
    parameters: JsonSchema

    def to_config(self) -> dict[str, object]:
        """Return the ``tool_config`` body for ``POST /v1/convai/tools``."""
        return {
            "type": "client",
            "name": self.name,
            "description": self.description,
            "expects_response": True,
            "response_timeout_secs": 20,
            "parameters": self.parameters,
        }


class SpikeTool(Protocol):
    """One locally-implemented client tool."""

    @property
    def spec(self) -> ToolSpec:
        """Declaration registered with the EL agent."""
        ...

    def __call__(self, params: ToolParams) -> str:
        """Execute the tool and return the result string for the LLM."""
        ...


@final
class ClockTool:
    """Fast tool: returns the current time; completes in well under 50ms."""

    __slots__ = ()

    def __new__(cls) -> Self:
        return super().__new__(cls)

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="clock",
            description=(
                "Report the current date and time. Use whenever the user "
                "asks what time or day it is."
            ),
            parameters={"type": "object", "properties": {}, "required": []},
        )

    def __call__(self, params: ToolParams) -> str:  # noqa: ARG002 -- protocol signature; clock takes no arguments
        now = datetime.now(tz=UTC)
        return f"Current time: {now.isoformat(timespec='seconds')}"


@final
class SearchCodeTool:
    """Slow tool: simulates search_code against a large tree (2-5s)."""

    _calls: int

    def __new__(cls) -> Self:
        self = super().__new__(cls)
        self._calls = 0
        return self

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="search_code",
            description=(
                "Search the project source code for a pattern and return "
                "matching lines. Use whenever the user asks to find, look "
                "up, or search for anything in the code."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "pattern": {
                        "type": "string",
                        "description": "Text or symbol to search for.",
                    }
                },
                "required": ["pattern"],
            },
        )

    def __call__(self, params: ToolParams) -> str:
        pattern = str(params.get("pattern", ""))
        duration = _SLOW_SCHEDULE_S[self._calls % len(_SLOW_SCHEDULE_S)]
        self._calls += 1
        time.sleep(duration)
        matches = "\n".join(_FAKE_MATCHES)
        return f"3 matches for {pattern!r}:\n{matches}"


@final
class WriteNoteTool:
    """Write tool: persists its argument to a file and returns an ack."""

    _path: Path

    def __new__(cls, path: Path) -> Self:
        self = super().__new__(cls)
        self._path = path
        return self

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="write_note",
            description=(
                "Persist a short note for the coding session. Use whenever "
                "the user asks you to note, record, or remember something."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "text": {
                        "type": "string",
                        "description": "The note text to persist.",
                    }
                },
                "required": ["text"],
            },
        )

    def __call__(self, params: ToolParams) -> str:
        text = str(params.get("text", "")).strip()
        if not text:
            msg = "write_note requires non-empty 'text'"
            raise ValueError(msg)
        stamp = datetime.now(tz=UTC).isoformat(timespec="seconds")
        with self._path.open("a", encoding="utf-8") as f:
            f.write(f"- [{stamp}] {text}\n")
        return f"Note saved ({len(text)} chars)."


@final
class ToolBelt:
    """The registry the session consults when a client_tool_call arrives."""

    _tools: dict[str, SpikeTool]

    def __new__(cls, notes_path: Path) -> Self:
        self = super().__new__(cls)
        tools: tuple[SpikeTool, ...] = (
            ClockTool(),
            SearchCodeTool(),
            WriteNoteTool(notes_path),
        )
        self._tools = {tool.spec.name: tool for tool in tools}
        return self

    @property
    def specs(self) -> tuple[ToolSpec, ...]:
        return tuple(tool.spec for tool in self._tools.values())

    def run(self, name: str, params: ToolParams) -> str:
        """Execute the named tool; raise ``KeyError`` for an unknown name."""
        if name not in self._tools:
            msg = f"unknown client tool: {name}"
            raise KeyError(msg)
        return self._tools[name](params)
