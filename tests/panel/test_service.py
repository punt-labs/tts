"""Tests for :mod:`punt_vox.panel.service`."""

from __future__ import annotations

import threading
from typing import TYPE_CHECKING, final

import pytest
from punt_lux import OpError

from punt_vox.client_errors import VoxdConnectionError, VoxdProtocolError
from punt_vox.panel.panel_notice import PanelNotice
from punt_vox.panel.service import VoxPanelService
from punt_vox.panel.state import PanelState
from punt_vox.panel.topics import PanelTopic
from punt_vox.types_errors import ConfigValueError
from punt_vox.types_synthesis import SynthesisSpec

if TYPE_CHECKING:
    from punt_lux import RenderRequest, SceneShown
    from punt_lux.applets import ClickLatency
    from punt_lux.hub_client import CallbackHandler, ConnectHandler, EventHandler
    from punt_lux.operations import Ok

    from punt_vox.client import SynthesizeResult
    from punt_vox.config import VoxConfig
    from punt_vox.panel.ports import HubListener


@final
class _FakeDaemonClient:
    """A ``PanelDaemonClient`` double: canned voices, and a raise-toggle synth."""

    def __init__(
        self, voices: list[str] | None = None, *, raise_on_synth: bool = False
    ) -> None:
        self._voices = voices if voices is not None else ["aria", "roger"]
        self._raise_on_synth = raise_on_synth
        self.synth_calls: list[tuple[str, SynthesisSpec | None]] = []
        self.roster_reads = 0

    def voices(self) -> list[str]:
        self.roster_reads += 1
        return self._voices

    def synthesize(
        self, text: str, spec: SynthesisSpec | None = None, *, once: int | None = None
    ) -> SynthesizeResult:
        if self._raise_on_synth:
            msg = "voxd unreachable"
            raise VoxdConnectionError(msg)
        self.synth_calls.append((text, spec))
        return {}  # type: ignore[return-value]


@final
class _FakeStore:
    """A ``SettingsStore`` double: an in-memory dict of written fields."""

    def __init__(self, cfg: VoxConfig) -> None:
        self._cfg = cfg
        self.written: dict[str, str] = {}

    def read(self) -> VoxConfig:
        return self._cfg

    def write_field(self, key: str, value: str) -> None:
        self.written[key] = value


@final
class _FakeRest:
    """A ``PanelRestClient`` double that only needs ``render`` for these tests."""

    def __init__(self, *, refuse: bool = False) -> None:
        self._refuse = refuse
        self.rendered: list[RenderRequest] = []

    def render(self, request: RenderRequest) -> SceneShown | OpError:
        if self._refuse:
            return OpError(code="rejected", reason="no display")
        self.rendered.append(request)
        return None  # type: ignore[return-value]

    def register_callback(self, callback_id: str, label: str) -> Ok | OpError:
        raise NotImplementedError

    def listener(
        self,
        *,
        on_callback: CallbackHandler,
        on_event: EventHandler,
        on_connect: ConnectHandler | None = None,
    ) -> HubListener:
        raise NotImplementedError


def _config(
    voice: str | None = "roger",
    provider: str | None = None,
    model: str | None = None,
) -> VoxConfig:
    from punt_vox.config import VoxConfig

    return VoxConfig(
        notify="y",
        speak="y",
        vibe_mode="auto",
        voice=voice,
        provider=provider,
        model=model,
        vibe=None,
        vibe_tags=None,
    )


