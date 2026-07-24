"""Tests for the consume-only ``vox music`` CLI (cli_music.MusicCli).

MusicCli is a humble object: each command method is driven directly with an
in-memory FakeProgramGateway and a mock formatter -- no daemon, no store -- so
the surface behaviour (album list via the gateway, tag/id replay, next/status,
and the F7 applied/rejected result) is asserted without a wire. A couple of
CliRunner smoke tests confirm build_music_app wires the Typer group.
"""

from __future__ import annotations

from pathlib import Path
from typing import Self, final
from unittest.mock import MagicMock

import pytest
import typer
from _program_fakes import FakeProgramGateway
from typer.testing import CliRunner
from websockets.exceptions import WebSocketException

from punt_vox.cli_music import MusicCli, build_music_app
from punt_vox.client_errors import VoxdProtocolError
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


@final
class InMemoryCatalogGateway:
    """A filesystem-backed ``CatalogGateway`` fake for the authoring verbs."""

    __slots__ = ("_albums", "_playing", "calls")
    _albums: dict[str, str]
    _playing: set[str]
    calls: list[tuple[str, str]]

    def __new__(
        cls,
        albums: dict[str, str] | None = None,
        playing: set[str] | None = None,
    ) -> Self:
        self = super().__new__(cls)
        self._albums = dict(albums) if albums is not None else {}
        self._playing = set(playing) if playing is not None else set()
        self.calls = []
        return self

    def new(self, prompt: str, name: str | None) -> str:
        self.calls.append(("new", prompt))
        album_id = name or f"{len(self._albums):06x}"
        self._albums[album_id] = f"album-{album_id}"
        return album_id

    def get(self, album_id: str, dest_dir: str) -> str:
        self.calls.append(("get", album_id))
        if album_id not in self._albums:
            raise VoxdProtocolError(f"no album named '{album_id}'")
        target = Path(dest_dir) / self._albums[album_id]
        target.mkdir(parents=True)  # exist_ok=False -> collision raises (D-1)
        return str(target)

    def remove(self, album_id: str) -> None:
        self.calls.append(("remove", album_id))
        if album_id not in self._albums:
            raise VoxdProtocolError(f"no album named '{album_id}'")
        if album_id in self._playing:
            raise VoxdProtocolError(f"album {album_id} is playing; stop it first")
        del self._albums[album_id]


def _cli_catalog(
    catalog: InMemoryCatalogGateway,
    program: FakeProgramGateway | None = None,
) -> tuple[MusicCli, MagicMock]:
    formatter = MagicMock(spec=OutputFormatter)
    prog = program if program is not None else FakeProgramGateway()
    return MusicCli(formatter, lambda: prog, lambda: catalog), formatter


# ---------------------------------------------------------------------------
# new -- catalog authoring (verbatim prompt, bare id, program untouched)
# ---------------------------------------------------------------------------


def test_new_passes_prompt_verbatim_and_prints_bare_id() -> None:
    catalog = InMemoryCatalogGateway()
    cli, formatter = _cli_catalog(catalog)

    cli.new("warm analog pads, slow, D minor, instrumental, loopable")

    assert catalog.calls == [
        ("new", "warm analog pads, slow, D minor, instrumental, loopable")
    ]
    payload, text = _emitted(formatter)
    assert payload == {"album_id": text}  # human text is exactly the bare id


def test_new_does_not_touch_the_active_program() -> None:
    """music new parks a track in the catalog; the Program is untouched (D-5)."""
    program = FakeProgramGateway()
    cli, _ = _cli_catalog(InMemoryCatalogGateway(), program)

    cli.new("ambient drone")

    assert program.calls == []  # no select/status/advance -- program untouched


def test_new_bad_prompt_is_clean_error() -> None:
    gateway = MagicMock()
    gateway.new.side_effect = VoxdProtocolError("bad_prompt")
    cli = MusicCli(MagicMock(spec=OutputFormatter), FakeProgramGateway, lambda: gateway)

    with pytest.raises(typer.Exit):
        cli.new("copyrighted work")


# ---------------------------------------------------------------------------
# get -- copy an album directory into the CWD, refuse collision (D-1)
# ---------------------------------------------------------------------------


def test_get_creates_album_directory_in_cwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    catalog = InMemoryCatalogGateway({"7f3a91": "warm-pads-7f3a91"})
    cli, formatter = _cli_catalog(catalog)

    cli.get("7f3a91")

    written = tmp_path / "warm-pads-7f3a91"
    assert written.is_dir()
    _, text = _emitted(formatter)
    assert text == str(written)


def test_get_collision_is_clean_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "warm-pads-7f3a91").mkdir()
    catalog = InMemoryCatalogGateway({"7f3a91": "warm-pads-7f3a91"})
    cli, _ = _cli_catalog(catalog)

    with pytest.raises(typer.Exit):
        cli.get("7f3a91")


def test_get_unknown_album_is_clean_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    cli, _ = _cli_catalog(InMemoryCatalogGateway())

    with pytest.raises(typer.Exit):
        cli.get("missing")


# ---------------------------------------------------------------------------
# remove -- delete an idle album, refuse a playing one (D-2)
# ---------------------------------------------------------------------------


def test_remove_deletes_idle_album() -> None:
    catalog = InMemoryCatalogGateway({"7f3a91": "warm-pads-7f3a91"})
    cli, formatter = _cli_catalog(catalog)

    cli.remove("7f3a91")

    assert ("remove", "7f3a91") in catalog.calls
    payload, text = _emitted(formatter)
    assert payload == {"removed": "7f3a91"}
    assert text == "removed 7f3a91"


def test_remove_refuses_playing_album() -> None:
    catalog = InMemoryCatalogGateway({"7f3a91": "warm-pads-7f3a91"}, playing={"7f3a91"})
    cli, _ = _cli_catalog(catalog)

    with pytest.raises(typer.Exit):
        cli.remove("7f3a91")


def test_music_group_exposes_the_unified_verb_set() -> None:
    app = build_music_app(OutputFormatter())
    names = {c.name for c in app.registered_commands if c.name is not None}
    assert names == {"new", "list", "play", "get", "remove", "next", "status"}


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


def test_status_websocket_handshake_error_is_clean_error() -> None:
    """A stale-token handshake failure on status surfaces cleanly, not raw."""
    gateway = MagicMock()
    gateway.status.side_effect = WebSocketException("invalid status 401")
    cli = MusicCli(MagicMock(spec=OutputFormatter), lambda: gateway)

    with pytest.raises(typer.Exit):
        cli.status()


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


def test_status_idle() -> None:
    cli, formatter = _cli(FakeProgramGateway(status=ProgramStatus.idle()))

    cli.status()

    _, text = _emitted(formatter)
    assert text == "Nothing playing."


# ---------------------------------------------------------------------------
# build_music_app wiring (CliRunner smoke)
# ---------------------------------------------------------------------------


def test_app_no_subcommand_shows_help() -> None:
    app = build_music_app(OutputFormatter())

    result = CliRunner().invoke(app, [])

    assert result.exit_code != 0 or "Usage" in result.output
