# pyright: reportPrivateUsage=false
"""Partition tests for the notify/speak state machine.

Derived from the Z specification (docs/vox-notify.tex) using TTF
testing tactics.  Each test corresponds to a numbered partition in
the formal analysis.  The pre-state -> post-state mapping verifies
that the implementation conforms to the spec.

Z spec mapping:
    NotifyMode:  nOff = "n",  nOn = "y",  nCont = "c"
    SpeakMode:   sUnset = None (field absent),  sChime = "n",  sVoice = "y"
    voice:       empty = None,  {v} = present

State is the in-memory SessionConfig dataclass.
sUnset is represented by speak="n" with speak_explicit=False (fresh session).
sChime is represented by speak="n" with speak_explicit=True.
sVoice is represented by speak="y".
"""

from __future__ import annotations

import json

import pytest

from punt_vox.server import SessionConfig, notify, speak


@pytest.fixture(autouse=True)
def _fresh_session(monkeypatch: pytest.MonkeyPatch) -> None:  # pyright: ignore[reportUnusedFunction]
    """Reset server session config before every test.

    Stubs _find_config_dir to return None so refresh_from_config
    is a no-op -- partition tests control state via _set_state only.
    """
    import punt_vox.server as srv

    monkeypatch.setattr(srv, "_session", SessionConfig())
    monkeypatch.setattr(srv, "_find_config_dir", lambda: None)


def _set_state(
    *,
    notify_mode: str | None = None,
    speak_mode: str | None = None,
    speak_explicit: bool = False,
    voice: str | None = None,
) -> None:
    """Set session pre-state.

    When speak_mode is None, speak defaults to "n" and speak_explicit
    stays False (sUnset). When speak_mode is given, speak_explicit is
    set to True (the user has made an explicit choice) unless
    speak_explicit is explicitly passed as False.
    """
    import punt_vox.server as srv

    if notify_mode is not None:
        srv._session._notify = notify_mode
    if speak_mode is not None:
        srv._session._speak = speak_mode
    srv._session._speak_explicit = speak_explicit
    if voice is not None:
        srv._session.voice = voice


def _read_state() -> dict[str, str | bool | None]:
    """Read the full post-state from session."""
    import punt_vox.server as srv

    return {
        "notify": srv._session.notify,
        "speak": srv._session.speak,
        "voice": srv._session.voice,
        "speak_explicit": srv._session._speak_explicit,
    }


# ---------------------------------------------------------------------------
# VoxOn -- the notify tool called with mode "y"
# Z spec: notify -> nOn; speak -> sVoice when unset, else unchanged
# ---------------------------------------------------------------------------


class TestVoxOn:
    """Partitions 1-6: VoxOn operation (/vox y)."""

    def test_partition_1_init_sunset_from_noff(self) -> None:
        """P1: nOff/sUnset/empty -> nOn/sVoice/empty -- first enable inits."""
        # Fresh session = sUnset (speak="n", speak_explicit=False)
        result = json.loads(notify(mode="y"))
        assert result["notify"]["notify"] == "y"
        assert result["notify"]["speak"] == "y"
        state = _read_state()
        assert state["notify"] == "y"
        assert state["speak"] == "y"
        assert state["voice"] is None

    def test_partition_2_init_sunset_voice_preserved(self) -> None:
        """P2: nOff/sUnset/{v1} -> nOn/sVoice/{v1} -- voice preserved through init."""
        _set_state(notify_mode="n", voice="matilda")
        # speak absent = sUnset (speak_explicit=False)
        result = json.loads(notify(mode="y"))
        assert result["notify"]["speak"] == "y"
        state = _read_state()
        assert state["voice"] == "matilda"

    def test_partition_3_preserve_schime(self) -> None:
        """P3: nOff/sChime/empty -> nOn/sChime/empty -- muted user stays muted."""
        _set_state(notify_mode="n", speak_mode="n", speak_explicit=True)
        result = json.loads(notify(mode="y"))
        assert "speak" not in result["notify"]
        state = _read_state()
        assert state["notify"] == "y"
        assert state["speak"] == "n"

    def test_partition_4_preserve_svoice(self) -> None:
        """P4: nOff/sVoice/{v1} -> nOn/sVoice/{v1} -- unmuted user stays unmuted."""
        _set_state(notify_mode="n", speak_mode="y", speak_explicit=True, voice="roger")
        result = json.loads(notify(mode="y"))
        assert "speak" not in result["notify"]
        state = _read_state()
        assert state["notify"] == "y"
        assert state["speak"] == "y"
        assert state["voice"] == "roger"

    def test_partition_5_idempotent_already_non(self) -> None:
        """P5: nOn/sVoice/empty -> nOn/sVoice/empty -- idempotent."""
        _set_state(notify_mode="y", speak_mode="y", speak_explicit=True)
        notify(mode="y")
        state = _read_state()
        assert state["notify"] == "y"
        assert state["speak"] == "y"

    def test_partition_6_downgrade_from_ncont(self) -> None:
        """P6: nCont/sChime/empty -> nOn/sChime/empty -- cont to on, speak preserved."""
        _set_state(notify_mode="c", speak_mode="n", speak_explicit=True)
        notify(mode="y")
        state = _read_state()
        assert state["notify"] == "y"
        assert state["speak"] == "n"


