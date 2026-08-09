"""Parity tests: the ``vox music`` CLI and the ``music`` MCP tool agree.

Both surfaces are thin adapters over the same two gateways, so driving each
against one :class:`FakeProgramGateway` (and one catalog fake) puts the same
underlying daemon state behind both and lets the two answers be compared field
for field. A field one surface reports and the other omits is the bug this file
exists to catch -- the CLI's ``list`` once dropped the ``format`` field the tool
reported, and nothing failed.

What is compared is the *field set and its values*, never the prose: the tool
speaks in its ``♪`` DJ voice and the CLI plainly, which is a deliberate
difference of voice, not of state.
"""

from __future__ import annotations

import io
import json
from typing import TYPE_CHECKING, Self, cast, final, get_args
from unittest.mock import MagicMock

import pytest
from _program_fakes import FakeProgramGateway

from punt_vox.cli_io import OutputFlags
from punt_vox.cli_music import MusicCli
from punt_vox.cli_music_catalog import CatalogCli
from punt_vox.output_formatter import OutputFormatter
from punt_vox.server_music_tool import MusicSubcommand, MusicTool
from punt_vox.types_programs import ProgramName, ProgramStatus
from punt_vox.types_programs.control import ProgramSummary
from punt_vox.types_programs.status_views import NowPlaying
from punt_vox.vibe_command import MusicPreference

if TYPE_CHECKING:
    from collections.abc import Callable

    from punt_vox.catalog_gateway import CatalogGateway
    from punt_vox.music_session import MusicSession
    from punt_vox.types_programs.prompts import PromptSet

# The verb key the CLI adds to name which command produced a payload. The MCP
# caller already knows -- it passed the subcommand -- so this is CLI-only
# context, not a field the tool is missing.
_CLI_ONLY = frozenset({"music"})


@final
class _FakeSession:
    """A minimal :class:`MusicSession` -- a fixed vibe, no config on disk."""

    __slots__ = ("_vibe",)
    _vibe: str | None

    def __new__(cls, vibe: str | None = None) -> Self:
        self = super().__new__(cls)
        self._vibe = vibe
        return self

    @property
    def vibe(self) -> str | None:
        return self._vibe

    def refresh_from_config(self) -> None:
        """Do nothing: the parity fixtures pin the vibe rather than read config."""


@final
class _FakeCatalog:
    """A filesystem-free :class:`CatalogGateway` both surfaces are given."""

    __slots__ = ("_albums", "calls")
    _albums: dict[str, str]
    calls: list[tuple[str, object]]

    def __new__(cls, albums: dict[str, str] | None = None) -> Self:
        self = super().__new__(cls)
        self._albums = dict(albums) if albums is not None else {}
        self.calls = []
        return self

    def new(self, prompts: PromptSet, name: str | None) -> str:
        self.calls.append(("new", name))
        album_id = name or f"{len(self._albums):06x}"
        self._albums[album_id] = f"album-{album_id}"
        return album_id

    def get(self, album_id: str, dest_dir: str) -> str:
        self.calls.append(("get", (album_id, dest_dir)))
        return f"{dest_dir}/{self._albums[album_id]}"

    def remove(self, album_id: str) -> None:
        self.calls.append(("remove", album_id))


def _cli(program: FakeProgramGateway) -> tuple[MusicCli, MagicMock]:
    """Return the playback CLI bound to *program*, plus its recording formatter."""
    formatter = MagicMock(spec=OutputFormatter)
    return MusicCli(formatter, lambda: program, vibe_source=lambda: None), formatter


def _catalog_cli(catalog: _FakeCatalog) -> tuple[CatalogCli, MagicMock]:
    """Return the authoring CLI bound to *catalog*, plus its recording formatter."""
    formatter = MagicMock(spec=OutputFormatter)
    return CatalogCli(formatter, lambda: catalog, OutputFlags(formatter)), formatter


def _tool(
    program: FakeProgramGateway, catalog: _FakeCatalog | None = None
) -> MusicTool:
    """Return an MCP tool bound to the same *program* the CLI is given."""
    cat = catalog if catalog is not None else _FakeCatalog()
    return MusicTool(
        lambda: program,
        cast("Callable[[], CatalogGateway]", lambda: cat),
        cast("Callable[[], MusicSession]", _FakeSession),
        MusicPreference,
    )


def _cli_payload(formatter: MagicMock) -> dict[str, object]:
    """Return the JSON payload the CLI emitted, minus its CLI-only verb key."""
    payload = cast("dict[str, object]", formatter.emit.call_args.args[0])
    return {key: value for key, value in payload.items() if key not in _CLI_ONLY}


