"""Tests for the ``vox music`` CLI (cli_music.MusicCli).

MusicCli is a humble object: each playback verb is driven directly with an
in-memory FakeProgramGateway and a mock formatter -- no daemon, no store -- so
the surface behaviour (album list via the gateway, tag/id replay, next/status,
and the F7 applied/rejected result) is asserted without a wire. A couple of
CliRunner smoke tests confirm MusicCli.build_app wires the Typer group.
"""

from __future__ import annotations

import io
import json
import re
from dataclasses import replace
from unittest.mock import MagicMock

import pytest
import typer
from _cli_introspect import app_help_texts
from _program_fakes import FakeProgramGateway
from typer.testing import CliRunner
from websockets.exceptions import WebSocketException

from punt_vox.cli_io import OutputFlags
from punt_vox.cli_music import MusicCli
from punt_vox.output_formatter import OutputFormatter
from punt_vox.types_programs import Reason
from punt_vox.types_programs.control import ProgramSummary
from punt_vox.types_programs.identifiers import ProgramName
from punt_vox.types_programs.status import ProgramStatus
from punt_vox.voxd.programs import Part, Program, ProgramState
from punt_vox.voxd.programs.playback_policy import Advance, AdvanceResult


class _AvoidRepeat:
    def next_part(self, pool: tuple[Part, ...], playing: Part | None) -> AdvanceResult:
        for part in pool:
            if part != playing:
                return Advance(part)
        return Advance(pool[0])


def _cli(gateway: FakeProgramGateway) -> tuple[MusicCli, MagicMock]:
    formatter = MagicMock(spec=OutputFormatter)
    return MusicCli(formatter, lambda: gateway), formatter


def _summary(album_id: str, style: str, vibe: str, ready: int) -> ProgramSummary:
    return ProgramSummary(
        id=album_id, style=style, vibe=vibe, format="music", ready=ready, total=ready
    )


def _emitted(formatter: MagicMock) -> tuple[object, str]:
    payload, text = formatter.emit.call_args.args
    return payload, text


def _build_music_app() -> typer.Typer:
    """Build the music Typer group with a formatter and its OutputFlags."""
    fmt = OutputFormatter()
    return MusicCli.build_app(fmt, OutputFlags(fmt))


def test_music_group_exposes_the_unified_verb_set() -> None:
    app = _build_music_app()
    names = {c.name for c in app.registered_commands if c.name is not None}
    assert names == {
        "on",
        "new",
        "list",
        "play",
        "stop",
        "get",
        "remove",
        "next",
        "prev",
        "pause",
        "resume",
        "status",
    }


# A design-decision label (D-1..D-9, DES-0xx) or the stale "consume-only" claim
# in user-facing help is a defect: help is the manual, so it must read plainly.
_INTERNAL_LABEL = re.compile(r"\bD-[0-9]\b|\bDES-|consume-only")


def test_music_help_carries_no_internal_labels() -> None:
    """No group/verb/option help leaks a design label or stale phrasing."""
    for text in app_help_texts(_build_music_app()):
        assert not _INTERNAL_LABEL.search(text), text


# ---------------------------------------------------------------------------
# list -- albums via the gateway catalog (no client-side store, R2)
# ---------------------------------------------------------------------------


def test_list_renders_albums_from_the_gateway() -> None:
    catalog = (
        _summary("a3f1c9", "trance", "calm", 5),
        _summary("7b2e04", "lofi", "focus", 1),
    )
    cli, formatter = _cli(FakeProgramGateway(catalog=catalog))

    cli.list_programs()

    payload, text = _emitted(formatter)
    ids = [p["id"] for p in payload["programs"]]  # type: ignore[index]
    assert ids == ["a3f1c9", "7b2e04"]
    assert "a3f1c9" in text


def test_list_empty() -> None:
    cli, formatter = _cli(FakeProgramGateway())

    cli.list_programs()

    payload, text = _emitted(formatter)
    assert payload == {"programs": []}
    assert "No saved albums" in text


