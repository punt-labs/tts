"""Tests for the ``program_select`` wire handler -- id vs. tag-query routing."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Self, cast, final

from punt_vox.voxd.programs.select_handler import SelectHandler

from .conftest import make_service, seed_album

if TYPE_CHECKING:
    from pathlib import Path

    import pytest
    from starlette.websockets import WebSocket

    from punt_vox.voxd.programs.service import ProgramService


@final
class FakeWebSocket:
    """Record every ``send_json`` payload the handler emits."""

    __slots__ = ("sent",)
    sent: list[dict[str, object]]

    def __new__(cls) -> Self:
        self = super().__new__(cls)
        self.sent = []
        return self

    async def send_json(self, payload: dict[str, object]) -> None:
        self.sent.append(payload)


async def _reply(service: ProgramService, msg: dict[str, object]) -> dict[str, object]:
    ws = FakeWebSocket()
    await SelectHandler(service)(msg, cast("WebSocket", ws))
    return ws.sent[0]


class TestSelectHandler:
    async def test_by_tags_resolves_and_plays(self, tmp_path: Path) -> None:
        seed_album(tmp_path / "programs", 1, 2, style="trance", vibe="calm")
        service = make_service(tmp_path / "programs")
        reply = await _reply(service, {"id": "1", "style": "trance", "vibe": "calm"})
        assert reply == {"type": "program_select", "id": "1"}
        await service.run_once()
        assert service.status().now_playing is not None

    async def test_by_id_is_a_direct_lookup(self, tmp_path: Path) -> None:
        seed_album(
            tmp_path / "programs", 1, style="trance", vibe="calm", album_id="a3f1c9"
        )
        service = make_service(tmp_path / "programs")
        reply = await _reply(service, {"id": "2", "album_id": "a3f1c9"})
        # A present 'album_id' routes to replay_album (direct lookup, not a query).
        assert reply["type"] == "program_select"
        await service.run_once()
        assert service.status().now_playing is not None

    async def test_by_name_positional_resolves_the_radio(self, tmp_path: Path) -> None:
        # D-3: the bare positional is id-or-name. A saved NAME resolves the radio,
        # never a hex-validation error -- the shipped ``music play <name>`` form.
        seed_album(
            tmp_path / "programs",
            1,
            2,
            style="trance",
            vibe="calm",
            name="focus-beats",
            album_id="a3f1c9",
        )
        service = make_service(tmp_path / "programs")
        reply = await _reply(service, {"id": "3", "album_id": "focus-beats"})
        assert reply == {"type": "program_select", "id": "3"}
        await service.run_once()
        assert service.status().now_playing is not None

    async def test_nonhex_positional_is_not_a_hex_error(self, tmp_path: Path) -> None:
        # A non-hex positional naming no album falls through to the name radio and
        # reports the tag-miss -- never "must be lowercase hex" (the regression).
        service = make_service(tmp_path / "programs")
        reply = await _reply(service, {"id": "req", "album_id": "focus-beats"})
        assert reply["type"] == "error"
        assert "no albums match" in str(reply["message"])
        assert "hex" not in str(reply["message"])

    async def test_unknown_hex_positional_falls_through_to_name(
        self, tmp_path: Path
    ) -> None:
        # A well-formed id naming no catalogued album is not a "known id"; it falls
        # through to the name axis and reports the tag-miss, not an id-lookup error.
        service = make_service(tmp_path / "programs")
        reply = await _reply(service, {"id": "req", "album_id": "badbad"})
        assert reply["type"] == "error"
        assert "no albums match" in str(reply["message"])

    async def test_no_match_is_a_boundary_error(self, tmp_path: Path) -> None:
        service = make_service(tmp_path / "programs")
        reply = await _reply(service, {"id": "req", "style": "ghost"})
        assert reply["type"] == "error"
        assert "no albums match" in str(reply["message"])

    async def test_blank_album_id_is_rejected_not_union_radio(
        self, tmp_path: Path
    ) -> None:
        # A present-but-blank album_id ("") is malformed input, not a name. It must
        # be rejected -- NOT collapse into a blank-name query that resolves nothing
        # and unions the whole catalog into an accidental play-everything radio.
        seed_album(tmp_path / "programs", 1, style="trance", vibe="calm")
        seed_album(
            tmp_path / "programs", 1, style="ambient", vibe="dark", album_id="bbbbbb"
        )
        service = make_service(tmp_path / "programs")
        reply = await _reply(service, {"id": "b", "album_id": ""})
        # Rejected before any replay -- an error frame, NOT a program_select ack.
        # A blank id never reaches service.replay, so no union selection is posted.
        assert reply["type"] == "error"
        assert "album_id must not be blank" in str(reply["message"])

    async def test_bare_play_without_history_errors_not_union_radio(
        self, tmp_path: Path
    ) -> None:
        # Absence of album_id AND every tag selector is the bare-``play`` path: it
        # repeats the last-played album, never a play-everything union radio. With
        # no album played yet it is rejected with the helpful message -- so a bare
        # play cannot silently start an arbitrary album over a fresh daemon.
        seed_album(tmp_path / "programs", 1, style="trance", vibe="calm")
        seed_album(
            tmp_path / "programs", 1, style="ambient", vibe="dark", album_id="bbbbbb"
        )
        service = make_service(tmp_path / "programs")
        reply = await _reply(service, {"id": "u"})
        assert reply["type"] == "error"
        assert "no album played yet" in str(reply["message"])

    async def test_bare_play_repeats_the_last_played_album(
        self, tmp_path: Path
    ) -> None:
        # Once an album has been played by id, a bare ``play`` (no album_id, no
        # tags) repeats that album -- the last-played register drives it.
        seed_album(
            tmp_path / "programs", 1, style="trance", vibe="calm", album_id="a3f1c9"
        )
        service = make_service(tmp_path / "programs")
        await _reply(service, {"id": "1", "album_id": "a3f1c9"})
        reply = await _reply(service, {"id": "2"})
        assert reply == {"type": "program_select", "id": "2"}
        await service.run_once()
        assert service.status().now_playing is not None

    async def test_service_os_error_is_an_operational_fault(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        # The ProgramCommandHandler boundary now splits fault-vs-error via
        # WireReply.reject_or_fault: an OSError from the service is a server-side
        # operational failure audited as a fault (ERROR "operation failed"), never
        # a WARNING "rejected op" -- and it still replies a clean error frame.
        seed_album(tmp_path / "programs", 1, style="trance", vibe="calm")
        service = make_service(tmp_path / "programs")

        def disk_fault(_self: ProgramService, _query: object) -> None:
            raise OSError("selection store fault")

        monkeypatch.setattr(type(service), "replay", disk_fault)
        ws = FakeWebSocket()
        with caplog.at_level(logging.WARNING, logger="punt_vox.voxd.wire_reply"):
            await SelectHandler(service)(
                {"id": "x", "style": "trance"}, cast("WebSocket", ws)
            )
        assert ws.sent[-1]["type"] == "error"
        assert any(
            r.levelno == logging.ERROR and "operation failed" in r.getMessage()
            for r in caplog.records
        )
        assert not any("rejected op" in r.getMessage() for r in caplog.records)