class TestPrefetch:
    def test_reads_settings_into_held_state(self) -> None:
        service = VoxPanelService(_FakeDaemonClient(), _FakeStore(_config()))
        service.prefetch()
        assert service.scene().voice == "roger"

    def test_daemon_unavailable_keeps_the_empty_default(self) -> None:
        class _BrokenClient:
            def voices(self) -> list[str]:
                msg = "voxd unreachable"
                raise VoxdConnectionError(msg)

            def synthesize(self, *args: object, **kwargs: object) -> object:
                raise NotImplementedError

        service = VoxPanelService(_BrokenClient(), _FakeStore(_config()))  # type: ignore[arg-type]
        service.prefetch()
        assert service.scene().voice == PanelState.empty().voice

    def test_daemon_unavailable_sets_the_notice(self) -> None:
        class _BrokenClient:
            def voices(self) -> list[str]:
                msg = "voxd unreachable"
                raise VoxdConnectionError(msg)

            def synthesize(self, *args: object, **kwargs: object) -> object:
                raise NotImplementedError

        service = VoxPanelService(_BrokenClient(), _FakeStore(_config()))  # type: ignore[arg-type]
        service.prefetch()
        assert service.scene().notice == PanelNotice.voxd_unavailable()

    def test_a_real_bug_in_the_roster_read_propagates(self) -> None:
        """Only the voxd-unreachable transient is swallowed -- a protocol bug is not."""

        class _MisbehavingClient:
            def voices(self) -> list[str]:
                msg = "unexpected reply"
                raise VoxdProtocolError(msg)

            def synthesize(self, *args: object, **kwargs: object) -> object:
                raise NotImplementedError

        service = VoxPanelService(_MisbehavingClient(), _FakeStore(_config()))  # type: ignore[arg-type]
        with pytest.raises(VoxdProtocolError):
            service.prefetch()


class TestApplyEvent:
    def test_notify_writes_the_code_and_updates_state(self) -> None:
        service = VoxPanelService(_FakeDaemonClient(), (store := _FakeStore(_config())))
        changed = service.apply_event(PanelTopic.NOTIFY, {"value": 2})
        assert changed is True
        assert store.written["notify"] == "c"
        assert service.scene().notify == "c"

    def test_mic_mode_writes_the_code_and_updates_state(self) -> None:
        service = VoxPanelService(_FakeDaemonClient(), (store := _FakeStore(_config())))
        service.apply_event(PanelTopic.MIC_MODE, {"value": 0})
        assert store.written["speak"] == "n"
        assert service.scene().speak == "n"

    def test_voice_writes_the_name_and_updates_state(self) -> None:
        service = VoxPanelService(_FakeDaemonClient(), (store := _FakeStore(_config())))
        service.prefetch()
        service.apply_event(PanelTopic.VOICE, {"value": 0})
        assert store.written["voice"] == "aria"
        assert service.scene().voice == "aria"

    def test_voice_preview_does_not_write_and_reports_no_repush(self) -> None:
        client = _FakeDaemonClient()
        service = VoxPanelService(client, _FakeStore(_config()))
        service.prefetch()
        changed = service.apply_event(PanelTopic.VOICE_PREVIEW, {})
        assert changed is False
        expected = ("This is my voice.", SynthesisSpec(voice="roger"))
        assert client.synth_calls == [expected]

    def test_voice_preview_with_no_voice_selected_is_a_silent_no_op(self) -> None:
        client = _FakeDaemonClient()
        service = VoxPanelService(client, _FakeStore(_config(voice=None)))
        service.prefetch()
        service.apply_event(PanelTopic.VOICE_PREVIEW, {})
        assert client.synth_calls == []

    def test_voice_preview_survives_an_unavailable_voxd(self) -> None:
        client = _FakeDaemonClient(raise_on_synth=True)
        service = VoxPanelService(client, _FakeStore(_config()))
        service.prefetch()
        # Must not raise -- a preview failure is logged, never propagated.
        changed = service.apply_event(PanelTopic.VOICE_PREVIEW, {})
        assert changed is True
        assert service.scene().notice == PanelNotice.voxd_unavailable()

    def test_provider_writes_the_name_and_clears_the_stale_model(self) -> None:
        service = VoxPanelService(
            _FakeDaemonClient(),
            (store := _FakeStore(_config(provider="elevenlabs", model="eleven_v3"))),
        )
        service.prefetch()
        service.apply_event(PanelTopic.PROVIDER, {"value": 1})  # openai
        assert store.written["provider"] == "openai"
        # Stale model must be cleared in the same commit -- an eleven_v3
        # left behind after a switch to openai would drive an invalid call.
        assert store.written["model"] == ""
        scene = service.scene()
        assert scene.provider == "openai"
        assert scene.model is None

    def test_model_writes_the_name_and_updates_state(self) -> None:
        service = VoxPanelService(
            _FakeDaemonClient(),
            (store := _FakeStore(_config(provider="openai"))),
        )
        service.prefetch()
        service.apply_event(PanelTopic.MODEL, {"value": 1})  # tts-1-hd
        assert store.written["model"] == "tts-1-hd"
        assert service.scene().model == "tts-1-hd"

    def test_voice_preview_lets_a_real_bug_propagate(self) -> None:
        """Only the voxd-unreachable transient is swallowed -- a protocol bug is not."""

        class _MisbehavingClient:
            def voices(self) -> list[str]:
                return ["roger"]

            def synthesize(self, *args: object, **kwargs: object) -> object:
                msg = "unexpected reply"
                raise VoxdProtocolError(msg)

        service = VoxPanelService(_MisbehavingClient(), _FakeStore(_config()))  # type: ignore[arg-type]
        service.prefetch()
        with pytest.raises(VoxdProtocolError):
            service.apply_event(PanelTopic.VOICE_PREVIEW, {})

    def test_write_failure_propagates_to_the_caller(self) -> None:
        """A persist failure is never swallowed -- the leg decides how to recover."""

        class _FailingStore:
            def read(self) -> VoxConfig:
                return _config()

            def write_field(self, key: str, value: str) -> None:
                msg = "disk full"
                raise OSError(msg)

        service = VoxPanelService(_FakeDaemonClient(), _FailingStore())
        with pytest.raises(OSError, match="disk full"):
            service.apply_event(PanelTopic.NOTIFY, {"value": 2})

    def test_a_refused_value_propagates_as_its_own_type(self) -> None:
        """A value the store will not serialize is not a malformed payload.

        Both are ``ValueError``s, and the runner tells them apart only by the
        subclass -- so this must reach it wearing that type, not flattened
        into the bare ``ValueError`` a bad payload raises.
        """

        class _RefusingStore:
            def read(self) -> VoxConfig:
                return _config()

            def write_field(self, key: str, value: str) -> None:
                msg = "config values must not contain double-quotes"
                raise ConfigValueError(msg)

        service = VoxPanelService(_FakeDaemonClient(), _RefusingStore())
        with pytest.raises(ConfigValueError, match="double-quotes"):
            service.apply_event(PanelTopic.NOTIFY, {"value": 2})

    def test_unknown_topic_is_ignored(self) -> None:
        service = VoxPanelService(_FakeDaemonClient(), _FakeStore(_config()))
        assert service.apply_event("vox.unknown", {}) is False

    @pytest.mark.parametrize("payload", [{}, {"value": "not-an-int"}, {"value": True}])
    def test_missing_or_wrong_typed_value_raises(
        self, payload: dict[str, object]
    ) -> None:
        service = VoxPanelService(_FakeDaemonClient(), _FakeStore(_config()))
        with pytest.raises(TypeError, match="value"):
            service.apply_event(PanelTopic.NOTIFY, payload)


