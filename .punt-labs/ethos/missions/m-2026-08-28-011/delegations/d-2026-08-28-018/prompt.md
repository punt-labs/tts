You are working under ethos mission m-2026-08-28-011 (round 2 of the vox-1gub fix — the prior mission m-2026-08-28-005 was closed status=escalated after evaluator review found real gaps). Run `ethos mission show m-2026-08-28-011` first to read your full contract — write-set, success criteria with exact file:line citations, and context. Do not skip this.

Working directory: <repo>/.claude/worktrees/vox-1gub-linux-desktop (dedicated git worktree, branch fix/vox-1gub-linux-desktop-config — already has 2 commits from round 1; continue on this branch, do not switch branches or touch the main checkout).

Summary: an evaluator (djb) found that round 1's change to `DesktopInstaller.config_path()` (now raises ValueError on unsupported platforms instead of always returning a path) broke `vox doctor`'s `check_claude_desktop()` (src/punt_vox/doctor.py) — it calls config_path() bare and will now crash instead of degrading gracefully. Also two smaller findings: XDG_CONFIG_HOME should reject relative paths per the XDG basedir spec, and the config file write should be atomic (temp file + os.replace()) with correct permissions. Full details, exact fixes, and precedent to follow are in the mission contract.

`src/punt_vox/__main__.py` has the identical bug but is OUT OF SCOPE — it's write-set-locked by another mission right now (tracked separately as bead vox-i9ee). Do not touch it.

Commit incrementally per the contract's commit discipline. Do not open a PR. When done and `make check` passes, submit your result via `ethos mission result` for mission m-2026-08-28-011, round 1.