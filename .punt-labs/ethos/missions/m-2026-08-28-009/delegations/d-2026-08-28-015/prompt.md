You are the evaluator for ethos mission m-2026-08-28-005. Run `ethos mission show m-2026-08-28-005` to read the contract, and `ethos mission results m-2026-08-28-005` (or `ethos mission log m-2026-08-28-005`) to see the worker's (adb) round-1 submission.

Working directory: <repo>/.claude/worktrees/vox-1gub-linux-desktop (a dedicated git worktree, branch fix/vox-1gub-linux-desktop-config — stay in this worktree, do not touch the main checkout or switch branches).

Review the two commits on this branch (`git log --oneline -5`, `git diff 97ebc61..HEAD`) that add Linux support to `DesktopInstaller.config_path()` (src/punt_vox/desktop_install.py) and consolidate the platform-dispatch logic in src/punt_vox/cli_desktop.py, plus the new tests in tests/test_desktop_install.py and tests/test_cli_desktop.py.

You are the security/correctness reviewer (evaluator ≠ worker per this repo's mission model). Specifically check:
1. Does the XDG_CONFIG_HOME handling correctly follow the XDG basedir spec (unset and empty-string both fall back to ~/.config)? Any path traversal or injection risk from an attacker-controlled XDG_CONFIG_HOME (e.g. relative paths, symlink issues)?
2. Is the platform dispatch genuinely consolidated to one place (no lingering duplicate `platform.system()` checks), matching the mission's PY-OO-7 requirement?
3. Does `make check` actually pass (re-run it yourself, don't trust the log)?
4. Any secret-handling regression relevant to PL-PP-4 (the module's own documented threat model — no API keys in the Claude Desktop config)?
5. Test quality: do the new tests actually exercise the Linux path and the XDG override, or do they just assert trivial things?

Report a pass/fail verdict with specific findings (file:line) for anything you'd fix before this merges. Do not edit files — this is a review pass. If you find a real issue, describe it precisely so the leader can decide whether to send it back for another round.