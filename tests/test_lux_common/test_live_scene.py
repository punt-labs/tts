"""Tests for :mod:`punt_vox.lux_common.live_scene`.

``plan`` is the whole state machine: nothing installed installs, an identical
render pushes nothing, a moved value patches, and anything a patch cannot express
-- a changed roster or a changed frame shell -- installs again.
"""

from __future__ import annotations

from punt_lux import RenderRequest
from punt_lux.operations.models import FrameSpec

from punt_vox.lux_common.live_scene import LiveScene
from punt_vox.lux_common.scene_push import InstallScene, NoPush, PatchScene


def _scene(
    *elements: dict[str, object], title: str = "Music", frame: FrameSpec | None = None
) -> RenderRequest:
    return RenderRequest(
        scene_id="vox.music", elements=list(elements), title=title, frame=frame
    )


def _text(element_id: str, content: str) -> dict[str, object]:
    return {"kind": "text", "id": element_id, "content": content}


class TestFirstPush:
    def test_installs_when_nothing_is_on_screen(self) -> None:
        assert isinstance(LiveScene().plan(_scene(_text("a", "1"))), InstallScene)

    def test_starts_disarmed(self) -> None:
        assert not LiveScene().armed

    def test_a_push_arms_the_scene(self) -> None:
        live = LiveScene()
        live.plan(_scene(_text("a", "1")))
        assert live.armed


class TestRefresh:
    def test_an_identical_render_pushes_nothing(self) -> None:
        live = LiveScene()
        live.plan(_scene(_text("a", "1")))
        assert isinstance(live.plan(_scene(_text("a", "1"))), NoPush)

    def test_a_moved_value_patches_only_that_element(self) -> None:
        live = LiveScene()
        live.plan(_scene(_text("a", "1"), _text("b", "2")))
        push = live.plan(_scene(_text("a", "9"), _text("b", "2")))
        assert isinstance(push, PatchScene)
        assert [patch.element_id for patch in push.patches.patches] == ["a"]

    def test_a_changed_roster_installs(self) -> None:
        live = LiveScene()
        live.plan(_scene(_text("a", "1")))
        assert isinstance(
            live.plan(_scene(_text("a", "1"), _text("b", "2"))), InstallScene
        )

    def test_a_changed_frame_shell_installs(self) -> None:
        live = LiveScene()
        live.plan(_scene(_text("a", "1")))
        assert isinstance(
            live.plan(_scene(_text("a", "1"), title="Other")), InstallScene
        )

    def test_a_changed_frame_spec_installs(self) -> None:
        live = LiveScene()
        live.plan(_scene(_text("a", "1"), frame=FrameSpec(size=(10, 10))))
        push = live.plan(_scene(_text("a", "1"), frame=FrameSpec(size=(20, 20))))
        assert isinstance(push, InstallScene)

    def test_a_group_child_patches_the_child_never_the_group(self) -> None:
        def _row(label: str) -> dict[str, object]:
            return {
                "kind": "group",
                "id": "row",
                "children": [{"kind": "button", "id": "row.go", "label": label}],
            }

        live = LiveScene()
        live.plan(_scene(_row("Play")))
        push = live.plan(_scene(_row("Pause")))
        assert isinstance(push, PatchScene)
        assert push.patches.patches[0].element_id == "row.go"
        assert "children" not in push.patches.patches[0].fields


class TestExplicitInstall:
    def test_install_shows_even_when_the_render_is_identical(self) -> None:
        live = LiveScene()
        request = _scene(_text("a", "1"))
        live.plan(request)
        assert isinstance(live.install(request), InstallScene)

    def test_install_arms_so_the_next_refresh_can_patch(self) -> None:
        live = LiveScene()
        live.install(_scene(_text("a", "1")))
        assert isinstance(live.plan(_scene(_text("a", "2"))), PatchScene)


class TestDisarm:
    def test_the_next_push_installs_again(self) -> None:
        live = LiveScene()
        live.plan(_scene(_text("a", "1")))
        live.disarm()
        assert not live.armed
        assert isinstance(live.plan(_scene(_text("a", "1"))), InstallScene)
