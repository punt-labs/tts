"""Per-repo enablement: the ``enable`` / ``disable`` / ``purge`` state machine.

Enablement is not a boolean field but a small state machine over three presence
facts (``docs/vox-enable-disable.tex``): the tool-owned directory
``.punt-labs/vox/``, the ``enabled`` marker inside it, and the canonical
``@.punt-labs/vox/CLAUDE.md`` import in the repo ``CLAUDE.md``. The load-bearing
invariant is the § 2.11 biconditional -- the marker is present exactly when the
import is present -- which every transition preserves:

- ``enable``  deposits the guide, adds the import, registers settings, sets an
  audible notify default, asks the DAEMON for a proposed starter provider
  (design §3.8: the *daemon* chooses, never a local probe of the enabling
  process's environment), writes that provider into ``vox.md`` when one was
  proposed, and finally writes the marker.  Idempotent, and the upgrade path.
  An enabled repo is audible by default -- silence is ``disable``.
- ``disable`` removes the import, deletes the marker, deregisters settings, and
  leaves the directory exactly as found (a frame, never a create/remove) -- the
  dormant state.
- ``purge``   is ``disable`` (which removes the import, so no orphan) *then* the
  subtree removal; removing the subtree alone would strand a 404ing import.

:class:`RepoEnablement` is the facade; it composes the import writer
(:class:`~punt_vox.claude_md.ClaudeMdImport`), the
:class:`~punt_vox.deposited_files.VoxMarker`, the
:class:`~punt_vox.deposited_files.DepositedGuide`, the
:class:`~punt_vox.settings_registration.SettingsRegistration`, the
:class:`~punt_vox.audible_notify.AudibleNotify` default, and the
:class:`ProviderProposal` daemon proposal.

:class:`ProviderProposal` is the one collaborator that reaches outside the
repo: it opens a WebSocket to voxd, calls the ``provider_status`` op, and
writes ``preferred`` into ``vox.md`` via :class:`ConfigStore`.  A daemon that
is unreachable, a payload that reports no provider is ready, or a repo whose
``vox.md`` already names a provider are all reported without writing --
enable never overrules an existing choice, and it never guesses when the
daemon has no answer to give.
"""

from __future__ import annotations

import logging
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Self, final

from punt_vox.audible_notify import AudibleNotify
from punt_vox.claude_md import ClaudeMdImport
from punt_vox.client_errors import VoxdConnectionError, VoxdProtocolError
from punt_vox.client_sync import VoxClientSync
from punt_vox.config import ConfigStore
from punt_vox.deposited_files import DepositedGuide, VoxMarker
from punt_vox.dirs import find_repo_root
from punt_vox.settings_registration import SettingsRegistration

if TYPE_CHECKING:
    from collections.abc import Callable

logger = logging.getLogger(__name__)

__all__ = ["EnableOutcome", "ProviderProposal", "RepoEnablement"]

# The exact canonical repo-scope import line (§ 2.4). Byte-identical across all
# tools and both surfaces; what ``enable`` writes and ``disable`` prunes.
_IMPORT_LINE = "@.punt-labs/vox/CLAUDE.md"

# The transport-side faults ``ProviderProposal`` treats as "voxd not reachable".
# Mirrors the tuple ``server.py`` uses for the same reason -- one contract, so
# a network glitch never surfaces at one surface and not another.  ``OSError``
# is included so a socket-level failure (address in use, no route to host)
# folds into the same reply rather than escaping the tool boundary.
_DAEMON_ERRORS: tuple[type[BaseException], ...] = (
    VoxdConnectionError,
    VoxdProtocolError,
    OSError,
)


@dataclass(frozen=True, slots=True)
class EnableOutcome:
    """What :meth:`RepoEnablement.enable` did with the provider slot.

    Every enable, whether it wrote or not, produces one of these so
    the CLI and MCP surfaces can report the same story.  The value is
    additive to the file operations -- guide, import, settings,
    marker land regardless -- and reports only what happened at the
    daemon-proposal step.

    ``reason`` is a closed set (see the docstring alternatives on the
    ``Literal`` cousin :data:`~punt_vox.types_provider.ProviderStatusReason`
    for the discipline): ``"written"`` when the daemon proposed a
    provider and it landed in ``vox.md``; ``"already_set"`` when the
    file already names a provider (enable never overwrites a human's
    choice); ``"voxd_unavailable"`` when the ``provider_status`` op
    could not be reached; ``"no_ready_provider"`` when the daemon
    answered but every provider on it is unready.
    """

    reason: str
    provider_written: str | None
    detail: str


