# /// script
# requires-python = ">=3.13"
# dependencies = ["httpx>=0.27"]
# ///
"""Delete the spike's EL agent and client tools, then remove agent.json."""

from __future__ import annotations

from pathlib import Path

from control_plane import AgentHandle, ControlPlane

_AGENT_FILE = Path(__file__).parent / "agent.json"


def main() -> None:
    """Remove everything setup_agent.py created."""
    if not _AGENT_FILE.exists():
        print("no agent.json -- nothing to tear down")
        return
    handle = AgentHandle.load(_AGENT_FILE)
    plane = ControlPlane()
    try:
        plane.delete_agent(handle.agent_id)
        print(f"deleted agent {handle.agent_id}")
        for tool_id in handle.tool_ids:
            plane.delete_tool(tool_id)
            print(f"deleted tool {tool_id}")
    finally:
        plane.close()
    _AGENT_FILE.unlink()
    print("removed agent.json")


if __name__ == "__main__":
    main()