# ---------------------------------------------------------------------------
# play -- a Selection resolved by tags or id, and the F7 result
# ---------------------------------------------------------------------------


def test_play_by_tags_forwards_the_query() -> None:
    """The --style/--vibe tag radio still resolves a union Selection (D-3)."""
    fake = FakeProgramGateway()
    cli, _ = _cli(fake)

    cli.play(style="trance", vibe="calm")

    assert fake.calls[0].verb == "select"
    assert fake.calls[0].selection is not None
    assert fake.calls[0].selection.style == "trance"
    assert fake.calls[0].selection.vibe == "calm"


def test_play_by_bare_id_positional_forwards_the_album_id() -> None:
    """The bare <id> positional is the unified-verb primary form (D-3)."""
    fake = FakeProgramGateway()
    cli, _ = _cli(fake)

    cli.play("a3f1c9")

    assert fake.calls[0].selection is not None
    assert fake.calls[0].selection.id == "a3f1c9"
    assert fake.calls[0].selection.style is None


def test_play_reports_rejected() -> None:
    fake = FakeProgramGateway(applied=False)
    cli, formatter = _cli(fake)

    cli.play(style="trance")

    payload, _ = _emitted(formatter)
    assert payload["applied"] is False  # type: ignore[index]


def test_play_websocket_error_is_clean_error() -> None:
    """A mid-request WebSocket close on play is a clean CLI error, not raw."""
    gateway = MagicMock()
    gateway.select.side_effect = WebSocketException("connection closed")
    cli = MusicCli(MagicMock(spec=OutputFormatter), lambda: gateway)

    with pytest.raises(typer.Exit):
        cli.play("a3f1c9")


def test_play_no_argument_replays_the_last_played() -> None:
    """A bare `vox music play` sends the empty request the daemon replays."""
    fake = FakeProgramGateway()
    cli, formatter = _cli(fake)

    cli.play()

    assert fake.calls[0].verb == "select"
    assert fake.calls[0].selection is not None
    assert fake.calls[0].selection.is_empty
    payload, _ = _emitted(formatter)
    assert payload == {
        "music": "play",
        "applied": True,
        "message": "Playing selection.",
    }


