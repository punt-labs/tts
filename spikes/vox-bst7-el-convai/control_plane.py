"""Thin REST client for the ElevenLabs Conv AI control plane."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Self, final

import httpx

from spike_tools import ToolSpec

_BASE_URL = "https://api.elevenlabs.io"

# Events the agent should push to the client. client_tool_call and
# agent_response are load-bearing for the harness; the rest are trace
# evidence (interruption + correction are the barge-in instrumentation).
_CLIENT_EVENTS: tuple[str, ...] = (
    "conversation_initiation_metadata",
    "ping",
    "audio",
    "interruption",
    "user_transcript",
    "agent_response",
    "agent_response_correction",
    "agent_response_complete",
    "client_tool_call",
    "agent_tool_response",
    "internal_tentative_agent_response",
)


@dataclass(frozen=True, slots=True)
class AgentHandle:
    """Identity of the spike agent and its registered tools."""

    agent_id: str
    tool_ids: tuple[str, ...]

    @classmethod
    def load(cls, path: Path) -> Self:
        data = json.loads(path.read_text(encoding="utf-8"))
        return cls(
            agent_id=str(data["agent_id"]),
            tool_ids=tuple(str(t) for t in data["tool_ids"]),
        )

    def save(self, path: Path) -> None:
        payload = {"agent_id": self.agent_id, "tool_ids": list(self.tool_ids)}
        path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


@final
class ControlPlane:
    """Create, resolve, and delete the spike's EL agent and client tools."""

    _http: httpx.Client

    def __new__(cls) -> Self:
        self = super().__new__(cls)
        self._http = httpx.Client(
            base_url=_BASE_URL,
            headers={"xi-api-key": cls.api_key()},
            timeout=30.0,
        )
        return self

    @staticmethod
    def api_key() -> str:
        """Return the API key from the environment; fail fast when absent."""
        key = os.environ.get("ELEVENLABS_API_KEY", "")
        if not key:
            msg = "ELEVENLABS_API_KEY is not set (run under direnv)"
            raise RuntimeError(msg)
        return key

    def create_tool(self, spec: ToolSpec) -> str:
        """Register one client tool; return its tool_id."""
        body = {"tool_config": spec.to_config()}
        data = self._post("/v1/convai/tools", body)
        return str(data["id"])

    def create_agent(
        self,
        *,
        name: str,
        prompt: str,
        first_message: str,
        llm: str,
        tool_ids: tuple[str, ...],
    ) -> str:
        """Create the spike agent; return its agent_id."""
        body = {
            "name": name,
            "tags": ["spike", "vox-bst7"],
            "conversation_config": {
                "agent": {
                    "first_message": first_message,
                    "language": "en",
                    "prompt": {
                        "prompt": prompt,
                        "llm": llm,
                        "temperature": 0.3,
                        "tool_ids": list(tool_ids),
                    },
                },
                "tts": {"agent_output_audio_format": "pcm_16000"},
                "asr": {"user_input_audio_format": "pcm_16000"},
                "conversation": {"client_events": list(_CLIENT_EVENTS)},
            },
            "platform_settings": {
                "overrides": {
                    "conversation_config_override": {
                        "agent": {
                            "prompt": {"prompt": True},
                            "first_message": True,
                        },
                        "conversation": {"text_only": True},
                    }
                }
            },
        }
        data = self._post("/v1/convai/agents/create", body)
        return str(data["agent_id"])

    def signed_url(self, agent_id: str) -> str:
        """Return a signed WebSocket URL for one conversation."""
        response = self._http.get(
            "/v1/convai/conversation/get-signed-url",
            params={"agent_id": agent_id},
        )
        self._raise_for_status(response)
        return str(response.json()["signed_url"])

    def delete_tool(self, tool_id: str) -> None:
        """Delete a tool; force on 409 (agent deletion propagates lazily).

        404 counts as success so a partially-failed teardown can re-run.
        """
        response = self._http.delete(f"/v1/convai/tools/{tool_id}")
        if response.status_code == 409:
            response = self._http.delete(
                f"/v1/convai/tools/{tool_id}", params={"force": "true"}
            )
        if response.status_code != 404:
            self._raise_for_status(response)

    def delete_agent(self, agent_id: str) -> None:
        """Delete the agent; 404 counts as success (idempotent teardown)."""
        response = self._http.delete(f"/v1/convai/agents/{agent_id}")
        if response.status_code != 404:
            self._raise_for_status(response)

    def close(self) -> None:
        self._http.close()

    def _post(self, path: str, body: dict[str, object]) -> dict[str, object]:
        response = self._http.post(path, json=body)
        self._raise_for_status(response)
        # Wire boundary: the response shape is EL's, narrowed by callers.
        return dict(response.json())

    @staticmethod
    def _raise_for_status(response: httpx.Response) -> None:
        if response.is_success:
            return
        request = response.request
        msg = (
            f"{request.method} {request.url.path} -> "
            f"{response.status_code}: {response.text[:500]}"
        )
        raise RuntimeError(msg)
