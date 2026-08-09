"""Tests for the ``vox music`` catalog verbs (cli_music_catalog.CatalogCli).

CatalogCli is a humble object: each verb is driven directly with an in-memory
catalog gateway and a mock formatter -- no daemon -- so the emitted payload, the
title canonicalisation, and the clean-error paths are asserted without a wire.
It holds no program gateway at all, so a catalog verb cannot disturb playback by
construction; the cross-surface counterpart is asserted in the parity tests.
"""

from __future__ import annotations

from pathlib import Path
from typing import Self, final
from unittest.mock import MagicMock

import pytest
import typer

from punt_vox.cli_io import OutputFlags
from punt_vox.cli_music_catalog import CatalogCli
from punt_vox.client_errors import VoxdProtocolError
from punt_vox.output_formatter import OutputFormatter
from punt_vox.types_programs.prompts import PromptSet


@final
class InMemoryCatalogGateway:
    """A filesystem-backed ``CatalogGateway`` fake for the authoring verbs."""

    __slots__ = ("_albums", "_calls", "_playing")
    _albums: dict[str, str]
    _playing: set[str]
    _calls: list[tuple[str, str | None]]

    def __new__(
        cls,
        albums: dict[str, str] | None = None,
        playing: set[str] | None = None,
    ) -> Self:
        self = super().__new__(cls)
        self._albums = dict(albums) if albums is not None else {}
        self._playing = set(playing) if playing is not None else set()
        self._calls = []
        return self

    @property
    def calls(self) -> list[tuple[str, str | None]]:
        """Return the recorded ``(verb, arg)`` calls for assertions."""
        return self._calls

    def new(self, prompts: PromptSet, name: str | None) -> str:
        self._calls.append(("new", prompts.base))
        album_id = name or f"{len(self._albums):06x}"
        self._albums[album_id] = f"album-{album_id}"
        return album_id

    def get(self, album_id: str, dest_dir: str) -> str:
        self._calls.append(("get", album_id))
        if album_id not in self._albums:
            raise VoxdProtocolError(f"no album named '{album_id}'")
        target = Path(dest_dir) / self._albums[album_id]
        target.mkdir(parents=True)  # exist_ok=False -> a collision raises
        return str(target)

    def remove(self, album_id: str) -> None:
        self._calls.append(("remove", album_id))
        if album_id not in self._albums:
            raise VoxdProtocolError(f"no album named '{album_id}'")
        if album_id in self._playing:
            raise VoxdProtocolError(f"album {album_id} is playing; stop it first")
        del self._albums[album_id]


def _cli(catalog: InMemoryCatalogGateway) -> tuple[CatalogCli, MagicMock]:
    formatter = MagicMock(spec=OutputFormatter)
    return CatalogCli(formatter, lambda: catalog, OutputFlags(formatter)), formatter


def _emitted(formatter: MagicMock) -> tuple[object, str]:
    payload, text = formatter.emit.call_args.args
    return payload, text


# ---------------------------------------------------------------------------
# new -- catalog authoring (verbatim prompt, bare id, program untouched)
# ---------------------------------------------------------------------------


def test_new_passes_prompt_verbatim_and_prints_bare_id() -> None:
    catalog = InMemoryCatalogGateway()
    cli, formatter = _cli(catalog)

    cli.new("warm analog pads, slow, D minor, instrumental, loopable")

    assert catalog.calls == [
        ("new", "warm analog pads, slow, D minor, instrumental, loopable")
    ]
    payload, text = _emitted(formatter)
    assert payload == {"album_id": text}  # human text is exactly the bare id


def test_new_forwards_the_title_as_the_album_name() -> None:
    """``vox music new --title`` hands the title to the catalog as the name."""
    cli, formatter = _cli(InMemoryCatalogGateway())

    cli.new("warm pads", title="Warm Pads")

    payload, _ = _emitted(formatter)
    # The fake keys the album on the name it was handed; the CLI prints that id.
    assert payload == {"album_id": "Warm Pads"}