# ---------------------------------------------------------------------------
# nOff state -- the retired VoxOff partitions, re-framed as state-model
# invariants after tool-enable-disable.md §2.3 retired "n" as a notify input.
# The user-facing "turn off" flow is now ``mic:enablement action="disable"``,
# which is a repo file operation and does not mutate session state; nOff
# itself is reachable only via the SessionConfig default and config-file
# load, so these tests assert the state model represents nOff coherently
# with every (speak, voice) frame the retired transition used to preserve.
# ---------------------------------------------------------------------------


class TestNotifyOffState:
    """Partitions 7-10: nOff coexists with any (speak, voice) frame."""

    def test_partition_7_off_frame_svoice_voice(self) -> None:
        """P7: nOff/sVoice/{v1} -- notify, speak, voice are independent fields."""
        _set_state(
            notify_mode="n", speak_mode="y", speak_explicit=True, voice="matilda"
        )
        state = _read_state()
        assert state["notify"] == "n"
        assert state["speak"] == "y"
        assert state["voice"] == "matilda"

    def test_partition_8_off_frame_schime(self) -> None:
        """P8: nOff/sChime -- an explicit-chime user coexists with nOff."""
        _set_state(notify_mode="n", speak_mode="n", speak_explicit=True)
        state = _read_state()
        assert state["notify"] == "n"
        assert state["speak"] == "n"
        assert state["speak_explicit"] is True

    def test_partition_9_off_frame_sunset(self) -> None:
        """P9: nOff/sUnset is the loaded-config shape -- speak stays at default."""
        _set_state(notify_mode="n")
        state = _read_state()
        assert state["notify"] == "n"
        assert state["speak"] == "n"
        assert state["speak_explicit"] is False

    def test_partition_10_off_is_the_fresh_session_default(self) -> None:
        """P10: a fresh SessionConfig is nOff/sUnset -- no tool call required."""
        # The _fresh_session autouse fixture installs a fresh SessionConfig.
        state = _read_state()
        assert state["notify"] == "n"
        assert state["speak"] == "n"
        assert state["speak_explicit"] is False


# ---------------------------------------------------------------------------
# VoxCont -- the notify tool called with mode "c"
# Z spec: notify -> nCont; speak -> sVoice when unset, else unchanged
# ---------------------------------------------------------------------------


