"""``vox provider`` -- list or set the TTS provider.

The command is a callable object (:class:`ProviderCommand`) so both
surfaces share one instance and the ratchet counts the class-per-command
shape the rest of vox uses (see
:class:`~punt_vox.server_switches.ProviderTool`).
"""

from __future__ import annotations

from typing import Self, final

from punt_vox.commands._result import CommandResult, Ctx, SwitchList
from punt_vox.server_switches import PROVIDER_NAMES


@final
class ProviderCommand:
    """List or set the TTS provider."""

    __slots__ = ()

    def __new__(cls) -> Self:
        return super().__new__(cls)

    async def __call__(self, ctx: Ctx, name: str | None = None) -> CommandResult:
        """List the five providers, or write a new one to the session config.

        No arg: list ``elevenlabs``, ``openai``, ``polly``, ``say``, ``espeak``,
        marking the current selection.

        Name given: validate against the closed enum and write to
        ``.punt-labs/vox/vox.md``. On a genuine provider change the stale
        model is cleared in the same write -- model names are
        provider-scoped, so ``eleven_v3`` reaching an OpenAI request is an
        invalid API call.
        """
        cfg = ctx.store.read()

        if name is None:
            listing = SwitchList(names=PROVIDER_NAMES, current=cfg.provider)
            return CommandResult(text=listing.render(), json_data=listing.payload())

        if name not in PROVIDER_NAMES:
            allowed = ", ".join(PROVIDER_NAMES)
            message = f"unknown provider {name!r}. Allowed: {allowed}"
            return CommandResult(
                text=message,
                json_data={"error": message},
                error=True,
                exit_code=1,
            )

        updates: dict[str, str] = {"provider": name}
        if cfg.provider != name:
            updates["model"] = ""
        ctx.store.write_fields(updates)
        return CommandResult(text=f"Provider: {name}", json_data={"provider": name})


provider: ProviderCommand = ProviderCommand()
