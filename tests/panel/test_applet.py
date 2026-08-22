"""Tests for :mod:`punt_vox.panel.applet`.

The applet's job is to wire the three pieces the panel's program needs -- a
claim on its session, the leg that holds the Hub connection, and the watch
that ends the program when the session does -- from a bare session pid. The
Hub keys a connection on the identity the leg declares, so a mistake in what
this file hands the leg silently collapses two programs in one session onto
one connection.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from punt_lux.applets import AppletIdentity
from punt_lux.applets.claim import NoClaim, SessionClaim
from punt_lux.applets.watch import NoSession, SessionWatch
from punt_lux.connection_identity import connection_for

from punt_vox.panel.applet import VoxPanelApplet
from punt_vox.panel.leg import VoxPanelLeg
from punt_vox.panel.service import VoxPanelService
from punt_vox.panel.topics import PanelTopic

if TYPE_CHECKING:
    from pathlib import Path

    import pytest
    from punt_lux.domain.hub.client_identity import ClientIdentity


# A synthetic pid that will not collide with any live process during the test
# run; SessionClaim opens (but does not lock) a file named after it, and using
# a distinctive number keeps the artefact easy to spot if it ever leaks.
_SESSION_PID = 987654


def _isolate_claim_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Redirect ``SessionClaim``'s pid-file directory into *tmp_path*.

    ``SessionClaim.__new__`` opens a file at
    ``$TMPDIR/<program>-<session_pid>.pid``; keeping that file inside the
    test's own tmp directory means the suite never litters ``/tmp`` and never
    races a real applet that happens to be using the same synthetic pid.
    """
    monkeypatch.setattr(
        "punt_lux.applets.claim.tempfile.gettempdir", lambda: str(tmp_path)
    )


def _isolate_config_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Redirect ``_leg_for``'s config-dir lookup into *tmp_path*.

    ``_leg_for`` builds its ``ConfigStore`` from ``find_config_dir()``, which
    walks the process cwd's parents for a ``.punt-labs/vox`` directory. Pinning
    the lookup at an empty tmp directory means the panel's store reads no
    ambient repo config -- the test does not rely on where it happens to run.
    """

    def _pinned(_start: Path | None = None) -> Path:
        return tmp_path

    monkeypatch.setattr("punt_vox.panel.applet.find_config_dir", _pinned)


class TestForSession:
    def test_binds_a_session_claim_a_leg_and_a_session_watch(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """A pid-bearing invocation wires the pid-specific claim and watch."""
        _isolate_claim_dir(monkeypatch, tmp_path)
        applet = VoxPanelApplet.for_session(_SESSION_PID)
        program = applet._program
        assert isinstance(program._claim, SessionClaim)
        assert isinstance(program._watch, SessionWatch)
        assert isinstance(program._leg, VoxPanelLeg)


class TestUnattended:
    def test_binds_the_null_claim_and_the_null_session(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """A hand-run invocation has no session, so its claim and watch are null."""
        _isolate_claim_dir(monkeypatch, tmp_path)
        applet = VoxPanelApplet.unattended()
        program = applet._program
        assert isinstance(program._claim, NoClaim)
        assert isinstance(program._watch, NoSession)
        assert isinstance(program._leg, VoxPanelLeg)


class TestLegFor:
    def test_subscribes_to_every_panel_topic(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """The leg carries every ``PanelTopic`` -- a rename cannot drift the two."""
        _isolate_config_dir(monkeypatch, tmp_path)
        leg = VoxPanelApplet._leg_for(_SESSION_PID)
        assert leg._topics == tuple(topic.value for topic in PanelTopic)

    def test_serves_a_vox_panel_service(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """The leg's service is the real ``VoxPanelService``, not a stand-in."""
        _isolate_config_dir(monkeypatch, tmp_path)
        leg = VoxPanelApplet._leg_for(_SESSION_PID)
        assert isinstance(leg._service, VoxPanelService)

    @staticmethod
    def _identity_from_leg(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> ClientIdentity:
        """Build the leg for the standard test pid and return the identity it declares.

        Captures the ``ClientIdentity`` the REST factory closes over by
        monkey-patching ``LuxClient.for_identity``, then invokes the
        factory once so the closed-over value flows into the capture. This
        is the identity the Hub attributes the panel's connection to.
        """
        _isolate_config_dir(monkeypatch, tmp_path)
        captured: list[ClientIdentity] = []

        def _capture(identity: ClientIdentity) -> object:
            captured.append(identity)
            return object()

        monkeypatch.setattr("punt_vox.panel.leg.LuxClient.for_identity", _capture)
        leg = VoxPanelApplet._leg_for(_SESSION_PID)
        leg._rest_factory()
        return captured[0]

    def test_declares_the_vox_panel_program_at_the_hub(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Positive control: the leg's identity IS the ``vox-panel`` identity.

        A fresh ``AppletIdentity.for_session("vox-panel", _SESSION_PID)``
        collapses to the same ``ConnectionId`` as the one the leg declares,
        so the applet is actually passing ``"vox-panel"`` -- not a typo like
        ``"vox-pannel"`` and not some other program constant a future
        refactor might quietly wire in. A distinctness-only assertion would
        pass those false-negatives green, since a mis-spelled token still
        differs from ``lux-beads``. It also pins ``connection_for``'s
        collapse-when-agreeing invariant: two identical declarations must
        resolve to one ``ConnectionId``, which is the property the whole
        Hub-connection design rests on.
        """
        panel_identity = self._identity_from_leg(monkeypatch, tmp_path)
        expected = AppletIdentity.for_session("vox-panel", _SESSION_PID).client
        assert connection_for(panel_identity.model_dump()) == connection_for(
            expected.model_dump()
        )

    def test_owns_a_hub_connection_distinct_from_the_beads_applet(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """The vox-panel and lux-beads identities for one session resolve apart.

        Both applets are ``kind="applet"``, declared against the same repo,
        with no agent handle -- so three of the four fields
        ``connection_for`` seeds its hash from agree. The fourth is the
        name, which must carry the program token. If it does not, the two
        identities hash to one ``ConnectionId`` and the second applet to
        connect silently takes the first's Hub connection over: the
        session's menu ends up with one entry where it should have two.
        The assertion is on the ``ConnectionId`` values themselves (the
        property that actually broke), not on the label strings that
        produce them.
        """
        panel_identity = self._identity_from_leg(monkeypatch, tmp_path)
        beads_identity = AppletIdentity.for_session("lux-beads", _SESSION_PID).client
        assert connection_for(panel_identity.model_dump()) != connection_for(
            beads_identity.model_dump()
        )