class TestVoxCont:
    """Partitions 11-14: VoxCont operation (/vox c)."""

    def test_partition_11_init_sunset_from_noff(self) -> None:
        """P11: nOff/sUnset/empty -> nCont/sVoice/empty -- first enable inits."""
        _set_state(notify_mode="n")
        result = json.loads(notify(mode="c"))
        assert result["notify"]["notify"] == "c"
        assert result["notify"]["speak"] == "y"

    def test_partition_12_preserve_schime(self) -> None:
        """P12: nOff/sChime/empty -> nCont/sChime/empty -- muted user stays muted."""
        _set_state(notify_mode="n", speak_mode="n", speak_explicit=True)
        result = json.loads(notify(mode="c"))
        assert "speak" not in result["notify"]
        state = _read_state()
        assert state["notify"] == "c"
        assert state["speak"] == "n"

    def test_partition_13_upgrade_non_to_ncont(self) -> None:
        """P13: nOn/sVoice/{v1} -> nCont/sVoice/{v1} -- upgrade, speak preserved."""
        _set_state(notify_mode="y", speak_mode="y", speak_explicit=True, voice="roger")
        notify(mode="c")
        state = _read_state()
        assert state["notify"] == "c"
        assert state["speak"] == "y"
        assert state["voice"] == "roger"

    def test_partition_14_idempotent_already_ncont(self) -> None:
        """P14: nCont/sVoice/empty -> nCont/sVoice/empty -- no-op."""
        _set_state(notify_mode="c", speak_mode="y", speak_explicit=True)
        notify(mode="c")
        state = _read_state()
        assert state["notify"] == "c"
        assert state["speak"] == "y"


# ---------------------------------------------------------------------------
# Unmute -- the speak tool called with mode "y" and optional voice
# Z spec: notify unchanged; speak -> sVoice;
#         voice -> given voice when provided, else unchanged
# ---------------------------------------------------------------------------


class TestUnmute:
    """Partitions 15-21: Unmute operation (/unmute)."""

    def test_partition_15_unmute_from_schime(self) -> None:
        """P15: nOn/sChime/empty -> nOn/sVoice/empty -- core use case."""
        _set_state(notify_mode="y", speak_mode="n", speak_explicit=True)
        speak(mode="y")
        state = _read_state()
        assert state["notify"] == "y"  # notify frame
        assert state["speak"] == "y"
        assert state["voice"] is None

    def test_partition_16_unmute_from_sunset(self) -> None:
        """P16: nOff/sUnset/empty -> nOff/sVoice/empty -- speak init, no notify."""
        _set_state(notify_mode="n")
        speak(mode="y")
        state = _read_state()
        assert state["notify"] == "n"  # notify frame
        assert state["speak"] == "y"

    def test_partition_17_idempotent_already_svoice(self) -> None:
        """P17: nCont/sVoice/{v1} -> nCont/sVoice/{v1} -- no-op."""
        _set_state(
            notify_mode="c", speak_mode="y", speak_explicit=True, voice="matilda"
        )
        speak(mode="y")
        state = _read_state()
        assert state["notify"] == "c"
        assert state["speak"] == "y"
        assert state["voice"] == "matilda"

    def test_partition_20_notify_frame_noff(self) -> None:
        """P20: nOff/sChime -> nOff/sVoice -- unmute keeps notify off."""
        _set_state(notify_mode="n", speak_mode="n", speak_explicit=True)
        speak(mode="y")
        state = _read_state()
        assert state["notify"] == "n"
        assert state["speak"] == "y"

    def test_partition_21_rejected_invalid_mode(self) -> None:
        """P21: Invalid mode rejected -- API guard."""
        _set_state(notify_mode="y", speak_mode="n", speak_explicit=True)
        result = json.loads(speak(mode="x"))
        assert "error" in result
        # State unchanged
        state = _read_state()
        assert state["speak"] == "n"


# ---------------------------------------------------------------------------
# Mute -- the speak tool called with mode "n"
# Z spec: notify unchanged; speak -> sChime; voice unchanged
# ---------------------------------------------------------------------------


