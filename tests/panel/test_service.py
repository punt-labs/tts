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
        self,
        voices: list[str] | None = None,
        *,
        raise_on_synth: bool = False,
        voices_by_provider: dict[str, list[str]] | None = None,
    ) -> None:
        self._voices = voices if voices is not None else ["aria", "roger"]
        self._voices_by_provider = voices_by_provider or {}
        self._raise_on_synth = raise_on_synth
        self.synth_calls: list[tuple[str, SynthesisSpec | None]] = []
        self.roster_reads = 0
        self.roster_reads_by_provider: list[str | None] = []

    def voices(self, provider: str | None = None) -> list[str]:
        self.roster_reads += 1
        self.roster_reads_by_provider.append(provider)
        if provider is not None and provider in self._voices_by_provider:
            return self._voices_by_provider[provider]
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

    def write_fields(self, updates: dict[str, str]) -> None:
        self.written.update(updates)


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
    provider: str | None = "elevenlabs",
    model: str | None = None,
) -> VoxConfig:
    """Return a ``VoxConfig`` for panel tests.

    The default provider is ``"elevenlabs"`` because :class:`PanelState.read`
    no longer fetches a roster when ``cfg.provider`` is unset (state is the
    sole authority on which provider voxd runs, per ``session_spec``). A
    default-provider config lets tests that exercise the daemon-side
    roster-fetch path (broken client, misbehaving client, cascade) hit that
    path; a test that specifically means to exercise the unset-provider
    branch overrides with ``provider=None``.
    """
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
            def voices(self, provider: str | None = None) -> list[str]:
                msg = "voxd unreachable"
                raise VoxdConnectionError(msg)

            def synthesize(self, *args: object, **kwargs: object) -> object:
                raise NotImplementedError

        service = VoxPanelService(_BrokenClient(), _FakeStore(_config()))  # type: ignore[arg-type]
        service.prefetch()
        assert service.scene().voice == PanelState.empty().voice

    def test_daemon_unavailable_sets_the_notice(self) -> None:
        class _BrokenClient:
            def voices(self, provider: str | None = None) -> list[str]:
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
            def voices(self, provider: str | None = None) -> list[str]:
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
        # The preview spec now fills provider from state through SessionSpec,
        # so the wire message carries the configured provider alongside the
        # candidate voice -- previously the preview sent no provider at all
        # and let the daemon guess.
        expected = (
            "This is my voice.",
            SynthesisSpec(voice="roger", provider="elevenlabs"),
        )
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

    def test_provider_writes_the_name_and_cascades_model_and_voice(self) -> None:
        """Cascade rule (vox-s5uv): provider + first-model + first-voice, atomic."""
        service = VoxPanelService(
            _FakeDaemonClient(voices_by_provider={"openai": ["alloy", "nova"]}),
            (store := _FakeStore(_config(provider="elevenlabs", model="eleven_v3"))),
        )
        service.prefetch()
        service.apply_event(PanelTopic.PROVIDER, {"value": 1})  # openai
        assert store.written["provider"] == "openai"
        # Model cascades to MODEL_TABLE.available("openai")[0].
        assert store.written["model"] == "tts-1"
        # Voice cascades to roster[0].
        assert store.written["voice"] == "alloy"
        scene = service.scene()
        assert scene.provider == "openai"
        assert scene.model == "tts-1"
        assert scene.voice == "alloy"

    def test_provider_refetches_roster_and_updates_scene(self) -> None:
        """A provider switch pulls the new roster into the panel scene (vox-w79f)."""
        client = _FakeDaemonClient(
            voices_by_provider={
                "elevenlabs": ["benno", "aria"],
                "espeak": ["en", "en-us"],
            }
        )
        service = VoxPanelService(
            client,
            _FakeStore(_config(voice="benno", provider="elevenlabs")),
        )
        service.prefetch()
        # espeak has index 4 in PROVIDER_NAMES (elevenlabs, openai, polly, say, espeak).
        service.apply_event(PanelTopic.PROVIDER, {"value": 4})
        scene = service.scene()
        assert scene.provider == "espeak"
        assert scene.roster == ("en", "en-us")
        # The roster refetch queried voxd for the new provider by name.
        assert "espeak" in client.roster_reads_by_provider

    def test_provider_cascades_voice_to_first_regardless_of_prior_voice(self) -> None:
        """Cascade rule (vox-s5uv): voice always becomes ``roster[0]``.

        Supersedes the vox-w79f 'clear when absent from roster' behavior with a
        stronger invariant: the new provider's first voice is written every
        time, so a stale voice can never leak past a switch.
        """
        client = _FakeDaemonClient(
            voices_by_provider={
                "elevenlabs": ["benno", "aria"],
                "espeak": ["en", "en-us"],
            }
        )
        service = VoxPanelService(
            client,
            (store := _FakeStore(_config(voice="benno", provider="elevenlabs"))),
        )
        service.prefetch()
        service.apply_event(PanelTopic.PROVIDER, {"value": 4})  # espeak
        assert store.written["voice"] == "en"
        assert service.scene().voice == "en"

    def test_provider_cascades_voice_to_first_even_when_prior_is_valid(self) -> None:
        """A voice that happens to exist in the new roster is still overwritten.

        The cascade is deterministic: setting a provider always writes
        ``roster[0]``. This is intentional -- the caller can override with
        a follow-up voice write.
        """
        client = _FakeDaemonClient(
            voices_by_provider={
                "openai": ["alloy", "nova"],
                "say": ["alloy", "samantha"],
            }
        )
        service = VoxPanelService(
            client, (store := _FakeStore(_config(voice="alloy", provider="openai")))
        )
        service.prefetch()
        service.apply_event(PanelTopic.PROVIDER, {"value": 3})  # say
        assert store.written["voice"] == "alloy"  # say roster[0]
        assert service.scene().voice == "alloy"

    def test_provider_no_op_when_unchanged_leaves_roster_and_voice_intact(self) -> None:
        """Re-publish of the same provider does not rewrite the disk or drop state."""
        client = _FakeDaemonClient(voices_by_provider={"elevenlabs": ["benno", "aria"]})
        service = VoxPanelService(
            client, (store := _FakeStore(_config(voice="benno", provider="elevenlabs")))
        )
        service.prefetch()
        client.roster_reads_by_provider.clear()
        service.apply_event(PanelTopic.PROVIDER, {"value": 0})  # elevenlabs (same)
        assert "provider" not in store.written
        assert "voice" not in store.written
        # And no wasteful roster refetch for a no-op switch.
        assert client.roster_reads_by_provider == []
        assert service.scene().voice == "benno"

    def test_provider_roster_fetch_error_aborts_the_write(self) -> None:
        """VoxdConnectionError on the roster fetch surfaces a notice, no disk write.

        The panel must not persist a provider whose roster we could not read --
        otherwise the on-disk voice can end up stale against a roster the panel
        never got to compare against.
        """

        class _RosterFailingClient:
            call_count = 0

            def voices(self, provider: str | None = None) -> list[str]:
                self.call_count += 1
                if self.call_count > 1:  # first call is the prefetch
                    msg = "voxd unreachable"
                    raise VoxdConnectionError(msg)
                return ["benno", "aria"]

            def synthesize(self, *args: object, **kwargs: object) -> object:
                raise NotImplementedError

        client = _RosterFailingClient()
        service = VoxPanelService(
            client,  # type: ignore[arg-type]
            (store := _FakeStore(_config(voice="benno", provider="elevenlabs"))),
        )
        service.prefetch()
        service.apply_event(PanelTopic.PROVIDER, {"value": 4})  # espeak
        assert "provider" not in store.written
        assert "voice" not in store.written
        assert service.scene().notice == PanelNotice.voxd_unavailable()
        # Original state is intact -- the failed switch left nothing changed.
        assert service.scene().provider == "elevenlabs"
        assert service.scene().voice == "benno"

    def test_provider_yields_when_another_thread_won_the_race(self) -> None:
        """A mid-flight competing provider commit wins; this call gives up.

        If two panel threads both dispatch a PROVIDER event and the roster
        RPC lands second, the second commit must not clobber the first.
        Modeled with a client whose ``voices()`` sneaks a competing state
        update into the service between fetch and re-check.
        """

        class _RacingClient:
            def __init__(self, service_ref: list[VoxPanelService]) -> None:
                self._service_ref = service_ref
                self.prefetch_done = False

            def voices(self, provider: str | None = None) -> list[str]:
                # First call (prefetch) returns the elevenlabs roster.
                if not self.prefetch_done:
                    self.prefetch_done = True
                    return ["benno", "aria"]
                # Subsequent calls: mid-flight, another thread wins with
                # openai. We simulate by mutating the service's state
                # directly (as if another thread had held the lock).
                svc = self._service_ref[0]
                svc._state = svc._state.with_provider(
                    "openai", roster=("alloy",), model="tts-1", voice="alloy"
                )
                return ["en", "en-us"]  # espeak roster (what our caller asked for)

            def synthesize(self, *args: object, **kwargs: object) -> object:
                raise NotImplementedError

        ref: list[VoxPanelService] = []
        client = _RacingClient(ref)
        service = VoxPanelService(
            client,  # type: ignore[arg-type]
            (store := _FakeStore(_config(voice="benno", provider="elevenlabs"))),
        )
        ref.append(service)
        service.prefetch()
        service.apply_event(PanelTopic.PROVIDER, {"value": 4})  # ask for espeak
        # The racing "openai" write happened during our roster RPC. We must
        # NOT write espeak on top of it.
        assert "provider" not in store.written
        assert "voice" not in store.written
        assert service.scene().provider == "openai"

    def test_model_writes_the_name_and_updates_state(self) -> None:
        service = VoxPanelService(
            _FakeDaemonClient(),
            (store := _FakeStore(_config(provider="openai"))),
        )
        service.prefetch()
        service.apply_event(PanelTopic.MODEL, {"value": 1})  # tts-1-hd
        assert store.written["model"] == "tts-1-hd"
        assert service.scene().model == "tts-1-hd"

    def test_model_yields_when_provider_changed_mid_fetch(self) -> None:
        """A mid-flight competing provider commit wins; this model commit gives up.

        Same race as ``_commit_provider``: if the roster RPC lands second,
        the model commit must not clobber the new provider's state with a
        voice default computed for the OLD provider.
        """

        class _RacingClient:
            def __init__(self, service_ref: list[VoxPanelService]) -> None:
                self._service_ref = service_ref
                self.prefetch_done = False

            def voices(self, provider: str | None = None) -> list[str]:
                _ = provider  # RacingClient ignores provider arg
                if not self.prefetch_done:
                    self.prefetch_done = True
                    return ["matilda", "aria"]
                # Mid-flight: another thread wins with espeak. Simulate by
                # mutating the service's state directly.
                svc = self._service_ref[0]
                svc._state = svc._state.with_provider(
                    "espeak", roster=("en",), model=None, voice="en"
                )
                return ["matilda", "aria"]  # old-provider roster

            def synthesize(self, *args: object, **kwargs: object) -> object:
                raise NotImplementedError

        ref: list[VoxPanelService] = []
        client = _RacingClient(ref)
        service = VoxPanelService(
            client,  # type: ignore[arg-type]
            (store := _FakeStore(_config(provider="elevenlabs"))),
        )
        ref.append(service)
        service.prefetch()
        service.apply_event(PanelTopic.MODEL, {"value": 1})  # tts-1-hd (arbitrary)
        # The racing "espeak" write happened mid-fetch. We MUST NOT write
        # a model + old-provider voice on top of it.
        assert "model" not in store.written
        assert "voice" not in store.written
        assert service.scene().provider == "espeak"

    def test_voice_preview_lets_a_real_bug_propagate(self) -> None:
        """Only the voxd-unreachable transient is swallowed -- a protocol bug is not."""

        class _MisbehavingClient:
            def voices(self, provider: str | None = None) -> list[str]:
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

            def write_fields(self, updates: dict[str, str]) -> None:
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

            def write_fields(self, updates: dict[str, str]) -> None:
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
            def voices(self, provider: str | None = None) -> list[str]:
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

            def write_fields(self, updates: dict[str, str]) -> None:
                store.write_fields(updates)

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
