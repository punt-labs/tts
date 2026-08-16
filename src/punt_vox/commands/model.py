"""``vox model`` -- list or set the TTS model for the current provider.

The command is a callable object (:class:`ModelCommand`) so both surfaces
share one instance and the ratchet counts the class-per-command shape the
rest of vox uses (see :class:`~punt_vox.server_switches.ModelTool`).
"""

from __future__ import annotations

from typing import Self, final

from punt_vox.cascade import Cascade, RosterError
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
        exits with code 1. The cascade rule (vox-awm9) also fires: setting
        model writes voice = first from the current provider's roster in
        the same atomic ``write_fields`` call. A daemon fault on the roster
        fetch aborts the write and returns an error result.
        """
        cfg = ctx.store.read()
        provider = cfg.provider
        if not provider:
            # ``vox model`` used to substitute ``"elevenlabs"`` for an unset
            # provider and quietly resolve model names against it -- the CLI
            # twin of the same substitution ``server_switches.py`` used to
            # carry. Listing gets an honest empty answer; resolving a name
            # against no provider is refused so a wrong-provider model
            # never lands in ``vox.md``.
            if name is None:
                listing: SwitchList = SwitchList(names=(), current=cfg.model)
                return CommandResult(
                    text=listing.render("No models for this provider."),
                    json_data=listing.payload(),
                )
            message = (
                "no TTS provider is configured for this repo; "
                "set one with vox provider <name>"
            )
            return CommandResult(
                text=f"Error: {message}",
                json_data={"error": message},
                error=True,
                exit_code=1,
            )

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

        voice_default = Cascade.fetch_first_voice(ctx.client, provider)
        if isinstance(voice_default, RosterError):
            return CommandResult(
                text=f"Error: {voice_default.message}",
                json_data={"error": voice_default.message},
                error=True,
                exit_code=1,
            )

        try:
            ctx.store.write_fields({"model": resolved, "voice": voice_default})
        except ValueError as exc:
            message = str(exc)
            return CommandResult(
                text=f"Error: {message}",
                json_data={"error": message},
                error=True,
                exit_code=1,
            )
        return CommandResult(
            text=f"Model: {resolved}",
            json_data={"model": resolved, "voice": voice_default},
        )


model: ModelCommand = ModelCommand()
