"""``vox voice`` -- list or set the session voice for the active provider.

The command is a callable object (:class:`VoiceCommand`) so both surfaces
share one instance and the ratchet counts the class-per-command shape
the rest of vox uses (see :class:`~punt_vox.server_switches.VoiceTool`).
"""

from __future__ import annotations

from typing import Self, final

from punt_vox.client_errors import VoxdConnectionError, VoxdProtocolError
from punt_vox.commands._result import CommandResult, Ctx, SwitchList
from punt_vox.types_synthesis import SynthesisSpec


@final
class VoiceCommand:
    """List or set the session voice for the active (or given) provider."""

    __slots__ = ()

    def __new__(cls) -> Self:
        return super().__new__(cls)

    async def __call__(
        self, ctx: Ctx, name: str | None = None, provider: str | None = None
    ) -> CommandResult:
        """List the roster (no arg) or set the session voice (name given).

        Roster branch: reach voxd through :attr:`Ctx.client` for the current
        provider's voices; a daemon fault is reported as an error result the
        CLI adapter turns into exit code 1.

        Set branch: strip a stray leading ``@`` via
        :meth:`SynthesisSpec.normalize_voice`; a blank/lone-``@`` write
        returns an error result rather than corrupting the config.
        """
        cfg = ctx.store.read()

        if name is None:
            return self._list(ctx, provider, cfg.voice)

        normalized = SynthesisSpec.normalize_voice(name)
        if normalized is None:
            return CommandResult(
                text="voice name is empty",
                json_data={"error": "voice name is empty"},
                error=True,
                exit_code=1,
            )
        try:
            ctx.store.write_field("voice", normalized)
        except ValueError as exc:
            # ConfigStore rejects control chars / unescaped quotes in string
            # values (raises ConfigValueError, a ValueError subclass). Return a
            # clean error envelope rather than let the exception cross the
            # boundary as a traceback.
            message = str(exc)
            return CommandResult(
                text=f"Error: {message}",
                json_data={"error": message},
                error=True,
                exit_code=1,
            )
        return CommandResult(
            text=f"{normalized}'s here.", json_data={"voice": normalized}
        )

    @staticmethod
    def _list(ctx: Ctx, provider: str | None, current: str | None) -> CommandResult:
        """Return the voice roster, or an error envelope on a daemon fault.

        The provider is required now -- ``VoxClientSync.voices`` no longer
        accepts ``None`` since state is the sole authority on which provider
        voxd runs. A ``vox voices`` invocation without ``--provider`` fills
        from the current state's provider; when state has no provider
        either, the caller sees the same F1 refusal every other synthesis
        surface uses.
        """
        cfg = ctx.store.read()
        resolved = provider or cfg.provider
        if not resolved:
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
        try:
            names = ctx.client.voices(resolved)
        except (VoxdConnectionError, VoxdProtocolError) as exc:
            message = str(exc)
            return CommandResult(
                text=f"Error: {message}",
                json_data={"error": message},
                error=True,
                exit_code=1,
            )
        listing = SwitchList(names=tuple(names), current=current)
        payload: dict[str, object] = dict(listing.payload())
        payload["provider"] = resolved
        return CommandResult(text=listing.render(), json_data=payload)


voice: VoiceCommand = VoiceCommand()
