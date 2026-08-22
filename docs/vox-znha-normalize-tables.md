# normalize.py Tables — Keep, Move, or Replace

## Problem

`src/punt_vox/normalize.py` is 708 lines. Two literals account for ~440 of
them:

- `_ABBREVIATIONS: dict[str, str]` — 35 whole-word expansions (`stderr` →
  "standard error"; `lol` → "laughing out loud"). Lines 16–51.
- `_PRONOUNCEABLE_ACRONYMS: frozenset[str]` — ~430 ALL-CAPS tokens
  (`HOME`, `START`, `JSON`, `AWS`) that TTS engines already speak correctly
  and should therefore *not* be spelled letter-by-letter. Lines 57–499.

The actual behavior — ten small functions covering camelCase splitting,
snake_case splitting, abbreviation expansion, acronym spacing, punctuation
stripping, and vibe-tag stripping — occupies roughly the last 200 lines.

The mix inflates `module_size` (708 vs the 300 target), depresses
`class_to_func_ratio` (data literals dominate the module), and blurs the
data/logic boundary: editing the acronym allowlist requires opening a file
otherwise full of regex and control flow.

## Options

### (a) Move the tables to a data file (JSON or TOML)

Load once at import via `importlib.resources.files("punt_vox") /
"normalize_data.json"`.

- Removes ~440 lines from `module_size` (708 → ~270, back under the 300
  target).
- One file open + parse at import time; negligible next to `re.compile`
  costs already at import.
- Adds a package-data entry and a JSON/TOML file to ship. `uv_build` picks
  up module-adjacent data files automatically.
- Costs the type checker's help: JSON is `dict[str, object]`, so the loader
  needs a small validator (or a `TypedDict` cast) to preserve
  `dict[str, str]` and `frozenset[str]`.
- Editing the allowlist stops touching a `.py` file — a real win for the
  people who will actually grow this list (voice/prose curation, not core
  engineers).

### (a′) Move the tables to a Python sibling module

`src/punt_vox/_normalize_data.py` exports `ABBREVIATIONS` and
`PRONOUNCEABLE_ACRONYMS`; `normalize.py` imports them.

- Same `module_size` win as (a) — normalize.py drops to ~270 lines.
- Zero I/O, zero packaging change, zero validator: the types survive
  (`dict[str, str]`, `frozenset[str]`), mypy still sees them, ruff still
  formats them.
- Reads the same way at every call site.
- The only thing (a) does that (a′) does not: hide the data from a `.py`
  grep. That is a curation ergonomics question, not a code-quality one.

### (b) Replace with a maintained library

| Library | Version | Scope | Overlap with vox tables |
|---|---|---|---|
| `inflect` | 7.5.0 (active) | Pluralization, singularization, indefinite articles, number-to-words | None. No abbreviation expansion, no ALL-CAPS allowlist, no camelCase awareness. |
| `num2words` | 0.5.14 (active) | Number → words, many locales | None. Vox does not currently normalize numbers; if it starts, `num2words` is the right choice — but that is *additive*, not a replacement. |
| `nemo-text-processing` | 1.2.0 (NVIDIA) | ASR/TTS text normalization (numbers, dates, addresses, money) via WFSTs | None for our tables. Heavy: pulls `pynini`/OpenFst, a C++ build. Wrong tool for a Python package that must `pip install` cleanly across five providers. |

No library covers the two domain-specific things vox's tables actually
encode:

1. **Programmer abbreviations** (`stderr`, `ctx`, `repo`, `impl`). This is
   a curated in-house glossary of dev jargon plus internet slang; not a
   general linguistic problem.
2. **English-word ALL-CAPS allowlist** — the *inverse* of a normalization
   list. Vox spells short ALL-CAPS tokens letter-by-letter *unless* they
   are common English words (`HOME`, `START`, `TRUE`) or known-pronounceable
   acronyms (`JSON`, `AWS`). No library ships this list because no other
   library needs it — it is peculiar to the ALL_CAPS-identifier-in-speech
   problem, which is a TTS-of-source-code concern.

### (c) Trie / marisa-trie

Overkill. `frozenset` lookup is already O(1) on ~450 items (a few tens of
kilobytes). A trie would only pay off in the millions.

## Recommendation

**Adopt (a′): extract the two literals into
`src/punt_vox/_normalize_data.py`, keep them as typed Python constants.**

Rationale:

- Retires the whole `module_size` overrun in one step — normalize.py falls
  from 708 to ~270 lines, back inside the 300 threshold without touching a
  single function.
- Zero runtime cost, zero packaging change, zero new validator, zero
  concessions to the type system.
- Data and logic separate on disk; the two files can be reviewed and
  changed independently.
- The leading underscore signals private — no `__all__` promise, no public
  API surface added.

Prefer (a′) over (a) unless a concrete non-engineer curator asks for JSON.
The JSON version is a strictly larger change (packaging, loader, schema)
for the same `module_size` payoff.

Reject (b): the libraries do not cover what our tables cover. `inflect`
and `num2words` are worth revisiting the day vox starts normalizing
*numbers*, but that is a separate feature, not a replacement for these
tables.

Reject (c): premature.

## Follow-up beads if adopted

- **If (a′)**: one small bead — "extract `_ABBREVIATIONS` and
  `_PRONOUNCEABLE_ACRONYMS` into `_normalize_data.py`; import into
  `normalize.py`; update tests to import from either location; confirm
  `oo_score` shows `module_size` improvement on `normalize.py`."
- **If (a) is preferred instead**: additional bead — "package `normalize_data.json`
  under `[tool.uv.build-backend]`, write `_load_normalize_data()` with a
  `TypedDict` schema and a fail-fast validator, add a round-trip test."
- **Number normalization (separable, not in scope here)**: if we later
  want digits spoken correctly ("v1.2.3" → "version one point two point
  three", "3.14" → "three point one four"), file a bead to adopt
  `num2words` as an additive normalization step. That is a feature bead,
  not a follow-up to this one.
