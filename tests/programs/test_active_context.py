"""Tests for the active-source context holder and its two backing shapes.

``locate`` runs the shared containment gate (F2): the untrusted manifest file
identity must resolve to a regular file inside the album directory, so these
tests write real files under ``tmp_path`` and assert a symlink or an escaping
identity is refused before the path could reach ``loadfile``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from punt_vox.types_programs.prompts import PromptSet
from punt_vox.voxd.programs.active_context import (
    ActiveContext,
    ActiveProgram,
    ActiveSelection,
)
from punt_vox.voxd.programs.album_id import AlbumId
from punt_vox.voxd.programs.album_tags import AlbumTags
from punt_vox.voxd.programs.part import Part
from punt_vox.voxd.programs.selection import Selection

from .conftest import InMemoryPartStore, make_manifest

_PROMPTS = PromptSet(base="pad", variations=("a", "b"))


def _album_dir(root: Path, name: str, *files: str) -> Path:
    """Create ``root/name`` with each file as a real regular file; return the dir."""
    directory = root / name
    directory.mkdir(parents=True, exist_ok=True)
    for filename in files:
        (directory / filename).write_bytes(b"audio")
    return directory


def _active(directory: Path) -> ActiveProgram:
    manifest = make_manifest(1, 2)
    return ActiveProgram(
        album_id=AlbumId("a3f1c9"),
        store=InMemoryPartStore(manifest),
        tags=AlbumTags(style="techno", vibe="ambient"),
        directory=directory,
        prompts=_PROMPTS,
    )


def _selection() -> Selection:
    return Selection.from_albums(
        [
            ("album-a", (Part("001.mp3", 1), Part("002.mp3", 2))),
            ("album-b", (Part("001.mp3", 1),)),
        ]
    )


class TestActiveProgram:
    def test_to_plan_carries_store_tags_and_prompts(self, tmp_path: Path) -> None:
        active = _active(_album_dir(tmp_path, "techno--ambient-a3f1c9"))
        plan = active.to_plan()
        assert plan.store is active.store
        assert plan.tags == active.tags
        assert plan.prompts == _PROMPTS

    def test_locate_resolves_a_contained_regular_file(self, tmp_path: Path) -> None:
        directory = _album_dir(tmp_path, "techno--ambient-a3f1c9", "001.mp3")
        active = _active(directory)
        assert active.locate(Part("001.mp3", 1)) == directory / "001.mp3"

    def test_locate_refuses_an_identity_escaping_the_album_dir(
        self, tmp_path: Path
    ) -> None:
        # An untrusted manifest file field cannot traverse out of the album dir:
        # the bare-name gate rejects a path separator / "..".
        active = _active(_album_dir(tmp_path, "techno--ambient-a3f1c9"))
        (tmp_path / "secret").write_bytes(b"top secret")
        with pytest.raises(ValueError, match="part name"):
            active.locate(Part("../secret", 1))

    def test_locate_refuses_a_symlink_identity(self, tmp_path: Path) -> None:
        # A symlink whose target is outside the album must not be followed: the
        # no-follow regular-file check refuses it, so loadfile never opens it.
        directory = _album_dir(tmp_path, "techno--ambient-a3f1c9")
        (tmp_path / "outside.mp3").write_bytes(b"outside")
        (directory / "001.mp3").symlink_to(tmp_path / "outside.mp3")
        active = _active(directory)
        with pytest.raises(ValueError, match="part name"):
            active.locate(Part("001.mp3", 1))

    def test_spec_for_composes_prompt_from_base_and_variation(
        self, tmp_path: Path
    ) -> None:
        plan = _active(_album_dir(tmp_path, "techno--ambient-a3f1c9")).to_plan()
        assert plan.spec_for(1).prompt == "pad a"
        assert plan.spec_for(2).prompt == "pad b"
        assert plan.spec_for(3).prompt == "pad a"  # cycles


class TestActiveSelection:
    def test_locate_resolves_each_part_under_root(self, tmp_path: Path) -> None:
        _album_dir(tmp_path, "album-a", "001.mp3", "002.mp3")
        active = ActiveSelection(tmp_path, _selection(), "radio")
        first = _selection().parts[0]
        assert active.locate(first.playable) == tmp_path / "album-a" / "001.mp3"

    def test_colliding_filenames_resolve_to_distinct_paths(
        self, tmp_path: Path
    ) -> None:
        _album_dir(tmp_path, "album-a", "001.mp3", "002.mp3")
        _album_dir(tmp_path, "album-b", "001.mp3")
        selection = _selection()
        active = ActiveSelection(tmp_path, selection, "radio")
        a_first = selection.parts[0].playable  # album-a/001.mp3
        b_first = selection.parts[2].playable  # album-b/001.mp3
        assert active.locate(a_first) != active.locate(b_first)

    def test_locate_refuses_a_symlink_part(self, tmp_path: Path) -> None:
        # A selection part whose on-disk file is a symlink out of the album is
        # refused, so a crafted saved album cannot redirect loadfile.
        directory = _album_dir(tmp_path, "album-a", "002.mp3")
        (tmp_path / "outside.mp3").write_bytes(b"outside")
        (directory / "001.mp3").symlink_to(tmp_path / "outside.mp3")
        active = ActiveSelection(tmp_path, _selection(), "radio")
        with pytest.raises(ValueError, match="part name"):
            active.locate(_selection().parts[0].playable)


class TestActiveContext:
    def test_idle_context_has_no_current(self) -> None:
        ctx = ActiveContext()
        assert ctx.current is None
        assert ctx.name() is None

    def test_plan_raises_while_idle(self) -> None:
        with pytest.raises(RuntimeError, match="no active source"):
            ActiveContext().plan()

    def test_locate_raises_while_idle(self) -> None:
        with pytest.raises(RuntimeError, match="no active source"):
            ActiveContext().locate(Part("001.mp3", 1))

    def test_switch_to_program_activates_it(self, tmp_path: Path) -> None:
        directory = _album_dir(tmp_path, "techno--ambient-a3f1c9", "001.mp3")
        ctx = ActiveContext()
        active = _active(directory)
        ctx.switch(active)
        assert ctx.current is active
        assert ctx.plan().store is active.store
        assert ctx.locate(Part("001.mp3", 1)) == directory / "001.mp3"

    def test_switch_to_selection_has_no_plan(self, tmp_path: Path) -> None:
        ctx = ActiveContext()
        ctx.switch(ActiveSelection(tmp_path, _selection(), "radio"))
        with pytest.raises(RuntimeError, match="consume-only selection"):
            ctx.plan()

    def test_clear_returns_to_idle(self, tmp_path: Path) -> None:
        ctx = ActiveContext()
        ctx.switch(_active(_album_dir(tmp_path, "techno--ambient-a3f1c9")))
        ctx.clear()
        assert ctx.current is None

    def test_switch_replaces_the_active_source(self, tmp_path: Path) -> None:
        _album_dir(tmp_path, "first-a3f1c9", "001.mp3")
        second = _album_dir(tmp_path, "second-7b2e04", "001.mp3")
        ctx = ActiveContext()
        ctx.switch(_active(tmp_path / "first-a3f1c9"))
        ctx.switch(_active(second))
        assert ctx.locate(Part("001.mp3", 1)) == second / "001.mp3"
