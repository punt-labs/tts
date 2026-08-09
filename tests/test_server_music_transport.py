"""Tests for the transport verbs (server_music_transport.TransportVerbs).

Each verb is driven directly with the in-memory ``FakeProgramGateway`` -- no
daemon, no socket -- so the gateway call each verb makes, the phrase it reports,
the fault envelope, and the per-call gateway lookup are asserted without a wire.

The verbs are parametrised through an explicit call table rather than
``getattr``-by-name, matching the dispatch discipline of the code under test.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, cast, final

import pytest
from _program_fakes import FakeProgramGateway

from punt_vox.client_errors import VoxdConnectionError
from punt_vox.music_phrases import MusicMarquee
from punt_vox.server_music_transport import TransportVerbs

if TYPE_CHECKING:
    from collections.abc import Callable

    from punt_vox.program_gateway import ProgramGateway
    from punt_vox.types_programs.control import CommandOutcome


@final
class _BrokenGateway:
    """A :class:`ProgramGateway` whose transport commands all fail."""

    __slots__ = ()

    def advance(self) -> CommandOutcome:
        raise VoxdConnectionError(self._msg())

    def prev(self) -> CommandOutcome:
        raise VoxdConnectionError(self._msg())

    def pause(self) -> CommandOutcome:
        raise VoxdConnectionError(self._msg())

    def resume(self) -> CommandOutcome:
        raise VoxdConnectionError(self._msg())

    def _msg(self) -> str:
        return "voxd is not running"


def _verbs(gateway: FakeProgramGateway | _BrokenGateway) -> TransportVerbs:
    """Build the verbs over *gateway*; the fakes serve the four commands used."""
    return TransportVerbs(lambda: cast("ProgramGateway", gateway), MusicMarquee())


type _VerbCall = Callable[[TransportVerbs], str]

# The four transport verbs, named for the daemon command each must drive.
VERB_CALLS: tuple[tuple[str, _VerbCall], ...] = (
    ("advance", lambda verbs: verbs.advance()),
    ("prev", lambda verbs: verbs.prev()),
    ("pause", lambda verbs: verbs.pause()),
    ("resume", lambda verbs: verbs.resume()),
)


class TestGatewayRouting:
    """Each verb drives its own daemon command, never another's."""

    @pytest.mark.parametrize(("command", "call"), VERB_CALLS)
    def test_verb_calls_its_own_command(self, command: str, call: _VerbCall) -> None:
        gateway = FakeProgramGateway()
        call(_verbs(gateway))
        assert [recorded.verb for recorded in gateway.calls] == [command]


class TestRendering:
    """Every verb answers with the applied flag and a marquee-marked message."""

    @pytest.mark.parametrize(("command", "call"), VERB_CALLS)
    def test_reports_the_applied_flag(self, command: str, call: _VerbCall) -> None:
        assert json.loads(call(_verbs(FakeProgramGateway())))["applied"] is True

    @pytest.mark.parametrize(("command", "call"), VERB_CALLS)
    def test_message_is_marquee_marked(self, command: str, call: _VerbCall) -> None:
        payload = json.loads(call(_verbs(FakeProgramGateway())))
        assert payload["message"].startswith("♪ ")

    @pytest.mark.parametrize(
        ("call", "phrase"),
        [
            (VERB_CALLS[1][1], "Previous part."),
            (VERB_CALLS[2][1], "Paused."),
            (VERB_CALLS[3][1], "Resumed."),
        ],
        ids=["prev", "pause", "resume"],
    )
    def test_fixed_phrase_verbs_announce_themselves(
        self, call: _VerbCall, phrase: str
    ) -> None:
        """The three fixed-phrase verbs name the transport move they made."""
        payload = json.loads(call(_verbs(FakeProgramGateway())))
        assert phrase in payload["message"]


class TestFaults:
    """A daemon fault reaches the caller as the shared error envelope."""

    @pytest.mark.parametrize(("command", "call"), VERB_CALLS)
    def test_fault_becomes_the_error_envelope(
        self, command: str, call: _VerbCall
    ) -> None:
        assert json.loads(call(_verbs(_BrokenGateway()))) == {
            "error": "voxd is not running"
        }

    @pytest.mark.parametrize(("command", "call"), VERB_CALLS)
    def test_fault_carries_no_applied_flag(self, command: str, call: _VerbCall) -> None:
        assert "applied" not in json.loads(call(_verbs(_BrokenGateway())))


class TestGatewayLifetime:
    """The gateway is resolved per call, never pinned at construction."""

    def test_each_verb_call_asks_the_factory_again(self) -> None:
        served: list[FakeProgramGateway] = []

        def factory() -> ProgramGateway:
            served.append(gateway := FakeProgramGateway())
            return gateway

        verbs = TransportVerbs(factory, MusicMarquee())
        verbs.pause()
        verbs.resume()
        assert len(served) == 2
