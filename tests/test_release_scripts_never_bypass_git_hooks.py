"""Regression test: release scripts must not bypass git hooks (pkit-hsyi)."""

from __future__ import annotations

from pathlib import Path


def test_release_scripts_never_bypass_git_hooks() -> None:
    """No release-path script may pass ``--no-verify`` to git.

    The org CLAUDE.md bans ``--no-verify`` outright. ``release-plugin.sh``
    previously carried it on the "prepare plugin for release" commit; this
    test greps both release-path scripts so a reintroduction fails
    immediately, not on the next release. Comments in the target scripts
    are stripped before scanning, so prose describing the ban (e.g. in
    ``restore-dev-plugin.sh``'s CONTRACT comment) does not trigger a
    false positive.
    """
    root = Path(__file__).parent.parent
    targets = [
        root / "scripts" / "release-plugin.sh",
        root / "scripts" / "restore-dev-plugin.sh",
    ]
    for path in targets:
        text = path.read_text(encoding="utf-8")
        code_only_lines: list[str] = []
        for raw in text.splitlines():
            stripped = raw.lstrip()
            if stripped.startswith("#"):
                continue
            code, sep, _ = raw.partition("#")
            code_only_lines.append(code if sep else raw)
        code_only = "\n".join(code_only_lines)
        assert "--no-verify" not in code_only, (
            f"{path.name} reintroduced --no-verify — org CLAUDE.md bans "
            "the flag; let the hooks run or surface a real hook failure."
        )
