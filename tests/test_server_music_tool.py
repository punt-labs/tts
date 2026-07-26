"""Tests for the single ``music`` MCP tool (server_music_tool.MusicTool).

MusicTool is a humble object: each subcommand is driven directly with an
in-memory ``FakeProgramGateway`` (playback) and an in-memory catalog fake --
no daemon, no socket -- so per-subcommand dispatch, the unknown-subcommand and
malformed-prompt errors, the required-argument guards, and the catalog-vs-
program isolation are asserted without a wire. MusicArgs canonicalisation is
asserted directly.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Self, cast, final

import pytest
from _program_fakes import FakeProgramGateway

from punt_vox.server_music_tool import MusicArgs, MusicSubcommand, MusicTool
from punt_vox.types_programs.control import ProgramSummary
from punt_vox.types_programs.prompts import POOL_SIZE, PromptSet
from punt_vox.vibe_command import MusicPreference

if TYPE_CHECKING:
    from collections.abc import Callable

    from punt_vox.catalog_gateway import CatalogGateway
    from punt_vox.server_music_tool import MusicSession


@final
class _FakeSession:
    """A minimal :class:`MusicSession`: a fixed vibe and a refresh counter."""

    __slots__ = ("_vibe", "refreshes")
    _vibe: str | None
    refreshes: int

    def __new__(cls, vibe: str | None = None) -> Self:
        self = super().__new__(cls)
        self._vibe = vibe
        self.refreshes = 0
        return self

    @property
    def vibe(self) -> str | None:
        return self._vibe

    def refresh_from_config(self) -> None:
        self.refreshes += 1


@final
class _FakeCatalog:
    """A filesystem-free :class:`CatalogGateway`: records calls, serves albums."""

    __slots__ = ("_albums", "calls")
    _albums: dict[str, str]
    calls: list[tuple[str, object]]

    def __new__(cls, albums: dict[str, str] | None = None) -> Self:
        self = super().__new__(cls)
        self._albums = dict(albums) if albums is not None else {}
        self.calls = []
        return self

    def new(self, prompts: PromptSet, name: str | None) -> str:
        self.calls.append(("new", prompts))
        album_id = name or f"{len(self._albums):06x}"
        self._albums[album_id] = f"album-{album_id}"
        return album_id

    def get(self, album_id: str, dest_dir: str) -> str:
        self.calls.append(("get", (album_id, dest_dir)))
        if album_id not in self._albums:
            raise ValueError(f"no album named '{album_id}'")
        return f"{dest_dir}/{self._albums[album_id]}"

    def remove(self, album_id: str) -> None:
        self.calls.append(("remove", album_id))
        if album_id not in self._albums:
            raise ValueError(f"no album named '{album_id}'")


def _tool(
    program: FakeProgramGateway | None = None,
    catalog: _FakeCatalog | None = None,
    session: _FakeSession | None = None,
) -> MusicTool:
    prog = program if program is not None else FakeProgramGateway()
    cat = catalog if catalog is not None else _FakeCatalog()
    ses = session if session is not None else _FakeSession()
    return MusicTool(
        lambda: prog,
        cast("Callable[[], CatalogGateway]", lambda: cat),
        cast("Callable[[], MusicSession]", lambda: ses),
        MusicPreference,
    )


def _pool() -> list[str]:
    return [f"variation {i}" for i in range(POOL_SIZE)]


# ---------------------------------------------------------------------------
# dispatch -- each subcommand reaches the right op
# ---------------------------------------------------------------------------


def test_dispatch_refreshes_the_session_first() -> None:
    session = _FakeSession()
    tool = _tool(session=session)

    tool.dispatch("list")

    assert session.refreshes == 1


def test_on_routes_to_program_start() -> None:
    program = FakeProgramGateway()
    result = json.loads(_tool(program=program).dispatch("on", style="techno"))

    assert result["applied"] is True
    assert program.verbs() == ["start"]
    request = program.calls[0].request
    assert request is not None and request.style == "techno"


def test_on_forwards_the_authored_pool() -> None:
    program = FakeProgramGateway()
    _tool(program=program).dispatch("on", base_prompt="deep techno", variations=_pool())

    request = program.calls[0].request
    assert request is not None and request.prompts is not None
    assert request.prompts.base == "deep techno"
    assert request.prompts.variations == tuple(_pool())


def test_on_without_prompts_sends_none_for_the_daemon_fallback() -> None:
    program = FakeProgramGateway()
    _tool(program=program).dispatch("on", style="techno")

    request = program.calls[0].request
    assert request is not None and request.prompts is None


def test_on_carries_the_session_vibe() -> None:
    program = FakeProgramGateway()
    _tool(program=program, session=_FakeSession(vibe="focused")).dispatch("on")

    request = program.calls[0].request
    assert request is not None and request.vibe == "focused"


def test_on_malformed_prompt_shape_is_a_clean_error() -> None:
    result = json.loads(
        _tool().dispatch("on", base_prompt="x", variations=["only one"])
    )
    assert "error" in result


def test_off_routes_to_program_stop() -> None:
    program = FakeProgramGateway()
    result = json.loads(_tool(program=program).dispatch("off"))

    assert result["applied"] is True
    assert program.verbs() == ["stop"]


def test_play_routes_to_program_select() -> None:
    program = FakeProgramGateway()
    result = json.loads(
        _tool(program=program).dispatch("play", style="trance", vibe="calm")
    )

    assert result["applied"] is True
    assert program.calls[0].verb == "select"
    assert program.calls[0].selection is not None
    assert program.calls[0].selection.style == "trance"


def test_next_routes_to_program_advance() -> None:
    program = FakeProgramGateway()
    result = json.loads(_tool(program=program).dispatch("next"))

    assert result["applied"] is True
    assert program.verbs() == ["advance"]


def test_list_routes_to_program_catalog() -> None:
    catalog = (
        ProgramSummary(
            id="a3f1c9", style="trance", vibe="calm", format="music", ready=5
        ),
    )
    program = FakeProgramGateway(catalog=catalog)
    result = json.loads(_tool(program=program).dispatch("list"))

    assert [p["id"] for p in result["programs"]] == ["a3f1c9"]
    assert "a3f1c9" in result["message"]


# ---------------------------------------------------------------------------
# catalog verbs -- new/get/remove reach the catalog gateway
# ---------------------------------------------------------------------------


def test_new_builds_promptset_single_and_reaches_the_catalog() -> None:
    catalog = _FakeCatalog()
    result = json.loads(_tool(catalog=catalog).dispatch("new", base_prompt="warm pads"))

    assert set(result) == {"album_id"}
    verb, prompts = catalog.calls[0]
    assert verb == "new"
    assert isinstance(prompts, PromptSet)
    assert prompts.base == "warm pads"
    assert prompts.variations == ()  # a single track has no variations


def test_new_canonicalises_a_blank_name_to_none() -> None:
    """A blank/whitespace name is canonicalised to None, so the daemon
    content-addresses the album rather than binding a whitespace handle."""
    result = json.loads(_tool().dispatch("new", base_prompt="warm pads", name="   "))
    # The _FakeCatalog content-addresses to "000000" when the name is None;
    # an uncanonicalised "   " would bind that whitespace handle instead.
    assert result["album_id"] == "000000"


def test_new_without_base_prompt_is_an_error() -> None:
    catalog = _FakeCatalog()
    result = json.loads(_tool(catalog=catalog).dispatch("new"))

    assert "error" in result
    assert catalog.calls == []


def test_get_exports_to_dest_and_returns_the_locator() -> None:
    catalog = _FakeCatalog({"7f3a91": "warm-pads-7f3a91"})
    result = json.loads(
        _tool(catalog=catalog).dispatch("get", album_id="7f3a91", dest="/out")
    )

    assert result == {"album_id": "7f3a91", "path": "/out/warm-pads-7f3a91"}


def test_get_requires_album_id_and_dest() -> None:
    catalog = _FakeCatalog()
    result = json.loads(_tool(catalog=catalog).dispatch("get", album_id="7f3a91"))

    assert "error" in result
    assert catalog.calls == []


def test_remove_deletes_via_the_catalog() -> None:
    catalog = _FakeCatalog({"7f3a91": "warm-pads-7f3a91"})
    result = json.loads(_tool(catalog=catalog).dispatch("remove", album_id="7f3a91"))

    assert result == {"removed": "7f3a91"}
    assert ("remove", "7f3a91") in catalog.calls


def test_remove_without_album_id_is_an_error() -> None:
    result = json.loads(_tool().dispatch("remove"))
    assert "error" in result


# ---------------------------------------------------------------------------
# Isolation -- playback verbs never touch the catalog, and vice versa
# ---------------------------------------------------------------------------


def test_catalog_verbs_leave_the_active_program_untouched() -> None:
    program = FakeProgramGateway()
    catalog = _FakeCatalog({"7f3a91": "warm-pads-7f3a91"})
    tool = _tool(program=program, catalog=catalog)

    tool.dispatch("new", base_prompt="warm pads")
    tool.dispatch("get", album_id="7f3a91", dest="/out")
    tool.dispatch("remove", album_id="7f3a91")

    assert program.calls == []  # no start/stop/select/advance/catalog


def test_playback_verbs_never_touch_the_catalog_gateway() -> None:
    catalog = _FakeCatalog()
    tool = _tool(catalog=catalog)

    tool.dispatch("on")
    tool.dispatch("off")
    tool.dispatch("next")

    assert catalog.calls == []


# ---------------------------------------------------------------------------
# unknown subcommand + MusicArgs canonicalisation
# ---------------------------------------------------------------------------


def test_unknown_subcommand_returns_an_error() -> None:
    result = json.loads(_tool().dispatch(cast("MusicSubcommand", "sideways")))
    assert "error" in result


@pytest.mark.parametrize("blank", ["", "   "])
def test_blank_tags_canonicalise_to_none(blank: str) -> None:
    args = MusicArgs("on", style=blank, vibe=blank, name=blank)
    assert args.canonical_style is None
    assert args.canonical_vibe is None
    assert args.canonical_name is None


def test_canonical_tags_trim_surrounding_whitespace() -> None:
    args = MusicArgs("play", style="  trance  ", vibe=" calm ", name=" mix ")
    assert args.canonical_style == "trance"
    assert args.canonical_vibe == "calm"
    assert args.canonical_name == "mix"


def test_authored_reflects_a_supplied_variation_pool() -> None:
    assert MusicArgs("on", variations=_pool()).authored is True
    assert MusicArgs("on").authored is False


def test_blank_style_on_reaches_the_daemon_as_none() -> None:
    program = FakeProgramGateway()
    _tool(program=program).dispatch("on", style="   ")

    request = program.calls[0].request
    assert request is not None and request.style is None
