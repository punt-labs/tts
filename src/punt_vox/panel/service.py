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
from punt_vox.panel.click_target import ClickTarget
from punt_vox.panel.control_push import ControlPush
from punt_vox.panel.panel_notice import PanelNotice
from punt_vox.panel.panel_push import PanelPush
from punt_vox.panel.radio_control import MIC_MODE_SPEC, NOTIFY_SPEC
from punt_vox.panel.state import PanelState
from punt_vox.panel.topics import PanelTopic
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

    async def correct_scene(self, client: LuxClient) -> None:
        """Reinstall the held scene in full, snapping back an optimistic widget.

        Reached only after a control-change failure that a widget already
        applied client-side before voxd answered -- see
        :meth:`~punt_vox.panel.panel_push.PanelPush.correct` for why a diff-based
        refresh cannot express this correction.
        """
        await self._push.correct(client, self.scene().render_request())

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

    def note_control_rejected(self, control: str) -> None:
        """Flag the scene with a control change that could not be applied.

        *control* is the control's human name, as
        :attr:`~punt_vox.panel.topics.PanelTopic.label` gives it. Reached
        from :class:`~punt_vox.panel.panel_runner.PanelRunner` when
        :meth:`apply_event` refuses an event, so the corrective reinstall
        that follows carries a reason instead of an unexplained revert.
        """
        with self._lock:
            self._notice = PanelNotice.control_rejected(control)

    def apply_event(self, topic: str, payload: Mapping[str, object]) -> ControlPush:
        """Apply one control-topic event; answer what kind of re-push it needs.

        A payload rejection (``TypeError``/``ValueError``), a value the store
        will not serialize (``ConfigValueError``), and a config-write failure
        (``OSError``) all propagate -- this method swallows none of them, so
        :class:`~punt_vox.panel.panel_runner.PanelRunner` can answer each.

        The three-way answer -- :attr:`~ControlPush.NONE`,
        :attr:`~ControlPush.REFRESH`, :attr:`~ControlPush.CORRECT` -- is not
        just "did the scene change": ``_commit_provider`` and ``_commit_model``
        can abandon a commit (an unreadable roster, a provider swapped
        mid-fetch) without raising, leaving ``_state`` exactly where it was.
        That abandonment is still a widget the caller already updated
        optimistically and this service never confirmed, so it answers
        :attr:`~ControlPush.CORRECT` like every raising failure does -- a
        bare re-derived ``bool`` cannot tell that apart from a genuine no-op.
        """
        if topic == PanelTopic.NOTIFY:
            code = NOTIFY_SPEC.code_for_index(self._index(payload))
            self._commit("notify", code, PanelState.with_notify)
            return ControlPush.REFRESH
        if topic == PanelTopic.MIC_MODE:
            code = MIC_MODE_SPEC.code_for_index(self._index(payload))
            self._commit("speak", code, PanelState.with_speak)
            return ControlPush.REFRESH
        if topic == PanelTopic.VOICE:
            voice = self._target().voice(self._index(payload))
            self._commit("voice", voice, PanelState.with_voice)
            return ControlPush.REFRESH
        if topic == PanelTopic.PROVIDER:
            return self._commit_provider(self._target().provider(self._index(payload)))
        if topic == PanelTopic.MODEL:
            return self._commit_model(self._target().model(self._index(payload)))
        if topic == PanelTopic.VOICE_PREVIEW:
            return self._preview()
        logger.warning("vox-panel: no handler for topic %r", topic)
        return ControlPush.NONE

    def _cleared_notice(self) -> ControlPush:
        """Drop any displayed notice; answer the push that drop needs.

        Callers hold ``_lock``, which is not reentrant, so this never takes
        it. That is a contract the type system cannot state: reading
        ``is_present`` and writing ``_notice`` is a read-modify-write, and a
        caller that arrives without the lock does not deadlock loudly -- it
        races another thread's notice and loses one silently.

        A notice going away is as much a scene change as a setting moving:
        the band the display renders disappears. Answering
        :attr:`~ControlPush.NONE` unconditionally left that change stranded
        in daemon memory -- the runner pushed nothing, so the display went
        on showing a warning this service no longer held.
        :attr:`~ControlPush.REFRESH` is the right push because the notice
        rode the last render vox pushed, so the diff sees it leave. Only an
        already-silent panel is a true no-op.
        """
        if not self._notice.is_present:
            return ControlPush.NONE
        self._notice = PanelNotice.silent()
        return ControlPush.REFRESH

    def _commit(
        self, field: str, value: str, update: Callable[[PanelState, str], PanelState]
    ) -> None:
        """Persist *field* and update the held state as one atomic step."""
        with self._lock:
            self._store.write_field(field, value)
            self._state = update(self._state, value)
            self._notice = PanelNotice.silent()

    def _target(self) -> ClickTarget:
        """Snapshot the held settings as the thing a click's index names."""
        with self._lock:
            return ClickTarget(self._state)

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

    def _commit_provider(self, provider: str) -> ControlPush:
        """Persist a provider change; cascade model + voice to their defaults.

        The cascade rule: setting a provider writes provider +
        ``MODEL_TABLE.available(provider)[0]`` (empty string for modelless
        providers) + ``roster[0]`` for the voice. All three land in one
        atomic write so a reader sees them together, never a partial
        state. A re-publish of the same provider neither rewrites the disk
        nor refetches the roster; it clears a stale notice and answers
        :meth:`_cleared_notice`'s verdict on whether that needs a push.

        Roster fetch happens OUTSIDE the lock -- it is a voxd round-trip
        that must not block other panel threads on their own reads. The
        provider is re-checked under the lock after the fetch: if another
        thread swapped the provider mid-flight (e.g. a second click hit
        first), this call gives up rather than clobber the newer state.
        That re-check is the *only* one the post-fetch section needs: the
        pre-fetch guard above already returned on ``provider ==
        pre_fetch_provider``, so once the re-check confirms ``_state``
        still holds ``pre_fetch_provider``, ``provider`` differs from it
        by construction and the write below always has work to do.

        A ``VoxdConnectionError`` on the roster fetch surfaces as a
        transient notice; the write is abandoned so the disk never lands
        a provider whose voice roster we could not read. That abandonment,
        and the mid-fetch-race abort below, both answer
        :attr:`~ControlPush.CORRECT` for the reason :meth:`apply_event`
        documents on its own three-way return.
        """
        with self._lock:
            pre_fetch_provider = self._state.provider
            if provider == pre_fetch_provider:
                return self._cleared_notice()
        fetched = self._fetch_roster_or_notice(provider, "provider-switch")
        if fetched is None:
            return ControlPush.CORRECT
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
                # Whatever the band is showing was put there by whoever
                # authored it, most likely the thread whose commit won this
                # race. This one changed nothing and has nothing to report,
                # so it leaves the notice alone rather than destroying
                # another thread's reason before anyone reads it.
                return ControlPush.CORRECT
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
            return ControlPush.REFRESH

    def _commit_model(self, model: str) -> ControlPush:
        """Persist a model change; cascade voice to the current-provider default.

        The cascade rule: setting a model writes model +
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

        Every abandoned commit below -- no provider selected yet, an
        unreadable roster, a provider changed mid-fetch -- answers
        :attr:`~ControlPush.CORRECT` for the same reason
        ``_commit_provider`` does: the widget already applied the pick and
        ``_state`` never moved, so only a full reinstall corrects it.
        """
        with self._lock:
            pre_fetch_provider = self._state.provider
        if pre_fetch_provider is None:
            # A provider-less panel renders no clickable model at all, so a
            # MODEL event cannot originate from the widget in this state.
            # What can reach here is a race: a concurrent resync clearing
            # the provider between the click's own snapshot and this lock.
            # Skip rather than fetch a cascaded voice roster from a
            # provider the session no longer declares. The notice is left
            # as it stands -- this call has nothing of its own to report.
            logger.info("vox-panel: model click ignored; no provider selected yet")
            return ControlPush.CORRECT
        fetched = self._fetch_roster_or_notice(pre_fetch_provider, "model-switch")
        if fetched is None:
            return ControlPush.CORRECT
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
                # Same as ``_commit_provider``'s mid-fetch abort: nothing
                # happened here, so nothing this call owns belongs on the
                # notice band, and another thread's reason stays put.
                return ControlPush.CORRECT
            self._store.write_fields({"model": model, "voice": voice_default})
            self._state = self._state.with_model(model, voice=voice_default or None)
            self._notice = PanelNotice.silent()
            return ControlPush.REFRESH

    def _preview(self) -> ControlPush:
        """Play the held voice back; answer the push the notice band needs.

        Builds the wire spec through :class:`SessionSpec` off a fresh read of
        the config store, so the preview sends the provider state declares
        rather than letting voxd guess. An unconfigured or misconfigured
        state surfaces on the notice band via
        :meth:`PanelNotice.voxd_rejected`.

        A preview the caller just *heard* is the strongest evidence there is
        that voxd is reachable, so success clears any warning still standing
        -- otherwise a "voxd is unreachable" line outlives its own cause,
        contradicted by the audio that just played. Every outcome here says
        something, including the one where no voice is selected: a button
        press that changes nothing on screen reads as a broken panel.
        """
        with self._lock:
            voice = self._state.voice
        if voice is None:
            logger.info("vox-panel: no voice selected yet; preview skipped")
            with self._lock:
                self._notice = PanelNotice.no_voice_selected()
            return ControlPush.REFRESH
        try:
            spec = SessionSpec(self._store.read()).fill(SynthesisSpec(voice=voice))
        except (ProviderNotConfiguredError, ModelNotAvailableError) as exc:
            logger.warning("vox-panel: voice preview refused: %s", exc)
            with self._lock:
                self._notice = PanelNotice.voxd_rejected(str(exc))
            return ControlPush.REFRESH
        try:
            self._client.synthesize(_PREVIEW_TEXT, spec)
        except VoxdConnectionError:
            logger.warning("vox-panel: voice preview failed -- voxd is not reachable")
            with self._lock:
                self._notice = PanelNotice.voxd_unavailable()
            return ControlPush.REFRESH
        with self._lock:
            return self._cleared_notice()

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
