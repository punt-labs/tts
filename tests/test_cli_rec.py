"""Tests for the ``vox rec`` CLI (cli_rec.RecCli).

RecCli is a humble object: each verb is driven directly with an in-memory
RecordGateway and a mock formatter -- no daemon, no socket -- so new/list/play/
get/remove behaviour (bare-id output, the collision refusal, byte-correct
get, error paths) is asserted without a wire. CliRunner tests confirm
build_rec_app wires the group and that the old top-level verbs are gone.
"""

from __future__ import annotations

import errno
import io
import json
import os
import re
from pathlib import Path
from typing import TYPE_CHECKING, NoReturn, Self, final
from unittest.mock import MagicMock

import pytest
import typer
from _cli_introspect import app_help_texts, command_opts
from typer.testing import CliRunner
from websockets.exceptions import WebSocketException

from punt_vox.__main__ import app
from punt_vox.cli_io import OutputFlags
from punt_vox.cli_rec import RecCli, RecordGateway, build_rec_app
from punt_vox.client import RecordingSummary, RecordResult
from punt_vox.client_errors import VoxdProtocolError
from punt_vox.output_formatter import OutputFormatter

if TYPE_CHECKING:
    from punt_vox.types_synthesis import SynthesisSpec


@final
class InMemoryRecordGateway:
    """A filesystem-free ``RecordGateway`` for surface tests: a dict store."""

    __slots__ = ("_store", "calls")
    _store: dict[str, bytes]
    calls: list[tuple[str, str]]

    def __new__(cls, store: dict[str, bytes] | None = None) -> Self:
        self = super().__new__(cls)
        self._store = dict(store) if store is not None else {}
        self.calls = []
        return self

    def new(self, text: str, spec: SynthesisSpec, name: str | None) -> RecordResult:
        store_name = name or f"{abs(hash(text)) % (10**12):012d}.mp3"
        data = text.encode()
        self._store[store_name] = data
        self.calls.append(("new", store_name))
        return RecordResult(
            id=store_name,
            name=store_name,
            store_path=Path(store_name),
            byte_count=len(data),
        )

    def recordings(self) -> tuple[RecordingSummary, ...]:
        self.calls.append(("recordings", ""))
        return tuple(
            RecordingSummary(name=n, byte_count=len(b)) for n, b in self._store.items()
        )

    def play(self, ref: str) -> None:
        self.calls.append(("play", ref))
        if ref not in self._store:
            raise VoxdProtocolError(f"no recording named '{ref}'")

    def get(self, ref: str) -> bytes:
        self.calls.append(("get", ref))
        if ref not in self._store:
            raise VoxdProtocolError(f"no recording named '{ref}'")
        return self._store[ref]

    def remove(self, ref: str) -> None:
        self.calls.append(("remove", ref))
        if ref not in self._store:
            raise VoxdProtocolError(f"no recording named '{ref}'")
        del self._store[ref]


def _cli(gateway: RecordGateway) -> tuple[RecCli, MagicMock]:
    formatter = MagicMock(spec=OutputFormatter)
    return RecCli(formatter, lambda: gateway), formatter


def _payload(formatter: MagicMock) -> dict[str, object]:
    payload, _ = formatter.emit.call_args.args
    assert isinstance(payload, dict)
    return payload


def _text(formatter: MagicMock) -> str:
    _, text = formatter.emit.call_args.args
    assert isinstance(text, str)
    return text


# ---------------------------------------------------------------------------
# new -- synthesize into the store, print the bare id
# ---------------------------------------------------------------------------


def test_new_prints_bare_id_no_path_or_host() -> None:
    cli, formatter = _cli(InMemoryRecordGateway())

    cli.new(text="the build is green")

    payload, text = _payload(formatter), _text(formatter)
    assert text.endswith(".mp3")
    assert "/" not in text  # no path
    assert "host" not in payload
    assert set(payload) == {"id", "bytes", "cached"}


def test_new_name_passthrough() -> None:
    fake = InMemoryRecordGateway()
    cli, formatter = _cli(fake)

    cli.new(text="hi", name="greeting.mp3")

    assert _text(formatter) == "greeting.mp3"
    assert ("new", "greeting.mp3") in fake.calls


