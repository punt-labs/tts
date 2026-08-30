"""``ChoiceList`` -- what a combo offers and which entry it shows as chosen.

A Lux combo's ``selected`` is a total index: the protocol has no way to say
"nothing is chosen" (``ComboElement`` documents ``selected`` as deliberately
not optional). The panel's three combos each hold a selection that genuinely
can be absent -- a fresh repo has no provider, a modelless provider has no
model, an unread roster has no voice -- and each used to answer index ``0``
for that, so the display asserted the first entry was chosen while the daemon
held nothing. Re-picking the entry already highlighted fires no ``changed``
event, so the user could not even confirm the claim into truth.

This type gives the missing state an entry of its own: every non-empty list is
rendered with a leading ``(none)``, and ``selected`` points at it when the
current value is absent or names something the list does not contain -- so
index ``0`` means what it says. Resolving a click subtracts that entry back off
and refuses a click on ``(none)`` itself.

The sentinel is unconditional rather than shown only while unchosen, and that
is a correctness requirement, not a style choice: an index is picked from the
list as rendered and resolved against state read later, so a sentinel that
comes and goes shifts the whole mapping under a click already in flight.

Both halves live here on purpose. The offered list and the click resolver have
to agree about whether the sentinel is present, and they agreed only by
coincidence when each control open-coded its own copy: the model combo once
offered a provider-less session five ElevenLabs models that its own resolver
refused categorically.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final, final

__all__ = ["ChoiceList"]

_UNCHOSEN_ITEM: Final = "(none)"


@final
@dataclass(frozen=True, slots=True)
class ChoiceList:
    """One combo's items, its current value, and the wire view of both."""

    items: tuple[str, ...]
    current: str | None
    empty_label: str

    @property
    def is_empty(self) -> bool:
        """Return whether there is nothing to choose from at all.

        Distinct from having nothing chosen: an empty list means the combo
        should not publish, since no click on it could name anything.
        """
        return not self.items

    @property
    def _unchosen(self) -> bool:
        """Return whether ``current`` names no entry in the list.

        An unknown value counts as unchosen, not as entry zero. A typo in
        ``vox.md`` used to render the first entry as selected, so the panel
        showed a voice the config did not name and no error said otherwise.
        """
        return self.current is None or self.current not in self.items

    def wire_items(self) -> list[str]:
        """Return the item list the combo renders, sentinel included.

        The sentinel leads every non-empty list, whether or not anything is
        chosen. Showing it only while unchosen would make an index mean
        different things at different moments: a click carries an index
        picked from the list as it was *rendered*, and it is resolved
        against state read later. A refresh landing a provider in between
        would retire the sentinel and shift every index down by one, so a
        click on ``openai`` would commit ``polly`` -- a silently wrong
        write, not a refusal. A constant leading entry keeps the offset
        constant, which is the only version of this that cannot drift.
        """
        if self.is_empty:
            return [self.empty_label]
        return [_UNCHOSEN_ITEM, *self.items]

    def selected_index(self) -> int:
        """Return the index the combo shows as chosen.

        Zero whenever nothing is chosen -- which now points at a real entry
        saying so, rather than at the first genuine choice.
        """
        if self.is_empty or self._unchosen:
            return 0
        return self.items.index(self.current) + 1

    def name_for_index(self, index: int, *, noun: str) -> str:
        """Return the entry a clicked *index* names, or raise if it names none.

        Index ``0`` is the sentinel and names nothing: this panel offers no
        way to unset a provider, model or voice, so a click there is
        refused rather than quietly treated as the first real entry.

        *noun* is the caller's own singular word for one entry
        (``"voice"``), so a refusal reads in the caller's vocabulary rather
        than this type's, and agrees with its own count -- a list of one
        reads "1 voice", not "1 voices".
        """
        position = index - 1 if not self.is_empty else index
        if not 0 <= position < len(self.items):
            count = len(self.items)
            plural = noun if count == 1 else f"{noun}s"
            msg = f"{noun} combo: index {index} out of range for {count} {plural}"
            raise ValueError(msg)
        return self.items[position]