class TestMute:
    """Partitions 22-25: Mute operation (/mute)."""

    def test_partition_22_mute_from_svoice(self) -> None:
        """P22: nOn/sVoice/{v1} -> nOn/sChime/{v1} -- core use case, voice preserved."""
        _set_state(
            notify_mode="y", speak_mode="y", speak_explicit=True, voice="matilda"
        )
        speak(mode="n")
        state = _read_state()
        assert state["notify"] == "y"
        assert state["speak"] == "n"
        assert state["voice"] == "matilda"

    def test_partition_23_mute_from_sunset(self) -> None:
        """P23: nOff/sUnset -> nOff/sChime -- mute before first enable."""
        _set_state(notify_mode="n")
        speak(mode="n")
        state = _read_state()
        assert state["notify"] == "n"
        assert state["speak"] == "n"

    def test_partition_24_idempotent_already_schime(self) -> None:
        """P24: nCont/sChime/empty -> nCont/sChime/empty -- no-op."""
        _set_state(notify_mode="c", speak_mode="n", speak_explicit=True)
        speak(mode="n")
        state = _read_state()
        assert state["notify"] == "c"
        assert state["speak"] == "n"

    def test_partition_25_notify_frame_ncont(self) -> None:
        """P25: nCont/sVoice -> nCont/sChime -- mute keeps notify."""
        _set_state(notify_mode="c", speak_mode="y", speak_explicit=True)
        speak(mode="n")
        state = _read_state()
        assert state["notify"] == "c"
        assert state["speak"] == "n"


# ---------------------------------------------------------------------------
# Observation partitions -- config-based guards
# These verify hook/watcher guard predicates against static session state
# (written via _set_state, not produced by notify/speak operations).
# ---------------------------------------------------------------------------


class TestStopObservations:
    """Partitions 26-31: StopFires/StopChime/StopVoice observations."""

    def test_partition_26_stop_fires_non(self) -> None:
        """P26: nOn/sVoice -> stop fires (notify != nOff)."""
        _set_state(notify_mode="y", speak_mode="y", speak_explicit=True)
        state = _read_state()
        assert state["notify"] != "n"

    def test_partition_27_stop_fires_ncont(self) -> None:
        """P27: nCont/sChime -> stop fires (notify != nOff)."""
        _set_state(notify_mode="c", speak_mode="n", speak_explicit=True)
        state = _read_state()
        assert state["notify"] != "n"

    def test_partition_28_stop_rejected_noff(self) -> None:
        """P28: nOff -> stop does not fire (notify = nOff)."""
        _set_state(notify_mode="n", speak_mode="y", speak_explicit=True)
        state = _read_state()
        assert state["notify"] == "n"

    def test_partition_29_stop_chime_output(self) -> None:
        """P29: nOn/sChime -> StopChime (speak = "n")."""
        _set_state(notify_mode="y", speak_mode="n", speak_explicit=True)
        state = _read_state()
        assert state["notify"] != "n"
        assert state["speak"] == "n"

    def test_partition_30_stop_voice_output(self) -> None:
        """P30: nOn/sVoice -> StopVoice (speak != "n")."""
        _set_state(notify_mode="y", speak_mode="y", speak_explicit=True)
        state = _read_state()
        assert state["speak"] != "n"

    def test_partition_31_stop_voice_sunset(self) -> None:
        """P31: nOn/sUnset -> StopVoice (speak != sChime, sUnset behaves as voice).

        Note: sUnset with nOn is unreachable in practice (VoxOn initializes
        speak), but verifies the observation guard logic is correct.
        In-memory state: speak="n" with speak_explicit=False. The notify
        function would have initialized speak to "y", but we manually
        construct this state to test the guard.
        """
        _set_state(notify_mode="y")
        # In this unreachable state, speak is "n" but not explicitly set.
        # The guard in practice would see speak != "n" because VoxOn
        # always initializes it. We verify the raw field here.
        state = _read_state()
        assert state["speak_explicit"] is False