def _tool_payload(result: str) -> dict[str, object]:
    """Return the tool's reply as a dict."""
    return cast("dict[str, object]", json.loads(result))


def _catalog() -> tuple[ProgramSummary, ...]:
    return (
        ProgramSummary(
            id="a3f1c9",
            style="trance",
            vibe="calm",
            format="music",
            ready=5,
            total=6,
            name="Night Drive",
        ),
        ProgramSummary(
            id="7b2e04", style="lofi", vibe="focus", format="music", ready=1, total=1
        ),
    )


# ---------------------------------------------------------------------------
# The verb sets themselves must match
# ---------------------------------------------------------------------------


def test_both_surfaces_expose_the_same_verbs() -> None:
    """A verb on one surface and not the other is a hole in the contract."""
    formatter = OutputFormatter()
    app = MusicCli.build_app(formatter, OutputFlags(formatter))
    cli_verbs = {c.name for c in app.registered_commands if c.name is not None}

    assert cli_verbs == set(get_args(MusicSubcommand))


# ---------------------------------------------------------------------------
# list -- the saved crate reads identically on both surfaces
# ---------------------------------------------------------------------------


def test_list_reports_the_same_album_records() -> None:
    """The ``programs`` records match field for field, ``format`` included."""
    catalog = _catalog()
    cli, formatter = _cli(FakeProgramGateway(catalog=catalog))
    cli.list_programs()

    tool_result = _tool_payload(
        _tool(FakeProgramGateway(catalog=catalog)).dispatch("list")
    )
    cli_result = _cli_payload(formatter)

    assert cli_result["programs"] == tool_result["programs"]
    assert set(cli_result) == set(tool_result)


def test_list_reports_the_format_of_every_album() -> None:
    """The regression this file was written for: the CLI dropped ``format``."""
    cli, formatter = _cli(FakeProgramGateway(catalog=_catalog()))
    cli.list_programs()

    programs = cast("list[dict[str, object]]", _cli_payload(formatter)["programs"])
    assert [album["format"] for album in programs] == ["music", "music"]


def test_list_of_an_empty_crate_matches() -> None:
    cli, formatter = _cli(FakeProgramGateway())
    cli.list_programs()

    tool_result = _tool_payload(_tool(FakeProgramGateway()).dispatch("list"))

    assert _cli_payload(formatter)["programs"] == tool_result["programs"] == []


# ---------------------------------------------------------------------------
# status -- the same program block and the same derived mode
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "status",
    [
        ProgramStatus.idle(),
        ProgramStatus.radio(ProgramName("ambient_techno"), NowPlaying(index=1, of=3)),
    ],
    ids=["idle", "playing"],
)
def test_status_reports_the_same_program_and_mode(status: ProgramStatus) -> None:
    cli, formatter = _cli(FakeProgramGateway(status=status))
    cli.status()

    tool_result = _tool_payload(
        _tool(FakeProgramGateway(status=status)).dispatch("status")
    )
    cli_result = _cli_payload(formatter)

    assert cli_result["program"] == tool_result["program"]
    assert cli_result["music_mode"] == tool_result["music_mode"]


def test_status_carries_the_music_mode_label_on_both_surfaces() -> None:
    """``music_mode`` was reported by the tool and omitted by the CLI."""
    playing = ProgramStatus.radio(ProgramName("x"), NowPlaying(index=1, of=2))
    cli, formatter = _cli(FakeProgramGateway(status=playing))
    cli.status()

    assert _cli_payload(formatter)["music_mode"] == "on"


def test_status_reports_the_same_field_set() -> None:
    cli, formatter = _cli(FakeProgramGateway())
    cli.status()

    tool_result = _tool_payload(_tool(FakeProgramGateway()).dispatch("status"))

    assert set(_cli_payload(formatter)) == set(tool_result)


# ---------------------------------------------------------------------------
# get -- the same locator fields
# ---------------------------------------------------------------------------


def test_get_reports_the_same_fields() -> None:
    cli, formatter = _catalog_cli(_FakeCatalog({"7f3a91": "warm-pads-7f3a91"}))
    cli.get("7f3a91", dest="/out")

    tool_result = _tool_payload(
        _tool(
            FakeProgramGateway(), _FakeCatalog({"7f3a91": "warm-pads-7f3a91"})
        ).dispatch("get", album_id="7f3a91", dest="/out")
    )

    assert _cli_payload(formatter) == tool_result


def test_remove_reports_the_same_fields() -> None:
    cli, formatter = _catalog_cli(_FakeCatalog({"7f3a91": "a"}))
    cli.remove("7f3a91")

    tool_result = _tool_payload(
        _tool(FakeProgramGateway(), _FakeCatalog({"7f3a91": "a"})).dispatch(
            "remove", album_id="7f3a91"
        )
    )

    assert _cli_payload(formatter) == tool_result


