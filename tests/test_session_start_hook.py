"""Behavioral tests for plugin/hooks/session-start.sh command deployment.

vox-ovz3: model.md, provider.md, voice.md, and recap.md are namespaced-only
commands (``/vox:model``, never bare ``/model``) because a bare top-level
form collides with a name Claude Code itself may claim — ``/model`` already
does. The deploy loop must skip them, and the RETIRED cleanup must remove
any copy a prior session already deployed — unless the file carries no
``mcp__plugin_vox_mic__`` fingerprint, in which case it is a user's own
hand-authored command sharing the name and must survive untouched. The
fingerprint check, not exact content equality, is deliberate: a command
file's prose can change release to release (recap.md's own H1 and Usage
text moved from bare to namespaced in the same release), so a stale file
from an older release is never byte-identical to the current source — only
a content-independent ownership signal survives that. The five
session-scoped verbs (``vox``, ``unmute``, ``mute``, ``vibe``, ``music``) are
unaffected and must keep deploying bare, unchanged. The permission grants for
the four namespaced commands must read ``Skill(vox:<name>)``, never the bare
form, and a stale bare grant is pruned only for a name this run's file
retirement actually cleaned — not unconditionally, and not in dev mode.

Driven as a subprocess against the real script, with a copy of the real
``plugin/`` tree and a sandboxed ``$HOME`` — the interface is the contract,
so this exercises the shell, not a reimplementation of it. Only ``$HOME`` is
sandboxed; ``jq``, ``git``, and the coreutils the script shells out to run
from the real system ``PATH``.
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
_PLUGIN_SRC = _REPO_ROOT / "plugin"

pytestmark = [
    pytest.mark.subprocess,
    pytest.mark.skipif(
        shutil.which("jq") is None, reason="session-start.sh requires jq"
    ),
]

# The four commands that must deploy namespaced-only (never bare).
_NAMESPACED_ONLY = ("model", "provider", "voice", "recap")
# The five commands that must keep deploying bare, unaffected.
_BARE = ("vox", "unmute", "mute", "vibe", "music")


def _make_prod_plugin(root: Path) -> Path:
    """Copy the real plugin/ tree into ``root`` with a prod-named manifest.

    The hook's dev/prod branch reads whether ``.claude-plugin/plugin.json``
    contains the literal ``"vox-dev"``; a prod name is required for the
    deploy loop and RETIRED cleanup to run at all (dev mode skips both).
    """
    dest = root / "plugin"
    shutil.copytree(_PLUGIN_SRC, dest)
    manifest = dest / ".claude-plugin" / "plugin.json"
    data = json.loads(manifest.read_text(encoding="utf-8"))
    data["name"] = "vox"
    manifest.write_text(json.dumps(data), encoding="utf-8")
    return dest


def _run(
    plugin_dir: Path, home: Path, *, bash: str = "bash"
) -> subprocess.CompletedProcess[str]:
    home.mkdir(parents=True, exist_ok=True)
    hook = plugin_dir / "hooks" / "session-start.sh"
    env = {"HOME": str(home), "PATH": _system_path()}
    return subprocess.run(
        [bash, str(hook)],
        input=json.dumps({"cwd": str(home)}),
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )


def _system_path() -> str:
    return os.environ.get("PATH", "/usr/bin:/bin:/usr/sbin:/sbin")


def _commands_dir(home: Path) -> Path:
    return home / ".claude" / "commands"


def _deployed_names(home: Path) -> set[str]:
    d = _commands_dir(home)
    if not d.is_dir():
        return set()
    return {p.name for p in d.glob("*.md")}


def _additional_context(result: subprocess.CompletedProcess[str]) -> str:
    if not result.stdout.strip():
        return ""
    payload = json.loads(result.stdout)
    context = payload["hookSpecificOutput"]["additionalContext"]
    assert isinstance(context, str)
    return context


_PRE_NAMESPACING_FIXTURES = (
    Path(__file__).resolve().parent / "fixtures" / "pre-namespacing"
)


def _pre_pr_content(name: str) -> str:
    """Return ``name``'s command file content as it shipped before namespacing.

    A real upgrading install never has the CURRENT plugin source deployed as
    its "stale" file -- it has whatever an earlier release shipped, which for
    a file whose prose keeps changing (recap.md's H1 and Usage text move from
    bare to namespaced) is never byte-identical to the source tree's present
    content. Seeding a test from the current plugin source instead of a real
    past release means the seeded file always matches the live source and
    the test can never observe a genuine content mismatch -- exactly the gap
    that hid this class of retirement bug.

    Vendored as fixture files under fixtures/pre-namespacing/ rather than
    fetched from git history at test time: a CI checkout can be shallow and
    tag-less, and a test that depends on `git show <tag>:<path>` succeeding
    is not hermetic -- it silently passes locally (full history) and fails
    in exactly the environment meant to catch a regression.
    """
    return (_PRE_NAMESPACING_FIXTURES / f"{name}.md").read_text(encoding="utf-8")


def _settings_allow(home: Path) -> list[str]:
    settings = home / ".claude" / "settings.json"
    if not settings.is_file():
        return []
    data = json.loads(settings.read_text(encoding="utf-8"))
    allow = data.get("permissions", {}).get("allow", [])
    assert isinstance(allow, list)
    return [str(r) for r in allow]


class TestFreshInstallNamespacesTheFour:
    """A session with no prior deployed commands never acquires the bare four."""

    def test_namespaced_commands_are_not_deployed(self, tmp_path: Path) -> None:
        plugin_dir = _make_prod_plugin(tmp_path)
        result = _run(plugin_dir, tmp_path / "home")
        assert result.returncode == 0, result.stderr
        deployed = _deployed_names(tmp_path / "home")
        for name in _NAMESPACED_ONLY:
            assert f"{name}.md" not in deployed, (
                f"{name}.md must not be deployed bare (namespaced-only)"
            )

    def test_bare_commands_still_deploy(self, tmp_path: Path) -> None:
        plugin_dir = _make_prod_plugin(tmp_path)
        result = _run(plugin_dir, tmp_path / "home")
        assert result.returncode == 0, result.stderr
        deployed = _deployed_names(tmp_path / "home")
        for name in _BARE:
            assert f"{name}.md" in deployed

    def test_deployed_bare_content_matches_source(self, tmp_path: Path) -> None:
        plugin_dir = _make_prod_plugin(tmp_path)
        home = tmp_path / "home"
        _run(plugin_dir, home)
        for name in _BARE:
            src = (plugin_dir / "commands" / f"{name}.md").read_text(encoding="utf-8")
            dest = (_commands_dir(home) / f"{name}.md").read_text(encoding="utf-8")
            assert src == dest


class TestStaleInstallIsRetired:
    """A `$HOME` already carrying the old bare four has them cleaned up."""

    def _seed_stale_commands(self, home: Path, plugin_dir: Path) -> None:
        commands_dir = _commands_dir(home)
        commands_dir.mkdir(parents=True, exist_ok=True)
        for name in (*_NAMESPACED_ONLY, *_BARE):
            (commands_dir / f"{name}.md").write_text(
                _pre_pr_content(name), encoding="utf-8"
            )

    def test_retired_cleanup_removes_the_four(self, tmp_path: Path) -> None:
        plugin_dir = _make_prod_plugin(tmp_path)
        home = tmp_path / "home"
        self._seed_stale_commands(home, plugin_dir)
        result = _run(plugin_dir, home)
        assert result.returncode == 0, result.stderr
        deployed = _deployed_names(home)
        for name in _NAMESPACED_ONLY:
            assert f"{name}.md" not in deployed, (
                f"stale {name}.md was not retired from an existing install"
            )

    def test_retired_cleanup_leaves_the_bare_five(self, tmp_path: Path) -> None:
        plugin_dir = _make_prod_plugin(tmp_path)
        home = tmp_path / "home"
        self._seed_stale_commands(home, plugin_dir)
        result = _run(plugin_dir, home)
        assert result.returncode == 0, result.stderr
        deployed = _deployed_names(home)
        for name in _BARE:
            assert f"{name}.md" in deployed

    def test_hook_reports_the_cleanup(self, tmp_path: Path) -> None:
        plugin_dir = _make_prod_plugin(tmp_path)
        home = tmp_path / "home"
        self._seed_stale_commands(home, plugin_dir)
        result = _run(plugin_dir, home)
        context = _additional_context(result)
        assert "Cleaned retired commands" in context
        for name in _NAMESPACED_ONLY:
            assert f"/{name}" in context

    def test_recap_is_retired_despite_prose_changing_in_the_same_release(
        self, tmp_path: Path
    ) -> None:
        # Regression test: recap.md's own H1 and Usage text moved from bare
        # /recap to /vox:recap in the same release that made it
        # namespaced-only, so it never byte-matches the current shipped
        # file. Content equality would leave it stuck bare forever; the
        # fingerprint check must retire it anyway.
        plugin_dir = _make_prod_plugin(tmp_path)
        home = tmp_path / "home"
        commands_dir = _commands_dir(home)
        commands_dir.mkdir(parents=True)
        stale = _pre_pr_content("recap")
        current = (plugin_dir / "commands" / "recap.md").read_text(encoding="utf-8")
        assert stale != current, (
            "fixture invalid -- recap.md's prose must actually differ "
            "between the pre-namespacing snapshot and the current source "
            "for this test to exercise the bug"
        )
        (commands_dir / "recap.md").write_text(stale, encoding="utf-8")
        result = _run(plugin_dir, home)
        assert result.returncode == 0, result.stderr
        assert "recap.md" not in _deployed_names(home)


class TestDevModeSkipsDeployment:
    """A vox-dev-named manifest never deploys or retires anything.

    The dev/prod split only runs command deployment in prod mode — dev mode
    relies on the sibling prod plugin (per session-start.sh's own comment).
    A namespace regression must not hide inside "well, dev mode never
    touched it anyway", so this pins that dev mode is a true no-op here.
    """

    def test_dev_manifest_deploys_nothing(self, tmp_path: Path) -> None:
        # _PLUGIN_SRC's own manifest already carries "vox-dev" in the
        # working tree, so a plain copy with no rename exercises dev mode.
        dest = tmp_path / "plugin"
        shutil.copytree(_PLUGIN_SRC, dest)
        home = tmp_path / "home"
        result = _run(dest, home)
        assert result.returncode == 0, result.stderr
        assert not _commands_dir(home).exists() or not _deployed_names(home)


class TestPermissionGrantsAreNamespaced:
    """The Skill() grants actually written to settings.json, not just the
    deploy-loop file list, must read the namespaced form for the four.
    """

    def test_namespaced_grants_present(self, tmp_path: Path) -> None:
        plugin_dir = _make_prod_plugin(tmp_path)
        home = tmp_path / "home"
        result = _run(plugin_dir, home)
        assert result.returncode == 0, result.stderr
        allow = _settings_allow(home)
        for name in _NAMESPACED_ONLY:
            assert f"Skill(vox:{name})" in allow

    def test_bare_grants_absent(self, tmp_path: Path) -> None:
        plugin_dir = _make_prod_plugin(tmp_path)
        home = tmp_path / "home"
        result = _run(plugin_dir, home)
        assert result.returncode == 0, result.stderr
        allow = _settings_allow(home)
        for name in _NAMESPACED_ONLY:
            assert f"Skill({name})" not in allow

    def test_bare_grants_present_for_the_five(self, tmp_path: Path) -> None:
        plugin_dir = _make_prod_plugin(tmp_path)
        home = tmp_path / "home"
        result = _run(plugin_dir, home)
        assert result.returncode == 0, result.stderr
        allow = _settings_allow(home)
        for name in _BARE:
            assert f"Skill({name})" in allow

    def _seed_settings_with_stale_grants(self, home: Path) -> None:
        claude_dir = home / ".claude"
        claude_dir.mkdir(parents=True, exist_ok=True)
        stale = {
            "permissions": {
                "allow": [
                    "Skill(model)",
                    "Skill(provider)",
                    "Skill(voice)",
                    "Skill(recap)",
                    "Skill(vibe)",
                ]
            }
        }
        (claude_dir / "settings.json").write_text(json.dumps(stale), encoding="utf-8")

    def test_stale_bare_grants_are_pruned_alongside_their_files(
        self, tmp_path: Path
    ) -> None:
        # A grant is pruned only for a name this run's retirement ACTUALLY
        # cleaned -- so the fixture must seed both the stale command file
        # (real vox content, eligible for retirement) and the stale grant.
        plugin_dir = _make_prod_plugin(tmp_path)
        home = tmp_path / "home"
        self._seed_settings_with_stale_grants(home)
        commands_dir = _commands_dir(home)
        commands_dir.mkdir(parents=True, exist_ok=True)
        for name in _NAMESPACED_ONLY:
            (commands_dir / f"{name}.md").write_text(
                _pre_pr_content(name), encoding="utf-8"
            )
        result = _run(plugin_dir, home)
        assert result.returncode == 0, result.stderr
        allow = _settings_allow(home)
        for name in _NAMESPACED_ONLY:
            assert f"Skill({name})" not in allow
        # An unrelated bare grant that was never namespaced survives.
        assert "Skill(vibe)" in allow

    def test_stale_grant_survives_when_its_file_was_never_deployed(
        self, tmp_path: Path
    ) -> None:
        # A grant with no corresponding file this run retired must NOT be
        # pruned -- it is exactly as plausibly the user's own permission for
        # their own hand-authored command as the file itself would be (see
        # TestForeignFileWithGenericNameSurvives). Pruning independent of
        # whether the file was actually vox's would silently revoke a grant
        # the hook has no evidence it ever wrote.
        plugin_dir = _make_prod_plugin(tmp_path)
        home = tmp_path / "home"
        self._seed_settings_with_stale_grants(home)
        result = _run(plugin_dir, home)
        assert result.returncode == 0, result.stderr
        allow = _settings_allow(home)
        for name in _NAMESPACED_ONLY:
            assert f"Skill({name})" in allow

    def test_stale_grant_survives_dev_mode(self, tmp_path: Path) -> None:
        # Dev mode never runs file retirement, so CLEANED is always empty --
        # the grant-pruning block must not run independently of it, or a
        # vox-dev session would strip grants the sibling prod plugin wrote.
        dest = tmp_path / "plugin"
        shutil.copytree(_PLUGIN_SRC, dest)
        home = tmp_path / "home"
        self._seed_settings_with_stale_grants(home)
        result = _run(dest, home)
        assert result.returncode == 0, result.stderr
        allow = _settings_allow(home)
        for name in _NAMESPACED_ONLY:
            assert f"Skill({name})" in allow

    def test_write_failure_is_reported(self, tmp_path: Path) -> None:
        # The stale-grant write goes through the same mktemp/jq/mv sequence
        # as the legacy-MCP-pattern cleanup and the main permissions write,
        # and must report failure the same way if that sequence can't run.
        # settings.json's PARENT directory (~/.claude), not settings.json
        # itself, needs to be unwritable: mktemp creates its temp file
        # directly inside that directory. ~/.claude/commands stays writable
        # (it is its own directory), so file retirement still succeeds and
        # populates CLEANED -- the write only fails at the settings.json step.
        plugin_dir = _make_prod_plugin(tmp_path)
        home = tmp_path / "home"
        claude_dir = home / ".claude"
        commands_dir = claude_dir / "commands"
        commands_dir.mkdir(parents=True)
        (commands_dir / "model.md").write_text(
            _pre_pr_content("model"), encoding="utf-8"
        )
        self._seed_settings_with_stale_grants(home)
        claude_dir.chmod(stat.S_IRUSR | stat.S_IXUSR)
        try:
            result = _run(plugin_dir, home)
        finally:
            claude_dir.chmod(stat.S_IRWXU)
        assert result.returncode == 0, result.stderr
        # The file side still succeeded -- model.md was genuinely retired.
        assert "model.md" not in _deployed_names(home)
        context = _additional_context(result)
        assert "Failed to remove stale bare Skill() grants for" in context
        assert "Skill(model)" in context


class TestForeignFileWithGenericNameSurvives:
    """A user's own command sharing a namespaced-only name is never deleted.

    model/provider/voice/recap are generic enough that a user could have
    hand-authored their own command under one of those names. RETIRED must
    only remove a NAMESPACED_ONLY file when it carries vox's own
    ``mcp__plugin_vox_mic__`` fingerprint — otherwise it is not vox's stale
    deployment.
    """

    def test_foreign_model_md_is_not_removed(self, tmp_path: Path) -> None:
        plugin_dir = _make_prod_plugin(tmp_path)
        home = tmp_path / "home"
        commands_dir = _commands_dir(home)
        commands_dir.mkdir(parents=True)
        foreign_content = "# /model command\n\nMy own thing, not vox's.\n"
        (commands_dir / "model.md").write_text(foreign_content, encoding="utf-8")
        result = _run(plugin_dir, home)
        assert result.returncode == 0, result.stderr
        assert "model.md" in _deployed_names(home)
        assert (commands_dir / "model.md").read_text(
            encoding="utf-8"
        ) == foreign_content

    def test_foreign_model_md_is_not_reported_as_cleaned(self, tmp_path: Path) -> None:
        plugin_dir = _make_prod_plugin(tmp_path)
        home = tmp_path / "home"
        commands_dir = _commands_dir(home)
        commands_dir.mkdir(parents=True)
        (commands_dir / "model.md").write_text(
            "# /model command\n\nMy own thing, not vox's.\n", encoding="utf-8"
        )
        result = _run(plugin_dir, home)
        context = _additional_context(result)
        assert "/model" not in context.split("Cleaned retired commands:", 1)[-1]


class TestDeployAndCleanupFailuresAreSurfaced:
    """FAILED_CLEAN and FAILED_DEPLOY must name the file and the OS error.

    Grows the RETIRED array (7 -> 11) and adds the NAMESPACED_ONLY skip to
    the deploy loop in this same change -- exactly the code these counters
    guard, so a silent failure here is the highest-risk regression class.
    """

    def test_undeletable_retired_command_is_reported(self, tmp_path: Path) -> None:
        plugin_dir = _make_prod_plugin(tmp_path)
        home = tmp_path / "home"
        commands_dir = _commands_dir(home)
        commands_dir.mkdir(parents=True)
        # A BARE name (vox-specific, unconditionally eligible for removal)
        # avoids conflating this failure-surfacing test with the
        # content-check behavior covered above.
        (commands_dir / "say.md").write_text("stale\n", encoding="utf-8")
        # Read-only parent directory: the file itself is still present, but
        # rm cannot unlink an entry from a directory it cannot write to.
        commands_dir.chmod(stat.S_IRUSR | stat.S_IXUSR)
        try:
            result = _run(plugin_dir, home)
        finally:
            commands_dir.chmod(stat.S_IRWXU)
        assert result.returncode == 0, result.stderr
        assert (commands_dir / "say.md").exists(), (
            "the file must still exist -- the rm genuinely failed"
        )
        context = _additional_context(result)
        assert "Failed to remove retired command" in context
        assert "say.md" in context
        assert "Failed to remove 1 retired command" in context

    def test_undeployable_command_is_reported(self, tmp_path: Path) -> None:
        plugin_dir = _make_prod_plugin(tmp_path)
        home = tmp_path / "home"
        commands_dir = _commands_dir(home)
        commands_dir.mkdir(parents=True)
        # mkdir -p on an already-existing directory returns 0 regardless of
        # its write permission (per the hook's own comment), so a
        # pre-existing read-only commands dir is what actually exercises
        # the cp failure path, not a missing directory.
        commands_dir.chmod(stat.S_IRUSR | stat.S_IXUSR)
        try:
            result = _run(plugin_dir, home)
        finally:
            commands_dir.chmod(stat.S_IRWXU)
        assert result.returncode == 0, result.stderr
        assert not (commands_dir / "vox.md").exists(), (
            "the cp genuinely failed -- nothing should have been written"
        )
        context = _additional_context(result)
        assert "Failed to deploy" in context
        assert "vox.md" in context


_STOCK_BASH = "/bin/bash"
# The empty-array "unbound variable" bug these tests regression-test was
# fixed in bash 4.4 -- a bash at or above that version, whatever binary it
# is, cannot reproduce it.
_MIN_BUGGY_BASH_VERSION = (4, 4)


def _stock_bash_version() -> tuple[int, int] | None:
    """Return (major, minor) for the bash at ``_STOCK_BASH``, or ``None``.

    ``None`` covers both "the binary is absent" and "its version couldn't be
    read" -- either way there is nothing to run the regression against.
    """
    if not Path(_STOCK_BASH).exists():
        return None
    result = subprocess.run(
        [
            _STOCK_BASH,
            "-c",
            'printf "%s.%s" "${BASH_VERSINFO[0]}" "${BASH_VERSINFO[1]}"',
        ],
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        return None
    major, _, minor = result.stdout.strip().partition(".")
    if not major.isdigit() or not minor.isdigit():
        return None
    return (int(major), int(minor))


def _stock_bash_skip_reason() -> str:
    """Return why ``TestStockMacBashCompatibility`` should skip, or "" to run.

    Checking binary presence alone is not enough: on Linux CI runners
    ``/bin/bash`` exists but is typically bash 5.x, which does not reproduce
    the bug this class regression-tests. Running there would report green
    without ever exercising the buggy code path -- false confidence, not
    coverage. The skip reason names the actual version found so a reader
    knows why the regression coverage isn't live in that environment.
    """
    version = _stock_bash_version()
    if version is None:
        return f"{_STOCK_BASH} not present or its version could not be read"
    if version >= _MIN_BUGGY_BASH_VERSION:
        return (
            f"bash {version[0]}.{version[1]} at {_STOCK_BASH} is >= "
            f"{_MIN_BUGGY_BASH_VERSION[0]}.{_MIN_BUGGY_BASH_VERSION[1]} "
            "(the version the bug was fixed in) -- found a newer bash "
            "where stock macOS's bash 3.2 was expected"
        )
    return ""


class TestStockMacBashCompatibility:
    """The hook must run under the exact bash stock macOS ships, not just PATH.

    Regression test: bash 3.2 (stock /bin/bash on every unmodified Mac, and
    still the default -- Apple has not shipped a newer bash since the GPLv3
    license change) raises "unbound variable" on "${empty_array[@]}" under
    `set -u`, even after `arr=()` -- a real bug in that bash version, fixed
    in 4.4. Every other test in this module spawns "bash" resolved via
    PATH, which on a dev machine is a newer Homebrew bash that doesn't
    reproduce the bug -- these tests spawn /bin/bash explicitly so this
    class of regression can't hide behind a modern shell again.
    """

    pytestmark = pytest.mark.skipif(
        bool(_stock_bash_skip_reason()), reason=_stock_bash_skip_reason()
    )

    def test_fresh_install_does_not_abort(self, tmp_path: Path) -> None:
        plugin_dir = _make_prod_plugin(tmp_path)
        home = tmp_path / "home"
        result = _run(plugin_dir, home, bash=_STOCK_BASH)
        assert result.returncode == 0, result.stderr
        assert "unbound variable" not in result.stderr
        deployed = _deployed_names(home)
        for name in _BARE:
            assert f"{name}.md" in deployed
        allow = _settings_allow(home)
        for name in _NAMESPACED_ONLY:
            assert f"Skill(vox:{name})" in allow

    def test_steady_state_session_does_not_abort(self, tmp_path: Path) -> None:
        # The common case after the one-time retirement: CLEANED is empty
        # on every subsequent session, which is exactly the condition that
        # aborted the whole hook under bash 3.2.
        plugin_dir = _make_prod_plugin(tmp_path)
        home = tmp_path / "home"
        _run(plugin_dir, home, bash=_STOCK_BASH)
        result = _run(plugin_dir, home, bash=_STOCK_BASH)
        assert result.returncode == 0, result.stderr
        assert "unbound variable" not in result.stderr

    def test_stale_grant_pruning_run_does_not_abort(self, tmp_path: Path) -> None:
        # The branch where CLEANED is non-empty -- populated, iterated, and
        # used to build STALE_SKILL_ARGS -- exercises every array expansion
        # this regression touched, not just the common empty-CLEANED path.
        plugin_dir = _make_prod_plugin(tmp_path)
        home = tmp_path / "home"
        commands_dir = _commands_dir(home)
        commands_dir.mkdir(parents=True)
        (commands_dir / "model.md").write_text(
            _pre_pr_content("model"), encoding="utf-8"
        )
        result = _run(plugin_dir, home, bash=_STOCK_BASH)
        assert result.returncode == 0, result.stderr
        assert "unbound variable" not in result.stderr
        context = _additional_context(result)
        assert "Cleaned retired commands: /model" in context