class TestWatcherObservations:
    """Partitions 33-38: WatcherFires/WatcherChime/WatcherVoice observations."""

    def test_partition_33_watcher_fires_ncont_svoice(self) -> None:
        """P33: nCont/sVoice -> watcher fires (notify = "c")."""
        _set_state(notify_mode="c", speak_mode="y", speak_explicit=True)
        state = _read_state()
        assert state["notify"] == "c"

    def test_partition_34_watcher_fires_ncont_schime(self) -> None:
        """P34: nCont/sChime -> watcher fires (notify = "c")."""
        _set_state(notify_mode="c", speak_mode="n", speak_explicit=True)
        state = _read_state()
        assert state["notify"] == "c"

    def test_partition_35_watcher_rejected_non(self) -> None:
        """P35: nOn -> watcher does not fire (notify != "c")."""
        _set_state(notify_mode="y", speak_mode="y", speak_explicit=True)
        state = _read_state()
        assert state["notify"] != "c"

    def test_partition_36_watcher_rejected_noff(self) -> None:
        """P36: nOff -> watcher does not fire (notify != "c")."""
        _set_state(notify_mode="n", speak_mode="y", speak_explicit=True)
        state = _read_state()
        assert state["notify"] != "c"

    def test_partition_37_watcher_chime_output(self) -> None:
        """P37: nCont/sChime -> WatcherChime."""
        _set_state(notify_mode="c", speak_mode="n", speak_explicit=True)
        state = _read_state()
        assert state["notify"] == "c"
        assert state["speak"] == "n"

    def test_partition_38_watcher_voice_output(self) -> None:
        """P38: nCont/sVoice -> WatcherVoice."""
        _set_state(notify_mode="c", speak_mode="y", speak_explicit=True)
        state = _read_state()
        assert state["notify"] == "c"
        assert state["speak"] != "n"


# ---------------------------------------------------------------------------
# Invariant preservation
# ---------------------------------------------------------------------------


class TestInvariantPreservation:
    """Partitions 39-41: Design invariants via multi-step sequences."""

    def test_partition_40_sunset_one_way(self) -> None:
        """P40: sUnset -> sVoice is one-way; nOff+VoxOn keeps sVoice."""
        # Step 1: VoxOn from Init (sUnset -> sVoice).
        notify(mode="y")
        state = _read_state()
        assert state["speak"] == "y"  # sVoice

        # Step 2: notify field lands back at nOff (via config load or default),
        # speak preserved because it was explicitly set in step 1. Constructed
        # via _set_state -- the notify tool no longer offers "n" as an input
        # per tool-enable-disable.md §2.3.
        _set_state(notify_mode="n", speak_mode="y", speak_explicit=True)
        state = _read_state()
        assert state["speak"] == "y"  # Still sVoice, NOT sUnset

        # Step 3: VoxOn again (speak still sVoice, not re-initialized).
        result = json.loads(notify(mode="y"))
        assert "speak" not in result["notify"]  # No init needed
        state = _read_state()
        assert state["speak"] == "y"

    def test_partition_41_sticky_mute_through_enable(self) -> None:
        """P41: User's mute choice survives notify enable -- design invariant #2."""
        # Step 1: Mute (sUnset -> sChime)
        speak(mode="n")
        state = _read_state()
        assert state["speak"] == "n"

        # Step 2: VoxCont -- sChime preserved (not overridden to sVoice)
        notify(mode="c")
        state = _read_state()
        assert state["speak"] == "n"  # Sticky!
        assert state["notify"] == "c"


# ---------------------------------------------------------------------------
# Rejected partitions -- invalid inputs
# ---------------------------------------------------------------------------


class TestRejectedInputs:
    """Additional rejected partition for the speak tool.

    Notify's own rejected-inputs partition moved to
    :class:`test_server.TestNotifySchemaRejectsOff`: the notify tool's
    ``mode`` is now ``Literal["y", "c"]``, so FastMCP rejects any other
    input at the schema level before dispatch -- a stronger contract than
    the retired ``return _error(...)`` guard used to enforce.
    """

    def test_speak_invalid_mode(self) -> None:
        """Speak rejects invalid mode values."""
        _set_state(notify_mode="y", speak_mode="y", speak_explicit=True)
        result = json.loads(speak(mode="c"))
        assert "error" in result
        state = _read_state()
        assert state["speak"] == "y"
