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

from pathlib import Path

from control_plane import AgentHandle, ControlPlane
from spike_tools import ToolBelt

_HERE = Path(__file__).parent
_AGENT_FILE = _HERE / "agent.json"
_LLM = "gemini-2.0-flash"

_BASE_PROMPT = """\
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


def main() -> None:
    """Create tools + agent, then persist the handle."""
    if _AGENT_FILE.exists():
        handle = AgentHandle.load(_AGENT_FILE)
        print(f"agent.json already exists (agent {handle.agent_id}).")
        print("Run teardown_agent.py first to recreate.")
        return
    plane = ControlPlane()
    try:
        belt = ToolBelt(_HERE / "notes.md")
        tool_ids = tuple(plane.create_tool(spec) for spec in belt.specs)
        print(f"created {len(tool_ids)} client tools: {', '.join(tool_ids)}")
        agent_id = plane.create_agent(
            name="vox-bst7-spike",
            prompt=_BASE_PROMPT,
            first_message=_FIRST_MESSAGE,
            llm=_LLM,
            tool_ids=tool_ids,
        )
        print(f"created agent {agent_id} (llm={_LLM})")
        AgentHandle(agent_id=agent_id, tool_ids=tool_ids).save(_AGENT_FILE)
        print(f"wrote {_AGENT_FILE}")
    finally:
        plane.close()


if __name__ == "__main__":
    main()