def test_new_empty_name_rejected_before_wire() -> None:
    fake = InMemoryRecordGateway()
    cli, _ = _cli(fake)

    with pytest.raises(typer.Exit):
        cli.new(text="hi", name="")
    assert fake.calls == []  # never reached the gateway


def test_new_daemon_error_is_clean_exit() -> None:
    gateway = MagicMock()
    gateway.new.side_effect = VoxdProtocolError("provider down")
    cli = RecCli(MagicMock(spec=OutputFormatter), lambda: gateway)

    with pytest.raises(typer.Exit):
        cli.new(text="hi")


def test_new_uses_configured_provider_from_state(
    hermetic_config: Path,
) -> None:
    """``vox rec new`` sends the provider ``vox.md`` names, not an empty field.

    ``vox rec new`` used to build a ``SynthesisSpec`` with only the flags the
    caller passed, so a bare ``vox rec new "hi"`` sent no provider and let
    the daemon guess -- the same substitution the client half of vox-w3f8
    exists to prevent. Routing through ``SessionSpec`` off ``vox.md`` fills
    provider from state.
    """
    hermetic_config.joinpath("vox.md").write_text(
        '---\nprovider: "elevenlabs"\nvoice: "matilda"\n---\n'
    )
    gateway = MagicMock(spec=RecordGateway)
    gateway.new.return_value = RecordResult(
        id="x.mp3", name="x.mp3", store_path=Path("x.mp3"), byte_count=3
    )
    cli = RecCli(MagicMock(spec=OutputFormatter), lambda: gateway)

    cli.new(text="hi")

    # The gateway saw a spec whose provider came from state, not empty.
    sent_spec = gateway.new.call_args.args[1]
    assert sent_spec.provider == "elevenlabs"
    assert sent_spec.voice == "matilda"


def test_new_fills_provider_through_session_spec_for_repo(
    hermetic_config: Path,
) -> None:
    """Regression: ``_fill_from_state`` must route through
    ``SessionSpec.for_repo()`` -- the one config-lookup path every
    synthesis surface is meant to share -- not a private, duplicated
    ``ConfigStore(find_config_dir() or DEFAULT_CONFIG_DIR).read()`` that
    would silently drift from ``vox say``/``vox call``'s.
    """
    from unittest.mock import patch

    from punt_vox.session_spec import SessionSpec

    hermetic_config.joinpath("vox.md").write_text('---\nprovider: "elevenlabs"\n---\n')
    gateway = MagicMock(spec=RecordGateway)
    gateway.new.return_value = RecordResult(
        id="x.mp3", name="x.mp3", store_path=Path("x.mp3"), byte_count=3
    )
    cli = RecCli(MagicMock(spec=OutputFormatter), lambda: gateway)

    with patch.object(
        SessionSpec, "for_repo", wraps=SessionSpec.for_repo
    ) as spy_for_repo:
        cli.new(text="hi")
    spy_for_repo.assert_called_once()


def test_new_unconfigured_provider_exits_1(hermetic_config: Path) -> None:
    """No provider in state -- ``vox rec new`` exits 1 (F1), never sends.

    The refusal happens before the gateway is touched: the daemon is not
    contacted, and no id is emitted. Same shape as ``vox say`` on the
    F1 path.
    """
    hermetic_config.joinpath("vox.md").write_text("---\n---\n")
    fake = InMemoryRecordGateway()
    cli, formatter = _cli(fake)

    with pytest.raises(typer.Exit) as excinfo:
        cli.new(text="hi")

    assert excinfo.value.exit_code == 1
    assert fake.calls == []
    formatter.emit.assert_not_called()


def test_new_alien_model_exits_1(hermetic_config: Path) -> None:
    """A hand-edited ``vox.md`` pair (provider openai, model eleven_v3) exits 1.

    The F7 refusal fires at the same seam as F1; the caller sees the pair
    state actually declared, never a silent substitution to the OpenAI
    default.
    """
    hermetic_config.joinpath("vox.md").write_text(
        '---\nprovider: "openai"\nmodel: "eleven_v3"\n---\n'
    )
    fake = InMemoryRecordGateway()
    cli, _ = _cli(fake)

    with pytest.raises(typer.Exit) as excinfo:
        cli.new(text="hi")

    assert excinfo.value.exit_code == 1
    assert fake.calls == []


