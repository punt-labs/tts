"""Tests for :class:`~punt_vox.commands.call_cues.DaemonCues`.

A structural fake stands in for :class:`~punt_vox.client_sync.VoxClientSync`
-- that class has no Protocol seam (it is the one concrete daemon client),
so the fake is cast to it, the same substitution pattern
``tests/test_call_live_driver.py`` already uses for ``MicAudioSource``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

from punt_vox.commands import call_cues as call_cues_module
from punt_vox.commands.call_cues import DaemonCues
from punt_vox.types_synthesis import SynthesisSpec

if TYPE_CHECKING:
    from punt_vox.client_sync import VoxClientSync


class _FakeVoxClientSync:
    """Records every ``synthesize``/``chime`` call; no daemon, no network."""

    def __init__(self) -> None:
        self.synthesize_calls: list[tuple[str, SynthesisSpec, float | None]] = []
        self.chime_calls: list[str] = []

    def synthesize(
        self,
        text: str,
        spec: SynthesisSpec | None = None,
        *,
        once: int | None = None,
        timeout: float | None = None,
    ) -> None:
        del once
        assert spec is not None
        self.synthesize_calls.append((text, spec, timeout))

    def chime(self, signal: str) -> None:
        self.chime_calls.append(signal)


def _cues() -> tuple[DaemonCues, _FakeVoxClientSync]:
    client = _FakeVoxClientSync()
    spec = SynthesisSpec(voice="sarah")
    cues = DaemonCues(cast("VoxClientSync", client), spec)
    return cues, client


async def test_speak_synthesizes_the_given_text() -> None:
    cues, client = _cues()

    await cues.speak("hello")

    assert [call[0] for call in client.synthesize_calls] == ["hello"]


async def test_speak_uses_a_longer_timeout_than_the_client_default() -> None:
    """A long reply's real synthesis time can exceed the client's own 30s
    default -- DaemonCues.speak must pass a longer bound, not rely on it."""
    cues, client = _cues()

    await cues.speak("a long reply")

    (_text, _spec, timeout) = client.synthesize_calls[0]
    assert timeout == call_cues_module._SPEAK_TIMEOUT_S
    assert timeout is not None
    assert timeout > 30.0


async def test_chime_plays_the_acknowledge_asset() -> None:
    cues, client = _cues()

    await cues.chime()

    assert client.chime_calls == ["acknowledge"]
