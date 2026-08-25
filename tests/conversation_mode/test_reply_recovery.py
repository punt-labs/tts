"""Tests for :class:`~.reply_recovery.ReplyRecovery`."""

from __future__ import annotations

from punt_vox.voxd.conversation_mode.call_actor import CallActor
from punt_vox.voxd.conversation_mode.mode import Mode
from punt_vox.voxd.conversation_mode.reply_recovery import ReplyRecovery
from punt_vox.voxd.conversation_mode.session_attach import (
    BareAuthMissingError,
    SessionAttachError,
)
from punt_vox.voxd.conversation_mode.start_call import StartCall
from punt_vox.voxd.conversation_mode.turn_detected import TurnDetected


class _Recorder:
    def __init__(self) -> None:
        self.said: list[str] = []

    async def __call__(self, text: str) -> None:
        self.said.append(text)


def _actor_waiting() -> CallActor:
    """Return a :class:`CallActor` already in ``waiting`` -- the mode every
    real mid-turn failure is raised from (see :meth:`ReplyRecovery.recover`'s
    own docstring for why)."""
    actor = CallActor()
    actor.apply(StartCall())
    actor.apply(TurnDetected())
    return actor


async def test_a_transient_session_attach_error_recovers_to_listening() -> None:
    speak = _Recorder()
    actor = _actor_waiting()
    recovery = ReplyRecovery(actor, speak)

    await recovery.recover(SessionAttachError("claude exited 1"))

    assert actor.mode is Mode.LISTENING
    assert any("wrong" in phrase.lower() for phrase in speak.said)


async def test_a_bare_auth_missing_error_ends_the_call() -> None:
    speak = _Recorder()
    actor = _actor_waiting()
    recovery = ReplyRecovery(actor, speak)

    await recovery.recover(BareAuthMissingError.for_missing_key())

    assert actor.mode is Mode.IDLE
    assert any("can't continue" in phrase.lower() for phrase in speak.said)


async def test_the_bare_auth_message_never_leaks_the_raw_exception_text() -> None:
    """A fixed sentence, never {exc} itself -- BareAuthMissingError's own
    message names an env var, which is fine to say, but the spoken text must
    be this class's own fixed sentence, not whatever the exception carries.
    """
    speak = _Recorder()
    actor = _actor_waiting()
    recovery = ReplyRecovery(actor, speak)
    exc = BareAuthMissingError.for_missing_key()

    await recovery.recover(exc)

    assert str(exc) not in speak.said