@final
class ProviderProposal:
    """Ask the daemon for a preferred provider and write it into ``vox.md``.

    The one collaborator that reaches outside the repo.  Kept apart
    from :class:`RepoEnablement` so the state machine's file
    operations stay pure and testable without a daemon, and so the
    daemon-proposal step is composable: a test binds a fake client
    factory, production binds :class:`VoxClientSync`.

    The write is guarded by two invariants:
    :meth:`ConfigStore.read_field` returning a non-empty provider name
    is a human's declared choice and is never overruled (``already_set``);
    a daemon that returns ``preferred is None`` reports rather than
    writes (``no_ready_provider``), because writing an empty string would
    be a lie about state.
    """

    __slots__ = ("_client_factory", "_store")
    _store: ConfigStore
    _client_factory: Callable[[], VoxClientSync]

    def __new__(
        cls,
        store: ConfigStore,
        client_factory: Callable[[], VoxClientSync] = VoxClientSync,
    ) -> Self:
        self = super().__new__(cls)
        self._store = store
        self._client_factory = client_factory
        return self

    def propose_and_write(self) -> EnableOutcome:
        """Do the daemon round-trip and, if warranted, write the provider.

        Order matters: the ``already_set`` guard runs BEFORE the
        daemon call, so a repo whose ``vox.md`` already declares a
        provider never even reaches the daemon -- there is nothing to
        write, and asking would be a wasted round-trip that also
        raised the failure surface (a daemon glitch would flip
        ``already_set`` to ``voxd_unavailable`` for no gain).

        Every :class:`OSError` at the write path becomes a
        ``voxd_unavailable`` outcome carrying the OSError text; the
        distinction between "daemon down" and "disk full" is captured
        in ``detail`` rather than a fifth reason string, because both
        end in "no provider was selected" from the caller's point of
        view.
        """
        existing = self._existing_provider()
        if existing is not None:
            return EnableOutcome(
                reason="already_set",
                provider_written=None,
                detail=f"vox.md already declares provider={existing!r}",
            )
        preferred = self._ask_daemon()
        if preferred.reason != "written" or preferred.provider_written is None:
            # ``_ask_daemon`` only sets ``reason == "written"`` when it has
            # a provider name to hand back; the ``or`` branch keeps ``mypy``
            # honest about the narrowing rather than smuggling an ``assert``
            # past the boundary (PY-EH-3, ruff S101).
            return preferred
        provider = preferred.provider_written
        try:
            self._store.write_field("provider", provider)
        except OSError as exc:
            logger.exception("enable: failed to write proposed provider")
            return EnableOutcome(
                reason="voxd_unavailable",
                provider_written=None,
                detail=f"could not write provider to vox.md: {exc}",
            )
        return EnableOutcome(
            reason="written",
            provider_written=provider,
            detail=f"wrote provider={provider!r} to vox.md",
        )

    def _existing_provider(self) -> str | None:
        """Return the existing provider string, or ``None`` if unset/empty."""
        try:
            raw = self._store.read_field("provider")
        except (OSError, ValueError):
            # A malformed vox.md is out of scope for the proposal
            # step -- treat as no declared provider so the caller
            # gets one from the daemon rather than crash mid-enable.
            return None
        if raw is None or not raw.strip():
            return None
        return raw.strip()

    def _ask_daemon(self) -> EnableOutcome:
        """Query voxd for its preferred provider and classify the answer."""
        client = self._client_factory()
        try:
            payload = client.provider_status()
        except _DAEMON_ERRORS as exc:
            return EnableOutcome(
                reason="voxd_unavailable",
                provider_written=None,
                detail=(
                    "voxd is not reachable; no provider was selected — "
                    "start the daemon and run `vox provider <name>` "
                    f"({exc})"
                ),
            )
        preferred = payload.preferred
        if preferred is None:
            return EnableOutcome(
                reason="no_ready_provider",
                provider_written=None,
                detail=(
                    "no provider on this daemon is ready; no provider was "
                    "selected — set at least one credential (e.g. "
                    "ELEVENLABS_API_KEY, OPENAI_API_KEY, or AWS "
                    "credentials) and run `vox provider <name>`"
                ),
            )
        # The write path picks up ``provider_written``; here it is only
        # a claim, not yet in vox.md.
        return EnableOutcome(
            reason="written",
            provider_written=preferred,
            detail="",
        )


