"""Tests for PublishButton: the exact publish attribute each button carries.

The button wire dicts are the publish half of the receive-leg contract; pinning them
here is the offline substitute for the (PR-3-gated) live click.
"""

from __future__ import annotations

from punt_vox.voxd.music_player.publish_button import PublishButton


def test_play_button_carries_its_topic_and_album_payload() -> None:
    assert PublishButton.play("aa11bb").to_dict() == {
        "kind": "button",
        "id": "play-aa11bb",
        "label": "Play",
        "publish": {"topic": "music.play", "payload": {"album_id": "aa11bb"}},
    }
