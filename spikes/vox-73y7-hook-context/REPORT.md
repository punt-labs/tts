# vox-73y7 REPORT — hook-fanout context spike (DES-070)

**Verdict, in the bead's acceptance terms: hook fanout reconstructs enough
state — the rolling store is load-bearing, not decorative.** The
seed-only condition also passes, but the seed here is *built from the
same fanout ledger*, so it does not make the fanout dispensable: it shows
that a curated bounded snapshot is the right way to *consume* the store,
and a raw last-N tail is the wrong way (it dropped the session goal at
one timepoint). DES-070's two-layer design stands, with one wording
correction that is a hard implementation requirement: **the per-session
monotonic sequence must be stamped at the SENDER, not at receipt in
voxd** — receiver-side sequencing provably cannot detect drops and its
numbering collides across store restarts (both observed live in this
run, including against this spike's own first analysis pass).

Adjudicated run: `results/run_20260830_235638/` (ledger, five timepoint
captures, gap windows, analysis JSONs, reconstructions). EL retest
artifacts: `results/el_retest/`. All committed artifacts are
path/username-sanitized; ledger payloads passed recursive
credential-key redaction at persist time.

## (a) Realism: what hook payloads carry

One real claude session (Opus, isolated scratch project) worked a
multi-file task — 8 files created/edited, a crash reproduced by
traceback, fixes, 27 tests green — with ALL hook events relayed:
SessionStart, UserPromptSubmit, PreToolUse (matcher `*`), PostToolUse
(matcher `*`), Notification, Stop, SubagentStart/Stop, PreCompact,
SessionEnd wired; the first six fired during the working session (the
profile denies Task and no compaction occurred), and SessionEnd fired
once at teardown (00:31:40, when the tmux session was killed) — arriving
without relay stamps and with an EMPTY payload (the store's
non-object-params fallback records the event and drops the content)
because the fork's process tree was being torn down mid-relay. It
landed after the mid-run analysis snapshots; `field_inventory.json` is
regenerated over the final 96-record ledger and includes it, while
`latency.json` remains the documented pre-SessionEnd snapshot (see (b)).

Field inventory (`field_inventory.json`, regenerated from the committed
ledger; state = work content, pointer = path to state elsewhere on
host, metadata = plumbing):

| event | n | payload bytes p50/p95/max | state bytes p50/p95/max | state fields |
|---|---|---|---|---|
| PostToolUse | 36 | 2115 / 7120 / 11425 | 1543 / 6263 / 10853 | tool_name, tool_input, tool_response |
| PreToolUse | 38 | 711 / 2612 / 3725 | 125 / 2079 / 3192 | tool_name, tool_input |
| UserPromptSubmit | 7 | 857 / 1971 / 1971 | 223 / 1530 / 1530 | prompt (verbatim) |
| Stop | 7 | 875 / 1535 / 1535 | 42 / 988 / 988 | last_assistant_message |
| Notification | 6 | 761 / 762 / 762 | 34 | message |
| SessionStart | 1 | 389 | 0 | — (source, model, cwd) |
| SessionEnd | 1 | 2 | 0 | — (teardown-time; empty payload, see above) |

**The payloads are state, not metadata.** PostToolUse carries the FULL
tool response — complete test-run output, tracebacks, written file
contents, structured diffs (`structuredPatch`), stdout/stderr — at a
median 1.5KB and up to ~11KB of pure state per event. UserPromptSubmit
carries the user's prompt verbatim. Stop carries
`last_assistant_message`: the agent's own end-of-turn account, which
turned out to be the single highest-value reconstruction field
(discovered in this run; the inventory's state list and the
reconstructor were extended to use it). Every event also carries
`transcript_path` — a pointer to the session's full JSONL history,
readable by a same-host voxd, worthless to a remote one.

## Context reconstruction: the verdict core

The reconstructor is deterministic (template over extracted fields — no
LLM), so every line of an answer traces to a ledger record and a FAIL
indicts the payloads, not a model. Two conditions per timepoint, same
answer shape: **ledger-tail** (last 15 raw events at the cutoff — DES-070
Layer 2 as stored) and **seed-only** (a ~10KB hand-picked snapshot built
from the ledger visible at the cutoff — Layer 1's shape). Five timepoints
sampled (bead minimum four); cutoffs are file-order indices because
receiver `recv_seq` collides across store restarts (see (c)).

**Post-adjudication sampler fix (labels unchanged here).** This run's
harness let the `end` trigger fire on ANY `Stop`, but Claude Code emits
`Stop` at every turn boundary — so the auto-sampled "end" (cutoff 46) is
really the first turn's end, graded honestly below as
`end-of-main-task`. The sampler now chains every timepoint strictly
after its predecessor (early → mid-debug → post-fix → end), making `end`
the first Stop after post-fix. Replayed over this run's committed
ledger, the chained sampler would have placed `end` at cutoff 88 (the
first Stop after the manual post-fix cutoff 79); the graded
reconstructions below are unchanged — they were produced at the recorded
cutoffs and stay byte-reproducible from the committed ledger.

**Grading rubric.** PASS: the answer names the current goal AND the
specific in-flight activity (file/command/result) matching ground truth,
and any stated failure state is consistent. PARTIAL: goal or activity
correct, the other missing or stale, no contradictions. FAIL: wrong or
empty on both, or contradicts ground truth. Ground truth is the pane
capture taken at the same instant as the cutoff (plus, for mid-debug,
the session's own timestamped transcript,
`ground_truth_transcript.txt`).

### t1 `early` — cutoff 5, ground truth `capture_early.txt`

Ground truth (pane): the task prompt on screen; the fork says "I'll
start by running the test suite as instructed." while its first tool
line reads `⎿ textstat/stats.py` — it is actually reading the source.

```text
[ledger-tail @ early] What was I just doing?
goal: You are working in the textstat project in the current directory. Work through these steps strictly in order. 1. IMPORTANT: as your VERY FIRST action, before reading or editing any file, run the test suite with: python3 -m unittest discover -s tests -v 2>&1 2. It fails. Read the failure output, then read only the code the failure points at, find the bug in textstat/stats.py, fix it, and re-run ...
recent actions:
  - Read: <scratch>/vox73y7-235638/project/textstat/stats.py
last result: """Tiny text statistics helpers.""" def word_count(text): """Number of whitespace-separated words in text.""" return len(text.split(",")) def longest_word(text): """The longest word in text; empty string for empty input.""" words = text.split() if not words: return "" return max(words, key=len) <scratch>/vox73y7-235638/project/textstat/stats.py text
open failure: none
files in play: <scratch>/vox73y7-235638/project/textstat/stats.py
agent last said: (nothing yet)
```

```text
[seed-only @ early] What was I just doing?
goal: You are working in the textstat project in the current directory. Work through these steps strictly in order. 1. IMPORTANT: as your VERY FIRST action, before reading or editing any file, run the test suite with: python3 -m unittest discover -s tests -v 2>&1 2. It fails. Read the failure output, then read only the code the failure points at, find the bug in textstat/stats.py, fix it, and re-run ...
recent actions:
  - Read: """Tiny text statistics helpers."""
last result: Read: """Tiny text statistics helpers.""" def word_count(text): """Number of whitespace-separated words in text.""" return len(text.split(",")) def longest_word(text): """The longest word in text; empty string for empty input.""" words = text.split() if not words: return "" return max(words, key=len) <scratch>/vox73y7-235638/project/textstat/stats.py text
open failure: none
files in play: <scratch>/vox73y7-235638/project/textstat/stats.py
agent last said: (nothing yet)
```

Both name the goal verbatim and the actual activity (reading
`stats.py` — including catching the fork doing the opposite of what it
announced). **ledger-tail: PASS. seed-only: PASS.**

### t2 `end-of-main-task` — cutoff 46, ground truth `capture_end.txt`

Ground truth (pane): README example fixed to match real output, a final
suite+CLI verification, and the reply `DONE`.

```text
[ledger-tail @ end-of-main-task] What was I just doing?
goal: (unknown)
recent actions:
  - Write: <scratch>/vox73y7-235638/project/README.md
  - Bash: python3 stats_cli.py README.md
  - Edit: <scratch>/vox73y7-235638/project/README.md
  - Bash: printf 'Two short sentences. Counted right here!' > .tmp_notes.txt && python3 stats_cli.py .tmp_notes.txt && rm .tmp_...
  - Bash: python3 -m unittest discover -s tests -v 2>&1
last result: ability.SentenceCountTests.test_text_without_terminator) ... ok test_counts_letters (test_stats.CharFrequenciesTests.test_counts_letters) ... ok test_empty_text (test_stats.CharFrequenciesTests.test_empty_text) ... ok test_ignores_non_letters (test_stats.CharFrequenciesTests.test_ignores_non_letters) ... ok test_is_case_insensitive (test_stats.CharFrequenciesTests.test_is_case_insensitive) ... ...
open failure: none
files in play: <scratch>/vox73y7-235638/project/README.md
agent last said: DONE
```

```text
[seed-only @ end-of-main-task] What was I just doing?
goal: You are working in the textstat project in the current directory. Work through these steps strictly in order. 1. IMPORTANT: as your VERY FIRST action, before reading or editing any file, run the test suite with: python3 -m unittest discover -s tests -v 2>&1 2. It fails. Read the failure output, then read only the code the failure points at, find the bug in textstat/stats.py, fix it, and re-run ...
recent actions:
  - Edit: <scratch>/vox73y7-235638/project/README.md
  - Bash: 6
  - Bash: test_readability.AvgWordsPerSentenceTests.test_empty_text_is_zero) ... ok
last result: internal_punctuation) ... ok test_returns_stripped_word (test_stats.LongestWordTests.test_returns_stripped_word) ... ok test_counts_whitespace_separated_words (test_stats.WordCountTests.test_counts_whitespace_separated_words) ... ok test_single_word (test_stats.WordCountTests.test_single_word) ... ok ---------------------------------------------------------------------- Ran 21 tests in 0.000s OK
open failure: none
files in play: <scratch>/vox73y7-235638/project/textstat/stats.py, <scratch>/vox73y7-235638/project/tests/test_stats.py, <scratch>/vox73y7-235638/project/textstat/__init__.py, <scratch>/vox73y7-235638/project/textstat/readability.py, <scratch>/vox73y7-235638/project/tests/test_readability.py, <scratch>/vox73y7-235638/project/stats_cli.py, <scratch>/vox73y7-235638/project/README.md
agent last said: DONE
```

The tail's activity, green suite, and `DONE` all match — but its goal is
`(unknown)`: 46 events into a long single turn, the UserPromptSubmit had
scrolled out of the 15-event window. The seed, curating over the whole
visible ledger under the same 10KB budget, keeps the goal and the full
file set. **ledger-tail: PARTIAL (activity right, goal lost to the raw
window). seed-only: PASS.** This is the sharpest single data point in
the spike: raw last-N is the wrong consumption shape; curation over the
same feed is the right one.

### t3 `mid-debug` — cutoff 65, ground truth `ground_truth_transcript.txt` (entries at/before 00:05:51)

Ground truth (transcript): at 00:05:37 the fork ran
`python3 stats_cli.py` (got usage, exit 1); at 00:05:50 it ran
`python3 stats_cli.py nosuchfile.txt` and got the `FileNotFoundError`
traceback; its next message begins "The reported bug didn't reproduce as
described."

```text
[ledger-tail @ mid-debug] What was I just doing?
goal: One more task: a user reported stats_cli.py crashes when run with no arguments. Reproduce it first: run python3 stats_cli.py 2>&1 and show the traceback. Then fix the script to print usage to stderr and exit with status 2 when no path is given, add a test for that behavior to a new tests/test_cli.py using subprocess, and run python3 -m unittest discover -s tests -v 2>&1 until the whole suite pa...
recent actions:
  - Bash: python3 -m unittest discover -s tests -v 2>&1
  - Bash: python3 -c " from textstat.readability import sentence_count as s cases = [ ('Dr. Smith arrived. He sat.', 2), ('Use ...
  - Bash: python3 stats_cli.py README.md
  - Bash: python3 stats_cli.py 2>&1; echo "exit=$?"
  - Bash: python3 stats_cli.py nosuchfile.txt 2>&1; echo "exit=$?"
last result: Traceback (most recent call last): File "<scratch>/vox73y7-235638/project/stats_cli.py", line 22, in <module> sys.exit(main(sys.argv)) ~~~~^^^^^^^^^^ File "<scratch>/vox73y7-235638/project/stats_cli.py", line 13, in main with open(argv[1], encoding="utf-8") as handle: ~~~~^^^^^^^^^^^^^^^^^^^^^^^^^^^ FileNotFoundError: [Errno 2] No such file or directory: 'nosuchfile.txt' exit=1
open failure: Traceback (most recent call last): File "<scratch>/vox73y7-235638/project/stats_cli.py", line 22, in <module> sys.exit(main(sys.argv)) ~~~~^^^^^^^^^^ File "<scratch>/vox73y7-235638/project/stats_cli.py", line 13, in main with open(argv[1], encoding="utf-8") as handle: ~~~~^^^^^^^^^^^^^^^^^^^^^^^^^^^ FileNotFoundError: [Errno 2] No such file or directory: 'nosuchfile.txt' exit=1
files in play: <scratch>/vox73y7-235638/project/textstat/readability.py
agent last said: TDD cycle complete: the test failed with `AssertionError: 3 != 2`, then the abbreviation handling in `textstat/readability.py:60` (`_is_sentence_end`) turned it green with all 22 tests passing and the CLI still working. One thing worth flagging: the abbreviation list is deliberately conservative, so a sentence genuinely ending in an abbreviation (`"...and so on, etc."`) is undercounted. That tr...
```

```text
[seed-only @ mid-debug] What was I just doing?
goal: One more task: a user reported stats_cli.py crashes when run with no arguments. Reproduce it first: run python3 stats_cli.py 2>&1 and show the traceback. Then fix the script to print usage to stderr and exit with status 2 when no path is given, add a test for that behavior to a new tests/test_cli.py using subprocess, and run python3 -m unittest discover -s tests -v 2>&1 until the whole suite pa...
recent actions:
  - Bash: 158
  - Bash: usage: stats_cli.py <file>
  - Bash: Traceback (most recent call last):
last result: t recent call last): File "<scratch>/vox73y7-235638/project/stats_cli.py", line 22, in <module> sys.exit(main(sys.argv)) ~~~~^^^^^^^^^^ File "<scratch>/vox73y7-235638/project/stats_cli.py", line 13, in main with open(argv[1], encoding="utf-8") as handle: ~~~~^^^^^^^^^^^^^^^^^^^^^^^^^^^ FileNotFoundError: [Errno 2] No such file or directory: 'nosuchfile.txt' exit=1
open failure: Traceback (most recent call last): File "<scratch>/vox73y7-235638/project/stats_cli.py", line 22, in <module> sys.exit(main(sys.argv)) ~~~~^^^^^^^^^^ File "<scratch>/vox73y7-235638/project/stats_cli.py", line 13, in main with open(argv[1], encoding="utf-8") as handle: ~~~~^^^^^^^^^^^^^^^^^^^^^^^^^^^ FileNotFoundError: [Errno 2] No such file or directory: 'nosuchfile.txt' exit=1
files in play: <scratch>/vox73y7-235638/project/textstat/stats.py, <scratch>/vox73y7-235638/project/tests/test_stats.py, <scratch>/vox73y7-235638/project/textstat/__init__.py, <scratch>/vox73y7-235638/project/textstat/readability.py, <scratch>/vox73y7-235638/project/tests/test_readability.py, <scratch>/vox73y7-235638/project/stats_cli.py, <scratch>/vox73y7-235638/project/README.md
agent last said: TDD cycle complete: the test failed with `AssertionError: 3 != 2`, then the abbreviation handling in `textstat/readability.py:60` (`_is_sentence_end`) turned it green with all 22 tests passing and the CLI still working. One thing worth flagging: the abbreviation list is deliberately conservative, so a sentence genuinely ending in an abbreviation (`"...and so on, etc."`) is undercounted. That tr...
```

Both name the crash-report goal, the exact repro commands, and the live
traceback as the open failure — precisely what was happening at
00:05:50. Caveat noted honestly: `agent last said` quotes the PREVIOUS
turn's Stop (the TDD summary), which is correct as "last said" but a
consumer must not read it as the current turn's status. **ledger-tail:
PASS. seed-only: PASS.**

### t4 `post-crash-fix` — cutoff 75, ground truth `capture_mid_debug.txt`

Ground truth (pane): the DONE3 summary — "The reported bug didn't
reproduce as described … exit 2 (as requested; was 1) …
tests/test_cli.py drives the script through subprocess …".

```text
[ledger-tail @ post-crash-fix] What was I just doing?
goal: One more task: a user reported stats_cli.py crashes when run with no arguments. Reproduce it first: run python3 stats_cli.py 2>&1 and show the traceback. Then fix the script to print usage to stderr and exit with status 2 when no path is given, add a test for that behavior to a new tests/test_cli.py using subprocess, and run python3 -m unittest discover -s tests -v 2>&1 until the whole suite pa...
recent actions:
  - Bash: python3 stats_cli.py nosuchfile.txt 2>&1; echo "exit=$?"
  - Write: <scratch>/vox73y7-235638/project/stats_cli.py
  - Write: <scratch>/vox73y7-235638/project/tests/test_cli.py
  - Bash: python3 -m unittest discover -s tests -v 2>&1
  - Bash: python3 stats_cli.py 2>&1; echo "exit=$?"; python3 stats_cli.py nosuchfile.txt 2>&1; echo "exit=$?"
last result: usage: stats_cli.py <file> exit=2 stats_cli.py: cannot read nosuchfile.txt: No such file or directory exit=1
open failure: none
files in play: <scratch>/vox73y7-235638/project/stats_cli.py, <scratch>/vox73y7-235638/project/tests/test_cli.py
agent last said: All 27 tests pass. Summary of what I found and changed: **The reported bug didn't reproduce as described.** `python3 stats_cli.py` with no arguments never crashed — it already printed usage to stderr and exited 1. The only traceback came from a *missing file* (`FileNotFoundError`), which is almost certainly the crash the user actually hit. Changes to `stats_cli.py`: - No path (or too many paths...
```

```text
[seed-only @ post-crash-fix] What was I just doing?
goal: One more task: a user reported stats_cli.py crashes when run with no arguments. Reproduce it first: run python3 stats_cli.py 2>&1 and show the traceback. Then fix the script to print usage to stderr and exit with status 2 when no path is given, add a test for that behavior to a new tests/test_cli.py using subprocess, and run python3 -m unittest discover -s tests -v 2>&1 until the whole suite pa...
recent actions:
  - Write: """Tests for stats_cli.py, driven as a real subprocess."""
  - Bash: dsPerSentenceTests.test_mean_across_sentences) ... ok
  - Bash: usage: stats_cli.py <file>
last result: Bash: usage: stats_cli.py <file> exit=2 stats_cli.py: cannot read nosuchfile.txt: No such file or directory exit=1
open failure: none
files in play: <scratch>/vox73y7-235638/project/textstat/stats.py, <scratch>/vox73y7-235638/project/tests/test_stats.py, <scratch>/vox73y7-235638/project/textstat/__init__.py, <scratch>/vox73y7-235638/project/textstat/readability.py, <scratch>/vox73y7-235638/project/tests/test_readability.py, <scratch>/vox73y7-235638/project/stats_cli.py, <scratch>/vox73y7-235638/project/README.md, <scratch>/vox73y7-235638/project/tests/test_cli.py
agent last said: All 27 tests pass. Summary of what I found and changed: **The reported bug didn't reproduce as described.** `python3 stats_cli.py` with no arguments never crashed — it already printed usage to stderr and exited 1. The only traceback came from a *missing file* (`FileNotFoundError`), which is almost certainly the crash the user actually hit. Changes to `stats_cli.py`: - No path (or too many paths...
```

Goal, fix activity, resolved failure state, and the agent's own summary
all match the pane. **ledger-tail: PASS. seed-only: PASS.**

### t5 `post-reverify` — cutoff 79, ground truth `capture_post_fix.txt`

Ground truth (pane): "27 tests, 0 failures, 0 errors. GREEN".

```text
[ledger-tail @ post-reverify] What was I just doing?
goal: Re-run the full suite once more to confirm: python3 -m unittest discover -s tests -v 2>&1 and reply GREEN if everything passes.
recent actions:
  - Write: <scratch>/vox73y7-235638/project/stats_cli.py
  - Write: <scratch>/vox73y7-235638/project/tests/test_cli.py
  - Bash: python3 -m unittest discover -s tests -v 2>&1
  - Bash: python3 stats_cli.py 2>&1; echo "exit=$?"; python3 stats_cli.py nosuchfile.txt 2>&1; echo "exit=$?"
  - Bash: python3 -m unittest discover -s tests -v 2>&1
last result: ability.SentenceCountTests.test_text_without_terminator) ... ok test_counts_letters (test_stats.CharFrequenciesTests.test_counts_letters) ... ok test_empty_text (test_stats.CharFrequenciesTests.test_empty_text) ... ok test_ignores_non_letters (test_stats.CharFrequenciesTests.test_ignores_non_letters) ... ok test_is_case_insensitive (test_stats.CharFrequenciesTests.test_is_case_insensitive) ... ...
open failure: none
files in play: <scratch>/vox73y7-235638/project/stats_cli.py, <scratch>/vox73y7-235638/project/tests/test_cli.py
agent last said: 27 tests, 0 failures, 0 errors. GREEN
```

```text
[seed-only @ post-reverify] What was I just doing?
goal: Re-run the full suite once more to confirm: python3 -m unittest discover -s tests -v 2>&1 and reply GREEN if everything passes.
recent actions:
  - Bash: dsPerSentenceTests.test_mean_across_sentences) ... ok
  - Bash: usage: stats_cli.py <file>
  - Bash: dsPerSentenceTests.test_mean_across_sentences) ... ok
last result: internal_punctuation) ... ok test_returns_stripped_word (test_stats.LongestWordTests.test_returns_stripped_word) ... ok test_counts_whitespace_separated_words (test_stats.WordCountTests.test_counts_whitespace_separated_words) ... ok test_single_word (test_stats.WordCountTests.test_single_word) ... ok ---------------------------------------------------------------------- Ran 27 tests in 0.089s OK
open failure: none
files in play: <scratch>/vox73y7-235638/project/textstat/stats.py, <scratch>/vox73y7-235638/project/tests/test_stats.py, <scratch>/vox73y7-235638/project/textstat/__init__.py, <scratch>/vox73y7-235638/project/textstat/readability.py, <scratch>/vox73y7-235638/project/tests/test_readability.py, <scratch>/vox73y7-235638/project/stats_cli.py, <scratch>/vox73y7-235638/project/README.md, <scratch>/vox73y7-235638/project/tests/test_cli.py
agent last said: 27 tests, 0 failures, 0 errors. GREEN
```

**ledger-tail: PASS. seed-only: PASS.**

**Score: ledger-tail 4 PASS / 1 PARTIAL / 0 FAIL; seed-only 5 PASS.**

## (b) Latency: hook fire to store-visible

Measured hook-command start (`date +%s%N` in the relay script, i.e. the
moment Claude Code exec's the hook) to store receipt (`time.time_ns` at
stamping), same host, same wall clock, over the whole session
(`latency.json`):

| metric | n | p50 | p95 | max |
|---|---|---|---|---|
| overall latency_ms | 95 | 32.3 | 43.2 | 96.3 |
| PostToolUse latency_ms | 36 | 31.3 | 39.9 | 69.6 |
| PreToolUse latency_ms | 38 | 32.3 | 41.6 | 96.3 |
| UserPromptSubmit latency_ms | 7 | 33.4 | 56.3 | 56.3 |
| Stop latency_ms | 7 | 32.8 | 39.5 | 39.5 |

~32ms median, worst case under 100ms, dominated by interpreter and
mcp-proxy process startup — the rolling store sees session state
effectively in real time on loopback. The committed `latency.json`
(n=95, `unstamped_records: 0`) was snapshotted before the teardown-time
SessionEnd landed; a re-run over the final 96-record ledger reports the
same percentiles with that one unstamped record excluded and counted
(`unstamped_records: 1`).

## (c) Gap detection: loss is detectable only with SENDER-side sequences

The load-bearing negative result first: **a receiver-assigned sequence
(DES-070's current wording: "voxd stamps a monotonic per-session
sequence") cannot detect loss.** Events lost while the store is down
were never received, so receiver sequences stay contiguous by
construction — and each store restart resets them to 1, which reads as a
reset, not a gap. This bit the spike itself: the harness's first
analysis pass bounded timepoints by `recv_seq` and silently mixed
post-restart records into earlier timepoints until the cutoffs were
rebuilt on file order.

The spike's relay wrapper therefore stamps a **sender-side** per-session
`relay_seq` (flock-guarded counter file owned by the launcher, one
increment per hook fire, survives store death because it never depends
on the store). Evidence from the run (`gap_report.json`,
`gap_window.json`, `gap_window_manual.json`):

- Store SIGKILLed at 00:20:14 with the loopback port CONFIRMED refusing
  connections; the fork then worked a full turn (README edit + suite
  run, `DONE5`) entirely inside the dead window — relays failed
  non-blocking, the session never stalled (juhw's survival result
  reconfirmed under load).
- Store restarted on the same port/ledger by 00:21:59 (machine
  evidence: first post-restart receipt, relay_seq 102 at
  00:21:59.134603; the hand-recorded 00:22:30 in
  `gap_window_manual.json` is annotated as ~30s late); a post-restart
  turn landed normally.
- `gap_check.py` on resume (`gap_report.json`): `received=95 lost=9
  receiver_resets=3 gap_detected=True` — the nine `relay_seq` holes are
  exactly the dead window's events, detected and quantified; the
  receiver-side numbering shows only its three restarts and zero loss
  signal.
- Two earlier kill/restart windows (the harness's scheduled one at
  00:03:40 and a mis-aimed manual one at 00:08:41) fell in idle periods
  and lost nothing — `gap_detected=False` with all sequences contiguous,
  i.e. the detector does not false-positive on restarts without loss.

Verdict for the DES-070 kill criterion: gap detection reliably catches
and quantifies drops — **provided the sequence is stamped at the
sender**. That precision must land in DES-070's text. One inherent limit
must land with it: sender-sequence detection catches INTERIOR losses
only — trailing losses at session end (nothing ever arrives after the
hole) are invisible to it, so DES-070 must pair the sequence with an
end-of-session handshake or explicitly accept that blind spot.

## (d) Seed prototype

`seed_builder.py` hand-picks from the ledger visible at each cutoff:
current goal (last prompt), active files, last three verbatim tool
results, freshest open failure, and the agent's last end-of-turn report,
bounded at 10,240 bytes with oldest-result-first trimming (actual seeds
in this run: 1,026-5,632 bytes across the five timepoints, per
`reconstructions.json`). Graded seed-only above: **5/5 PASS**, including
the timepoint where the raw tail lost the goal.

Reading it correctly: the seed is *derived from the fanout ledger*, so
this does not show Layer 2 is dispensable — in Mode A the call-start
seed is authored by the primary session, but mid-call updates and ALL of
Mode B have no author except the hook feed. What it shows is the
consumption shape: the store should serve a curated bounded snapshot
(goal + files + last results + open failure + last report), not a raw
last-N dump. Known seed weakness, visible verbatim above: its
`recent actions` lines degrade to result-fragment strings ("Bash: 6") —
curation should keep the tool SUBJECT (command/file), not the response
head. One-line fix for the implementation, recorded here.

## Inherited EL retest (from vox-bst7, Bugbot-confounded)

Re-run on main's fixed harness (`run_automated.py`, turn-end bug fixed —
pending-invocation now holds the turn open), from
`spikes/vox-bst7-el-convai/`, unmodified; agent created and torn down
around the run; 3 billed text-only sessions (the cap). Artifacts copied
to `results/el_retest/` (`metrics_20260830T235840Z.json` +
`trace_20260830T235840Z_seed{1024,10240,51200}.jsonl`; the run's
`notes.txt` appends preserved as `notes_appended.txt`, the frozen spike
restored to its committed state).

**The 50KB quality degradation does NOT reproduce.** At every seed size
including 50KB (prompt_bytes 51,773 — accepted, no rejection or
truncation), every slow-tool turn now delivers the grounded result
answer after the pre-tool narration, e.g. at 50KB: "Searching the
codebase for playback queue." followed in the same turn by "I found
three matches for playback queue in the daemon, playback, and provider
registry files." `incomplete_invocations: 0` on all three runs. The
bst7 observation was the harness's turn-end bug closing the turn on
pre-tool narration, exactly as PR #481's fix hypothesized. Latency gate
re-confirmed in passing: EL-attributable overhead p95 976ms over n=27
(< 1.5s).

Seed-size guidance for DES-070: 1KB-50KB seeds are all safe at session
start; 10KB (the Layer 1 budget used here) is comfortably inside the
envelope.

## Bounds, costs, and honesty

- **Forks: 2** (the cap): run 1 was invalidated and its evidence
  deleted — the fork fixed the seeded bug before running the tests, so
  no failure ever hit the wire, and a marker bug (JSON-escaped `\nOK`
  never matching) would have missed it anyway. Both defects fixed before
  run 2 (string-leaf response extraction; tests-first task order;
  time-fallback gap window).
- **Run 2 needed three extra driven turns** (crash-fix, re-verify,
  loss-window turn) sent into the SAME session via tmux — no extra
  forks. Reason, reported plainly: the model dodged the scripted failure
  three times (fixed the planted bug on sight twice, wrote the
  implementation before the "failing" test once). The genuine failure
  that grounds the mid-debug timepoint is a crash reproduction
  (`FileNotFoundError` traceback) — reality-sourced, not test-authored.
  This is itself a realism finding: a rolling store cannot count on
  failures appearing on schedule; they arrive organically or not at all.
- **The 20-minute target compressed**: the fork completed the six-step
  main task in ~2 minutes and all four turns in ~16 minutes of session
  lifetime. Payload richness, not wall clock, was the measurand, and the
  session produced 95 events across 8 files with a traceback,
  fixes, and three green suite runs.
- **EL spend: exactly 3 billed sessions**, text-only (no TTS credits);
  agent force-deleted after.
- **Offline-first held**: `dry_run.py` (synthetic ledger + real
  mcp-proxy wire leg) passed before any fork; bst7's offline pytest
  suite passed before any billed call. The verdict-bearing logic
  (inventory, latency, gap, reconstructors, seed) is offline-testable
  with no forks.
- Teardown ran twice, both clean (`teardown.log`); no tmux sessions, no
  scratch, no credentials copies left; committed artifacts are
  path/username-sanitized and credential-redacted.
- Harness gotcha, hit live and since fixed in the harness: markdownlint's
  `.tmp/` ignore covers only the repo root, so a LIVE fork's scratch tree
  under `spikes/*/.tmp/` failed `make docs` until teardown (the fork's
  config dir pulls vendored plugin markdown). The scratch root now lives
  at the repo root's `.tmp/vox73y7-scratch/`, inside the ignore.

## Design notes to carry into DES-070

1. **Stamp the per-session sequence at the sender** (hook-side wrapper
   or mcp-proxy itself), not at receipt. Receiver stamping cannot see
   loss and resets across restarts. `relay_stamp.py` is the working
   shape: flock-guarded per-session counter + start-timestamp, ~30ms
   total added pipeline cost.
2. **Store raw events keyed by arrival order; never treat receiver
   sequence as a timeline across restarts.**
3. **Serve curated snapshots, not raw tails**: goal, active files, last
   results, open failure, and `last_assistant_message` (the
   highest-value single field — put it in the retention set explicitly).
4. **Retain the recursive credential redaction** (juhw copy-forward,
   re-proven here) — tool payloads carry whole file contents.
5. `transcript_path` makes a same-host voxd strictly richer (full
   session history on disk); it is dead weight remote — the fanout is
   the only remote-capable feed.
6. Seed budget: 10KB is comfortably safe at EL session start (retest
   above); even 50KB is accepted.