class TestPushScene:
    def test_pushes_the_held_scene(self) -> None:
        service = VoxPanelService(_FakeDaemonClient(), _FakeStore(_config()))
        rest = _FakeRest()
        service.push_scene(rest)
        assert len(rest.rendered) == 1
        assert rest.rendered[0].scene_id == "vox.panel"

    def test_luxd_refusal_is_logged_not_raised(self) -> None:
        service = VoxPanelService(_FakeDaemonClient(), _FakeStore(_config()))
        service.push_scene(_FakeRest(refuse=True))  # must not raise


class TestRefreshAndRecover:
    def test_refresh_clears_a_stale_notice(self) -> None:
        # synthesize fails (sets the notice) but voices() still works, so a
        # later refresh -- e.g. the next click -- can clear it.
        client = _FakeDaemonClient(raise_on_synth=True)
        service = VoxPanelService(client, _FakeStore(_config()))
        service.prefetch()
        service.apply_event(PanelTopic.VOICE_PREVIEW, {})
        assert service.scene().notice != PanelNotice.silent()

        service.refresh()
        assert service.scene().notice == PanelNotice.silent()

    def test_recover_from_write_failure_resyncs_and_flags_the_scene(self) -> None:
        store = _FakeStore(_config(voice="roger"))
        service = VoxPanelService(_FakeDaemonClient(), store)
        service.recover_from_write_failure("notify")
        scene = service.scene()
        assert scene.voice == "roger"  # re-read from the real source of truth
        assert scene.notice == PanelNotice.write_failed("notify")

    def test_recover_from_write_failure_when_resync_also_fails_composes_both(
        self,
    ) -> None:
        """The specific write-failure notice must not be discarded for a
        generic voxd-unavailable one when the confirming resync also fails --
        two unrelated subsystems failed, and the message must say both."""

        class _BrokenClient:
            def voices(self) -> list[str]:
                msg = "voxd unreachable"
                raise VoxdConnectionError(msg)

            def synthesize(self, *args: object, **kwargs: object) -> object:
                raise NotImplementedError

        service = VoxPanelService(_BrokenClient(), _FakeStore(_config()))  # type: ignore[arg-type]
        service.recover_from_write_failure("notify")
        notice = service.scene().notice
        assert notice == PanelNotice.write_failed_and_voxd_unavailable("notify")
        assert "notify" in notice.message
        assert "voxd" in notice.message


