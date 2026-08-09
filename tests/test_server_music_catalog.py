"""Tests for the saved-album module (server_music_catalog).

CatalogVerbs is a humble object: it is driven with an in-memory catalog fake --
no daemon, no filesystem -- so the required-argument guards, the fault envelope,
and the per-call gateway lookup are asserted without a wire. SavedAlbums is a
pure renderer, asserted directly on its three output shapes.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Self, final

import pytest

from punt_vox.client_errors import VoxdConnectionError
from punt_vox.music_args import MusicArgs
from punt_vox.server_music_catalog import CatalogVerbs, SavedAlbums
from punt_vox.types_programs.control import ProgramSummary

if TYPE_CHECKING:
    from punt_vox.catalog_gateway import CatalogGateway
    from punt_vox.types_programs.prompts import PromptSet


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
            msg = f"no album named '{album_id}'"
            raise ValueError(msg)
        return f"{dest_dir}/{self._albums[album_id]}"

    def remove(self, album_id: str) -> None:
        self.calls.append(("remove", album_id))
        if album_id not in self._albums:
            msg = f"no album named '{album_id}'"
            raise ValueError(msg)
        del self._albums[album_id]


@final
class _BrokenCatalog:
    """A :class:`CatalogGateway` whose every call fails the transport."""

    __slots__ = ()

    def new(self, prompts: PromptSet, name: str | None) -> str:
        raise VoxdConnectionError(self._msg())

    def get(self, album_id: str, dest_dir: str) -> str:
        raise VoxdConnectionError(self._msg())

    def remove(self, album_id: str) -> None:
        raise VoxdConnectionError(self._msg())

    def _msg(self) -> str:
        return "voxd is not running"


def _verbs(catalog: CatalogGateway) -> CatalogVerbs:
    return CatalogVerbs(lambda: catalog)


def _args(
    subcommand: str,
    *,
    base_prompt: str | None = None,
    title: str | None = None,
    album_id: str | None = None,
    dest: str | None = None,
) -> MusicArgs:
    return MusicArgs(
        subcommand=subcommand,
        base_prompt=base_prompt,
        title=title,
        album_id=album_id,
        dest=dest,
    )


class TestNew:
    """``music new`` -- author one track into a fresh album."""

    def test_returns_the_authored_album_id(self) -> None:
        catalog = _FakeCatalog()
        result = _verbs(catalog).new(_args("new", base_prompt="klezmer", title="Set"))
        assert json.loads(result) == {"album_id": "Set"}

    def test_requires_a_base_prompt(self) -> None:
        catalog = _FakeCatalog()
        result = _verbs(catalog).new(_args("new"))
        assert json.loads(result) == {"error": "music new requires base_prompt"}

    def test_missing_base_prompt_never_reaches_the_catalog(self) -> None:
        catalog = _FakeCatalog()
        _verbs(catalog).new(_args("new"))
        assert catalog.calls == []

    def test_daemon_fault_becomes_the_error_envelope(self) -> None:
        result = _verbs(_BrokenCatalog()).new(_args("new", base_prompt="klezmer"))
        assert json.loads(result) == {"error": "voxd is not running"}


class TestGet:
    """``music get`` -- export a saved album to a destination directory."""

    def test_returns_the_written_locator(self) -> None:
        catalog = _FakeCatalog({"abc": "album-abc"})
        result = _verbs(catalog).get(_args("get", album_id="abc", dest="/out"))
        assert json.loads(result) == {"album_id": "abc", "path": "/out/album-abc"}

    @pytest.mark.parametrize(
        ("album_id", "dest"),
        [("abc", None), (None, "/out"), (None, None)],
        ids=["no-dest", "no-album-id", "neither"],
    )
    def test_requires_both_album_id_and_dest(
        self, album_id: str | None, dest: str | None
    ) -> None:
        result = _verbs(_FakeCatalog()).get(_args("get", album_id=album_id, dest=dest))
        assert json.loads(result) == {"error": "music get requires album_id and dest"}

    def test_unknown_album_becomes_the_error_envelope(self) -> None:
        catalog = _FakeCatalog()
        result = _verbs(catalog).get(_args("get", album_id="nope", dest="/out"))
        assert json.loads(result) == {"error": "no album named 'nope'"}


class TestRemove:
    """``music remove`` -- delete a saved album by id."""

    def test_reports_the_removed_album(self) -> None:
        catalog = _FakeCatalog({"abc": "album-abc"})
        result = _verbs(catalog).remove(_args("remove", album_id="abc"))
        assert json.loads(result) == {"removed": "abc"}

    def test_removes_it_from_the_catalog(self) -> None:
        catalog = _FakeCatalog({"abc": "album-abc"})
        _verbs(catalog).remove(_args("remove", album_id="abc"))
        assert ("remove", "abc") in catalog.calls

    def test_requires_an_album_id(self) -> None:
        result = _verbs(_FakeCatalog()).remove(_args("remove"))
        assert json.loads(result) == {"error": "music remove requires album_id"}

    def test_daemon_fault_becomes_the_error_envelope(self) -> None:
        result = _verbs(_BrokenCatalog()).remove(_args("remove", album_id="abc"))
        assert json.loads(result) == {"error": "voxd is not running"}


def _summary(album_id: str, name: str | None = None) -> ProgramSummary:
    return ProgramSummary(
        id=album_id,
        style="klezmer",
        vibe="bright",
        format="music",
        ready=3,
        total=4,
        name=name,
    )


class TestSavedAlbumsAnnounced:
    """The crate as ``list`` announces it."""

    def test_empty_crate_says_so(self) -> None:
        assert SavedAlbums([]).announced() == "♪ No saved albums."

    def test_counts_the_albums(self) -> None:
        listing = SavedAlbums([_summary("a"), _summary("b")]).announced()
        assert listing.splitlines()[0] == "♪ 2 saved album(s):"

    def test_marks_every_album_line(self) -> None:
        listing = SavedAlbums([_summary("a"), _summary("b")]).announced()
        assert [line.startswith("  ♪ ") for line in listing.splitlines()[1:]] == [
            True,
            True,
        ]


class TestSavedAlbumsAppendedTo:
    """The crate as a bare ``play`` with no history appends it to the reject."""

    def test_empty_crate_leaves_the_message_bare(self) -> None:
        assert SavedAlbums([]).appended_to("nothing played yet") == "nothing played yet"

    def test_keeps_the_message_as_the_first_line(self) -> None:
        text = SavedAlbums([_summary("a")]).appended_to("nothing played yet")
        assert text.splitlines()[0] == "nothing played yet"

    def test_lists_the_albums_under_a_heading(self) -> None:
        text = SavedAlbums([_summary("a")]).appended_to("nothing played yet")
        assert text.splitlines()[1] == "saved albums:"

    def test_album_lines_are_not_marquee_marked(self) -> None:
        """The reject is an error envelope, not a DJ announcement."""
        text = SavedAlbums([_summary("a")]).appended_to("nothing played yet")
        assert "♪" not in text


class TestSavedAlbumsToWire:
    """The ``programs`` records a reply carries."""

    def test_empty_crate_is_an_empty_list(self) -> None:
        assert SavedAlbums([]).to_wire() == []

    def test_carries_every_reported_field(self) -> None:
        assert SavedAlbums([_summary("a", name="Set")]).to_wire() == [
            {
                "id": "a",
                "style": "klezmer",
                "vibe": "bright",
                "name": "Set",
                "format": "music",
                "ready": 3,
                "total": 4,
            }
        ]

    def test_reports_an_untitled_album_with_a_null_name(self) -> None:
        assert SavedAlbums([_summary("a")]).to_wire()[0]["name"] is None


class TestGatewayLifetime:
    """The gateway is resolved per call, never pinned at construction."""

    def test_each_verb_call_asks_the_factory_again(self) -> None:
        served: list[_FakeCatalog] = []

        def factory() -> CatalogGateway:
            served.append(catalog := _FakeCatalog({"abc": "album-abc"}))
            return catalog

        verbs = CatalogVerbs(factory)
        verbs.remove(_args("remove", album_id="abc"))
        verbs.remove(_args("remove", album_id="abc"))
        assert len(served) == 2