def test_new_trims_the_title_before_binding_it() -> None:
    cli, formatter = _cli(InMemoryCatalogGateway())

    cli.new("warm pads", title="  Warm Pads  ")

    payload, _ = _emitted(formatter)
    assert payload == {"album_id": "Warm Pads"}


@pytest.mark.parametrize("blank", ["", "   "])
def test_new_treats_a_blank_title_as_absent(blank: str) -> None:
    """A whitespace title must not bind a whitespace album handle."""
    cli, formatter = _cli(InMemoryCatalogGateway())

    cli.new("warm pads", title=blank)

    payload, _ = _emitted(formatter)
    # The fake content-addresses to "000000" when the name is None.
    assert payload == {"album_id": "000000"}


def test_new_bad_prompt_is_clean_error() -> None:
    gateway = MagicMock()
    gateway.new.side_effect = VoxdProtocolError("bad_prompt")
    formatter = MagicMock(spec=OutputFormatter)
    cli = CatalogCli(formatter, lambda: gateway, OutputFlags(formatter))

    with pytest.raises(typer.Exit):
        cli.new("copyrighted work")


# ---------------------------------------------------------------------------
# get -- copy an album directory out, refuse a collision
# ---------------------------------------------------------------------------


def test_get_creates_album_directory_in_cwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    cli, formatter = _cli(InMemoryCatalogGateway({"7f3a91": "warm-pads-7f3a91"}))

    cli.get("7f3a91")

    written = tmp_path / "warm-pads-7f3a91"
    assert written.is_dir()
    _, text = _emitted(formatter)
    assert text == str(written)


def test_get_exports_into_the_named_destination(tmp_path: Path) -> None:
    """``--dest`` names the export directory, matching ``mic:music get``."""
    cli, formatter = _cli(InMemoryCatalogGateway({"7f3a91": "warm-pads-7f3a91"}))
    elsewhere = tmp_path / "crate"

    cli.get("7f3a91", dest=str(elsewhere))

    written = elsewhere / "warm-pads-7f3a91"
    assert written.is_dir()
    payload, text = _emitted(formatter)
    assert payload == {"album_id": "7f3a91", "path": str(written)}
    assert text == str(written)


def test_get_collision_is_clean_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "warm-pads-7f3a91").mkdir()
    cli, _ = _cli(InMemoryCatalogGateway({"7f3a91": "warm-pads-7f3a91"}))

    with pytest.raises(typer.Exit):
        cli.get("7f3a91")


def test_get_unknown_album_is_clean_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    cli, _ = _cli(InMemoryCatalogGateway())

    with pytest.raises(typer.Exit):
        cli.get("missing")


# ---------------------------------------------------------------------------
# remove -- delete an idle album, refuse a playing one
# ---------------------------------------------------------------------------


def test_remove_deletes_idle_album() -> None:
    catalog = InMemoryCatalogGateway({"7f3a91": "warm-pads-7f3a91"})
    cli, formatter = _cli(catalog)

    cli.remove("7f3a91")

    assert ("remove", "7f3a91") in catalog.calls
    payload, text = _emitted(formatter)
    assert payload == {"removed": "7f3a91"}
    assert text == "removed 7f3a91"


def test_remove_refuses_playing_album() -> None:
    catalog = InMemoryCatalogGateway({"7f3a91": "warm-pads-7f3a91"}, playing={"7f3a91"})
    cli, _ = _cli(catalog)

    with pytest.raises(typer.Exit):
        cli.remove("7f3a91")


def test_a_daemon_fault_is_reported_as_a_json_error_under_json_output() -> None:
    """A ``--json`` consumer parses one object, never a blank stdout."""
    formatter = OutputFormatter(json_output=True)
    cli = CatalogCli(formatter, InMemoryCatalogGateway, OutputFlags(formatter))

    with pytest.raises(typer.Exit):
        cli.remove("missing")