@final
class RepoEnablement:
    """Turn vox on and off in one repo, preserving the marker-import biconditional.

    Bind the six collaborators at construction; :meth:`for_repo` wires the real
    per-repo paths and :meth:`for_cwd` discovers the repo from the working
    directory. Each transition writes one of the three legal states, so no
    sequence of :meth:`enable` / :meth:`disable` / :meth:`purge` can leave the
    marker and the import disagreeing.
    """

    __slots__ = (
        "_audible",
        "_guide",
        "_import",
        "_marker",
        "_proposal",
        "_settings",
    )

    _import: ClaudeMdImport
    _marker: VoxMarker
    _guide: DepositedGuide
    _settings: SettingsRegistration
    _audible: AudibleNotify
    _proposal: ProviderProposal

    def __new__(
        cls,
        *,
        import_writer: ClaudeMdImport,
        marker: VoxMarker,
        guide: DepositedGuide,
        settings: SettingsRegistration,
        audible: AudibleNotify,
        proposal: ProviderProposal,
    ) -> Self:
        self = super().__new__(cls)
        self._import = import_writer
        self._marker = marker
        self._guide = guide
        self._settings = settings
        self._audible = audible
        self._proposal = proposal
        return self

    @classmethod
    def for_repo(cls, repo_root: Path) -> Self:
        """Wire the real per-repo paths for *repo_root*."""
        vox_dir = repo_root / ".punt-labs" / "vox"
        return cls(
            import_writer=ClaudeMdImport(repo_root / "CLAUDE.md", _IMPORT_LINE),
            marker=VoxMarker(vox_dir / "enabled", repo_root),
            guide=DepositedGuide(vox_dir / "CLAUDE.md", repo_root),
            settings=SettingsRegistration(repo_root / ".claude" / "settings.json"),
            audible=AudibleNotify(vox_dir),
            proposal=ProviderProposal(ConfigStore(vox_dir)),
        )

    @classmethod
    def for_cwd(cls) -> Self:
        """Wire the repo discovered from the working directory.

        Raises ``ValueError`` when the working directory is not inside a git
        repository -- ``enable`` / ``disable`` are repo-scoped verbs (§ 2.3), so a
        non-repo invocation is a clean boundary failure, not a silent no-op.
        """
        root = find_repo_root()
        if root is None:
            msg = "not inside a git repository"
            raise ValueError(msg)
        return cls.for_repo(root)

    @property
    def root(self) -> Path:
        """Return the repository root this instance operates on."""
        # marker path is <root>/.punt-labs/vox/enabled -> root is three parents up.
        return self._marker.path.parents[2]

    @property
    def marker_path(self) -> Path:
        """Return the ``enabled`` marker path."""
        return self._marker.path

    @property
    def import_line(self) -> str:
        """Return the canonical ``@``-import line enablement owns."""
        return self._import.import_line

    def is_enabled(self) -> bool:
        """Return whether the repo is enabled (the marker is present)."""
        return self._marker.is_present()

    def enable(self) -> EnableOutcome:
        """Reach the Enabled state from anywhere; idempotent (also the upgrade path).

        Order matters for crash-safety: guide first (so the import never points
        at a missing guide), then the import, the settings, the audible notify
        default, the daemon-proposed provider (§3.8), and the marker **last**.
        The marker is vox's on-signal -- the hooks gate on it -- so if any
        earlier step raises, the repo is left observably OFF (no marker) rather
        than half-on (a marker with no guidance behind it). The audible default
        lands before the marker so a completed ``enable`` is audible: silence
        is ``disable`` (marker gone), never an enabled repo left at ``notify=n``.
        Re-running rewrites the guide, leaves the single import in place,
        preserves an existing audible level, adds no duplicate, and takes the
        ``already_set`` branch on the provider slot (§3.8: enable never
        overrules a human's declared provider, so a re-run does not thrash it).

        Returns the outcome of the provider-proposal step (design §3.8):
        ``written`` / ``already_set`` are success paths; ``voxd_unavailable``
        / ``no_ready_provider`` are true reports of an unusable host, not
        errors -- the rest of enable still landed and the caller sees which
        slot did not get filled.
        """
        self._guide.deposit()
        self._import.register()
        self._settings.register()
        self._audible.ensure_audible()
        outcome = self._proposal.propose_and_write()
        self._marker.write()
        return outcome

    def disable(self) -> None:
        """Reach the Dormant/Absent state non-destructively.

        Remove the import first (so the biconditional holds the moment the marker
        goes), delete the marker, and deregister the settings entries. The
        directory is left exactly as found -- ``disable`` never creates or removes
        it -- so it lands in Dormant when a directory was present and stays Absent
        when it was not.
        """
        self._import.prune()
        self._marker.remove()
        self._settings.deregister()

    def purge(self) -> None:
        """Reach the Absent state by removing the subtree, leaving no orphan import.

        ``purge`` is ``disable`` -- which removes the import line that lives in
        ``CLAUDE.md``, *outside* the subtree -- followed by the subtree removal.
        Removing the subtree alone would strand a 404ing ``@``-import and violate
        the § 2.11 biconditional.
        """
        self.disable()
        self._remove_subtree()

    def _remove_subtree(self) -> None:
        """Remove the ``.punt-labs/vox/`` directory if it is present."""
        vox_dir = self._marker.path.parent
        if vox_dir.is_dir():
            shutil.rmtree(vox_dir)
