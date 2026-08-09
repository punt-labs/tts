"""FastMCP schema introspection tests for the three switch tools.

Each ``@mcp.tool()`` builds its ``inputSchema`` from the Python signature;
these tests read the emitted schema and assert:

* ``mic:model.inputSchema.properties.name`` is optional and typed as string.
* ``mic:provider.inputSchema.properties.name.enum`` is exactly the closed
  provider list ``[elevenlabs, openai, polly, say, espeak]`` per §3.2.
* ``mic:voice.inputSchema.properties.name`` is optional and typed as string
  (the roster is provider-specific and lookup-time, so no enum).

The schema is what an MCP client reads; a regression that widens ``mic:provider``
back to ``str`` re-opens the "unknown provider" path that the closed enum was
introduced to close (§4a).
"""

from __future__ import annotations

from typing import Any, cast

import pytest


@pytest.mark.asyncio
async def test_model_schema_name_is_optional_string() -> None:
    """mic:model advertises a single optional string ``name`` argument."""
    import punt_vox.server as srv

    tools = await srv.mcp.list_tools()
    schema = next(t for t in tools if t.name == "model").inputSchema
    properties = cast("dict[str, Any]", schema["properties"])
    assert set(properties) == {"name"}
    assert schema.get("required", []) == []
    name_prop = cast("dict[str, Any]", properties["name"])
    # The property is a nullable string -- FastMCP encodes ``str | None`` as
    # ``anyOf``: [{"type": "string"}, {"type": "null"}] or a ``type`` list.
    assert _has_string_type(name_prop)
    assert "enum" not in name_prop


@pytest.mark.asyncio
async def test_provider_schema_name_is_closed_enum() -> None:
    """mic:provider narrows ``name`` to the five-provider Literal (§3.2)."""
    import punt_vox.server as srv

    tools = await srv.mcp.list_tools()
    schema = next(t for t in tools if t.name == "provider").inputSchema
    properties = cast("dict[str, Any]", schema["properties"])
    assert set(properties) == {"name"}
    assert schema.get("required", []) == []
    enum_values = _extract_enum(cast("dict[str, Any]", properties["name"]))
    assert enum_values == ["elevenlabs", "openai", "polly", "say", "espeak"]


@pytest.mark.asyncio
async def test_voice_schema_name_is_optional_string_with_no_enum() -> None:
    """mic:voice takes any voice name -- the roster is looked up at runtime."""
    import punt_vox.server as srv

    tools = await srv.mcp.list_tools()
    schema = next(t for t in tools if t.name == "voice").inputSchema
    properties = cast("dict[str, Any]", schema["properties"])
    assert set(properties) == {"name"}
    assert schema.get("required", []) == []
    name_prop = cast("dict[str, Any]", properties["name"])
    assert _has_string_type(name_prop)
    assert "enum" not in name_prop


@pytest.mark.asyncio
async def test_unmute_schema_still_carries_provider_and_model_kwargs() -> None:
    """Unit A does not touch mic:unmute overloads -- Unit B (vox-0rp9.4) does.

    This pin-test asserts the current state: model/provider are still schema
    parameters on unmute. Flipped to negative assertions when Unit B lands.
    """
    import punt_vox.server as srv

    tools = await srv.mcp.list_tools()
    schema = next(t for t in tools if t.name == "unmute").inputSchema
    properties = cast("dict[str, Any]", schema["properties"])
    assert "provider" in properties
    assert "model" in properties


def _has_string_type(prop: dict[str, Any]) -> bool:
    """Return True when *prop* accepts ``string`` in any FastMCP encoding.

    An optional ``str | None`` renders as one of:
      - ``{"type": "string"}`` with ``required`` omitting the key
      - ``{"type": ["string", "null"]}``
      - ``{"anyOf": [{"type": "string"}, {"type": "null"}]}``
    """
    if prop.get("type") == "string":
        return True
    type_value = prop.get("type")
    if isinstance(type_value, list) and "string" in type_value:
        return True
    any_of = prop.get("anyOf")
    if isinstance(any_of, list):
        return any(
            isinstance(entry, dict) and entry.get("type") == "string"
            for entry in any_of
        )
    return False


def _extract_enum(prop: dict[str, Any]) -> list[str] | None:
    """Return the Literal's enum list from *prop*, or None when absent.

    FastMCP encodes ``Literal[a, b] | None`` as either ``{"enum": [...]}`` at
    the top level or as an ``anyOf`` containing one enum branch and a null
    branch. Both are checked so a downstream FastMCP shape change is caught,
    not silently swallowed.
    """
    if "enum" in prop:
        return cast("list[str]", prop["enum"])
    any_of = prop.get("anyOf")
    if isinstance(any_of, list):
        for entry in any_of:
            if isinstance(entry, dict) and "enum" in entry:
                return cast("list[str]", entry["enum"])
    return None