def test_play_no_argument_without_history_errors_and_lists(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A bare play with no history fails with the message AND the album list."""
    fake = FakeProgramGateway(
        catalog=(_summary("a3f1c9", "trance", "calm", 3),),
        select_error="no album played yet; specify an album by id, name, or style/vibe",
    )
    cli = MusicCli(OutputFormatter(), lambda: fake)

    with pytest.raises(typer.Exit):
        cli.play()

    text = capsys.readouterr().err
    assert "no album played yet" in text
    assert "a3f1c9" in text  # the saved-album list is printed
    assert fake.verbs() == ["select", "catalog"]  # never a play of album #1


def test_status_websocket_handshake_error_is_clean_error() -> None:
    """A stale-token handshake failure on status surfaces cleanly, not raw."""
    gateway = MagicMock()
    gateway.status.side_effect = WebSocketException("invalid status 401")
    cli = MusicCli(MagicMock(spec=OutputFormatter), lambda: gateway)

    with pytest.raises(typer.Exit):
        cli.status()


# ---------------------------------------------------------------------------
# stop -- the one CLI halt verb, routed to the daemon program-stop op
# ---------------------------------------------------------------------------


def test_stop_invokes_the_program_stop_op() -> None:
    """`vox music stop` issues the gateway stop() -- the daemon program-stop path."""
    fake = FakeProgramGateway()
    cli, formatter = _cli(fake)

    cli.stop()

    assert fake.verbs() == ["stop"]
    payload, text = _emitted(formatter)
    assert payload == {"music": "stop", "applied": True, "message": "Music stopped."}
    assert text == "Music stopped."


def test_stop_is_idempotent_when_already_stopped() -> None:
    """Stopping an already-idle Program is a clean no-op, not an error."""
    fake = FakeProgramGateway(status=ProgramStatus.idle())
    cli, formatter = _cli(fake)

    cli.stop()
    cli.stop()

    assert fake.verbs() == ["stop", "stop"]
    _, text = _emitted(formatter)
    assert text == "Music stopped."


def test_stop_websocket_error_is_clean_error() -> None:
    """A mid-request WebSocket close on stop is a clean CLI error, not raw."""
    gateway = MagicMock()
    gateway.stop.side_effect = WebSocketException("connection closed")
    cli = MusicCli(MagicMock(spec=OutputFormatter), lambda: gateway)

    with pytest.raises(typer.Exit):
        cli.stop()


# ---------------------------------------------------------------------------
# next / status
# ---------------------------------------------------------------------------


def test_next_advances() -> None:
    fake = FakeProgramGateway()
    cli, _ = _cli(fake)

    cli.advance()

    assert fake.verbs() == ["advance"]


def test_next_rejected_surfaces_reason() -> None:
    """A rejected advance shows the daemon's reason, not a canned line (F4/F7)."""
    fake = FakeProgramGateway(applied=False, reason="nothing is playing")
    cli, formatter = _cli(fake)

    cli.advance()

    payload, text = _emitted(formatter)
    assert payload["applied"] is False  # type: ignore[index]
    assert text == "nothing is playing"


def test_status_renders_now_playing_and_failures() -> None:
    program = Program(ProgramState.initial(), _AvoidRepeat())
    program.turn_on()
    program.first_track_ok(Part("id001", 1))
    program.fill_bad_part(Part("id002", 2), Reason("ToS"))
    status = program.to_status(ProgramName("ambient_techno"))
    cli, formatter = _cli(FakeProgramGateway(status=status))

    cli.status()

    _, text = _emitted(formatter)
    assert "ambient_techno" in text
    assert "playing 1 of 1" in text
    assert "part 2 failed" in text


def test_status_renders_paused_when_the_source_is_suspended() -> None:
    program = Program(ProgramState.initial(), _AvoidRepeat())
    program.turn_on()
    program.first_track_ok(Part("id001", 1))
    status = replace(program.to_status(ProgramName("ambient_techno")), paused=True)
    cli, formatter = _cli(FakeProgramGateway(status=status))

    cli.status()

    _, text = _emitted(formatter)
    assert "paused 1 of 1" in text  # not the misleading "playing" for a held source
    assert "playing 1 of 1" not in text


def test_status_idle() -> None:
    cli, formatter = _cli(FakeProgramGateway(status=ProgramStatus.idle()))

    cli.status()

    _, text = _emitted(formatter)
    assert text == "Nothing playing."


# ---------------------------------------------------------------------------
# MusicCli.build_app wiring (CliRunner smoke)
# ---------------------------------------------------------------------------


def test_app_no_subcommand_shows_help() -> None:
    app = _build_music_app()

    result = CliRunner().invoke(app, [])

    assert result.exit_code != 0 or "Usage" in result.output


def test_list_accepts_json_flag_after_the_subcommand() -> None:
    """vox-cnak: --json parses AFTER the subcommand, not only before it."""
    fmt = OutputFormatter()
    flags = OutputFlags(fmt)
    cli = MusicCli(fmt, lambda: FakeProgramGateway(), flags=flags)
    # A second command makes this a multi-command group (as `vox music` is), so
    # the runner treats "list" as the subcommand -- where --json failed (vox-cnak).
    app = typer.Typer()
    app.command("list")(cli.list_programs)
    app.command("stop")(cli.stop)

    result = CliRunner().invoke(app, ["list", "--json"])

    assert result.exit_code == 0
    assert json.loads(result.stdout) == {"programs": []}


# ---------------------------------------------------------------------------
# on -- start music from an authored pool piped on stdin
# ---------------------------------------------------------------------------


def _on_cli(gateway: FakeProgramGateway, vibe: str | None = None) -> MusicCli:
    """Build a MusicCli whose vibe comes from a pinned source, not config."""
    return MusicCli(
        MagicMock(spec=OutputFormatter), lambda: gateway, vibe_source=lambda: vibe
    )


def test_on_pipes_the_authored_pool_into_the_start_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pool = {"base_prompt": "deep techno", "variations": [f"v{i}" for i in range(12)]}
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(pool)))
    fake = FakeProgramGateway()

    _on_cli(fake).on(style="trance")

    request = fake.calls[0].request
    assert request is not None and request.prompts is not None
    assert request.prompts.base == "deep techno"
    assert request.prompts.variations == tuple(f"v{i}" for i in range(12))
    assert request.style == "trance"


