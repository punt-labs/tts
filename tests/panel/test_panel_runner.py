"""Tests for :mod:`punt_vox.panel.panel_runner`.

The work the leg starts and never waits on, so each piece is driven here
directly: the warm-up behind a handshake, a click, and a control change --
and, for each, what it does when voxd refuses, when luxd is away, and when
the change cannot be saved.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, cast

import pytest
from punt_lux import HubUnavailableError

from panel.doubles import (
    PANEL_LOGGER,
    Failure,
    FakeRest,
    FakeService,
    panel_records,
)
from punt_vox.panel.topics import PanelTopic

if TYPE_CHECKING:
    from collections.abc import Callable

    from punt_lux import LuxClient

    from punt_vox.panel.panel_runner import PanelRunner


def _luxd_is_down() -> LuxClient:
    raise HubUnavailableError("down")


class TestWarmed:
    async def test_a_refusal_notices_without_opening_the_panel(
        self, build_runner: Callable[..., PanelRunner]
    ) -> None:
        # The third read voxd can refuse, after the click's and the preview's:
        # it must reach the user like those two, but as a held notice only --
        # a push here would open a panel nobody clicked for, showing the
        # pre-read defaults as if they were the session's real settings.
        rest = FakeRest()
        service = FakeService(raise_on="prefetch")
        await build_runner(service, lambda: rest).warmed()  # must not raise
        assert service.rejections == [service.refusal]
        assert service.pushed == 0
        assert rest.rendered_count == 0

    async def test_a_refusal_is_logged_at_error_with_a_traceback(
        self,
        caplog: pytest.LogCaptureFixture,
        build_runner: Callable[..., PanelRunner],
    ) -> None:
        caplog.set_level(logging.DEBUG, logger=PANEL_LOGGER.name)
        service = FakeService(raise_on="prefetch")
        await build_runner(service, FakeRest).warmed()
        assert [r.levelno for r in panel_records(caplog)] == [logging.ERROR]
        assert panel_records(caplog)[0].exc_info is not None

    async def test_a_refusal_is_logged_as_the_refusal_it_is(
        self,
        caplog: pytest.LogCaptureFixture,
        build_runner: Callable[..., PanelRunner],
    ) -> None:
        # Unguarded this escapes into the hub client's own on_connect
        # isolation, which logs every failure alike as "on_connect callback
        # failed" -- true, and no help at all in naming what refused.
        caplog.set_level(logging.DEBUG, logger=PANEL_LOGGER.name)
        service = FakeService(raise_on="prefetch")
        await build_runner(service, FakeRest).warmed()
        assert [r.getMessage() for r in panel_records(caplog)] == [
            "vox-panel: voxd refused the settings read on connect"
        ]

    async def test_an_unexpected_warm_up_failure_is_logged_at_error(
        self,
        caplog: pytest.LogCaptureFixture,
        build_runner: Callable[..., PanelRunner],
    ) -> None:
        # The guard around the read answers voxd's refusal and nothing else, so
        # a bug wearing any other exception reaches the warm-up's own boundary
        # -- the last one there is, since the leg starts this and never awaits
        # it. No notice either: nothing here is voxd's answer to report.
        caplog.set_level(logging.DEBUG, logger=PANEL_LOGGER.name)
        service = FakeService(raise_on="unexpected")
        await build_runner(service, FakeRest).warmed()  # must not raise
        assert [r.levelno for r in panel_records(caplog)] == [logging.ERROR]
        assert panel_records(caplog)[0].exc_info is not None
        assert service.rejections == []


class TestClicked:
    async def test_a_click_acknowledges_then_serves(
        self, build_runner: Callable[..., PanelRunner]
    ) -> None:
        rest = FakeRest()
        service = FakeService()
        await build_runner(service, lambda: rest).clicked()
        assert service.acknowledged == 1
        assert service.serviced == 1

    async def test_a_click_acknowledges_before_it_services(
        self, build_runner: Callable[..., PanelRunner]
    ) -> None:
        # The click's two halves have opposite intents. acknowledge answers the
        # gesture -- the user asked to see the panel, so it shows and the frame
        # comes forward. service lands milliseconds later onto the window that
        # just came forward, so it refreshes rather than raising it again. That
        # ordering is what the runner itself guarantees -- the call log is the
        # only witness to it: acknowledged/serviced counts, or installed/pushed
        # counts, would read the same at (1, 1) whichever order the two ran in.
        rest = FakeRest()
        service = FakeService()
        await build_runner(service, lambda: rest).clicked()
        assert service.calls == ["acknowledge", "service"]

    async def test_an_unexpected_click_failure_is_logged_at_error(
        self,
        caplog: pytest.LogCaptureFixture,
        build_runner: Callable[..., PanelRunner],
    ) -> None:
        caplog.set_level(logging.DEBUG, logger=PANEL_LOGGER.name)

        def _raise() -> LuxClient:
            msg = "boom"
            raise RuntimeError(msg)

        await build_runner(FakeService(), _raise).clicked()  # must not raise
        assert [r.levelno for r in panel_records(caplog)] == [logging.ERROR]


class TestChanged:
    async def test_a_changed_event_re_pushes_the_scene(
        self, build_runner: Callable[..., PanelRunner]
    ) -> None:
        rest = FakeRest()
        service = FakeService()
        await build_runner(service, lambda: rest).changed(
            PanelTopic.NOTIFY.value, {"value": 1}
        )
        assert service.applied == [(PanelTopic.NOTIFY.value, {"value": 1})]
        assert service.pushed == 1
        # The user is mid-interaction with a panel already on screen, so the
        # re-push must not raise its frame.
        assert service.installed == 0

    async def test_an_unchanged_event_does_not_re_push(
        self, build_runner: Callable[..., PanelRunner]
    ) -> None:
        rest = FakeRest()
        service = FakeService()
        service.apply_returns = False
        await build_runner(service, lambda: rest).changed(
            PanelTopic.VOICE_PREVIEW.value, {}
        )
        assert service.pushed == 0

    async def test_a_rejected_event_never_raises_out_of_the_runner(
        self, build_runner: Callable[..., PanelRunner]
    ) -> None:
        service = FakeService(raise_on="apply")
        await build_runner(service, FakeRest).changed(PanelTopic.NOTIFY.value, {})

    async def test_luxd_down_on_the_re_push_is_swallowed(
        self, build_runner: Callable[..., PanelRunner]
    ) -> None:
        service = FakeService()
        runner = build_runner(service, _luxd_is_down)
        await runner.changed(PanelTopic.NOTIFY.value, {"value": 0})  # must not raise

    async def test_a_write_failure_is_caught_distinctly_and_corrects_the_scene(
        self, build_runner: Callable[..., PanelRunner]
    ) -> None:
        # The widget already shows the change optimistically; a diff-based
        # re-push (``pushed``) would see the last-successful render as still
        # true and skip it, so the correction must go through the full-reinstall
        # path (``corrected``) instead.
        rest = FakeRest()
        service = FakeService(raise_on="write")
        await build_runner(service, lambda: rest).changed(
            PanelTopic.NOTIFY.value, {"value": 0}
        )  # must not raise
        assert service.recovered == ["notify"]
        assert service.corrected == 1
        assert service.pushed == 0

    async def test_a_refused_value_is_corrected_like_a_write_that_would_not_land(
        self, build_runner: Callable[..., PanelRunner]
    ) -> None:
        # A real selection the config store will not serialize -- a voice whose
        # name carries a quote, say. The payload was fine and the user really
        # chose something, so this owes the same on-screen correction a failed
        # write does. It is a ValueError subclass, so catching it after the
        # bare (TypeError, ValueError) bucket would file it as a bad payload
        # and revert the widget with nothing on screen saying why.
        rest = FakeRest()
        service = FakeService(raise_on="refused")
        await build_runner(service, lambda: rest).changed(
            PanelTopic.VOICE.value, {"value": 0}
        )  # must not raise
        assert service.recovered == ["voice"]
        assert service.corrected == 1
        assert service.pushed == 0

    async def test_a_refused_value_is_logged_at_error_with_a_traceback(
        self,
        caplog: pytest.LogCaptureFixture,
        build_runner: Callable[..., PanelRunner],
    ) -> None:
        caplog.set_level(logging.DEBUG, logger=PANEL_LOGGER.name)
        service = FakeService(raise_on="refused")
        await build_runner(service, FakeRest).changed(
            PanelTopic.VOICE.value, {"value": 0}
        )
        assert [r.levelno for r in panel_records(caplog)] == [logging.ERROR]
        assert panel_records(caplog)[0].exc_info is not None
        assert "voice" in panel_records(caplog)[0].getMessage()

    async def test_a_rejected_payload_corrects_but_does_not_recover(
        self, build_runner: Callable[..., PanelRunner]
    ) -> None:
        # The malformed-event path sets no notice at all, so a diff-based
        # re-push (``pushed``) would find the corrective render byte-identical
        # to the last one this session landed and push nothing -- the widget's
        # wrongly-set field would never get reasserted on the wire.
        rest = FakeRest()
        service = FakeService(raise_on="apply")
        await build_runner(service, lambda: rest).changed(PanelTopic.NOTIFY.value, {})
        assert service.recovered == []
        assert service.corrected == 1
        assert service.pushed == 0

    async def test_an_unexpected_change_failure_is_logged_at_error(
        self,
        caplog: pytest.LogCaptureFixture,
        build_runner: Callable[..., PanelRunner],
    ) -> None:
        # Every failure the apply names is answered below it -- a rejected
        # payload, a write that would not land, a refusal from voxd -- and each
        # of those re-pushes. A bug names none of them, so it passes every
        # handler and stops only at the change's own boundary, with no scene
        # correction to make because nothing was decided about the change.
        caplog.set_level(logging.DEBUG, logger=PANEL_LOGGER.name)
        service = FakeService(raise_on="unexpected")
        await build_runner(service, FakeRest).changed(
            PanelTopic.NOTIFY.value, {"value": 0}
        )  # must not raise
        assert [r.levelno for r in panel_records(caplog)] == [logging.ERROR]
        assert panel_records(caplog)[0].exc_info is not None
        assert service.pushed == 0
        assert service.recovered == []


class TestVoxdRejection:
    """voxd answering with a refusal reaches the user, never just the log.

    An unreachable voxd is a transient the next tick retries away; a refusal
    is a real failure -- so every call path that can meet one turns it into a
    notice instead of letting the blanket ``except Exception`` reduce it to a
    log line nobody reads.
    """

    async def test_a_refused_preview_shows_a_notice_and_corrects_the_scene(
        self, build_runner: Callable[..., PanelRunner]
    ) -> None:
        # The preview button already shows its optimistic state client-side, so
        # this refusal must snap it back with a full reinstall (``corrected``),
        # not the diff-based re-push (``pushed``) that would find nothing to
        # patch and leave the widget's guess on screen.
        rest = FakeRest()
        service = FakeService(raise_on="preview")
        await build_runner(service, lambda: rest).changed(
            PanelTopic.VOICE_PREVIEW.value, {}
        )  # must not raise
        assert service.rejections == [service.refusal]
        assert service.corrected == 1
        assert service.pushed == 0

    async def test_a_refused_preview_is_logged_at_error_with_a_traceback(
        self,
        caplog: pytest.LogCaptureFixture,
        build_runner: Callable[..., PanelRunner],
    ) -> None:
        caplog.set_level(logging.DEBUG, logger=PANEL_LOGGER.name)
        service = FakeService(raise_on="preview")
        await build_runner(service, FakeRest).changed(
            PanelTopic.VOICE_PREVIEW.value, {}
        )
        assert [r.levelno for r in panel_records(caplog)] == [logging.ERROR]
        assert panel_records(caplog)[0].exc_info is not None

    async def test_a_refused_click_refresh_shows_a_notice_and_re_pushes(
        self, build_runner: Callable[..., PanelRunner]
    ) -> None:
        # The click's own settings read is the second path a refusal reaches:
        # acknowledge() already put the stale scene up, so the notice only
        # becomes visible if the runner pushes again after catching this.
        rest = FakeRest()
        service = FakeService(raise_on="service")
        await build_runner(service, lambda: rest).clicked()  # must not raise
        assert service.rejections == [service.refusal]
        assert service.pushed == 1

    async def test_a_refused_click_refresh_is_logged_at_error(
        self,
        caplog: pytest.LogCaptureFixture,
        build_runner: Callable[..., PanelRunner],
    ) -> None:
        caplog.set_level(logging.DEBUG, logger=PANEL_LOGGER.name)
        service = FakeService(raise_on="service")
        await build_runner(service, FakeRest).clicked()
        # Only the panel's own records: the click also reports its latency, on
        # punt_lux's logger, and that line is not what this test is about.
        assert [r.levelno for r in panel_records(caplog)] == [logging.ERROR]
        assert panel_records(caplog)[0].exc_info is not None


class TestOutageLogging:
    async def test_a_click_while_luxd_is_down_escalates_like_every_other_path(
        self,
        caplog: pytest.LogCaptureFixture,
        build_runner: Callable[..., PanelRunner],
    ) -> None:
        # A click arriving mid-outage is the retry loop's business, not an
        # ERROR traceback per click: the throttled second tick proves it went
        # through HubOutageLog rather than the blanket handler.
        caplog.set_level(logging.DEBUG, logger=PANEL_LOGGER.name)
        runner = build_runner(FakeService(), _luxd_is_down)
        await runner.clicked()
        await runner.clicked()
        assert [r.levelno for r in panel_records(caplog)] == [
            logging.WARNING,
            logging.DEBUG,
        ]


class TestStartedWork:
    """Nothing awaits this work, so no failure may escape any of it."""

    @pytest.mark.parametrize(
        "failure",
        [
            "",
            "prefetch",
            "service",
            "apply",
            "refused",
            "write",
            "preview",
            "unexpected",
        ],
        ids=str,
    )
    async def test_no_failure_escapes_the_work_the_leg_started(
        self, failure: str, build_runner: Callable[..., PanelRunner]
    ) -> None:
        # Whatever a piece of work meets, it ends inside itself: the leg starts
        # these and holds the task, so an escape would surface only as an
        # unretrieved exception at collection time, in nobody's log.
        runner = build_runner(FakeService(raise_on=cast("Failure", failure)), FakeRest)
        await runner.warmed()
        await runner.clicked()
        await runner.changed(PanelTopic.NOTIFY.value, {"value": 0})
