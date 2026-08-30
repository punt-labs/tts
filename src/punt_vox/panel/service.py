"""``VoxPanelService`` -- the ``Vox`` menu entry a session owns.

Reads vox's current settings, applies a control change to the same config store
and daemon RPCs the CLI and MCP tool already use, and pushes the confirmed scene.
Satisfies :class:`punt_lux.applets.AppletService` structurally (``callback_id``,
``label``, ``prefetch``, ``acknowledge``, ``service``) plus the extra methods
:class:`~punt_vox.panel.leg.VoxPanelLeg` calls when a control event arrives.

The held ``_state``/``_notice`` pair is read from more than one thread: a menu
click and a control event each run their sync work on an ``asyncio.to_thread``
worker, so two can be mid-update at once. ``_lock`` serializes every
read-modify-write so one thread's commit is never overwritten.

Reaching luxd is :class:`~punt_vox.panel.panel_push.PanelPush`'s job, not this
one's. The two verbs it exposes differ in intent rather than content:
:meth:`VoxPanelService.install_scene` shows the scene and then explicitly
raises the frame, and is reached only from the ``Vox`` menu click;
:meth:`VoxPanelService.push_scene` refreshes the panel where it already sits,
which is what the confirm push behind a click and every control-change re-push
want. Radio clicks used to take the ``show`` path, so changing a setting
yanked the panel in front of whatever was on top of it.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from dataclasses import replace
from typing import TYPE_CHECKING, Self, final

from punt_vox.cascade import Cascade, RosterError, RosterRejectedError
from punt_vox.client_errors import VoxdConnectionError, VoxdRejectionError
from punt_vox.models import MODEL_TABLE
from punt_vox.panel.model_control import ModelControl
from punt_vox.panel.panel_notice import PanelNotice
from punt_vox.panel.panel_push import PanelPush
from punt_vox.panel.provider_control import ProviderControl
from punt_vox.panel.radio_control import MIC_MODE_SPEC, NOTIFY_SPEC
from punt_vox.panel.state import PanelState
from punt_vox.panel.topics import PanelTopic
from punt_vox.panel.voice_control import VoiceControl
from punt_vox.server_switches import PROVIDER_NAMES
from punt_vox.session_spec import SessionSpec
from punt_vox.types_synthesis import SynthesisSpec
from punt_vox.types_synthesis_errors import (
    ModelNotAvailableError,
    ProviderNotConfiguredError,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from punt_lux import LuxClient
    from punt_lux.applets import ClickLatency

    from punt_vox.panel.panel_scene import PanelScene
    from punt_vox.panel.ports import PanelDaemonClient, SettingsStore

logger = logging.getLogger(__name__)

__all__ = ["VoxPanelService"]

_CALLBACK_ID = "vox-panel"
_LABEL = "Vox"
_PREVIEW_TEXT = "This is my voice."


@final
class VoxPanelService:
    """A session's Vox settings entry: read settings, apply a change, show the scene."""

    _client: PanelDaemonClient
    _store: SettingsStore
    _state: PanelState
    _notice: PanelNotice
    _lock: threading.Lock
    _push: PanelPush
    __slots__ = ("_client", "_lock", "_notice", "_push", "_state", "_store")

    def __new__(cls, client: PanelDaemonClient, store: SettingsStore) -> Self:
        self = super().__new__(cls)
        self._client = client
        self._store = store
        self._state = PanelState.empty()
        self._notice = PanelNotice.silent()
        self._lock = threading.Lock()
        self._push = PanelPush()
        return self

    @property
    def callback_id(self) -> str:
        """The id the ``Vox`` menu entry's clicks carry back to this session."""
        return _CALLBACK_ID

    @property
    def label(self) -> str:
        """The entry the display shows under this session's submenu."""
        return _LABEL

    def prefetch(self) -> None:
        """Read settings once before any click, so the first click has some to show."""
        self.refresh()

    async def acknowledge(self, client: LuxClient, latency: ClickLatency) -> None:
        """Install the held scene now -- the visible half of the click.

        This is the ``Vox`` menu entry answering, so showing the scene (and with
        it raising the frame) is the answer, not a side effect.
        """
        with latency.answering():
            await self.install_scene(client)

    async def service(self, client: LuxClient, latency: ClickLatency) -> None:
        """Re-read settings fresh and refresh the scene with the confirmed ones.

        A few milliseconds behind :meth:`acknowledge`, onto the same window it
        just raised -- so this refreshes rather than installing again.
        """
        with latency.stage("refreshed"):
            await asyncio.to_thread(self.refresh)
        await self.push_scene(client)

    def scene(self) -> PanelScene:
        """Return the currently-held settings and notice as a scene."""
        with self._lock:
            return replace(self._state.scene(), notice=self._notice)

    async def install_scene(self, client: LuxClient) -> None:
        """Show the held scene outright, frame raise and all (a menu click)."""
        await self._push.install(client, self.scene().render_request())

    async def push_scene(self, client: LuxClient) -> None:
        """Refresh the installed scene with the currently-held one."""
        await self._push.refresh(client, self.scene().render_request())

    def refresh(self) -> None:
        """Re-read settings from disk and voxd; note staleness if voxd is down."""
        self._resync(
            PanelNotice.silent(),
            on_read_failure=PanelNotice.voxd_unavailable(),
            on_rejection=PanelNotice.voxd_rejected,
        )

    def recover_from_write_failure(self, field: str) -> None:
        """Re-sync from the real settings after a failed persist, and flag the scene."""
        self._resync(
            PanelNotice.write_failed(field),
            on_read_failure=PanelNotice.write_failed_and_voxd_unavailable(field),
            on_rejection=lambda detail: PanelNotice.write_failed_and_voxd_rejected(
                field, detail
            ),
        )

    def note_rejection(self, detail: str) -> None:
        """Flag the scene with voxd's refusal, keeping the last-known settings.

        Unlike :meth:`recover_from_write_failure` this does not re-sync:
        voxd has just proved it answers this session with a refusal, so a
        confirming read would either fail the same way or trade this
        specific reason for a generic one.
        """
        with self._lock:
            self._notice = PanelNotice.voxd_rejected(detail)

    def apply_event(self, topic: str, payload: Mapping[str, object]) -> bool:
        """Apply one control-topic event; return whether the scene needs a re-push.

        A payload rejection (``TypeError``/``ValueError``), a value the store
        will not serialize (``ConfigValueError``), and a config-write failure
        (``OSError``) all propagate -- this method swallows none of them, so
        :class:`~punt_vox.panel.panel_runner.PanelRunner` can answer each.
        """
        if topic == PanelTopic.NOTIFY:
            code = NOTIFY_SPEC.code_for_index(self._index(payload))
            self._commit("notify", code, PanelState.with_notify)
        elif topic == PanelTopic.MIC_MODE:
            code = MIC_MODE_SPEC.code_for_index(self._index(payload))
            self._commit("speak", code, PanelState.with_speak)
        elif topic == PanelTopic.VOICE:
            voice = self._voice_for(self._index(payload))
            self._commit("voice", voice, PanelState.with_voice)
        elif topic == PanelTopic.PROVIDER:
            provider = self._provider_for(self._index(payload))
            self._commit_provider(provider)
        elif topic == PanelTopic.MODEL:
            model = self._model_for(self._index(payload))
            self._commit_model(model)
        elif topic == PanelTopic.VOICE_PREVIEW:
            return self._preview()
        else:
            logger.warning("vox-panel: no handler for topic %r", topic)
            return False
        return True

    def _commit(
        self, field: str, value: str, update: Callable[[PanelState, str], PanelState]
    ) -> None:
        """Persist *field* and update the held state as one atomic step."""
        with self._lock:
            self._store.write_field(field, value)
            self._state = update(self._state, value)
            self._notice = PanelNotice.silent()

    def _voice_for(self, index: int) -> str:
        with self._lock:
            roster, current = self._state.roster, self._state.voice
        return VoiceControl(roster=roster, current=current).voice_for_index(index)

    def _provider_for(self, index: int) -> str:
        with self._lock:
            current = self._state.provider
        control = ProviderControl(providers=PROVIDER_NAMES, current=current)
        return control.provider_for_index(index)

    def _model_for(self, index: int) -> str:
        with self._lock:
            provider, current = self._state.provider, self._state.model
        # A model click needs to name a provider -- previously the panel
        # substituted ``"elevenlabs"`` when state had none, offering the
        # ElevenLabs model list under an unset-provider session, which is
        # the same silent substitution this bead deletes. An empty tuple
        # produces an out-of-range click that ``ModelControl`` refuses,
        # so the panel's guard reaches the caller as the same rejection
        # a menu click on a genuinely modelless provider gets.
        models = MODEL_TABLE.available(provider) if provider else ()
        return ModelControl(models=models, current=current).model_for_index(index)

    def _fetch_roster_or_notice(
        self, provider: str, context: str
    ) -> tuple[str, ...] | None:
        """Fetch *provider*'s voice roster; on daemon fault, set the notice.

        Returns the roster tuple on success. On :class:`RosterError`
        (voxd unreachable or malformed), sets ``PanelNotice.voxd_unavailable()``
        under the lock and returns ``None`` -- the caller aborts its
        commit. Extracted so ``_commit_provider`` and ``_commit_model``
        share one fetch-and-guard body.
        """
        result = Cascade.fetch_roster(self._client, provider)
        if isinstance(result, RosterError):
            logger.warning(
                "vox-panel: roster fetch failed during %s (provider=%r): %s",
                context,
                provider,
                result.message,
            )
            # A daemon-sent rejection carries the reason voxd refused (an
            # unknown provider name, a wire mismatch from a long-lived
            # panel talking to a fresh strict daemon) — surface it
            # verbatim so the operator sees WHY the roster is missing
            # rather than a generic "unavailable" line that reads as
            # "the daemon is down" when the daemon is right there and
            # said no.
            with self._lock:
                if isinstance(result, RosterRejectedError):
                    self._notice = PanelNotice.voxd_rejected(result.message)
                else:
                    self._notice = PanelNotice.voxd_unavailable()
            return None
        return tuple(result)

    def _commit_provider(self, provider: str) -> None:
        """Persist a provider change; cascade model + voice to their defaults.

        The cascade rule (vox-s5uv): setting a provider writes provider +
        ``MODEL_TABLE.available(provider)[0]`` (empty string for modelless
        providers) + ``roster[0]`` for the voice. All three land in one
        atomic write so a reader sees them together, never a partial
        state. A re-publish of the same provider is still a no-op -- an
        echoed event neither rewrites the disk nor refetches the roster.

        Roster fetch happens OUTSIDE the lock -- it is a voxd round-trip
        that must not block other panel threads on their own reads. The
        provider is re-checked under the lock after the fetch: if another
        thread swapped the provider mid-flight (e.g. a second click hit
        first), this call gives up rather than clobber the newer state.

        A ``VoxdConnectionError`` on the roster fetch surfaces as a
        transient notice; the write is abandoned so the disk never lands
        a provider whose voice roster we could not read.
        """
        with self._lock:
            pre_fetch_provider = self._state.provider
            if provider == pre_fetch_provider:
                self._notice = PanelNotice.silent()
                return
        fetched = self._fetch_roster_or_notice(provider, "provider-switch")
        if fetched is None:
            return
        roster = fetched
        model_default = Cascade.default_model(provider)
        voice_default = Cascade.first_or_empty(roster)
        with self._lock:
            # Re-check under the lock: another thread may have committed a
            # different provider while our roster RPC was in flight. If the
            # provider we saw before the fetch is not the one we see now,
            # give up rather than overwrite that newer state.
            if self._state.provider != pre_fetch_provider:
                logger.info(
                    "vox-panel: provider changed to %r mid-fetch; "
                    "aborting our %r commit",
                    self._state.provider,
                    provider,
                )
                self._notice = PanelNotice.silent()
                return
            if provider == self._state.provider:
                self._notice = PanelNotice.silent()
                return
            self._store.write_fields(
                {
                    "provider": provider,
                    "model": model_default,
                    "voice": voice_default,
                }
            )
            self._state = self._state.with_provider(
                provider,
                roster=roster,
                model=model_default or None,
                voice=voice_default or None,
            )
            self._notice = PanelNotice.silent()

    def _commit_model(self, model: str) -> None:
        """Persist a model change; cascade voice to the current-provider default.

        The cascade rule (vox-s5uv): setting a model writes model +
        ``client.voices(current_provider)[0]`` for the voice, in one
        atomic write. A ``VoxdConnectionError`` on the roster fetch
        surfaces as a transient notice; the write is abandoned so the
        disk never lands a model whose companion voice we could not read.

        Roster fetch happens OUTSIDE the lock -- it is a voxd round-trip
        that must not block other panel threads on their own reads. The
        provider is re-checked under the lock after the fetch: if another
        thread swapped the provider mid-flight, this commit gives up
        rather than clobber the newer state with a voice from the wrong
        provider. Mirrors ``_commit_provider``'s pre-fetch guard.
        """
        with self._lock:
            pre_fetch_provider = self._state.provider
        if pre_fetch_provider is None:
            # A model click before any provider has been chosen would have
            # been dispatched against the substituted ``"elevenlabs"``, its
            # cascaded voice roster fetched from a provider the session
            # never picked. Skip cleanly instead so the panel never writes
            # a wrong-provider voice on behalf of a caller who declared
            # no provider.
            logger.info("vox-panel: model click ignored; no provider selected yet")
            with self._lock:
                self._notice = PanelNotice.silent()
            return
        fetched = self._fetch_roster_or_notice(pre_fetch_provider, "model-switch")
        if fetched is None:
            return
        roster = fetched
        voice_default = Cascade.first_or_empty(roster)
        with self._lock:
            # Re-check under the lock: another thread may have committed a
            # different provider (via PROVIDER topic) while our roster RPC
            # was in flight. Give up rather than persist a voice default
            # from the wrong provider on top of that newer state.
            current_provider = self._state.provider
            if current_provider != pre_fetch_provider:
                logger.info(
                    "vox-panel: provider changed to %r mid-fetch; "
                    "aborting our model %r commit",
                    current_provider,
                    model,
                )
                self._notice = PanelNotice.silent()
                return
            self._store.write_fields({"model": model, "voice": voice_default})
            self._state = self._state.with_model(model, voice=voice_default or None)
            self._notice = PanelNotice.silent()

    def _preview(self) -> bool:
        """Play the held voice back; return whether a status notice needs to show.

        Builds the wire spec through :class:`SessionSpec` off a fresh read of
        the config store, so the preview sends the provider state declares
        rather than letting voxd guess -- previously the preview sent no
        provider at all (``docs/provider-authority.md`` §1.3, panel row).
        An unconfigured or misconfigured state surfaces on the notice band
        via :class:`PanelNotice.voxd_rejected` (F1 / F7).
        """
        with self._lock:
            voice = self._state.voice
        if voice is None:
            logger.info("vox-panel: no voice selected yet; preview skipped")
            return False
        try:
            spec = SessionSpec(self._store.read()).fill(SynthesisSpec(voice=voice))
        except (ProviderNotConfiguredError, ModelNotAvailableError) as exc:
            logger.warning("vox-panel: voice preview refused: %s", exc)
            with self._lock:
                self._notice = PanelNotice.voxd_rejected(str(exc))
            return True
        try:
            self._client.synthesize(_PREVIEW_TEXT, spec)
        except VoxdConnectionError:
            logger.warning("vox-panel: voice preview failed -- voxd is not reachable")
            with self._lock:
                self._notice = PanelNotice.voxd_unavailable()
            return True
        return False

    def _resync(
        self,
        notice_on_success: PanelNotice,
        *,
        on_read_failure: PanelNotice,
        on_rejection: Callable[[str], PanelNotice],
    ) -> None:
        """Re-read settings fresh, holding the last-known ones if voxd is down.

        *on_read_failure* and *on_rejection* are the caller's choice, not
        hardcoded generic notices: ``recover_from_write_failure`` already
        has a specific "couldn't save X" notice in flight, and if the
        resync meant to confirm the reverted value also finds voxd down
        OR gets refused, that specific write-failure context must not be
        silently replaced by a generic one blaming an unrelated
        subsystem -- the caller supplies a composed notice for each.
        """
        try:
            fresh = PanelState.read(self._client, self._store)
        except VoxdRejectionError as exc:
            # Daemon was reached and refused the roster fetch. The caller
            # supplies the composition so a write-failure in flight is not
            # dropped when the confirming read also hits a rejection.
            logger.warning("vox-panel: voxd refused the resync: %s", exc)
            with self._lock:
                self._notice = on_rejection(str(exc))
            return
        except VoxdConnectionError:
            logger.warning(
                "vox-panel: voxd unreachable during resync: %s",
                on_read_failure.message,
            )
            with self._lock:
                self._notice = on_read_failure
            return
        with self._lock:
            self._state = fresh
            self._notice = notice_on_success

    @staticmethod
    def _index(payload: Mapping[str, object]) -> int:
        """Return the payload's int ``value``, or raise if it is missing/wrong-typed."""
        value = payload.get("value")
        if not isinstance(value, int) or isinstance(value, bool):
            msg = f"expected an int 'value' in the control payload, got {value!r}"
            raise TypeError(msg)
        return value
