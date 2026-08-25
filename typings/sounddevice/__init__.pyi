from __future__ import annotations

from collections.abc import Buffer, Callable
from typing import Self

# The subset of PortAudio's ``CallbackFlags`` this project inspects: whether
# capture overran the buffer between two callback invocations, which is a
# genuine capture-quality signal a caller may want to log or surface, not a
# fatal condition -- PortAudio keeps delivering audio after an overflow.
class CallbackFlags:
    input_overflow: bool

# The CFFI buffer object ``RawInputStream``'s callback receives as *indata*
# satisfies PEP 688's buffer protocol (``collections.abc.Buffer``, 3.12+);
# only that protocol is used here (``bytes(indata)``), so it is typed
# precisely rather than as ``Any``.
type RawInputCallback = Callable[[Buffer, int, object, CallbackFlags], None]

class RawInputStream:
    """PortAudio input stream on raw buffer objects -- no NumPy dependency.

    Only the constructor keyword arguments and the context-manager protocol
    this project actually calls are declared; the real class accepts more
    (finished_callback, clip_off, ...) that this project never passes.
    """

    def __init__(
        self,
        *,
        samplerate: float,
        blocksize: int,
        channels: int,
        dtype: str,
        callback: RawInputCallback,
    ) -> None: ...
    def __enter__(self) -> Self: ...
    def __exit__(self, *exc_info: object) -> None: ...
    def close(self) -> None: ...
