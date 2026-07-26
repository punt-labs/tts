# Resume: Packaging Remediation Effort

**Status:** Planning ("a plan for a plan"). No code moved yet. This document is
the starting point for a new session. Author: Claude (COO), 2026-07-25.

Read this top to bottom, then continue at **Next Step** (bottom).

---

## The problem

`src/punt_vox/` has grown a packaging problem, noticed by the operator while
reviewing the tool/module list:

1. **The root package is huge and flat — 62 top-level modules.** The organized
   parts of the tree are the *sub-packages*; the root itself is the junk drawer.
2. **Two packages have "programs" in the name** — `types_programs/` and
   `voxd/programs/` — which reads as confusing.
3. General worry that the packaging *design* has drifted and that our gates
   never caught it.

The operator has deep packaging experience (dependency flow, separation of
domain models that accept dependency injection from the implementations
required for full app integration) and wants this handled rigorously, not with
a cursory "group the root" pass.

## How this effort will be run (the frame the operator set)

These are constraints on the whole effort, not suggestions:

- **Ratchet, not a heroic reorg.** Encode packaging quality into the dev process
  the way the OO ratchet is encoded — measured, gated, paid down a little per
  commit. Do **not** attempt one big-bang repackaging branch.
- **PyCharm does the moves; Claude does not.** The mechanical relocation
  (move module, update all imports) is done by the operator in PyCharm, which is
  import-safe. Claude must **never** do the moves with Edit, `sed`, or scripts —
  it is unreliable at this. Claude's lane: the principles doc, the scoring tool
  (delegated), the baseline, the plan, and **verifying** each step (`make check`
  and the packaging ratchet green, diff review, drive the PR).
- **Pass 1 is move-only.** The first pass through all phases relocates code and
  changes nothing else. Dependency-injection changes, domain-model reshaping,
  and any implementation change are a separate **Pass 2**, later. Mixing
  relocation with implementation change produces an unreviewable, unrevertable
  diff.

## The six-phase plan

Strictly in order; each phase gates the next.

- **Phase 0 — Audit** (Claude). A written gap-analysis mapping every existing
  `PL-*` packaging rule to its enforcement status (enforced / advisory / gap),
  plus the enforcement findings below. Feeds Phase 1. *(In progress — see
  findings below; the full per-rule map is not yet written.)*
- **Phase 1 — Principles** (operator drives; Claude transcribes and sharpens).
  Keep the good existing rules, enforce them, fill the gaps. Deliverable: a
  packaging-principles standard. Operator's experience is the authority; Claude
  captures, does not invent.
- **Phase 2 — Scoring tool** (specialist builds; operator approves metrics).
  Extend `tools/coupling/` with the missing metrics. Every metric traces to a
  Phase-1 principle. Deterministic, exits 0/1, per-package and per-repo.
- **Phase 3 — Baseline** (mechanical, Claude). `.packaging-baseline.json` +
  `make check-packaging` wired into `make check` as a ratchet (no regression;
  >=1 improvement on touched packages), mirroring `check-oo`. This permanently
  closes the gate hole.
- **Phase 4 — Remediation plan** (design mission; operator rules). The
  *considered* target structure + an ordered sequence of small,
  behavior-preserving **moves** (Pass 1 = move-only), each improving the score.
- **Phase 5 — Incremental fixes** (operator/PyCharm move; Claude verifies).
  One small improvement per PR, over many PRs, until the target is reached.

## Phase 0 audit findings so far

Question the operator posed: do we have good packaging standards we simply
haven't enforced, or are there real gaps? **Answer: both.**

**Rules that exist** (in `../.claude/rules/`): `python-package-architecture.md`
(`PL-PA-1..6`), `python-module-design.md` (`PL-MD-1..5`), `python-coupling.md`
(`PL-CU-1..4`), `python-cohesion.md` (`PL-CO-1..3`), `python-project-layout.md`
(`PL-PL-1..3`), plus `python-oo-design-requirements.md` (`PY-OO-2` module
size/classes, `PY-OO-7`). Org standards: `punt-kit/standards/architecture.md`
(the one-engine/thin-clients model), `punt-kit/standards/python.md` (Package
Architecture section). These are decent — they state dependency direction
(`PL-MD-1`, inward-only), cohesion (`PL-CO`, `PL-MD-2`), coupling (`PL-CU`), and
layout (`PL-PL-1`).

**What is actually enforced.** `make check` runs `check-coupling` — a real
merge-base ratchet in `tools/coupling/` (`oo_coupling.py` is a shim into that
package) — gating: **efferent coupling, public-names (interface width),
circular imports, and package cohesion.** So coupling and cohesion *are* gated.
Also gated: `check-oo` (OO metrics) and `check-suppressions`.

**The gaps that let the drift happen:**

1. **No package-flatness / modules-per-package metric.** Nothing measures "62
   modules in one package." The rules cap module *size* (<=300 lines) and
   *classes per module* (<=3) but never modules-per-package. This is the exact
   hole the flat root grew through — no tool could complain.
2. **Dependency direction is a rule, not a gate.** `PL-MD-1` (core must not
   import presentation) is tooled as "grep + LLM review," not a failing check.
   A wrong-direction import passes `make check` today.
3. **Layer purity is not gated.** Nothing verifies the types layer imports
   clean of implementations.
