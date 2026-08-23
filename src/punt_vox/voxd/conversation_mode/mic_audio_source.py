"""Live microphone capture, yielding :class:`AudioChunk` at a fixed cadence.

``sounddevice`` (PortAudio bindings) is the capture backend -- cross-platform
by construction, so there is no macOS/Linux dispatch here the way
``providers/say.py`` vs. ``providers/espeak.py`` need one for playback: both
platforms reach the same :class:`~sounddevice.RawInputStream` class, and
PortAudio itself resolves to CoreAudio or ALSA underneath. PortAudio's C
library is a separate runtime dependency from the Python package (Homebrew's
``portaudio`` on macOS, ``libportaudio2`` on most Linux distributions) --
the same shape as ``pydub``'s ffmpeg dependency, which this project already
accepts.

The callback PortAudio invokes runs on its own audio thread, never the event
loop's thread -- :meth:`MicAudioSource.chunks` bridges that with
``loop.call_soon_threadsafe`` onto an :class:`asyncio.Queue`, the same
pattern a WebSocket server uses to accept work from a non-asyncio thread.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Buffer
from typing import TYPE_CHECKING, Protocol, Self, final, runtime_checkable

from punt_vox.voxd.conversation_mode.audio_chunk import SAMPLE_RATE_HZ, AudioChunk

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    # Type-only: neither name exists on the real, stub-free ``sounddevice``
    # runtime module -- both are declared in ``typings/sounddevice/__init__.pyi``
    # for the type checkers only. Importing either unconditionally would raise
    # ``ImportError`` the moment this module is loaded.
    from sounddevice import CallbackFlags, RawInputCallback

logger = logging.getLogger(__name__)

__all__ = ["MicAudioSource"]

# The chunk size :class:`~.turn_detector.TurnDetector` was designed and
# tested against (its default ``silence_gap_s=0.2`` and the fixture chunks
# in ``tests/conversation_mode/test_turn_detector.py`` both assume roughly
# this granularity). Smaller would raise callback overhead for no detection
# benefit; larger would coarsen the detector's silence-gap timing.
_CHUNK_S = 0.02


@runtime_checkable
class InputStreamFactory(Protocol):
    """Constructs a :class:`~sounddevice.RawInputStream`-shaped context manager.

    The real default (:func:`_default_input_stream_factory`) constructs a
    genuine ``RawInputStream`` against the system microphone.
    ``tests/conversation_mode/test_mic_audio_source.py`` injects a fake that
    never touches audio hardware -- the sole reason this Protocol exists
    rather than calling ``RawInputStream`` directly.
    """

    def __call__(
        self,
        *,
        samplerate: float,
        blocksize: int,
        channels: int,
        dtype: str,
        callback: RawInputCallback,
    ) -> InputStreamHandle: ...


@runtime_checkable
class InputStreamHandle(Protocol):
    """The context-manager surface :class:`MicAudioSource` needs from a stream."""

    def __enter__(self) -> Self: ...

    def __exit__(self, *exc_info: object) -> None: ...


@final
class MicAudioSource:
    """Yields :class:`AudioChunk` from the system microphone, forever.

    :meth:`chunks` is an unbounded async generator -- a live call has no
    natural end to capture until the human hangs up or the call times out,
    both decided by :class:`~.call_session.CallSession`'s state machine, not
    by this class. A caller stops consuming (``break`` out of the ``async
    for``, or cancel the task) to close the stream; the ``with`` block's
    ``__exit__`` then releases the audio device.
    """

    __slots__ = (
        "_channels",
        "_chunk_s",
        "_input_stream_factory",
        "_queue",
        "_sample_rate_hz",
    )
    _sample_rate_hz: int
    _chunk_s: float
    _channels: int
    _input_stream_factory: InputStreamFactory
    # Set for the lifetime of one :meth:`chunks` call, ``None`` otherwise --
    # exposed as an attribute (rather than a local variable inside
    # :meth:`chunks`) solely so :meth:`drain_pending` can reach it from
    # outside the generator.
    _queue: asyncio.Queue[bytes] | None

    def __new__(
        cls,
        *,
        sample_rate_hz: int = SAMPLE_RATE_HZ,
        chunk_s: float = _CHUNK_S,
        input_stream_factory: InputStreamFactory | None = None,
    ) -> Self:
        self = super().__new__(cls)
        self._sample_rate_hz = sample_rate_hz
        self._chunk_s = chunk_s
        self._channels = 1
        self._input_stream_factory = (
            input_stream_factory or _default_input_stream_factory
        )
        self._queue = None
        return self

    async def chunks(self) -> AsyncGenerator[AudioChunk]:
        """Capture indefinitely, yielding one :class:`AudioChunk` per block.

        Every chunk carries ``duration_s=self._chunk_s`` regardless of the
        block's actual wall-clock arrival time -- the same caller-supplied-
        timing contract :class:`AudioChunk` documents, satisfied here
        because the block size is fixed and PortAudio delivers it at a
        steady cadence; a genuinely late callback (an overrun PortAudio
        reports via ``status.input_overflow``) is logged, not measured into
        a corrected duration, since a live call has no test harness to
        replay a corrected value against.
        """
        block_frames = max(1, round(self._chunk_s * self._sample_rate_hz))
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue[bytes] = asyncio.Queue()
        self._queue = queue

        def _callback(
            indata: Buffer, _frames: int, _time: object, status: CallbackFlags
        ) -> None:
            if status.input_overflow:
                logger.warning("mic capture: input overflow (callback ran late)")
            loop.call_soon_threadsafe(queue.put_nowait, bytes(indata))

        stream = self._input_stream_factory(
            samplerate=float(self._sample_rate_hz),
            blocksize=block_frames,
            channels=self._channels,
            dtype="int16",
            callback=_callback,
        )
        try:
            with stream:
                while True:
                    pcm = await queue.get()
                    yield AudioChunk(pcm=pcm, duration_s=self._chunk_s)
        finally:
            self._queue = None

    def drain_pending(self) -> int:
        """Discard every chunk queued but not yet yielded by :meth:`chunks`.

        The microphone keeps capturing while a caller is busy elsewhere --
        most importantly, while the agent's own reply plays through the
        speakers, which the microphone picks back up. Those self-captured
        chunks queue up during that window; without draining them, the next
        chunks :meth:`chunks` yields once the caller resumes consuming are a
        backlog of the agent's own voice, which a turn detector would
        process as if it were the human's next turn. A no-op (returns 0)
        before :meth:`chunks` has started or after it has finished.
        """
        queue = self._queue
        if queue is None:
            return 0
        drained = 0
        while not queue.empty():
            queue.get_nowait()
            drained += 1
        return drained

    async def capture_seconds(self, duration_s: float) -> list[AudioChunk]:
        """Capture *duration_s* of ambient audio, for
        :meth:`~.turn_detector.TurnDetector.calibrate`.

        FR-1's "a few seconds of 'say something'" calibration step: opens
        its own short-lived stream (via :meth:`chunks`) rather than sharing
        one with the call's main capture loop, so calibration finishes and
        releases the device before the main loop opens its own. Closes the
        generator explicitly on the way out -- breaking out of an ``async
        for`` does not itself run the generator's ``with`` block, only
        ``aclose()`` (implicit, nondeterministic GC finalization, or this
        explicit call) does, and the device must be released deterministically
        so the main capture loop's own stream can open cleanly right after.
        """
        count = max(1, round(duration_s / self._chunk_s))
        collected: list[AudioChunk] = []
        gen = self.chunks()
        try:
            async for chunk in gen:
                collected.append(chunk)
                if len(collected) >= count:
                    break
        finally:
            await gen.aclose()
        return collected


def _default_input_stream_factory(
    *,
    samplerate: float,
    blocksize: int,
    channels: int,
    dtype: str,
    callback: RawInputCallback,
) -> InputStreamHandle:
    """Construct a real :class:`~sounddevice.RawInputStream` on the system mic.

    ``RawInputStream`` (not ``InputStream``) -- its callback hands raw CFFI
    buffer objects instead of NumPy arrays, so this project does not need
    NumPy as a dependency just to read 16-bit PCM bytes out of a callback.

    Imports ``sounddevice`` here, not at module scope: ``sounddevice``
    requires the PortAudio C library, a separate runtime dependency this
    project does not otherwise need (it is declared as an optional extra,
    not a hard dependency -- see ``pyproject.toml``'s ``call`` extra). A
    module-scope import would make every ``vox`` invocation -- ``vox say``,
    ``vox status``, every hook -- fail on a host without PortAudio installed,
    even when nothing touches a live call. This is the only call site.
    """
    from sounddevice import RawInputStream

    return RawInputStream(
        samplerate=samplerate,
        blocksize=blocksize,
        channels=channels,
        dtype=dtype,
        callback=callback,
    )
