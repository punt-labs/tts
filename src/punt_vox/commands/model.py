"""``vox model`` -- list or set the TTS model for the current provider.

The command is a callable object (:class:`ModelCommand`) so both surfaces
share one instance and the ratchet counts the class-per-command shape the
rest of vox uses (see :class:`~punt_vox.server_switches.ModelTool`).
"""

from __future__ import annotations

from typing import Self, final

from punt_vox.commands._result import CommandResult, Ctx, SwitchList
from punt_vox.models import MODEL_TABLE, resolve_model


@final
class ModelCommand:
    """List or set the TTS model for the current provider."""

    __slots__ = ()

    def __new__(cls) -> Self:
        return super().__new__(cls)

    async def __call__(self, ctx: Ctx, name: str | None = None) -> CommandResult:
        """List models for the current provider, or resolve and set a new one.

        No arg: list the provider's available model names, marking the current
        selection (empty list means the provider is modelless -- Polly, say,
        espeak).

        Name given: resolve ElevenLabs shorthand (``v3`` -> ``eleven_v3``,
        etc.) and write the full name to ``.punt-labs/vox/vox.md``. Unknown
        or modelless-provider names return an error result; the CLI adapter
        exits with code 1.
        """
        cfg = ctx.store.read()
        provider = cfg.provider or "elevenlabs"

        if name is None:
            listing = SwitchList(
                names=MODEL_TABLE.available(provider), current=cfg.model
            )
            return CommandResult(
                text=listing.render("No models for this provider."),
                json_data=listing.payload(),
            )

        try:
            resolved = resolve_model(name, provider)
        except ValueError as exc:
            message = str(exc)
            return CommandResult(
                text=message,
                json_data={"error": message},
                error=True,
                exit_code=1,
            )

        ctx.store.write_field("model", resolved)
        return CommandResult(text=f"Model: {resolved}", json_data={"model": resolved})


model: ModelCommand = ModelCommand()
