"""Each ``SinkCommand`` variant applies the matching :class:`FakeSink` operation."""

from __future__ import annotations

from conversation_mode._playback_sink_fakes import FakeSink
from punt_vox.voxd.conversation_mode.sink_clear import SinkClear
from punt_vox.voxd.conversation_mode.sink_close import SinkClose
from punt_vox.voxd.conversation_mode.sink_status import SinkStatus
from punt_vox.voxd.conversation_mode.sink_write import SinkWrite


async def test_sink_write_appends_the_chunk_and_marks_writing() -> None:
    sink = FakeSink()

    await SinkWrite(chunk=b"hello").apply(sink)

    assert sink.buffered_bytes == len(b"hello")
    assert sink.status is SinkStatus.WRITING
    assert sink.history == ("write:5",)


async def test_sink_clear_empties_the_buffer_and_marks_idle() -> None:
    sink = FakeSink()
    await SinkWrite(chunk=b"hello").apply(sink)

    await SinkClear(reason="barge-in").apply(sink)

    assert sink.buffered_bytes == 0
    assert sink.status is SinkStatus.IDLE
    assert sink.history == ("write:5", "clear")


async def test_sink_clear_on_an_already_idle_sink_is_a_no_op() -> None:
    sink = FakeSink()

    await SinkClear(reason="redundant").apply(sink)

    assert sink.buffered_bytes == 0
    assert sink.status is SinkStatus.IDLE
    assert sink.history == ("clear",)


async def test_sink_close_empties_the_buffer_and_is_terminal() -> None:
    sink = FakeSink()
    await SinkWrite(chunk=b"hello").apply(sink)

    await SinkClose().apply(sink)

    assert sink.status is SinkStatus.CLOSED
    assert sink.buffered_bytes == 0
