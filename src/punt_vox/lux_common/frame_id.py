"""``FrameId`` -- the frame id a render request resolves to, once it lands.

An unnamed frame -- or a named one with no explicit ``frame_id`` -- self-frames
by the scene id; ``punt_lux``'s ``RenderRequest`` applies exactly this rule when
it resolves the ``ScenePresentation`` it hands the Hub (``FrameSpec.frame_id``:
"None defaults to the scene id"), but that resolution is private to punt_lux.
A caller that wants to name the frame explicitly -- ``client.frame.raise_``
addresses a frame by id, not by scene -- needs the same rule on this side of the
wire, so it lives here once rather than duplicated at each call site that raises
a frame after installing one (DES-072 addendum).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Self, final

if TYPE_CHECKING:
    from punt_lux import RenderRequest

__all__ = ["FrameId"]


@final
class FrameId:
    """The frame id ``request`` resolves to display-side."""

    __slots__ = ("_value",)
    _value: str

    def __new__(cls, request: RenderRequest) -> Self:
        self = super().__new__(cls)
        named = request.frame.frame_id if request.frame is not None else None
        self._value = named if named is not None else request.scene_id
        return self

    def __str__(self) -> str:
        """Return the resolved id -- what a ``raise_frame`` call must name."""
        return self._value
