"""Behavioral tests for the plugin/hooks/suppress-output.sh PostToolUse hook.

The hook drives the two-channel display: ``updatedMCPToolOutput`` (the panel
line the user sees) and ``additionalContext`` (text injected back into the
agent). The invariant under test is which tools stay silent.

Silence is decided per action by whether the flow driving it wants the panel as
the whole response. Only the music-control subcommands and vibe qualify: on
success ``music`` with ``subcommand`` ``on``/``off``/``play``/``next`` and
``vibe`` put a terminal stop-narration directive in ``additionalContext``
instead of the result JSON. Every other action keeps its RESULT so the flow that
needs it can reply — the ``rec`` verbs and the ``music`` catalog/list/status
subcommands return ids, paths, bytes, and program state the agent addresses
later, ``unmute``
drives ``/vox model|provider``, ``speak`` drives ``/mute``, ``notify`` drives
``/vox c``, and the query subcommands report data. On any tool error the failure
must still reach ``additionalContext``.

Driven as a subprocess against the real script — the interface is the
contract, so we exercise the shell, not a reimplementation of it.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

_HOOK = (
    Path(__file__).resolve().parent.parent / "plugin" / "hooks" / "suppress-output.sh"
)

# A distinctive slice of STOP_NARRATION — stable enough to assert on without
# pinning the whole sentence.
_STOP_MARK = "reply with no text, no summary, no narration. Stop."

pytestmark = [
    pytest.mark.subprocess,
    pytest.mark.skipif(
        shutil.which("jq") is None, reason="suppress-output.sh requires jq"
    ),
]


def _run_hook(
    tool: str, result: object, *, subcommand: str | None = None
) -> dict[str, str]:
    """Run the hook for ``tool`` with ``result`` as the tool response.

    ``result`` is wrapped the way FastMCP delivers a string return: a
    single-element content array whose ``text`` is the JSON payload. The single
    ``music`` tool routes on ``subcommand`` (its first input argument), so pass
    it to drive a specific music panel form. Returns the parsed
    ``hookSpecificOutput`` mapping. Raises if the hook exits non-zero or emits
    no output.
    """
    return _invoke(tool, json.dumps(result), subcommand=subcommand)


def _run_hook_raw(tool: str, text: str) -> dict[str, str]:
    """Run the hook with a raw, non-JSON ``text`` as the tool response.

    FastMCP surfaces an uncaught tool exception as a bare content string
    (e.g. "Error executing tool music: KeyError: 'style'"), not our
    ``{"error": ...}`` contract. This drives that path.
    """
    return _invoke(tool, text)


def _invoke(tool: str, text: str, *, subcommand: str | None = None) -> dict[str, str]:
    """Run the hook for ``tool`` with ``text`` as the response content.

    Returns the parsed ``hookSpecificOutput`` mapping. Raises if the hook
    exits non-zero or emits no output.
    """
    payload: dict[str, object] = {
        "tool_name": f"mcp__plugin_vox_mic__{tool}",
        "tool_response": [{"type": "text", "text": text}],
    }
    if subcommand is not None:
        payload["tool_input"] = {"subcommand": subcommand}
    proc = subprocess.run(
        ["bash", str(_HOOK)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        check=True,
    )
    parsed = json.loads(proc.stdout)
    output = parsed["hookSpecificOutput"]
    assert isinstance(output, dict)
    return {str(k): str(v) for k, v in output.items()}


class TestSilentToolsStopNarration:
    """Music-control and vibe success replace the result JSON with the directive.

    These are the only tools every slash-command flow drives silently, so the
    panel line is the whole response and the payload must not leak.
    """

    def test_vibe_context_is_the_directive_not_json(self) -> None:
        # Music off (no music_hint): the vibe change is silent.
        out = _run_hook("vibe", {"vibe": {"vibe": "focused", "vibe_tags": "[calm]"}})
        assert _STOP_MARK in out["additionalContext"]
        # The raw result must NOT leak — no data for the agent to narrate.
        assert "vibe_tags" not in out["additionalContext"]
        assert "focused" not in out["additionalContext"]
        # The panel line is untouched.
        assert out["updatedMCPToolOutput"] == "♪ vibe shifted to [calm]"


class TestVibeMusicHint:
    """A vibe change while music plays must deliver the re-pool directive.

    vox-q1z4 makes ``vibe`` return a ``music_hint`` — an imperative "author 12
    prompts and call music(...)" instruction — whenever a Program is playing.
    ``vibe`` is in the STOP_NARRATION silent set, so without this branch the
    directive is swallowed and the whole re-pool feature does nothing through
    the hook. When a hint is present it REPLACES the stop directive; when music
    is off (no hint) the vibe change stays silent.
    """

    def test_music_hint_replaces_stop_narration(self) -> None:
        hint = (
            "Music is playing (style=trance). Author 12 rich trance x focused "
            'prompts and call music(mode="on", style="trance", base_prompt=..., '
            "variations=[<12 genre-mood prompts>]). Do it now."
        )
        out = _run_hook(
            "vibe",
            {
                "vibe": {"vibe": "focused", "vibe_tags": "[calm]"},
                "music": {"playing": True, "style": "trance"},
                "music_hint": hint,
            },
        )
        # The imperative directive reaches the agent verbatim…
        assert out["additionalContext"] == hint
        # …and the stop-narration directive is gone — the hint is the action.
        assert _STOP_MARK not in out["additionalContext"]
        # The panel line is the vibe phrase, unchanged.
        assert out["updatedMCPToolOutput"] == "♪ vibe shifted to [calm]"

    def test_no_music_hint_keeps_stop_narration(self) -> None:
        # Music off: the reply has no music_hint, so the vibe change is silent.
        out = _run_hook("vibe", {"vibe": {"vibe": "focused", "vibe_tags": "[calm]"}})
        assert _STOP_MARK in out["additionalContext"]
        assert out["updatedMCPToolOutput"] == "♪ vibe shifted to [calm]"

    def test_music_on_panel_is_the_message(self) -> None:
        # music returns {"message", "applied"} — the panel is the message line,
        # and additionalContext is the stop directive (no payload to narrate).
        out = _run_hook(
            "music",
            {"message": "♪ Music on — generating a trance track...", "applied": True},
        )
        assert _STOP_MARK in out["additionalContext"]
        assert "applied" not in out["additionalContext"]
        assert (
            out["updatedMCPToolOutput"] == "♪ Music on — generating a trance track..."
        )

    def test_music_on_ambient_panel_is_the_message(self) -> None:
        out = _run_hook(
            "music",
            {"message": "♪ Music on — generating ambient music...", "applied": True},
        )
        assert _STOP_MARK in out["additionalContext"]
        assert out["updatedMCPToolOutput"] == "♪ Music on — generating ambient music..."

    def test_music_off_panel_is_the_message(self) -> None:
        out = _run_hook("music", {"message": "♪ Music off.", "applied": True})
        assert _STOP_MARK in out["additionalContext"]
        assert out["updatedMCPToolOutput"] == "♪ Music off."

    def test_music_multiline_message_panel_takes_first_line(self) -> None:
        out = _run_hook(
            "music",
            {"message": "♪ Music on — generating...\nsecond line", "applied": True},
        )
        assert _STOP_MARK in out["additionalContext"]
        assert out["updatedMCPToolOutput"] == "♪ Music on — generating..."

    def test_music_play_panel_is_the_message(self) -> None:
        # music play returns {"message", "applied"} — no "name" field.
        out = _run_hook(
            "music",
            {"message": "♪ Playing selection.", "applied": True},
            subcommand="play",
        )
        assert _STOP_MARK in out["additionalContext"]
        assert "applied" not in out["additionalContext"]
        assert out["updatedMCPToolOutput"] == "♪ Playing selection."

    def test_music_next_context_is_the_directive(self) -> None:
        out = _run_hook(
            "music",
            {"message": "♪ Skipping — generating next track...", "applied": True},
            subcommand="next",
        )
        assert _STOP_MARK in out["additionalContext"]
        assert "applied" not in out["additionalContext"]
        # The panel keeps the tool's own message line.
        assert out["updatedMCPToolOutput"] == "♪ Skipping — generating next track..."


class TestRecMusicCatalogVerbs:
    """The rec_* and music-catalog verbs get purpose-built panel phrasing.

    Each has its own result shape (a list for ``rec_new``; an object keyed by
    ``recordings``/``played``/``base64``/``album_id``/etc. for the rest), and
    each keeps its payload in ``additionalContext`` so the agent can address the
    result later. The panel line is a compact summary — and for ``rec_get`` it
    names the id and size only, NEVER the base64 blob, so the compact channel is
    not flooded with an inline recording.
    """

    def test_rec_new_panel_counts_tracks_and_keeps_ids(self) -> None:
        out = _run_hook(
            "rec",
            [{"id": "abc123.mp3", "bytes": 10, "cached": False}],
            subcommand="new",
        )
        assert "abc123.mp3" in out["additionalContext"]  # ids reach the agent
        assert _STOP_MARK not in out["additionalContext"]
        assert out["updatedMCPToolOutput"].startswith("♪")
        assert "1" in out["updatedMCPToolOutput"]  # one track

    def test_rec_list_panel_counts_recordings(self) -> None:
        out = _run_hook(
            "rec", {"recordings": [{"id": "a.mp3", "bytes": 3}]}, subcommand="list"
        )
        assert "a.mp3" in out["additionalContext"]
        assert out["updatedMCPToolOutput"] == "♪ 1 recording(s) in the store"

    def test_rec_play_panel_names_ref(self) -> None:
        out = _run_hook("rec", {"played": "take-1.mp3"}, subcommand="play")
        assert "take-1.mp3" in out["additionalContext"]
        assert out["updatedMCPToolOutput"] == "♪ playing take-1.mp3"

    def test_rec_get_keeps_base64_in_context_not_panel(self) -> None:
        # HIGH regression: the blob is the agent's payload (additionalContext),
        # but must NEVER be dumped into the compact panel line.
        blob = "QUJDREVGR0hJSktMTU5PUA=="
        out = _run_hook(
            "rec", {"id": "clip.mp3", "bytes": 16, "base64": blob}, subcommand="get"
        )
        assert blob in out["additionalContext"]
        assert blob not in out["updatedMCPToolOutput"]
        assert out["updatedMCPToolOutput"] == "♪ fetched clip.mp3 (16 bytes)"

    def test_rec_remove_panel_names_ref(self) -> None:
        out = _run_hook("rec", {"removed": "old.mp3"}, subcommand="remove")
        assert "old.mp3" in out["additionalContext"]
        assert out["updatedMCPToolOutput"] == "♪ removed old.mp3"

    def test_music_new_panel_names_album(self) -> None:
        out = _run_hook("music", {"album_id": "7f3a91"}, subcommand="new")
        assert "7f3a91" in out["additionalContext"]
        assert out["updatedMCPToolOutput"] == "♪ generated album 7f3a91"

    def test_music_get_panel_names_album_keeps_path(self) -> None:
        out = _run_hook(
            "music",
            {"album_id": "7f3a91", "path": "/dest/warm-7f3a91"},
            subcommand="get",
        )
        assert "/dest/warm-7f3a91" in out["additionalContext"]  # the path reaches back
        assert out["updatedMCPToolOutput"] == "♪ exported 7f3a91"

    def test_music_remove_panel_names_album(self) -> None:
        out = _run_hook("music", {"removed": "7f3a91"}, subcommand="remove")
        assert out["updatedMCPToolOutput"] == "♪ removed album 7f3a91"

    def test_rec_get_not_found_error_reaches_context(self) -> None:
        # On failure the verbs return {"error":...}; the error guard surfaces it.
        detail = "no recording named 'missing.mp3'"
        out = _run_hook("rec", {"error": detail}, subcommand="get")
        assert "missing.mp3" in out["additionalContext"]
        assert _STOP_MARK not in out["additionalContext"]
        assert out["updatedMCPToolOutput"] == f"♪ error: {detail}"


class TestReplyToolsKeepData:
    """Tools whose slash-command flows need an agent reply keep the JSON.

    unmute drives ``/vox model|provider`` ("Switched … to X"), speak drives
    ``/mute`` (a phrase reply), and notify drives ``/vox c`` (lists featured
    voices).
    """

    def test_unmute_payload_reaches_context(self) -> None:
        # MED regression: /vox model|provider derive their confirmation text
        # from the payload.
        out = _run_hook("unmute", [{"voice": "Matilda", "model": "eleven_v3"}])
        assert "eleven_v3" in out["additionalContext"]
        assert _STOP_MARK not in out["additionalContext"]
        assert out["updatedMCPToolOutput"].startswith("♪")

    def test_speak_payload_reaches_context(self) -> None:
        out = _run_hook("speak", {"speak": "n"})
        assert '"speak"' in out["additionalContext"]
        assert _STOP_MARK not in out["additionalContext"]
        assert out["updatedMCPToolOutput"] == "♪ chimes only"

    def test_notify_payload_reaches_context(self) -> None:
        out = _run_hook("notify", {"notify": {"notify": "y"}})
        assert '"notify"' in out["additionalContext"]
        assert _STOP_MARK not in out["additionalContext"]
        assert out["updatedMCPToolOutput"] == "♪ vox enabled"


class TestQueryToolsKeepData:
    """A query-tool success keeps the JSON so the agent can report it."""

    def test_status_context_carries_the_data(self) -> None:
        out = _run_hook("status", {"voice": "Matilda", "notify": "y"})
        assert "Matilda" in out["additionalContext"]
        assert _STOP_MARK not in out["additionalContext"]
        assert out["updatedMCPToolOutput"] == "♪ Matilda · notify=y"

    def test_music_status_context_carries_the_data(self) -> None:
        """``status`` is a query verb and must not fall through the control catch-all.

        Every unlisted subcommand takes the ``*`` branch, which swaps the payload
        for the stop-narration directive -- correct for a fire-and-forget control
        action, exactly wrong for a verb whose whole purpose is to hand the agent
        the program state to report.
        """
        out = _run_hook(
            "music",
            {
                "message": "♪ focus-beats [music] — playing 2 of 5 (playing_rotating)",
                "program": {"mode": "playing_rotating", "name": "focus-beats"},
                "music_mode": "on",
            },
            subcommand="status",
        )
        assert "playing_rotating" in out["additionalContext"]
        assert "music_mode" in out["additionalContext"]
        assert _STOP_MARK not in out["additionalContext"]
        assert (
            out["updatedMCPToolOutput"]
            == "♪ focus-beats [music] — playing 2 of 5 (playing_rotating)"
        )

    def test_music_status_panel_takes_only_the_first_line(self) -> None:
        # A failing part adds lines to the summary; the compact channel takes one.
        head = "♪ focus-beats [music] — stopped (failed)"
        out = _run_hook(
            "music",
            {
                "message": f"{head}\n  error: bad_prompt",
                "program": {"mode": "failed"},
                "music_mode": "on",
            },
            subcommand="status",
        )
        assert out["updatedMCPToolOutput"] == head
        assert "bad_prompt" in out["additionalContext"]

    def test_music_list_context_carries_the_data(self) -> None:
        # music list returns {"message", "programs"} — the panel counts programs.
        out = _run_hook(
            "music",
            {
                "message": "♪ 1 saved album(s):",
                "programs": [{"id": "a1", "name": "focus-beats"}],
            },
            subcommand="list",
        )
        assert "focus-beats" in out["additionalContext"]
        assert _STOP_MARK not in out["additionalContext"]
        assert "1 album(s)" in out["updatedMCPToolOutput"]


class TestErrorGuardPreserved:
    """On any tool error the failure still reaches additionalContext."""

    def test_control_error_reaches_context(self) -> None:
        out = _run_hook("music", {"error": "voxd unreachable"})
        assert "voxd unreachable" in out["additionalContext"]
        assert _STOP_MARK not in out["additionalContext"]
        assert out["updatedMCPToolOutput"] == "♪ error: voxd unreachable"

    def test_uncaught_exception_string_reaches_context(self) -> None:
        # FastMCP surfaces an uncaught tool exception as a bare, non-JSON
        # string. It matches neither the {"error":...} contract nor a success
        # object/array, so without the bare-string guard it would fall through
        # to a success branch and be overwritten by the stop-directive.
        msg = "Error executing tool music: KeyError: 'style'"
        out = _run_hook_raw("music", msg)
        assert msg in out["additionalContext"]
        assert _STOP_MARK not in out["additionalContext"]
        assert out["updatedMCPToolOutput"] == "♪ error"
