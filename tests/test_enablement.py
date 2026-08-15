"""Tests for the per-repo enablement state machine (``docs/vox-enable-disable.tex``).

The two load-bearing properties are asserted by name: the § 2.11 biconditional
(marker present iff exactly one canonical import) after every op, and the
no-orphan-on-purge property (purge removes the import, never stranding a 404ing
``@``-import).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, final

import pytest

from punt_vox.audible_notify import AudibleNotify
from punt_vox.claude_md import ClaudeMdImport
from punt_vox.client_errors import VoxdConnectionError
from punt_vox.config import ConfigStore
from punt_vox.deposited_files import DepositedGuide, VoxMarker
from punt_vox.enablement import (
    ProviderProposal,
    RepoEnablement,
)
from punt_vox.hook_payload import StopPayload
from punt_vox.hooks import handle_stop
from punt_vox.settings_registration import SettingsRegistration
from punt_vox.types_provider import ProviderReadiness, ProviderStatusPayload

if TYPE_CHECKING:
    from collections.abc import Callable

    from punt_vox.client_sync import VoxClientSync

_IMPORT = "@.punt-labs/vox/CLAUDE.md"


def _config_store(repo: Path) -> ConfigStore:
    return ConfigStore(repo / ".punt-labs" / "vox")


def _import_count(repo: Path) -> int:
    """Count top-level occurrences of the canonical import in the repo CLAUDE.md."""
    host = repo / "CLAUDE.md"
    if not host.is_file():
        return 0
    return sum(
        1 for line in host.read_text(encoding="utf-8").splitlines() if line == _IMPORT
    )


def _marker_present(repo: Path) -> bool:
    return (repo / ".punt-labs" / "vox" / "enabled").is_file()


def _dir_present(repo: Path) -> bool:
    return (repo / ".punt-labs" / "vox").is_dir()


def _assert_biconditional(repo: Path) -> None:
    """The § 2.11 invariant: marker present iff exactly one import line."""
    if _marker_present(repo):
        assert _import_count(repo) == 1
    else:
        assert _import_count(repo) == 0


# ---------------------------------------------------------------------------
# Enable -- reach Enabled, idempotent
# ---------------------------------------------------------------------------


def test_enable_reaches_enabled(tmp_path: Path) -> None:
    RepoEnablement.for_repo(tmp_path).enable()
    assert _marker_present(tmp_path)
    assert _import_count(tmp_path) == 1
    assert (tmp_path / ".punt-labs" / "vox" / "CLAUDE.md").is_file()
    assert _dir_present(tmp_path)
    _assert_biconditional(tmp_path)


def test_enable_registers_settings(tmp_path: Path) -> None:
    RepoEnablement.for_repo(tmp_path).enable()
    settings = tmp_path / ".claude" / "settings.json"
    data = json.loads(settings.read_text(encoding="utf-8"))
    assert "Bash(vox:*)" in data["permissions"]["allow"]


def test_enable_is_idempotent_no_second_import(tmp_path: Path) -> None:
    enablement = RepoEnablement.for_repo(tmp_path)
    enablement.enable()
    enablement.enable()
    enablement.enable()
    # AppendImport is 0->1->1: the marked repo carries exactly one import.
    assert _import_count(tmp_path) == 1
    assert _marker_present(tmp_path)
    _assert_biconditional(tmp_path)


# ---------------------------------------------------------------------------
# Enable -> audible: an enabled repo chimes/speaks by default (silence is disable)
# ---------------------------------------------------------------------------


def test_enable_leaves_repo_audible(tmp_path: Path) -> None:
    # The enable->audible property: a fresh enable must establish an audible
    # notify level so the notify-gated hooks fire. Without it the marker gate
    # passes yet handle_stop skips on notify=n, and the repo is silent -- audibly
    # identical to disabled.
    RepoEnablement.for_repo(tmp_path).enable()
    assert _config_store(tmp_path).read().notify == "y"


def test_enable_makes_handle_stop_fire_in_enabled_repo(tmp_path: Path) -> None:
    # End-to-end: after enable, the stop hook returns a decision-block (speak),
    # not None. This is the audible outcome a user hears on task completion.
    RepoEnablement.for_repo(tmp_path).enable()
    config = _config_store(tmp_path).read()
    result = handle_stop(
        StopPayload(stop_hook_active=False), config, tmp_path / ".punt-labs" / "vox"
    )
    assert result is not None
    assert result["decision"] == "block"


def test_enable_preserves_a_continuous_level(tmp_path: Path) -> None:
    # The upgrade path must not downgrade a user's continuous choice back to
    # normal: a re-enable over notify=c leaves it continuous.
    enablement = RepoEnablement.for_repo(tmp_path)
    enablement.enable()
    _config_store(tmp_path).write_field("notify", "c")
    enablement.enable()
    assert _config_store(tmp_path).read().notify == "c"


def test_disable_closes_the_gate(tmp_path: Path) -> None:
    # Silence is disable, not notify=n: after disable the marker is gone, so the
    # hook gate is closed regardless of the (dormant) notify value.
    enablement = RepoEnablement.for_repo(tmp_path)
    enablement.enable()
    enablement.disable()
    assert not _marker_present(tmp_path)


def test_enable_preserves_user_prose_in_claude_md(tmp_path: Path) -> None:
    host = tmp_path / "CLAUDE.md"
    host.write_text("# My rules\n\nKeep me.\n", encoding="utf-8")
    RepoEnablement.for_repo(tmp_path).enable()
    text = host.read_text(encoding="utf-8")
    assert text == f"# My rules\n\nKeep me.\n{_IMPORT}\n"


# ---------------------------------------------------------------------------
# Disable -- non-destructive, removes import + marker
# ---------------------------------------------------------------------------


def test_disable_removes_import_and_marker(tmp_path: Path) -> None:
    enablement = RepoEnablement.for_repo(tmp_path)
    enablement.enable()
    enablement.disable()
    assert not _marker_present(tmp_path)
    assert _import_count(tmp_path) == 0
    _assert_biconditional(tmp_path)


def test_disable_leaves_the_directory_dormant(tmp_path: Path) -> None:
    enablement = RepoEnablement.for_repo(tmp_path)
    enablement.enable()
    enablement.disable()
    # dirPresent' = dirPresent: enable created the dir, disable leaves it (Dormant).
    assert _dir_present(tmp_path)
    assert (tmp_path / ".punt-labs" / "vox" / "CLAUDE.md").is_file()


def test_disable_deregisters_settings(tmp_path: Path) -> None:
    enablement = RepoEnablement.for_repo(tmp_path)
    enablement.enable()
    enablement.disable()
    data = json.loads((tmp_path / ".claude" / "settings.json").read_text("utf-8"))
    assert "Bash(vox:*)" not in data["permissions"]["allow"]


def test_disable_on_absent_repo_does_not_create_a_directory(tmp_path: Path) -> None:
    # Disable's frame: run on an already-Absent repo, it must not conjure an empty
    # .punt-labs/vox/ (a spurious Dormant state) -- dirPresent' = dirPresent.
    RepoEnablement.for_repo(tmp_path).disable()
    assert not _dir_present(tmp_path)
    assert not _marker_present(tmp_path)
    _assert_biconditional(tmp_path)


# ---------------------------------------------------------------------------
# Purge -- reach Absent, no orphan import
# ---------------------------------------------------------------------------


def test_purge_removes_the_subtree_and_the_import(tmp_path: Path) -> None:
    enablement = RepoEnablement.for_repo(tmp_path)
    enablement.enable()
    enablement.purge()
    assert not _dir_present(tmp_path)
    assert not _marker_present(tmp_path)
    assert _import_count(tmp_path) == 0
    _assert_biconditional(tmp_path)


def test_purge_leaves_no_orphan_import(tmp_path: Path) -> None:
    # The load-bearing no-orphan property: purge must remove the import (which
    # lives in CLAUDE.md, OUTSIDE the subtree) before deleting the guide file it
    # points at. A subtree-only purge would leave a 404ing @-import.
    enablement = RepoEnablement.for_repo(tmp_path)
    enablement.enable()
    enablement.purge()
    guide = tmp_path / ".punt-labs" / "vox" / "CLAUDE.md"
    assert not guide.is_file()
    # No import line survives that would point at the now-deleted guide.
    assert _import_count(tmp_path) == 0


def test_purge_preserves_user_prose(tmp_path: Path) -> None:
    host = tmp_path / "CLAUDE.md"
    host.write_text("# Rules\n\nKeep me.\n", encoding="utf-8")
    enablement = RepoEnablement.for_repo(tmp_path)
    enablement.enable()
    enablement.purge()
    assert host.read_text(encoding="utf-8") == "# Rules\n\nKeep me.\n"


# ---------------------------------------------------------------------------
# The full walk: every reachable state preserves the biconditional
# ---------------------------------------------------------------------------


def test_biconditional_holds_across_the_state_walk(tmp_path: Path) -> None:
    enablement = RepoEnablement.for_repo(tmp_path)
    _assert_biconditional(tmp_path)  # Absent
    enablement.enable()
    _assert_biconditional(tmp_path)  # Enabled
    enablement.disable()
    _assert_biconditional(tmp_path)  # Dormant
    enablement.enable()
    _assert_biconditional(tmp_path)  # Enabled again (upgrade path)
    enablement.purge()
    _assert_biconditional(tmp_path)  # Absent


def test_disable_heals_a_racing_writers_duplicate(tmp_path: Path) -> None:
    # RemoveImport 2->0: a non-conformant writer could leave two import lines;
    # disable removes every match, restoring the biconditional.
    host = tmp_path / "CLAUDE.md"
    host.write_text(f"# rules\n{_IMPORT}\nmore\n{_IMPORT}\n", encoding="utf-8")
    RepoEnablement.for_repo(tmp_path).disable()
    assert _import_count(tmp_path) == 0


# ---------------------------------------------------------------------------
# Crash-safety: the marker is written LAST, so a partial enable leaves vox OFF
# ---------------------------------------------------------------------------


def _raise_oserror(*_args: object, **_kwargs: object) -> None:
    raise OSError("simulated step failure")


def _assert_partial_enable_writes_no_marker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    target: type,
    method: str,
) -> None:
    """A step failing mid-``enable`` must leave no marker (vox observably OFF).

    The marker is written last precisely so this holds: the hooks gate on the
    marker, so a crash mid-``enable`` degrades to OFF rather than half-on (a
    marker with no guidance behind it). The reverse residue -- an import already
    written when a later step fails -- is benign: the hooks ignore it, and a
    subsequent ``disable`` or a re-run of ``enable`` heals it. The § 2.11
    biconditional is a steady-state property of a *completed* transition, not of
    a crash, so it is not asserted here.
    """
    monkeypatch.setattr(target, method, _raise_oserror)
    with pytest.raises(OSError, match="simulated step failure"):
        RepoEnablement.for_repo(tmp_path).enable()
    assert not _marker_present(tmp_path)


def test_enable_leaves_no_marker_when_import_register_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _assert_partial_enable_writes_no_marker(
        tmp_path, monkeypatch, ClaudeMdImport, "register"
    )


def test_enable_leaves_no_marker_when_settings_register_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _assert_partial_enable_writes_no_marker(
        tmp_path, monkeypatch, SettingsRegistration, "register"
    )


# ---------------------------------------------------------------------------
# Symlink refusal: an untrusted repo cannot redirect a tool-owned write
# ---------------------------------------------------------------------------


def test_enable_refuses_symlink_at_marker_path_leaving_target_intact(
    tmp_path: Path,
) -> None:
    """A symlink planted at ``.punt-labs/vox/enabled`` is refused, not followed.

    Without the ``O_NOFOLLOW`` guard, ``enable`` would overwrite the symlink's
    *target* (e.g. ``~/.ssh/id_rsa``) with the marker text -- data destruction.
    """
    secret = tmp_path / "secret.txt"
    secret.write_text("PRIVATE KEY\n", encoding="utf-8")
    vox = tmp_path / ".punt-labs" / "vox"
    vox.mkdir(parents=True)
    (vox / "enabled").symlink_to(secret)

    with pytest.raises(ValueError, match="symlink at a tool-owned path"):
        RepoEnablement.for_repo(tmp_path).enable()

    assert (vox / "enabled").is_symlink()
    assert secret.read_text(encoding="utf-8") == "PRIVATE KEY\n"


def test_enable_refuses_symlink_at_guide_path_leaving_target_intact(
    tmp_path: Path,
) -> None:
    """A symlink planted at the deposited ``CLAUDE.md`` guide is refused.

    The guide is the first step of ``enable``, so its target is protected and the
    marker is never written (the repo stays observably OFF).
    """
    secret = tmp_path / "secret.txt"
    secret.write_text("PRIVATE KEY\n", encoding="utf-8")
    vox = tmp_path / ".punt-labs" / "vox"
    vox.mkdir(parents=True)
    (vox / "CLAUDE.md").symlink_to(secret)

    with pytest.raises(ValueError, match="symlink at a tool-owned path"):
        RepoEnablement.for_repo(tmp_path).enable()

    assert (vox / "CLAUDE.md").is_symlink()
    assert secret.read_text(encoding="utf-8") == "PRIVATE KEY\n"
    assert not _marker_present(tmp_path)


def test_root_property_reports_the_repo_root(tmp_path: Path) -> None:
    assert RepoEnablement.for_repo(tmp_path).root == tmp_path


# ---------------------------------------------------------------------------
# Enable -> daemon-proposed provider (design §3.8, D1 as amended)
# ---------------------------------------------------------------------------


@final
class _FakeClient:
    """A :class:`VoxClientSync` stand-in with a canned provider_status reply."""

    def __init__(self, payload: ProviderStatusPayload | None = None) -> None:
        # A public attribute is fine on a test double, but the state is
        # tracked through the property below so a caller reads through
        # the same seam it would on the real client.
        self._payload = payload

    def provider_status(self, provider: str | None = None) -> ProviderStatusPayload:
        _ = provider
        if self._payload is None:
            msg = "test setup: no payload"
            raise VoxdConnectionError(msg)
        return self._payload


@final
class _ConnectionErrorClient:
    """A :class:`VoxClientSync` stand-in that always fails with the daemon down."""

    def provider_status(self, provider: str | None = None) -> ProviderStatusPayload:
        _ = provider
        msg = "connection refused"
        raise VoxdConnectionError(msg)


def _fake_factory(
    client: _FakeClient | _ConnectionErrorClient,
) -> Callable[[], VoxClientSync]:
    """Return a callable that produces *client*, typed as :class:`VoxClientSync`.

    :class:`ProviderProposal` calls its ``client_factory`` and drives
    the returned object through the ``provider_status`` method the
    test doubles here implement.  The cast bridges the double to the
    formal type without an implementation subclass -- structural
    substitution at the seam, no ``Any`` at either side.
    """
    # Local import so the test module keeps the runtime import graph
    # to the shapes it actually calls (the real class), while the
    # helper's factory returns the same class the seam expects.
    from typing import cast

    from punt_vox.client_sync import VoxClientSync as RealClient

    def factory() -> VoxClientSync:
        return cast("RealClient", client)

    return factory


def _wire_enablement(
    tmp_path: Path,
    client: _FakeClient | _ConnectionErrorClient,
) -> RepoEnablement:
    """Build a :class:`RepoEnablement` bound to *tmp_path* with a fake daemon.

    Wires the six collaborators the way ``for_repo`` would but with the
    proposal collaborator pointed at the test double, so the enable
    flow exercises the real file operations against ``tmp_path`` and
    the fake WebSocket boundary at once.
    """
    vox_dir = tmp_path / ".punt-labs" / "vox"
    proposal = ProviderProposal(
        ConfigStore(vox_dir), client_factory=_fake_factory(client)
    )
    return RepoEnablement(
        import_writer=ClaudeMdImport(tmp_path / "CLAUDE.md", _IMPORT),
        marker=VoxMarker(vox_dir / "enabled", tmp_path),
        guide=DepositedGuide(vox_dir / "CLAUDE.md", tmp_path),
        settings=SettingsRegistration(tmp_path / ".claude" / "settings.json"),
        audible=AudibleNotify(vox_dir),
        proposal=proposal,
    )


def _repo_with_daemon(tmp_path: Path, payload: ProviderStatusPayload) -> RepoEnablement:
    """Build a :class:`RepoEnablement` whose proposal uses a canned reply."""
    return _wire_enablement(tmp_path, _FakeClient(payload))


def test_enable_writes_the_daemon_s_preferred_provider(tmp_path: Path) -> None:
    """After enable, ``vox.md`` names the provider the daemon proposed."""
    payload = ProviderStatusPayload(
        (ProviderReadiness(name="elevenlabs", ready=True, reason="ok", detail=""),),
        preferred="elevenlabs",
    )
    outcome = _repo_with_daemon(tmp_path, payload).enable()
    assert outcome.reason == "written"
    assert outcome.provider_written == "elevenlabs"
    assert _config_store(tmp_path).read().provider == "elevenlabs"


def test_enable_does_not_read_its_own_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The client process's env must not influence enable's proposal.

    D1's whole amendment: doctor used to read the caller environment
    when the daemon's is the one that matters, and the first draft of
    enable repeated the mistake.  Here the client process exports
    ``ELEVENLABS_API_KEY`` and ``TTS_PROVIDER=openai`` while the fake
    daemon proposes ``polly`` -- enable must write ``polly``, never
    the ``elevenlabs``/``openai`` a local probe would pick.
    """
    monkeypatch.setenv("ELEVENLABS_API_KEY", "sk-client-side")
    monkeypatch.setenv("TTS_PROVIDER", "openai")
    payload = ProviderStatusPayload(
        (ProviderReadiness(name="polly", ready=True, reason="ok", detail=""),),
        preferred="polly",
    )
    outcome = _repo_with_daemon(tmp_path, payload).enable()
    assert outcome.provider_written == "polly"
    assert _config_store(tmp_path).read().provider == "polly"


