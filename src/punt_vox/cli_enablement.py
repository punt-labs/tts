"""The ``vox enable`` / ``vox disable`` / ``vox notify`` CLI verbs.

``enable`` / ``disable`` turn vox on and off in the repo the CLI runs from --
pure file operations over :class:`~punt_vox.enablement.RepoEnablement`, never a
daemon round-trip (design § 1). ``notify`` sets the per-repo notification level
(``normal`` or ``continuous``) -- the level within "on", distinct from the
enablement marker (§ 2 correction). :class:`EnablementCli` is a humble object;
:func:`build_enablement_commands` binds its methods onto the top-level ``vox``
app so they read as ``vox enable``, not ``vox enablement enable``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated, ClassVar, Self, final

import typer

from punt_vox.config import ConfigStore
from punt_vox.dirs import DEFAULT_CONFIG_DIR, find_config_dir
from punt_vox.enablement import EnableOutcome, RepoEnablement

if TYPE_CHECKING:
    from collections.abc import Callable

    from punt_vox.cli_io import OutputFlags
    from punt_vox.output_formatter import OutputFormatter

__all__ = ["EnablementCli", "build_enablement_commands"]

# The three output flags, redeclared on each verb so ``--json`` parses after the
# subcommand as well as before it (matching the music/rec groups).
_JsonOutput = Annotated[bool, typer.Option("--json", help="Output JSON.")]
_Verbose = Annotated[
    bool, typer.Option("--verbose", "-v", help="Enable debug logging.")
]
_Quiet = Annotated[
    bool, typer.Option("--quiet", "-q", help="Suppress non-JSON output.")
]

_PurgeFlag = Annotated[
    bool,
    typer.Option("--purge", help="Also remove the .punt-labs/vox/ subtree."),
]
_NotifyMode = Annotated[
    str,
    typer.Argument(
        help=(
            "'normal' fires on task completion + permission prompts; "
            "'continuous' also announces real-time signals."
        ),
    ),
]


@final
class EnablementCli:
    """The enable/disable/notify verbs, bound onto the top-level ``vox`` app."""

    __slots__ = ("_flags", "_formatter")

    _formatter: OutputFormatter
    _flags: OutputFlags

    # normal is task-completion + permission prompts; continuous also announces
    # real-time signals. The stored config values stay y/c so hooks and the daemon
    # read one unchanged contract.
    _NOTIFY_VALUES: ClassVar[dict[str, str]] = {"normal": "y", "continuous": "c"}

    def __new__(cls, formatter: OutputFormatter, flags: OutputFlags) -> Self:
        self = super().__new__(cls)
        self._formatter = formatter
        self._flags = flags
        return self

    def enable(
        self,
        *,
        json_output: _JsonOutput = False,
        verbose: _Verbose = False,
        quiet: _Quiet = False,
    ) -> None:
        """Turn vox on in this repo: deposit the guide, marker, import, and settings.

        Idempotent -- a re-run upgrades the deposited guide and adds no second
        import. Also asks the DAEMON (never the local environment) for a
        starter provider and writes it into ``vox.md`` on the ``written``
        branch (design §3.8); an unreachable daemon or a host with nothing
        ready is reported inline, and the rest of enable still lands. Writes
        a working-tree change committed via a PR, never runs git.
        """
        self._flags.apply(json_output=json_output, verbose=verbose, quiet=quiet)
        outcome_holder: dict[str, EnableOutcome] = {}

        def _do_enable(e: RepoEnablement) -> None:
            outcome_holder["value"] = e.enable()

        enablement = self._transition(_do_enable)
        outcome = outcome_holder["value"]
        payload: dict[str, object] = {
            "enabled": True,
            "repo": str(enablement.root),
            "marker": str(enablement.marker_path),
            "provider_proposal": {
                "reason": outcome.reason,
                "provider_written": outcome.provider_written,
                "detail": outcome.detail,
            },
        }
        headline = f"vox enabled in {enablement.root}"
        # The outcome sentence is appended so a plain-text reader sees the
        # daemon-proposal result without decoding the JSON envelope (the
        # ``detail`` string is written to be read directly).
        text = f"{headline}\n{outcome.detail}" if outcome.detail else headline
        self._formatter.emit(payload, text)

    def disable(
        self,
        *,
        purge: _PurgeFlag = False,
        json_output: _JsonOutput = False,
        verbose: _Verbose = False,
        quiet: _Quiet = False,
    ) -> None:
        """Turn vox off in this repo: remove the import, marker, and settings.

        Non-destructive by default -- the ``.punt-labs/vox/`` subtree is left
        dormant. ``--purge`` removes the subtree too (after removing the import,
        so no orphan ``@``-import is left behind).
        """
        self._flags.apply(json_output=json_output, verbose=verbose, quiet=quiet)
        transition: Callable[[RepoEnablement], None] = (
            (lambda e: e.purge()) if purge else (lambda e: e.disable())
        )
        enablement = self._transition(transition)
        verb = "purged" if purge else "disabled"
        self._formatter.emit(
            {"enabled": False, "purged": purge, "repo": str(enablement.root)},
            f"vox {verb} in {enablement.root}",
        )

    def notify(
        self,
        mode: _NotifyMode,
        *,
        json_output: _JsonOutput = False,
        verbose: _Verbose = False,
        quiet: _Quiet = False,
    ) -> None:
        """Set the per-repo notification level within "on" (normal or continuous).

        Distinct from the enablement marker: ``off`` is no longer a level; use
        ``vox disable`` to turn vox off for the repo. ``continuous`` implies
        speech (sets ``speak=y``); ``normal`` leaves ``speak`` alone after the
        first init. Change the session voice through ``vox voice`` -- notify
        is a mode toggle, not a voice-write channel.

        Example: vox notify normal
        Example: vox notify continuous

        See also: vox enable/disable (enablement marker), vox speak (voice on/off),
        vox voice (session voice), mic:notify (MCP peer).
        """
        self._flags.apply(json_output=json_output, verbose=verbose, quiet=quiet)
        if mode not in self._NOTIFY_VALUES:
            raise typer.BadParameter("mode must be normal or continuous")
        value = self._NOTIFY_VALUES[mode]

        store = ConfigStore(find_config_dir() or DEFAULT_CONFIG_DIR)
        first_init = store.read_field("notify") is None
        updates: dict[str, str] = {"notify": value}
        # Continuous always speaks; normal defaults to voice only on first init,
        # so a later `vox notify normal` never silently re-enables speech.
        if value == "c" or (first_init and value == "y"):
            updates["speak"] = "y"
        store.write_fields(updates)
        self._formatter.emit(updates, f"Notify: {mode}.")

    @staticmethod
    def _transition(action: Callable[[RepoEnablement], None]) -> RepoEnablement:
        """Wire the repo, run *action*, and return it; map boundary faults to exit 1.

        Two fault classes reach here, both at the filesystem boundary. A non-repo
        working directory (``for_cwd``) and a malformed ``.claude/settings.json``
        reached during *action* (the
        :class:`~punt_vox.settings_registration.SettingsRegistration` guard) raise
        ``ValueError``; a failed write of the marker, guide, or settings
        (permission denied, ``ENOSPC``; ``TimeoutError`` is an ``OSError``) raises
        ``OSError``. Each becomes a clean CLI error and exit, symmetric with the
        MCP surface, never a traceback.
        """
        try:
            enablement = RepoEnablement.for_cwd()
            action(enablement)
        except (OSError, ValueError) as exc:
            typer.echo(f"vox: {exc}", err=True)
            raise typer.Exit(code=1) from exc
        return enablement


def build_enablement_commands(
    app: typer.Typer, formatter: OutputFormatter, flags: OutputFlags
) -> None:
    """Register ``enable`` / ``disable`` / ``notify`` as top-level ``vox`` commands."""
    cli = EnablementCli(formatter, flags)
    app.command("enable")(cli.enable)
    app.command("disable")(cli.disable)
    app.command("notify")(cli.notify)
