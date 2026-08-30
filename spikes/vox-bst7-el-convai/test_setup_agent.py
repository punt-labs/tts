"""Offline pins for setup_agent's failure cleanup.

Tools and the agent are billed resources: any failure after a creation
must delete everything already created (agent first, then tools) before
the original error re-raises. The fake plane records every call so the
pins can assert exactly what was cleaned up.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Self, final

import pytest

from setup_agent import create_spike_agent

if TYPE_CHECKING:
    from pathlib import Path

    from spike_tools import ToolSpec


@final
class _FakePlane:
    """Records creations and deletions; fails where the test says."""

    _fail_agent: bool
    _fail_tool_after: int  # create_tool raises once this many succeeded
    created_tools: list[str]
    deleted_tools: list[str]
    deleted_agents: list[str]

    def __new__(cls, *, fail_agent: bool = False, fail_tool_after: int = -1) -> Self:
        self = super().__new__(cls)
        self._fail_agent = fail_agent
        self._fail_tool_after = fail_tool_after
        self.created_tools = []
        self.deleted_tools = []
        self.deleted_agents = []
        return self

    def create_tool(self, spec: ToolSpec) -> str:
        if self._fail_tool_after == len(self.created_tools):
            msg = "tool creation exploded"
            raise RuntimeError(msg)
        tool_id = f"tool-{spec.name}"
        self.created_tools.append(tool_id)
        return tool_id

    def create_agent(self, **_kwargs: object) -> str:
        if self._fail_agent:
            msg = "agent creation exploded"
            raise RuntimeError(msg)
        return "agent-1"

    def delete_tool(self, tool_id: str) -> None:
        self.deleted_tools.append(tool_id)

    def delete_agent(self, agent_id: str) -> None:
        self.deleted_agents.append(agent_id)


class TestSetupCleanup:
    """No billed resource survives a failed setup."""

    def test_agent_creation_failure_deletes_every_tool(self, tmp_path: Path) -> None:
        plane = _FakePlane(fail_agent=True)
        with pytest.raises(RuntimeError, match="agent creation exploded"):
            create_spike_agent(plane, tmp_path / "agent.json")
        assert plane.deleted_tools == plane.created_tools
        assert len(plane.deleted_tools) == 3
        assert plane.deleted_agents == []  # never created, nothing to delete
        assert not (tmp_path / "agent.json").exists()

    def test_mid_tool_creation_failure_deletes_the_created_prefix(
        self, tmp_path: Path
    ) -> None:
        plane = _FakePlane(fail_tool_after=2)
        with pytest.raises(RuntimeError, match="tool creation exploded"):
            create_spike_agent(plane, tmp_path / "agent.json")
        assert plane.deleted_tools == plane.created_tools
        assert len(plane.deleted_tools) == 2

    def test_persistence_failure_deletes_agent_and_tools(self, tmp_path: Path) -> None:
        plane = _FakePlane()
        # A handle path inside a missing directory: save() raises after
        # the agent exists -- the most expensive leak the cleanup covers.
        with pytest.raises(FileNotFoundError):
            create_spike_agent(plane, tmp_path / "no_dir" / "agent.json")
        assert plane.deleted_agents == ["agent-1"]
        assert plane.deleted_tools == plane.created_tools

    def test_success_writes_the_handle_and_deletes_nothing(
        self, tmp_path: Path
    ) -> None:
        plane = _FakePlane()
        handle = create_spike_agent(plane, tmp_path / "agent.json")
        assert handle.agent_id == "agent-1"
        assert (tmp_path / "agent.json").exists()
        assert plane.deleted_tools == []
        assert plane.deleted_agents == []
