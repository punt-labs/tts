# /// script
# requires-python = ">=3.13"
# dependencies = ["httpx>=0.27"]
# ///
"""Create the spike's EL Conv AI agent and its three client tools.

Run once from this directory:

    direnv exec ../../ uv run setup_agent.py

Writes agent.json (agent_id + tool_ids) for the other scripts.
"""

from __future__ import annotations

from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

from control_plane import AgentHandle, ControlPlane
from spike_tools import ToolBelt

if TYPE_CHECKING:
    from collections.abc import Callable

    from spike_tools import ToolSpec


class AgentPlane(Protocol):
    """The control-plane surface setup needs; ControlPlane satisfies it."""

    def create_tool(self, spec: ToolSpec) -> str:
        """Register one client tool; return its tool_id."""
        ...

    def create_agent(
        self,
        *,
        name: str,
        prompt: str,
        first_message: str,
        llm: str,
        tool_ids: tuple[str, ...],
    ) -> str:
        """Create the agent; return its agent_id."""
        ...

    def delete_tool(self, tool_id: str) -> None:
        """Delete a tool (idempotent)."""
        ...

    def delete_agent(self, agent_id: str) -> None:
        """Delete the agent (idempotent)."""
        ...


_HERE = Path(__file__).parent
_AGENT_FILE = _HERE / "agent.json"

# Shared with run_automated.py: the prompt override must carry the base
# prompt too, because an override REPLACES the agent prompt wholesale.
LLM_ID = "gemini-2.0-flash"

BASE_PROMPT = """\
You are the vox voice agent for a coding session at Punt Labs.
Keep replies short -- one or two sentences -- this is a voice conversation.

You have three tools: clock, search_code, write_note. ALWAYS use the tools
instead of answering from memory:
- any question about the time or date -> call clock
- any request to find, search, or look up code -> call search_code
- any request to note, record, or remember something -> call write_note
When one request needs several tools, call every applicable tool.
After tool results arrive, summarize them in one short sentence.
"""

_FIRST_MESSAGE = "Voice agent ready. What do you need?"


def create_spike_agent(plane: AgentPlane, agent_file: Path) -> AgentHandle:
    """Create tools + agent and persist the handle; clean up on failure.

    Tools and the agent are billed resources: if any step after a
    creation fails (agent creation, agent.json persistence), everything
    already created is deleted via the idempotent force-delete paths
    before the original error re-raises -- nothing leaks for manual
    cleanup.
    """
    created_tools: list[str] = []
    agent_id = ""  # empty until the agent exists; cleanup keys off it
    try:
        belt = ToolBelt(_HERE / "notes.txt")
        # extend() appends as the generator yields, so a mid-loop failure
        # leaves the already-created prefix in the list for cleanup.
        created_tools.extend(plane.create_tool(spec) for spec in belt.specs)
        print(f"created {len(created_tools)} client tools: {', '.join(created_tools)}")
        agent_id = plane.create_agent(
            name="vox-bst7-spike",
            prompt=BASE_PROMPT,
            first_message=_FIRST_MESSAGE,
            llm=LLM_ID,
            tool_ids=tuple(created_tools),
        )
        print(f"created agent {agent_id} (llm={LLM_ID})")
        handle = AgentHandle(agent_id=agent_id, tool_ids=tuple(created_tools))
        handle.save(agent_file)
        print(f"wrote {agent_file}")
    except BaseException:  # includes Ctrl-C: billed resources must not leak
        _cleanup(plane, agent_id, created_tools)
        raise
    return handle


def _cleanup(plane: AgentPlane, agent_id: str, tool_ids: list[str]) -> None:
    """Best-effort delete of partially created resources, agent first."""
    print("setup failed -- deleting partially created resources")
    if agent_id:
        _try_delete(partial(plane.delete_agent, agent_id), f"agent {agent_id}")
    for tool_id in tool_ids:
        _try_delete(partial(plane.delete_tool, tool_id), f"tool {tool_id}")


def _try_delete(action: Callable[[], None], label: str) -> None:
    try:
        action()
        print(f"cleaned up {label}")
    except RuntimeError as exc:
        # Best-effort: the ORIGINAL setup error must surface, not the
        # cleanup's; a resource that survived is named for manual removal.
        print(f"cleanup of {label} failed -- remove manually: {exc}")


def main() -> None:
    """Create tools + agent, then persist the handle."""
    if _AGENT_FILE.exists():
        handle = AgentHandle.load(_AGENT_FILE)
        print(f"agent.json already exists (agent {handle.agent_id}).")
        print("Run teardown_agent.py first to recreate.")
        return
    plane = ControlPlane()
    try:
        create_spike_agent(plane, _AGENT_FILE)
    finally:
        plane.close()


if __name__ == "__main__":
    main()