def test_new_from_file_emits_one_id_per_segment(tmp_path: Path) -> None:
    segments_file = tmp_path / "segs.json"
    segments_file.write_text('["first line", "second line"]', encoding="utf-8")
    fake = InMemoryRecordGateway()
    cli, formatter = _cli(fake)

    cli.new(from_file=segments_file)

    assert formatter.emit.call_count == 2  # one bare id per segment
    assert [verb for verb, _ in fake.calls] == ["new", "new"]


def test_new_name_rejected_for_multiple_segments(tmp_path: Path) -> None:
    segments_file = tmp_path / "segs.json"
    segments_file.write_text('["one", "two"]', encoding="utf-8")
    fake = InMemoryRecordGateway()
    cli, _ = _cli(fake)

    with pytest.raises(typer.Exit):
        cli.new(from_file=segments_file, name="fixed.mp3")
    assert fake.calls == []  # a single --name cannot address many segments


def test_new_reads_text_from_stdin(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("sys.stdin", io.StringIO("recorded from a pipe\n"))
    fake = InMemoryRecordGateway()
    cli, formatter = _cli(fake)

    cli.new(text="-")

    assert fake.calls[0][0] == "new"
    assert formatter.emit.call_count == 1


# ---------------------------------------------------------------------------
# list
# ---------------------------------------------------------------------------


def test_list_empty_store() -> None:
    cli, formatter = _cli(InMemoryRecordGateway())

    cli.list_recordings()

    assert _payload(formatter) == {"recordings": []}
    assert "No recordings" in _text(formatter)


def test_list_ids_one_per_line_no_size_column() -> None:
    store = {"a1b2c3d4e5f6.mp3": b"x" * 10, "greeting.mp3": b"yy"}
    cli, formatter = _cli(InMemoryRecordGateway(store))

    cli.list_recordings()

    assert _text(formatter) == "a1b2c3d4e5f6.mp3\ngreeting.mp3"
    assert "10" not in _text(formatter)  # human output pipes -- no size column
    assert _payload(formatter)["recordings"] == [
        {"id": "a1b2c3d4e5f6.mp3", "bytes": 10},
        {"id": "greeting.mp3", "bytes": 2},
    ]


# ---------------------------------------------------------------------------
# play
# ---------------------------------------------------------------------------


def test_play_delegates_id_to_gateway() -> None:
    fake = InMemoryRecordGateway({"a1b2c3.mp3": b"x"})
    cli, formatter = _cli(fake)

    cli.play("a1b2c3.mp3")

    assert ("play", "a1b2c3.mp3") in fake.calls
    assert _payload(formatter) == {"played": "a1b2c3.mp3"}


def test_play_daemon_error_is_clean_exit() -> None:
    cli, _ = _cli(InMemoryRecordGateway())  # empty store -> play raises

    with pytest.raises(typer.Exit):
        cli.play("missing.mp3")


def test_play_websocket_error_is_clean_error() -> None:
    gateway = MagicMock()
    gateway.play.side_effect = WebSocketException("connection closed")
    cli = RecCli(MagicMock(spec=OutputFormatter), lambda: gateway)

    with pytest.raises(typer.Exit):
        cli.play("a1b2c3.mp3")


# ---------------------------------------------------------------------------
# get -- write ./<id>, refuse collision, byte-correct, no partial
# ---------------------------------------------------------------------------


def test_get_writes_into_cwd_under_store_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    payload_bytes = b"\xff\xfb\x90\x00" * 5
    cli, formatter = _cli(InMemoryRecordGateway({"rec.mp3": payload_bytes}))

    cli.get("rec.mp3")

    written = tmp_path / "rec.mp3"
    assert written.read_bytes() == payload_bytes
    assert _text(formatter) == "./rec.mp3"
    assert _payload(formatter) == {"path": str(written), "bytes": len(payload_bytes)}


def test_get_large_multichunk_roundtrips_byte_correct(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A large recording (many client-side chunks) reassembles byte-correct."""
    monkeypatch.chdir(tmp_path)
    big = bytes(range(256)) * 4000  # ~1 MB, spans many FETCH_CHUNK_BYTES chunks
    cli, _ = _cli(InMemoryRecordGateway({"big.mp3": big}))

    cli.get("big.mp3")

    assert (tmp_path / "big.mp3").read_bytes() == big


def test_get_collision_errors_and_leaves_existing_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    existing = tmp_path / "rec.mp3"
    existing.write_bytes(b"ORIGINAL")
    fake = InMemoryRecordGateway({"rec.mp3": b"DAEMON-BYTES"})
    cli, _ = _cli(fake)

    with pytest.raises(typer.Exit):
        cli.get("rec.mp3")
    assert existing.read_bytes() == b"ORIGINAL"  # untouched
    assert fake.calls == []  # refused before the fetch


def test_get_not_found_exits_and_leaves_no_partial(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    cli, _ = _cli(InMemoryRecordGateway())  # empty -> get raises

    with pytest.raises(typer.Exit):
        cli.get("missing.mp3")
    assert not (tmp_path / "missing.mp3").exists()  # no partial file


def test_get_file_racing_in_mid_fetch_is_not_clobbered(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A file that appears at ./<id> after the absence check is not overwritten.

    The gateway lands ./rec.mp3 during the fetch (a second process writing the
    name), so the exclusive-link landing must refuse rather than clobber it --
    closing the TOCTOU gap between the check and the write.
    """
    monkeypatch.chdir(tmp_path)
    raced = tmp_path / "rec.mp3"

    def racing_get(ref: str) -> bytes:
        raced.write_bytes(b"RACED-IN")  # appears after get()'s absence check
        return b"DAEMON-BYTES"

    gateway = MagicMock()
    gateway.get.side_effect = racing_get
    cli = RecCli(MagicMock(spec=OutputFormatter), lambda: gateway)

    with pytest.raises(typer.Exit):
        cli.get("rec.mp3")
    assert raced.read_bytes() == b"RACED-IN"  # exclusive link refused to clobber


def test_get_interrupt_mid_write_leaves_no_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A KeyboardInterrupt mid-write strands no file, so a retry is not blocked.

    O_EXCL reserves the final path before the write, so a non-OSError interrupt
    (SIGINT) partway through would otherwise leave a truncated file that the
    guard reads as an existing target -- a retry then fails "exists". The
    BaseException cleanup unlinks *dest* on ANY interruption, so nothing is left
    and the guard stays honest.
    """
    monkeypatch.chdir(tmp_path)

    class _Interrupting:
        def __enter__(self) -> Self:
            return self

        def __exit__(self, *_exc: object) -> None:
            return None

        def write(self, _data: bytes) -> int:
            raise KeyboardInterrupt

    def fake_fdopen(fd: int, *_args: object, **_kwargs: object) -> _Interrupting:
        os.close(fd)  # release the real descriptor the O_EXCL open reserved
        return _Interrupting()

    monkeypatch.setattr("punt_vox.cli_rec.os.fdopen", fake_fdopen)
    cli, _ = _cli(InMemoryRecordGateway({"rec.mp3": b"DATA"}))

    with pytest.raises(KeyboardInterrupt):
        cli.get("rec.mp3")
    # The interrupt left nothing at the final path -- the O_EXCL guard is clear.
    assert not (tmp_path / "rec.mp3").exists()


def test_get_does_not_require_hardlink_support(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The landing uses O_EXCL, not os.link, so it works on FAT/network mounts.

    ``os.link`` raises ENOTSUP/EPERM on filesystems without hard-link support.
    Poisoning it proves the no-clobber landing never reaches for a hard link.
    """
    monkeypatch.chdir(tmp_path)

    def no_hardlinks(*_args: object, **_kwargs: object) -> NoReturn:
        raise OSError(errno.ENOTSUP, "hard links unsupported")

    monkeypatch.setattr("punt_vox.cli_rec.os.link", no_hardlinks)
    payload_bytes = b"\xff\xfb\x90\x00" * 5
    cli, _ = _cli(InMemoryRecordGateway({"rec.mp3": payload_bytes}))

    cli.get("rec.mp3")

    assert (tmp_path / "rec.mp3").read_bytes() == payload_bytes


@pytest.mark.parametrize(
    "hostile",
    ["../escape.mp3", "/etc/passwd", "sub/dir.mp3", "..", ".", "a\\b.mp3", ""],
)
def test_get_rejects_non_bare_ref_and_writes_nothing(
    hostile: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A ref with a separator, ``.``/``..``, or absolute path escapes the CWD.

    ``rec get`` joins the ref onto the working directory, so a hostile ref
    would write outside it. BareName rejects it before the dest is computed or
    the fetch runs: the command exits cleanly and nothing lands anywhere.
    """
    monkeypatch.chdir(tmp_path)
    fake = InMemoryRecordGateway({"rec.mp3": b"DATA"})
    cli, _ = _cli(fake)

    with pytest.raises(typer.Exit):
        cli.get(hostile)
    assert fake.calls == []  # rejected before the fetch -- no daemon round-trip
    assert list(tmp_path.iterdir()) == []  # nothing written into the CWD


# ---------------------------------------------------------------------------
# remove
# ---------------------------------------------------------------------------


def test_remove_delegates_and_reports() -> None:
    fake = InMemoryRecordGateway({"rec.mp3": b"x"})
    cli, formatter = _cli(fake)

    cli.remove("rec.mp3")

    assert ("remove", "rec.mp3") in fake.calls
    assert _payload(formatter) == {"removed": "rec.mp3"}
    assert _text(formatter) == "removed rec.mp3"


def test_remove_not_found_is_clean_error() -> None:
    cli, _ = _cli(InMemoryRecordGateway())

    with pytest.raises(typer.Exit):
        cli.remove("missing.mp3")


# ---------------------------------------------------------------------------
# build_rec_app wiring + forward-integration guard (old verbs gone, no -o)
# ---------------------------------------------------------------------------


def _build_rec_app() -> typer.Typer:
    """Build the rec Typer group with a formatter and its OutputFlags."""
    fmt = OutputFormatter()
    return build_rec_app(fmt, OutputFlags(fmt))


def _rec_verb_names() -> set[str]:
    rec_app = _build_rec_app()
    return {c.name for c in rec_app.registered_commands if c.name is not None}


def test_rec_group_exposes_exactly_the_verb_set() -> None:
    assert _rec_verb_names() == {"new", "list", "play", "get", "remove"}


def test_top_level_record_play_fetch_do_not_exist() -> None:
    names = {c.name for c in app.registered_commands if c.name is not None}
    assert "record" not in names
    assert "play" not in names
    assert "fetch" not in names


def test_rec_and_music_groups_are_registered() -> None:
    groups = {g.name for g in app.registered_groups if g.name is not None}
    assert {"rec", "music"} <= groups


def test_no_output_option_in_rec_get_surface() -> None:
    opts = command_opts("rec", "get")
    assert "-o" not in opts
    assert "--output" not in opts


def test_output_dir_kept_only_on_desktop_install() -> None:
    """--output-dir lives on ``vox desktop install``, not on ``vox rec get``."""
    opts = command_opts("desktop", "install")
    assert "--output-dir" in opts
    assert "-d" in opts
    assert "--output-dir" not in command_opts("rec", "get")


def test_rec_app_no_subcommand_shows_help() -> None:
    result = CliRunner().invoke(_build_rec_app(), [])
    assert result.exit_code != 0 or "Usage" in result.output


# A design-decision label (D-1..D-9, DES-0xx) in user-facing help is a defect:
# help is the manual and must read plainly, with no internal shorthand.
_INTERNAL_LABEL = re.compile(r"\bD-[0-9]\b|\bDES-")


def test_rec_help_carries_no_internal_labels() -> None:
    """No group/verb/option help leaks a design label."""
    for text in app_help_texts(_build_rec_app()):
        assert not _INTERNAL_LABEL.search(text), text


def test_list_accepts_json_flag_after_the_subcommand() -> None:
    """vox-cnak: --json parses AFTER the subcommand, not only before it."""
    fmt = OutputFormatter()
    flags = OutputFlags(fmt)
    cli = RecCli(fmt, lambda: InMemoryRecordGateway(), flags=flags)
    # A second command makes this a multi-command group (as `vox rec` is), so the
    # runner treats "list" as the subcommand -- the position where --json failed.
    app = typer.Typer()
    app.command("list")(cli.list_recordings)
    app.command("remove")(cli.remove)

    result = CliRunner().invoke(app, ["list", "--json"])

    assert result.exit_code == 0
    assert json.loads(result.stdout) == {"recordings": []}
