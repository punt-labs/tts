"""Tests for the segment-batch synthesis renderer."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from punt_vox.client_errors import VoxdConnectionError, VoxdProtocolError
from punt_vox.synthesis_batch import SegmentBatch
from punt_vox.types_synthesis import SynthesisSpec

if TYPE_CHECKING:
    import pytest


def _echo(seg_text: str, seg_spec: SynthesisSpec) -> dict[str, object]:
    """A handler that echoes the text and the resolved voice/language/tags."""
    return {
        "text": seg_text,
        "voice": seg_spec.voice,
        "language": seg_spec.language,
        "vibe_tags": seg_spec.vibe_tags,
    }


class TestRender:
    """``render`` synthesizes each non-empty segment against the defaults."""

    def test_renders_each_segment(self) -> None:
        segments = [{"text": "one"}, {"text": "two"}]
        batch = SegmentBatch(segments, SynthesisSpec(voice="roger"))

        results = json.loads(batch.render(handler=_echo, error_label="Test"))

        assert [r["text"] for r in results] == ["one", "two"]

    def test_skips_empty_and_missing_text(self) -> None:
        segments = [{"text": "keep"}, {"text": ""}, {"voice": "x"}]
        batch = SegmentBatch(segments, SynthesisSpec())

        results = json.loads(batch.render(handler=_echo, error_label="Test"))

        assert [r["text"] for r in results] == ["keep"]

    def test_empty_segments_yield_empty_list(self) -> None:
        batch = SegmentBatch([], SynthesisSpec())

        assert batch.render(handler=_echo, error_label="Test") == "[]"


class TestOverrides:
    """Per-segment fields override the defaults; defaults fill the rest."""

    def test_segment_overrides_win(self) -> None:
        segments = [
            {"text": "hi", "voice": "sam", "language": "de", "vibe_tags": "[x]"}
        ]
        defaults = SynthesisSpec(voice="roger", language="en", vibe_tags="[y]")
        batch = SegmentBatch(segments, defaults)

        result = json.loads(batch.render(handler=_echo, error_label="Test"))[0]

        assert result["voice"] == "sam"
        assert result["language"] == "de"
        assert result["vibe_tags"] == "[x]"

    def test_defaults_fill_omitted_fields(self) -> None:
        batch = SegmentBatch(
            [{"text": "hi"}],
            SynthesisSpec(voice="roger", language="en", vibe_tags="[y]"),
        )

        result = json.loads(batch.render(handler=_echo, error_label="Test"))[0]

        assert result["voice"] == "roger"
        assert result["language"] == "en"
        assert result["vibe_tags"] == "[y]"


class TestErrorHandling:
    """A daemon failure short-circuits to a JSON error envelope."""

    def test_connection_error_becomes_error_json(self) -> None:
        def boom(_text: str, _spec: SynthesisSpec) -> dict[str, object]:
            raise VoxdConnectionError("voxd down")

        batch = SegmentBatch([{"text": "hi"}], SynthesisSpec())

        result = json.loads(batch.render(handler=boom, error_label="Test"))

        assert result == {"error": "voxd down"}

    def test_protocol_error_becomes_error_json(self) -> None:
        def boom(_text: str, _spec: SynthesisSpec) -> dict[str, object]:
            raise VoxdProtocolError("bad frame")

        batch = SegmentBatch([{"text": "hi"}], SynthesisSpec())

        result = json.loads(batch.render(handler=boom, error_label="Test"))

        assert result == {"error": "bad frame"}

    def test_value_error_becomes_error_json(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        def boom(_text: str, _spec: SynthesisSpec) -> dict[str, object]:
            raise ValueError("bad value")

        batch = SegmentBatch([{"text": "hi"}], SynthesisSpec())

        result = json.loads(batch.render(handler=boom, error_label="Record"))

        assert result == {"error": "bad value"}
        assert "Record failed" in caplog.text


class TestPartialFailure:
    """A partway failure surfaces the landed ids beside the error."""

    def test_landed_results_ride_with_the_error(self) -> None:
        # Segment 1 lands, segment 2 raises: the recorded id must not be lost.
        seen: list[str] = []

        def handler(text: str, _spec: SynthesisSpec) -> dict[str, object]:
            seen.append(text)
            if len(seen) == 2:
                raise VoxdProtocolError("segment 2 died")
            return {"id": f"{text}.mp3", "bytes": 3}

        batch = SegmentBatch([{"text": "one"}, {"text": "two"}], SynthesisSpec())

        result = json.loads(batch.render(handler=handler, error_label="Record"))

        assert result["error"] == "segment 2 died"
        assert result["results"] == [{"id": "one.mp3", "bytes": 3}]

    def test_total_failure_omits_empty_results(self) -> None:
        # Nothing landed: the envelope carries no empty ``results`` key.
        def boom(_text: str, _spec: SynthesisSpec) -> dict[str, object]:
            raise VoxdConnectionError("voxd down")

        batch = SegmentBatch([{"text": "one"}], SynthesisSpec())

        result = json.loads(batch.render(handler=boom, error_label="Record"))

        assert result == {"error": "voxd down"}