def test_catalog_verbs_leave_the_active_program_untouched_on_both_surfaces() -> None:
    """Authoring parks a track in the catalog; playback is not disturbed."""
    cli, _ = _catalog_cli(_FakeCatalog({"7f3a91": "a"}))
    cli.new("warm pads")
    cli.remove("7f3a91")

    program = FakeProgramGateway()
    tool = _tool(program, _FakeCatalog({"7f3a91": "a"}))
    tool.dispatch("new", base_prompt="warm pads")
    tool.dispatch("remove", album_id="7f3a91")

    # The CLI cannot reach a program gateway at all -- CatalogCli holds none.
    assert program.calls == []


def test_new_reports_the_same_fields() -> None:
    cli, formatter = _catalog_cli(_FakeCatalog())
    cli.new("warm analog pads")

    tool_result = _tool_payload(
        _tool(FakeProgramGateway(), _FakeCatalog()).dispatch(
            "new", base_prompt="warm analog pads"
        )
    )

    assert _cli_payload(formatter) == tool_result


# ---------------------------------------------------------------------------
# Canonicalisation -- a blank tag is absent on both surfaces, or neither
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("blank", ["", "   ", "\t"])
def test_a_blank_play_tag_is_absent_on_both_surfaces(blank: str) -> None:
    """A whitespace tag must not become an album handle that matches nothing."""
    cli_gateway = FakeProgramGateway()
    cli, _ = _cli(cli_gateway)
    cli.play(style=blank, vibe=blank, name=blank)

    tool_gateway = FakeProgramGateway()
    _tool(tool_gateway).dispatch("play", style=blank, vibe=blank, name=blank)

    assert cli_gateway.calls[0].selection == tool_gateway.calls[0].selection


def test_a_blank_play_id_replays_the_last_played_on_both_surfaces() -> None:
    """``play "  "`` carries no axis, so it is the bare replay-last request."""
    tool_gateway = FakeProgramGateway()
    _tool(tool_gateway).dispatch("play", album_id="   ")

    selection = tool_gateway.calls[0].selection
    assert selection is not None
    assert selection.is_empty


@pytest.mark.parametrize("blank", ["", "   "])
def test_a_blank_new_title_is_absent_on_both_surfaces(blank: str) -> None:
    """A blank title must not bind a whitespace album handle on either surface."""
    cli_catalog = _FakeCatalog()
    cli, _ = _catalog_cli(cli_catalog)
    cli.new("warm pads", title=blank)

    tool_catalog = _FakeCatalog()
    _tool(FakeProgramGateway(), tool_catalog).dispatch(
        "new", base_prompt="warm pads", title=blank
    )

    assert cli_catalog.calls == tool_catalog.calls == [("new", None)]


def test_a_blank_on_style_is_absent_on_both_surfaces(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # No piped pool on either side, so both fall back to the daemon's literal.
    monkeypatch.setattr("sys.stdin", io.StringIO(""))
    cli_gateway = FakeProgramGateway()
    cli, _ = _cli(cli_gateway)
    cli.on(style="   ", title="  ")

    tool_gateway = FakeProgramGateway()
    _tool(tool_gateway).dispatch("on", style="   ", title="  ")

    assert cli_gateway.calls[0].request == tool_gateway.calls[0].request


# ---------------------------------------------------------------------------
# Control verbs -- the daemon's own reason reaches a JSON consumer on both
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("cli_verb", "subcommand"),
    [
        (MusicCli.stop, "stop"),
        (MusicCli.advance, "next"),
        (MusicCli.prev, "prev"),
        (MusicCli.pause, "pause"),
        (MusicCli.resume, "resume"),
    ],
    ids=["stop", "next", "prev", "pause", "resume"],
)
def test_a_rejected_control_verb_reports_its_reason_on_both_surfaces(
    cli_verb: Callable[[MusicCli], None], subcommand: MusicSubcommand
) -> None:
    """A reject that explains itself in prose and not in JSON is undiagnosable."""
    reason = "nothing is playing"
    cli_gateway = FakeProgramGateway(applied=False, reason=reason)
    cli, formatter = _cli(cli_gateway)
    cli_verb(cli)

    tool_result = _tool_payload(
        _tool(FakeProgramGateway(applied=False, reason=reason)).dispatch(subcommand)
    )
    cli_result = _cli_payload(formatter)

    assert cli_result["applied"] is tool_result["applied"] is False
    assert cli_result["message"] == reason
    assert reason in cast("str", tool_result["message"])
