"""The element-level difference between two renders of one scene.

A :class:`SceneTree` is a render request flattened to its id-addressed elements,
in tree order. Diffing two of them yields a :class:`ScenePatchSet` -- the ordered
:class:`ElementPatch`es that carry the newer render's changed fields onto the
installed one.

Three properties of luxd's patch seam shape the whole module, and each is load-
bearing rather than incidental:

* **A patch sets fields on an existing element or removes it; it cannot add one.**
  So a render whose element roster differs from the installed one is not
  expressible as a patch at all -- :meth:`SceneTree.patchable_against` answers
  that question, and the caller re-installs when it says no.
* **``children`` and ``tabs`` are refused by the seam.** Containers are therefore
  *descended through*, never compared: a group whose child button changed yields
  a patch addressed to the button's own id.
* **Setters run in the order the fields arrive.** The changed fields keep the
  order the current render emitted them in, which the table depends on:
  ``table._set_selected_row_ids`` intersects the ids against the rows *as they
  stand at setter time*, so ``rows`` must precede ``selected_row_ids`` or the new
  ids are silently dropped. ``combo`` is the asymmetric case -- it validates
  ``selected`` against ``items`` after the whole batch -- so only the table needs
  the guarantee, but the ordering is preserved for every element uniformly.

There is deliberately no allowlist of patchable field names per element kind: a
hand-copied list of luxd's setters would be a second source of truth that drifts.
A field luxd cannot set produces a rejection that mutates nothing, and the caller
re-installs -- self-correcting beats synchronized.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import TYPE_CHECKING, Final, Self, cast, final

if TYPE_CHECKING:
    from collections.abc import Iterator, Sequence

    from punt_lux import RenderRequest

__all__ = ["ElementPatch", "ScenePatchSet", "SceneTree"]

# Structure, not state: ``kind`` and ``id`` identify the element, and the two
# container fields are descended into rather than set (luxd refuses both).
_STRUCTURAL_FIELDS: Final = frozenset({"kind", "id", "children", "tabs"})
_CONTAINER_FIELDS: Final = ("children", "tabs")
# Stands in for a node that is not an element wire dict at all. It carries no id,
# so it counts as unidentified and forces a re-install rather than being dropped.
_OPAQUE: Final[Mapping[str, object]] = MappingProxyType({})


@final
@dataclass(frozen=True, slots=True)
class ElementPatch:
    """One element's changed fields, in the order the current render emitted them."""

    element_id: str
    # PY-TS-14: wire boundary -- element field values are open per kind and are
    # coerced by luxd's own setters, so the value type stays ``object``.
    fields: Mapping[str, object]

    def to_wire(self) -> dict[str, object]:
        """Return the ``{"id", "set"}`` wire entry luxd's patch batch consumes."""
        return {"id": self.element_id, "set": dict(self.fields)}


@final
@dataclass(frozen=True, slots=True)
class ScenePatchSet:
    """The ordered element patches taking an installed scene to a newer render."""

    patches: tuple[ElementPatch, ...]

    def to_wire(self) -> list[dict[str, object]]:
        """Return the patch entries in tree order, ready for ``UpdateRequest``."""
        return [patch.to_wire() for patch in self.patches]

    def __len__(self) -> int:
        """Return how many elements this set patches (zero means nothing changed)."""
        return len(self.patches)


@final
@dataclass(frozen=True, slots=True)
class SceneTree:
    """One render's elements flattened to ``(id, wire dict)`` pairs, in tree order.

    An element carrying no string ``id`` -- a tab wrapper, say -- cannot be
    addressed by a patch, so it is descended into but never registered, and its
    presence makes the whole tree unpatchable. vox's own scenes give every element
    an id, so that guard never fires for them; it exists so a scene that grows an
    unaddressable element re-installs rather than silently skipping it.
    """

    elements: tuple[tuple[str, Mapping[str, object]], ...]
    unidentified: int

    @classmethod
    def of(cls, request: RenderRequest) -> Self:
        """Flatten ``request``'s element tree, descending through its containers."""
        found = tuple(cls._walk(request.elements))
        identified = tuple(pair for pair in found if pair[0])
        return cls(identified, len(found) - len(identified))

    @property
    def ids(self) -> tuple[str, ...]:
        """Return the addressable element ids in tree order."""
        return tuple(element_id for element_id, _ in self.elements)

    def patchable_against(self, previous: SceneTree) -> bool:
        """Return whether a patch can express the step from ``previous`` to here.

        It can only when both trees are wholly addressable, carry the same ids in
        the same order, and give each element the same field names -- a field that
        appears in one render and vanishes in the other is a roster change the
        patch seam cannot express, so it re-installs instead.
        """
        if self.unidentified or previous.unidentified or self.ids != previous.ids:
            return False
        before = dict(previous.elements)
        return all(
            self._state_fields(element) == self._state_fields(before[element_id])
            for element_id, element in self.elements
        )

    def patches_against(self, previous: SceneTree) -> ScenePatchSet:
        """Return the patches carrying ``previous`` to this tree (elements aligned).

        The caller has already established :meth:`patchable_against`, so every id
        here is present there with the same field names; only the values differ.
        """
        before = dict(previous.elements)
        patches = (
            ElementPatch(element_id, changed)
            for element_id, element in self.elements
            if (changed := self._changed_fields(element, before[element_id]))
        )
        return ScenePatchSet(tuple(patches))

    @classmethod
    def _walk(
        cls, elements: Sequence[object]
    ) -> Iterator[tuple[str, Mapping[str, object]]]:
        """Yield each element as ``(id, element)``, descending through containers.

        A node with no string ``id`` -- or one that is not a wire dict at all --
        yields an empty id, which :meth:`of` counts as unidentified rather than
        registering, so the tree re-installs instead of skipping it.
        """
        for node in elements:
            if not isinstance(node, Mapping):
                yield "", _OPAQUE
                continue
            element = cast("Mapping[str, object]", node)
            element_id = element.get("id")
            yield (element_id if isinstance(element_id, str) else ""), element
            for field in _CONTAINER_FIELDS:
                nested = element.get(field)
                if isinstance(nested, list):
                    yield from cls._walk(cast("Sequence[object]", nested))

    @staticmethod
    def _state_fields(element: Mapping[str, object]) -> frozenset[str]:
        """Return the element's patchable field names -- its state, not its shape."""
        return frozenset(element) - _STRUCTURAL_FIELDS

    @classmethod
    def _changed_fields(
        cls, element: Mapping[str, object], previous: Mapping[str, object]
    ) -> dict[str, object]:
        """Return the fields whose value moved, in the current render's own order."""
        return {
            field: value
            for field, value in element.items()
            if field not in _STRUCTURAL_FIELDS and previous.get(field) != value
        }