class TestNoteRejection:
    def test_flags_the_scene_with_the_reason_voxd_gave(self) -> None:
        service = VoxPanelService(_FakeDaemonClient(), _FakeStore(_config()))
        service.note_rejection("unknown voice 'nope'")
        notice = service.scene().notice
        assert notice == PanelNotice.voxd_rejected("unknown voice 'nope'")
        assert "unknown voice 'nope'" in notice.message

    def test_does_not_re_read_and_keeps_the_last_known_settings(self) -> None:
        """A refusal means voxd answers this session with no -- a confirming
        read would fail the same way, or trade this reason for a generic one."""
        client = _FakeDaemonClient()
        service = VoxPanelService(client, _FakeStore(_config(voice="roger")))
        service.prefetch()
        reads_before = client.roster_reads

        service.note_rejection("unexpected reply")
        assert client.roster_reads == reads_before
        assert service.scene().voice == "roger"


class TestConcurrentApplyEvent:
    def test_overlapping_events_on_different_fields_both_land(self) -> None:
        """Two threads writing different fields must not clobber one another."""
        store = _FakeStore(_config())
        first_entered = threading.Event()
        release_first = threading.Event()
        second_started = threading.Event()

        class _BlockingStore:
            def read(self) -> VoxConfig:
                return store.read()

            def write_field(self, key: str, value: str) -> None:
                if key == "notify":
                    first_entered.set()
                    release_first.wait(timeout=2)
                store.write_field(key, value)

        service = VoxPanelService(_FakeDaemonClient(), _BlockingStore())
        service.prefetch()

        def apply_notify() -> None:
            service.apply_event(PanelTopic.NOTIFY, {"value": 2})

        def apply_speak() -> None:
            second_started.set()
            service.apply_event(PanelTopic.MIC_MODE, {"value": 0})

        notify_thread = threading.Thread(target=apply_notify)
        notify_thread.start()
        assert first_entered.wait(timeout=2)

        speak_thread = threading.Thread(target=apply_speak)
        speak_thread.start()
        assert second_started.wait(timeout=2)
        # The lock must block the second writer while the first still holds
        # it -- if it raced ahead instead, it would compute from a stale
        # snapshot and its write would be the one to survive, not both.
        speak_thread.join(timeout=0.2)
        assert speak_thread.is_alive(), "a second writer got in mid-update"

        release_first.set()
        notify_thread.join(timeout=2)
        speak_thread.join(timeout=2)

        scene = service.scene()
        assert scene.notify == "c"
        assert scene.speak == "n"


class TestAcknowledgeAndService:
    def test_acknowledge_pushes_the_held_scene(self) -> None:
        from punt_lux.applets import ClickLatency

        service = VoxPanelService(_FakeDaemonClient(), _FakeStore(_config()))
        rest = _FakeRest()
        service.acknowledge(rest, ClickLatency("vox-panel"))
        assert len(rest.rendered) == 1

    def test_service_refreshes_then_pushes(self) -> None:
        from punt_lux.applets import ClickLatency

        store = _FakeStore(_config(voice="aria"))
        service = VoxPanelService(_FakeDaemonClient(), store)
        rest = _FakeRest()
        latency: ClickLatency = ClickLatency("vox-panel")
        service.service(rest, latency)
        assert service.scene().voice == "aria"
        assert len(rest.rendered) == 1
