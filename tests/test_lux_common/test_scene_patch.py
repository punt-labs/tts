"""Tests for :mod:`punt_vox.lux_common.scene_patch`.

The properties that matter are the ones luxd's patch seam imposes: a patch can
only address an element that is already installed, containers are descended
through rather than compared, and the changed fields keep the order the current
render emitted them in (the table's selection is intersected against the rows as
they stand at setter time, so ``rows`` must go first).
"""

from __future__ import annotations

from typing import cast

from punt_lux import RenderRequest

from punt_vox.lux_common.scene_patch import ElementPatch, ScenePatchSet, SceneTree


def _scene(*elements: dict[str, object]) -> RenderRequest:
    return RenderRequest(scene_id="vox.music", elements=list(elements), title="Music")


def _text(element_id: str, content: str) -> dict[str, object]:
    return {"kind": "text", "id": element_id, "content": content}


def _tree(*elements: dict[str, object]) -> SceneTree:
    return SceneTree.of(_scene(*elements))


class TestElementPatch:
    def test_to_wire_carries_the_id_and_the_set(self) -> None:
        patch = ElementPatch("music.now.position", {"content": "3 of 12"})
        assert patch.to_wire() == {
            "id": "music.now.position",
            "set": {"content": "3 of 12"},
        }


class TestScenePatchSet:
    def test_empty_set_is_falsy(self) -> None:
        assert not ScenePatchSet(())

    def test_to_wire_preserves_patch_order(self) -> None:
        patches = ScenePatchSet(
            (ElementPatch("a", {"content": "1"}), ElementPatch("b", {"content": "2"}))
        )
        assert [entry["id"] for entry in patches.to_wire()] == ["a", "b"]


class TestFlatten:
    def test_ids_follow_tree_order(self) -> None:
        tree = _tree(_text("one", "a"), _text("two", "b"))
        assert tree.ids == ("one", "two")

    def test_a_group_is_descended_into_and_also_addressed(self) -> None:
        group: dict[str, object] = {
            "kind": "group",
            "id": "music.transport",
            "children": [_text("music.transport.prev", "prev")],
        }
        assert _tree(group).ids == ("music.transport", "music.transport.prev")

    def test_an_element_without_an_id_makes_the_tree_unpatchable(self) -> None:
        tree = _tree({"kind": "text", "content": "anonymous"})
        assert tree.unidentified == 1
        assert not tree.patchable_against(tree)


class TestPatchability:
    def test_identical_trees_are_patchable(self) -> None:
        tree = _tree(_text("one", "a"))
        assert tree.patchable_against(_tree(_text("one", "b")))

    def test_an_added_element_is_not_patchable(self) -> None:
        current = _tree(_text("one", "a"), _text("two", "b"))
        assert not current.patchable_against(_tree(_text("one", "a")))

    def test_a_reordered_roster_is_not_patchable(self) -> None:
        current = _tree(_text("two", "b"), _text("one", "a"))
        assert not current.patchable_against(
            _tree(_text("one", "a"), _text("two", "b"))
        )

    def test_a_vanishing_field_is_not_patchable(self) -> None:
        before = _tree({"kind": "table", "id": "t", "rows": [], "selected_row_ids": []})
        current = _tree({"kind": "table", "id": "t", "rows": []})
        assert not current.patchable_against(before)


class TestPatchesAgainst:
    def test_an_unchanged_element_yields_no_patch(self) -> None:
        tree = _tree(_text("one", "a"))
        assert not tree.patches_against(_tree(_text("one", "a")))

    def test_only_the_changed_field_is_emitted(self) -> None:
        before = _tree({"kind": "button", "id": "b", "label": "Play", "disabled": True})
        current = _tree(
            {"kind": "button", "id": "b", "label": "Pause", "disabled": True}
        )
        assert current.patches_against(before).patches == (
            ElementPatch("b", {"label": "Pause"}),
        )

    def test_structural_fields_are_never_patched(self) -> None:
        group: dict[str, object] = {
            "kind": "group",
            "id": "g",
            "children": [_text("g.child", "new")],
        }
        before: dict[str, object] = {
            "kind": "group",
            "id": "g",
            "children": [_text("g.child", "old")],
        }
        patches = _tree(group).patches_against(_tree(before))
        assert patches.patches == (ElementPatch("g.child", {"content": "new"}),)

    def test_a_table_patch_orders_rows_before_the_selection(self) -> None:
        before = _tree(
            {
                "kind": "table",
                "id": "music.albums",
                "rows": [["a"]],
                "selected_row_ids": [],
            }
        )
        current = _tree(
            {
                "kind": "table",
                "id": "music.albums",
                "rows": [["a"], ["b"]],
                "selected_row_ids": ["b"],
            }
        )
        wire = current.patches_against(before).to_wire()
        fields = cast("dict[str, object]", wire[0]["set"])
        assert list(fields) == ["rows", "selected_row_ids"]