def test_enable_writes_nothing_when_voxd_is_unreachable(tmp_path: Path) -> None:
    """Marker, guide, settings still land; no provider is written; reply names it."""
    enablement = _wire_enablement(tmp_path, _ConnectionErrorClient())
    outcome = enablement.enable()
    assert outcome.reason == "voxd_unavailable"
    assert outcome.provider_written is None
    assert "voxd is not reachable" in outcome.detail
    # The rest of enable STILL lands -- the marker, the guide, the settings.
    assert _marker_present(tmp_path)
    assert (tmp_path / ".punt-labs" / "vox" / "CLAUDE.md").is_file()
    assert _config_store(tmp_path).read().provider is None


def test_enable_writes_nothing_when_no_provider_is_ready(tmp_path: Path) -> None:
    """``preferred is None`` from the daemon reports; it does not silently write."""
    payload = ProviderStatusPayload(providers=(), preferred=None)
    outcome = _repo_with_daemon(tmp_path, payload).enable()
    assert outcome.reason == "no_ready_provider"
    assert outcome.provider_written is None
    assert _config_store(tmp_path).read().provider is None


def test_enable_does_not_overwrite_a_declared_provider(tmp_path: Path) -> None:
    """A repo whose ``vox.md`` already declares a provider is left alone.

    Enable's proposal is one-shot: a human's declared choice (or a
    previous enable's write) is preserved.  Re-running enable takes the
    ``already_set`` branch, so an upgrade path does not thrash the
    committed value.
    """
    # First enable writes elevenlabs.
    payload = ProviderStatusPayload(
        (ProviderReadiness(name="elevenlabs", ready=True, reason="ok", detail=""),),
        preferred="elevenlabs",
    )
    _repo_with_daemon(tmp_path, payload).enable()
    assert _config_store(tmp_path).read().provider == "elevenlabs"

    # Second enable -- daemon would propose polly, but the file already says
    # elevenlabs.  Verify we take the ``already_set`` branch.
    second_payload = ProviderStatusPayload(
        (ProviderReadiness(name="polly", ready=True, reason="ok", detail=""),),
        preferred="polly",
    )
    outcome = _repo_with_daemon(tmp_path, second_payload).enable()
    assert outcome.reason == "already_set"
    assert outcome.provider_written is None
    assert _config_store(tmp_path).read().provider == "elevenlabs"


def test_marker_content_is_deterministic(tmp_path: Path) -> None:
    # The marker bytes must be identical everywhere so the CLI and MCP surfaces
    # write the same file (§ 2.14). Two independent enables produce equal bytes.
    other = tmp_path / "other"
    other.mkdir()
    RepoEnablement.for_repo(tmp_path).enable()
    RepoEnablement.for_repo(other).enable()
    a = (tmp_path / ".punt-labs" / "vox" / "enabled").read_bytes()
    b = (other / ".punt-labs" / "vox" / "enabled").read_bytes()
    assert a == b
