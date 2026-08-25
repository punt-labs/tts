"""A fake PortAudio input stream for :mod:`.mic_audio_source`.

No real audio hardware, no ``sounddevice`` C extension calls -- the
callback :class:`MicAudioSource` registers is invoked directly by
:meth:`FakeInputStream.feed`, the same way a real PortAudio callback thread
would invoke it, but synchronously and under the test's control.
"""

from __future__ import annotations

from typing import Self, final

from punt_vox.voxd.conversation_mode.mic_audio_source import InputStreamFactory

__all__ = ["FakeCallbackFlags", "FakeInputStream", "fake_input_stream_factory"]


@final
class FakeCallbackFlags:
    """Stand-in for :class:`sounddevice.CallbackFlags`."""

    __slots__ = ("_input_overflow",)
    _input_overflow: bool

    def __new__(cls, *, input_overflow: bool = False) -> Self:
        self = super().__new__(cls)
        self._input_overflow = input_overflow
        return self

    @property
    def input_overflow(self) -> bool:
        return self._input_overflow


@final
class FakeInputStream:
    """Records construction kwargs; :meth:`feed` drives the registered callback."""

    __slots__ = ("_callback", "_closed", "_kwargs")
    _callback: object
    _kwargs: dict[str, object]
    _closed: bool

    def __new__(cls, **kwargs: object) -> Self:
        self = super().__new__(cls)
        self._kwargs = kwargs
        self._callback = kwargs["callback"]
        self._closed = False
        return self

    @property
    def closed(self) -> bool:
        """Return whether ``__exit__`` has run -- the device was released."""
        return self._closed

    @property
    def kwargs(self) -> dict[str, object]:
        """Return the keyword arguments the factory was constructed with."""
        return dict(self._kwargs)

    def feed(self, pcm: bytes, *, input_overflow: bool = False) -> None:
        """Invoke the registered callback with *pcm*, as PortAudio's thread would."""
        callback = self._callback
        assert callable(callback)
        flags = FakeCallbackFlags(input_overflow=input_overflow)
        callback(pcm, len(pcm) // 2, None, flags)

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self._closed = True


def fake_input_stream_factory(created: list[FakeInputStream]) -> InputStreamFactory:
    """Return a factory that records every :class:`FakeInputStream` it makes.

    *created* accumulates streams in construction order, so a test can
    reach the most recent one to :meth:`~FakeInputStream.feed` it once the
    generator under test has reached its first ``await``.
    """

    def factory(**kwargs: object) -> FakeInputStream:
        stream = FakeInputStream(**kwargs)
        created.append(stream)
        return stream

    return factory