def test_on_with_empty_stdin_falls_back_to_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No pipe (empty stdin) sends prompts=None -- the daemon's literal fallback."""
    monkeypatch.setattr("sys.stdin", io.StringIO(""))
    fake = FakeProgramGateway()

    _on_cli(fake).on()

    request = fake.calls[0].request
    assert request is not None and request.prompts is None


def test_on_carries_the_config_vibe(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("sys.stdin", io.StringIO(""))
    fake = FakeProgramGateway()

    _on_cli(fake, vibe="focused").on()

    request = fake.calls[0].request
    assert request is not None and request.vibe == "focused"


def test_on_malformed_pool_is_a_clean_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("sys.stdin", io.StringIO("{not json"))
    fake = FakeProgramGateway()

    with pytest.raises(typer.Exit):
        _on_cli(fake).on()
    assert fake.calls == []  # rejected before the gateway start


def test_on_wrong_variation_count_is_a_clean_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pool = {"base_prompt": "x", "variations": ["only one"]}
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(pool)))
    fake = FakeProgramGateway()

    with pytest.raises(typer.Exit):
        _on_cli(fake).on()
    assert fake.calls == []


@pytest.mark.parametrize("payload", ["[1, 2]", '"a bare string"', "42"])
def test_on_non_object_pool_is_a_clean_error(
    monkeypatch: pytest.MonkeyPatch, payload: str
) -> None:
    """Valid JSON that is not an object is a clean CLI error, not a traceback."""
    monkeypatch.setattr("sys.stdin", io.StringIO(payload))
    fake = FakeProgramGateway()

    with pytest.raises(typer.Exit):
        _on_cli(fake).on()
    assert fake.calls == []  # rejected before the gateway start


@pytest.mark.parametrize(
    "payload",
    ["{}", '{"style": "trance"}', '{"variations": ["a", "b"]}'],
)
def test_on_incomplete_object_pool_is_a_clean_error(
    monkeypatch: pytest.MonkeyPatch, payload: str
) -> None:
    """A piped object lacking a non-empty base_prompt is malformed, not a fallback.

    Nothing piped falls back to the daemon (None); but a piped object -- empty,
    or with variations and no base_prompt -- clearly supplied a payload, so it is
    a clean CLI error rather than a silent fallback to the minimal literal prompt.
    """
    monkeypatch.setattr("sys.stdin", io.StringIO(payload))
    fake = FakeProgramGateway()

    with pytest.raises(typer.Exit):
        _on_cli(fake).on()
    assert fake.calls == []  # rejected before the gateway start


def test_on_blank_style_and_title_reach_the_daemon_as_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A blank/whitespace style or title is canonicalised to None in the request,
    matching the MCP tool so the two surfaces build one StartRequest."""
    monkeypatch.setattr("sys.stdin", io.StringIO(""))
    fake = FakeProgramGateway()

    _on_cli(fake).on(style="   ", title="  ")

    request = fake.calls[0].request
    assert request is not None
    assert request.style is None
    assert request.name is None


def test_on_title_becomes_the_request_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The authored ``--title`` becomes the album ``name`` on the StartRequest."""
    monkeypatch.setattr("sys.stdin", io.StringIO(""))
    fake = FakeProgramGateway()

    _on_cli(fake).on(title="  Midnight Drive  ")

    request = fake.calls[0].request
    assert request is not None
    assert request.name == "Midnight Drive"