4. **The coupling ratchet is regression-only.** It froze today's flat root as
   the baseline — it prevents getting *worse* but started from an
   already-drifted state and never pulls the structure toward a target.

**Conclusion:** good direction/cohesion/coupling standards that are
under-enforced (direction especially), plus a genuine gap (no
flatness/modules-per-package/layer-purity metric). Phase 1 keeps the good rules;
Phases 2-3 add the missing metrics and, where it matters, move from
regression-only to target-directed.

## Structural facts (the current tree, for the design)

Counts (`src/punt_vox/`, 2026-07-25):

- **Root: 62 flat top-level modules** — roughly ten concerns with no grouping:
  CLI (`__main__`, `cli_io`, `cli_music`, `cli_rec`), MCP server (`server`,
  `server_audio_tools`, `server_music_tool`), client + gateways (`client`,
  `client_sync`, `client_gateway`, `client_catalog_gateway`, `program_gateway`,
  `catalog_gateway`, `client_errors`, `client_env`), config/state (`config`,
  `private_state`, `frontmatter`, `managed_section`, `claude_md`, `guidance`),
  hooks + vibe (`hooks`, `hook_payload`, `hook_envelope`, `nudge_hook`,
  `vibe_nudge`, `vibe_command`, `vibe_trace`, `vibe`), logging (`log_format`,
  `logging_config`, `log_sanitize`, `log_append_handler`, `append_log`), types
  (`types`, `types_audio`, `types_errors`, `types_synthesis`, `types_health`),
  audio/synthesis (`core`, `normalize`, `playback`, `cache`, `resolve`,
  `voices`, `quips`, `music_hint`, `music_phrases`, `synthesis_batch`),
  paths/util (`dirs`, `paths`, `atomic_file`, `bare_name`, `markdown_fence`,
  `output`, `output_formatter`), install/keys (`daemon_restarter`,
  `desktop_install`, `keys`, `api_key_resolver`).
- **Sub-packages:** `providers/` (11), `service/` (10), `types_programs/` (11),
  `voxd/` (82, which includes `voxd/programs/` at 57), `assets/` (1).

**The two "programs" packages:**

- `types_programs/` (11) — the Program **domain types**: `format`, `mode`,
  `prompts` (`PromptSet`), `control` (the requests + `StartRequest.canonical_tag`),
  `status`/`status_views`, `identifiers`, `wire`, `playback_fault`, `vibe_label`.
  Dependency-free, importable without the daemon.
- `voxd/programs/` (57) — the daemon **implementation**: the `Program` state
  machine (`program`, `state`, `invariants`), stores (`filesystem_store`,
  `catalog`, `manifest`), producers (`producer`, `music_producer`), handlers,
  the six `*_signal` modules, `filler` + fill machinery, album modules,
  policies.

The layering (types separate from daemon implementation) is correct and worth
keeping — clients import the types without pulling in `voxd`. The problem is the
**name collision** and a broader **scattered-types** issue: types live as five
flat `types_*.py` at the root **plus** the `types_programs/` package.
Consolidating all types into one `types/` package would fix both (the scattered
root types and the collision — `types/program*` vs `voxd/programs` reads
cleanly as types vs engine). This is a Phase-1/Phase-4 design decision, not
settled here.

`voxd/programs/` at 57 modules is large but genuinely one cohesive subsystem; it
is somewhat over-decomposed (six near-identical `*_signal` modules). Lower
priority; revisit if podcast/audiobook bloat it.

## Open decisions (for the new session)

1. **Doc scope of the principles standard:** org-wide
   `punt-kit/standards/packaging.md` from the start, or vox-local first then
   promote? Claude's lean: org-wide (the drift is not vox-specific).
2. **How to start Phase 1:** operator dictates the principles, or Claude puts up
   a strawman of candidate principles (drawn from the audit) to red-line?
3. **Finish the Phase 0 audit doc first?** Complete the per-rule
   enforced/advisory/gap map before Phase 1, or shape principles first and let
   the audit confirm.

## Relevant files and tooling

- **Existing rules:** `../.claude/rules/python-package-architecture.md`,
  `python-module-design.md`, `python-coupling.md`, `python-cohesion.md`,
  `python-project-layout.md`, `python-oo-design-requirements.md`.
- **Org standards:** `../punt-kit/standards/architecture.md`,
  `../punt-kit/standards/python.md` (Package Architecture section).
- **Coupling tool (extend in Phase 2):** `tools/coupling/` — `metrics.py`,
  `thresholds.py`, `ratchet.py`, `scorer.py`, `packages.py`, `imports.py`,
  `graph.py`, `layout.py`, `baseline.py`, `compare.py`; shim `oo_coupling.py`;
  baseline `.oo-coupling-baseline.json`; `make check-coupling` /
  `make update-coupling`.
- **The model to mirror (OO ratchet):** `tools/oo_score.py`, `.oo-baseline.json`,
  `.oo-audit.jsonl`, `make check-oo` / `make update-oo`.
- **Makefile:** `make check` = `lint type docs test check-oo check-coupling
  check-suppressions`.

## Next step

Finish **Phase 0**: complete the gap-analysis into a per-rule map (each `PL-*`
rule -> enforced / advisory / gap, with the tooling that does or should enforce
it), reading the full standards rather than the partial read above. Then move to
**Phase 1** (principles), which the operator drives.

Do not move any code until Phases 1-4 are done and a remediation plan is
ratified. Pass 1 is move-only.
