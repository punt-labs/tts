"""The ``PlaybackSink`` interface: a continuously-running audio output stream.

``docs/conversation-mode-call-state.tex`` section 9 names the shape this
Protocol formalizes: production playback should own a single, continuously
running output stream that can be *silenced by clearing its buffer
directly*, not by killing and restarting a per-segment subprocess (the
shape :class:`~punt_vox.voxd.playback.PlaybackQueue` has today, and remains
unchanged by this module -- see that section for why the two are
architecturally distinct).

A concrete sink (PortAudio, an OS mixer handle, a subprocess pipe -- Slice
2b's decision) is out of this module's scope; this Protocol only fixes the
three operations any such implementation must expose, and each operation's
contract:

* :meth:`write` appends synthesized audio to what is currently playing.
* :meth:`clear` silences whatever is buffered *right now*, without closing
  the stream -- the barge-in primitive.
* :meth:`close` releases the stream; no further operation is valid after.

None of the three is safe to call concurrently with another -- see
:class:`~.playback_sink_actor.PlaybackSinkActor` for the serialization
discipline every caller must go through instead of calling these directly.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from punt_vox.voxd.conversation_mode.sink_status import SinkStatus

__all__ = ["PlaybackSink"]


@runtime_checkable
class PlaybackSink(Protocol):
    """A continuous audio output stream, per ``docs/conversation-mode-call-state.tex``.

    Implementations are single-caller: exactly one logical operation
    (write, clear, or close) may be in flight at a time. Callers reach this
    contract only through :class:`~.playback_sink_actor.PlaybackSinkActor`,
    which is what makes "single caller" true in practice.
    """

    @property
    def status(self) -> SinkStatus:
        """Return the sink's current lifecycle state."""
        ...

    async def write(self, chunk: bytes) -> None:
        """Append *chunk* to the stream's output buffer.

        Raises if :attr:`status` is :attr:`~.sink_status.SinkStatus.CLOSED`.
        """
        ...

    async def clear(self) -> None:
        """Silence whatever is currently buffered; the stream stays open.

        Idempotent: clearing an already-idle sink is a no-op that leaves
        :attr:`status` at :attr:`~.sink_status.SinkStatus.IDLE`. Raises if
        :attr:`status` is :attr:`~.sink_status.SinkStatus.CLOSED`.
        """
        ...

    async def close(self) -> None:
        """Release the stream. Idempotent; safe to call on an already-closed sink."""
        ...
