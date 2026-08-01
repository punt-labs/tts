"""The mpv ``end-file`` reason -- how a loaded part stopped.

Playing a part on the persistent mpv process ends with an ``end-file`` event,
discriminated by its reason. The first five are mpv's own; ``CRASHED`` is
synthetic -- mpv emits no ``end-file`` when it dies, so the connection reader
injects this reason into the loop's ended-future on socket EOF, making the one
channel the loop awaits carry a crash too (the I7 loop-liveness guarantee).

This enum lives in its own module because it is the program tier's most widely
imported value: the loop, the interrupt race, the player, and the connection all
name it, and none of them should have to import the command/response/event wire
types alongside it.
"""

from __future__ import annotations

from enum import StrEnum

__all__ = ["EndFileReason"]


class EndFileReason(StrEnum):
    """Why a loaded part stopped -- the discriminant of an ``end-file`` event."""

    EOF = "eof"  # natural end of the loaded part -- the loop advances
    STOP = "stop"  # a deliberate ``stop`` command -- teardown, do not advance
    REDIRECT = "redirect"  # a ``loadfile`` replace displaced it -- do not advance
    QUIT = "quit"  # mpv is quitting (shutdown) -- do not advance
    ERROR = "error"  # a bad/corrupt file -- record a per-part fault, advance past it
    CRASHED = "crashed"  # SYNTHETIC: the process died (socket EOF), never an mpv event

    @property
    def advances(self) -> bool:
        """Return whether this reason drives the loop to the next part.

        Only a natural end (``eof``) or a bad file (``error``, advance past it)
        moves the cursor. A deliberate teardown (``stop``/``redirect``/``quit``)
        or a crash never advances -- the loop replays or idles instead.
        """
        return self in (EndFileReason.EOF, EndFileReason.ERROR)
