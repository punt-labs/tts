Bead vox-otj9, P1, in <repo>. Run `bd show vox-otj9` — I filed it with full reproduction. You wrote the code this regressed in; that is why you are on it.

THE BUG. `vox say --provider X` fails for every provider except the one already configured. Reproduced against main (9b9eaba) with state = elevenlabs/eleven_v3:

    vox say "test" --provider polly   -> model 'eleven_v3' is not available for provider 'polly' (available: )
    vox say "test" --provider say     -> same
    vox say "test" --provider espeak  -> same
    vox say "test" --provider openai  -> model 'eleven_v3' is not available for provider 'openai' (available: tts-1, tts-1-hd, gpt-4o-mini-tts)

Four for four. The `--provider` flag is useless for the entire purpose it exists for.

CAUSE. `SessionSpec.fill` (session_spec.py:106) resolves provider and model INDEPENDENTLY. `_resolve_provider` honours the override; `_resolve_model`, with no model override, falls through to STATE's model — which is scoped to state's PROVIDER — and validates it against the new one. It passes only if both providers happen to share a model name.

READ THIS PART CAREFULLY, because it is the interesting bit. This is NOT the modelless case your design anticipated. `docs/provider-authority.md` §3.9 reasoned carefully about an EMPTY model and correctly preserved polly/say/espeak — that reasoning is intact and I am not asking you to revisit it. The broken case is a STALE model: populated, valid for its own provider, wrong for this call. openai is the proof — it has models of its own and still fails. The design asked "what if the model is absent?" and never asked "what if the model belongs to a different provider?"

The cascade invariant — changing the provider resets the model — IS implemented in `mic:provider` and in the panel's `_commit_provider`. It is absent from the per-call override path. So the two ways of choosing a provider disagree, and only one of them is exercised by tests.

THE FIX. When the provider is overridden and no model override is supplied, the model must not come from state — resolve to None and let the provider default apply. State's model is meaningful only in the context of state's provider; carrying it across an override is the defect. Apply the same rule at every per-call override site: CLI `--provider`, `mic:unmute`, `mic:rec new`, and the panel voice preview. Verify that list yourself by grepping rather than trusting mine.

THE REGRESSION TEST IS THE POINT. Parameterise over all five providers: with state set to one, a per-call override to each of the other four must succeed with no model argument. Every existing test overrides both or neither, which is exactly why four providers broke with a full green suite. Make the omission impossible to repeat.

Do not fix this by loosening model validation. F7 exists to refuse a hand-edited incompatible pair and that must keep working — a test proving F7 still refuses is part of this change.

BRANCH: `fix/vox-otj9-provider-override` off current main. WORK IN A WORKTREE at `.claude/worktrees/vox-otj9` — the main working tree is occupied by another agent (rmh, on fix/vox-prfr-guide-staleness). Do not touch it, do not switch its branch. Pin every cwd-resolving tool: `git -C`, `uv --project`.

Do NOT use `git commit -a` — it sweeps `.punt-labs/vox/vox.md`, the daemon's live session state; it leaked into a PR that way last night. Stage explicit paths.

`make check` green before every commit; no `noqa`, no `type: ignore`, no `xfail`. Suppression count on main is 201 and CI's ratchet is merge-base scoped. `check-coupling`'s baseline is defective (vox-orvz) — verify a REGRESSED verdict against the base blob and label relaxations Class A vs Class B distinctly.

No push, no PR, no `make install`, no daemon restart, no bd close. Report with SHAs and state what your evidence does not cover.