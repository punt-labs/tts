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
    def test_subscribes_to_every_panel_topic(self) -> None:
        """The leg carries every ``PanelTopic`` -- a rename cannot drift the two."""
        leg = VoxPanelApplet._leg_for(_SESSION_PID)
        assert leg._topics == tuple(topic.value for topic in PanelTopic)

    def test_serves_a_vox_panel_service(self) -> None:
        """The leg's service is the real ``VoxPanelService``, not a stand-in."""
        leg = VoxPanelApplet._leg_for(_SESSION_PID)
        assert isinstance(leg._service, VoxPanelService)

    def test_declares_the_identity_lux_derives_for_this_session(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The REST leg's identity is what ``AppletIdentity.for_session`` yields.

        The Hub keys its connections on that identity's fields, so the value
        the REST factory closes over IS what determines the connection this
        program owns. We ask the factory who it built for by capturing the
        argument it hands to ``LuxRestClient.for_identity``.
        """
        captured: list[ClientIdentity] = []

        def _capture(identity: ClientIdentity) -> object:
            captured.append(identity)
            return object()

        monkeypatch.setattr("punt_vox.panel.leg.LuxRestClient.for_identity", _capture)
        leg = VoxPanelApplet._leg_for(_SESSION_PID)
        leg._rest_factory()

        expected = AppletIdentity.for_session(_SESSION_PID).client
        assert captured == [expected]
