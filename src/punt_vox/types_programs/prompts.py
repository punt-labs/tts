"""Shared music-pool prompt value object -- importable by client and daemon alike.

Genre understanding lives in the calling agent (an LLM), never in vox. The agent
authors a base prompt plus one literal, genre-accurate variation per pool slot
and hands them to voxd; vox composes ``base + variation[i]`` for track ``i`` and
pipes the string to the provider. When no agent is in the loop (a hook-driven
vibe change), the pool falls back to a minimal literal prompt that names the
genre and mood and nothing else -- no "background music for deep work"
boilerplate, which homogenized every genre into the same mellow bed.

This module is dependency-free (stdlib only) so the lightweight ``client`` layer
can bundle prompts into a :class:`PromptSet` without importing the daemon's
music subsystem.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Self, cast

__all__ = ["POOL_SIZE", "PromptSet"]

POOL_SIZE = 12


@dataclass(frozen=True, slots=True)
class PromptSet:
    """A pool's generation prompts: an agent base plus per-track variations.

    ``variations`` is empty for a fallback set (every track uses ``base``) and
    holds ``POOL_SIZE`` entries for an agent set (track ``i`` uses variation
    ``i``). Frozen and hashable, so it can key a ``FillTarget``: swapping in
    fresh agent prompts changes the target and restarts the background fill.
    """

    base: str
    variations: tuple[str, ...]

    @classmethod
    def from_wire(cls, msg: object) -> Self | None:
        """Return the agent's prompt set parsed from a wire message, or None.

        A missing, JSON-null, or empty ``base_prompt`` (or ``variations``) is
        normalised to ``None`` so this shares the one validation funnel in
        :meth:`from_tool_args` (DRY): a half-supplied pair raises, both absent
        falls back to a minimal prompt, and a null base never seeds garbage.

        Reject a non-object payload with ``ValueError`` -- valid JSON that is a
        list, string, or number (``echo '[1,2]' | vox music on``) would otherwise
        fail ``.get`` with an ``AttributeError``. The parameter is ``object`` so
        this owns the type check at the wire boundary rather than trusting the
        caller's ``json.loads`` result.
        """
        if not isinstance(msg, Mapping):
            detail = "pool must be a JSON object with base_prompt and variations"
            raise ValueError(detail)
        # isinstance narrows to Mapping[Unknown, Unknown]; restore the wire shape.
        mapping = cast("Mapping[str, object]", msg)
        raw_base = mapping.get("base_prompt")
        raw_vars = mapping.get("variations")
        variations = (
            [str(v) for v in cast("list[object]", raw_vars)]
            if isinstance(raw_vars, list) and raw_vars
            else None
        )
        return cls.from_tool_args(str(raw_base) if raw_base else None, variations)

    @classmethod
    def from_tool_args(
        cls, base_prompt: str | None, variations: list[str] | None
    ) -> Self | None:
        """Build a prompt set from the MCP tool's args, or None when both unset.

        Raises ``ValueError`` (via :meth:`from_agent`) on a malformed shape, so
        the tool surfaces the error at the MCP boundary rather than the daemon.
        """
        if base_prompt is None and variations is None:
            return None
        return cls.from_agent(base_prompt or "", variations or [])

    @classmethod
    def from_agent(cls, base: str, variations: Sequence[str]) -> Self:
        """Build a validated agent prompt set: a base plus ``POOL_SIZE`` variations.

        Raise ``ValueError`` on any shape the agent got wrong -- an empty base, a
        wrong variation count, or a blank variation -- so the tool surfaces a
        clear message rather than silently generating a degenerate pool.
        """
        clean_base = base.strip()
        if not clean_base:
            msg = "base_prompt must be a non-empty string"
            raise ValueError(msg)
        cleaned = tuple(v.strip() for v in variations)
        if len(cleaned) != POOL_SIZE:
            msg = f"variations must be exactly {POOL_SIZE} entries, got {len(cleaned)}"
            raise ValueError(msg)
        if any(not v for v in cleaned):
            msg = "every variation must be a non-empty string"
            raise ValueError(msg)
        return cls(base=clean_base, variations=cleaned)

    @classmethod
    def single(cls, prompt: str) -> Self:
        """Return a one-track set: the verbatim prompt as base, no variations.

        The authored-input builder for ``music new``. Kept distinct from
        :meth:`fallback` because ``new`` sends the prompt **untouched** -- no
        ``". instrumental, loopable."`` decoration -- while still being the same
        :class:`PromptSet` shape ``on`` produces, so both verbs author one object.
        """
        clean = prompt.strip()
        if not clean:
            msg = "prompt must be a non-empty string"
            raise ValueError(msg)
        return cls(base=clean, variations=())

    @classmethod
    def fallback(cls, style: str, mood: str) -> Self:
        """Build the minimal literal fallback for a pool with no agent prompts.

        Shape: ``"<style> music, <mood>. instrumental, loopable."`` -- genre
        first, no generic deep-work boilerplate. An empty style becomes
        ``ambient``; an empty mood is dropped.
        """
        genre = f"{style.strip()} music" if style.strip() else "ambient music"
        head = f"{genre}, {mood.strip()}" if mood.strip() else genre
        return cls(base=f"{head}. instrumental, loopable.", variations=())

    def prompt_for(self, index: int) -> str:
        """Return the final prompt for track ``index``.

        A fallback set (no variations) returns ``base`` for every track. An agent
        set composes ``base`` with the ``index``-th variation so the pool spans
        the agent's genre-accurate descriptions.
        """
        if not self.variations:
            return self.base
        return f"{self.base} {self.variations[index % len(self.variations)]}"
