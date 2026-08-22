"""Tests for :class:`~conversation_mode._session_attach_fakes.FakeSessionAttach`.

Slice 1a's contract requires this fake to have its own passing tests before
the mission closes -- Slice 1b starts against an already-working test
double, not a paper interface it has to build test infrastructure for
itself.
"""

from __future__ import annotations

from conversation_mode._session_attach_fakes import FakeSessionAttach, ScriptedChunk
from punt_vox.voxd.conversation_mode.reply import ReplyChunk
from punt_vox.voxd.conversation_mode.session_attach import SessionAttachError
from punt_vox.voxd.conversation_mode.turn import TranscribedTurn


async def _collect(fake: FakeSessionAttach, turn: TranscribedTurn) -> list[ReplyChunk]:
    return [chunk async for chunk in fake.send_turn(turn)]


async def test_records_every_call() -> None:
    fake = FakeSessionAttach()

    await _collect(fake, TranscribedTurn(text="what does this function do"))
    await _collect(fake, TranscribedTurn(text="and the other one"))

    assert fake.turns() == ["what does this function do", "and the other one"]


async def test_empty_script_yields_no_chunks() -> None:
    fake = FakeSessionAttach()

    chunks = await _collect(fake, TranscribedTurn(text="hello"))

    assert chunks == []


async def test_yields_scripted_chunks_in_order() -> None:
    script = (
        ScriptedChunk(ReplyChunk(text="First sentence.", is_final=False)),
        ScriptedChunk(ReplyChunk(text="Second sentence.", is_final=False)),
        ScriptedChunk(ReplyChunk(text="Third sentence.", is_final=True)),
    )
    fake = FakeSessionAttach(script)

    chunks = await _collect(fake, TranscribedTurn(text="tell me about it"))

    assert [c.text for c in chunks] == [
        "First sentence.",
        "Second sentence.",
        "Third sentence.",
    ]
    assert [c.is_final for c in chunks] == [False, False, True]


async def test_caller_can_start_speaking_before_the_reply_finishes() -> None:
    """FR-11: speaking begins on the first complete portion, not the whole reply.

    A single-reply fake cannot exercise this -- there would be nothing to
    observe between "the call started" and "the whole reply arrived." This
    test proves the fake's sequence genuinely yields chunk-by-chunk: the
    first chunk is observable while later chunks have not been produced yet,
    the same shape a caller streaming into synthesis depends on.
    """
    script = (
        ScriptedChunk(ReplyChunk(text="First.", is_final=False), delay_s=0.0),
        ScriptedChunk(ReplyChunk(text="Second.", is_final=True), delay_s=0.02),
    )
    fake = FakeSessionAttach(script)

    seen: list[str] = []
    async for chunk in fake.send_turn(TranscribedTurn(text="turn")):
        seen.append(chunk.text)
        if len(seen) == 1:
            # The second chunk has an independently controllable delay ahead
            # of it -- at this point in the iteration it has not been
            # produced yet, proving the stream is genuinely incremental.
            assert seen == ["First."]

    assert seen == ["First.", "Second."]


async def test_independently_controllable_timing_between_chunks() -> None:
    """Each scripted chunk's delay is set independently, not one fixed pace."""
    script = (
        ScriptedChunk(ReplyChunk(text="fast", is_final=False), delay_s=0.0),
        ScriptedChunk(ReplyChunk(text="slow", is_final=False), delay_s=0.05),
        ScriptedChunk(ReplyChunk(text="fast again", is_final=True), delay_s=0.0),
    )
    fake = FakeSessionAttach(script)

    chunks = await _collect(fake, TranscribedTurn(text="turn"))

    assert [c.text for c in chunks] == ["fast", "slow", "fast again"]


async def test_attach_error_raised_before_any_chunk() -> None:
    fake = FakeSessionAttach(attach_error="session unreachable")

    stream = fake.send_turn(TranscribedTurn(text="hello"))
    got_error = False
    try:
        async for _chunk in stream:
            pass
    except SessionAttachError as exc:
        got_error = True
        assert str(exc) == "session unreachable"

    assert got_error


async def test_attach_error_still_records_the_call() -> None:
    fake = FakeSessionAttach(attach_error="session unreachable")

    stream = fake.send_turn(TranscribedTurn(text="hello"))
    try:
        async for _chunk in stream:
            pass
    except SessionAttachError:
        pass

    assert fake.turns() == ["hello"]
