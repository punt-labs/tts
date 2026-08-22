# Conversation Mode: Session Attachment (ADR)

Companion to [`conversation-mode-prd.tex`](conversation-mode-prd.tex) Chapter
2, section "The unresolved core: session attachment", and Chapter 2's
"Decisions required before implementation" item 1 (no recommendation
offered there — this document is that investigation). It answers one
question: **what does Claude Code's session surface actually permit for
injecting a transcribed human turn into, and extracting a streamed reply
from, the user's already-running Claude Code session** — not a fresh,
context-free one (FR-4)?

This is a recommendation for the operator to ratify, per this project's
design-review-before-implementation gate (`punt-labs/CLAUDE.md`). It is not
a decision already made. See the escalation at the end.

## Why this gap is load-bearing

Every one of the five spikes (`conversation-mode-spikes.md`) that needed "the
agent" used the authoring session itself, driven by hand. None attempted
programmatic injection of a transcribed turn into an independent,
already-running session, nor programmatic extraction of that session's
streamed reply. Turn detection, barge-in, and sentence-streamed synthesis are
all real and measured; none of them matter if there is no mechanism to
connect them to a live agent session that is not the one authoring this
document.

## Investigation performed

No spike or prior design work in this codebase touched this surface, so this
section is new investigation, not a summary of existing findings. It is
scoped to what is directly observable from this machine: the installed
`claude` CLI's documented flags (`claude --help`, `claude agents --help`) and
this repo's own hook/plugin surface (`plugin/hooks/hooks.json`, already
built for a different purpose). No web access was available in this session;
where the investigation could not go further (concurrent-session safety,
undocumented internals), that is named as an open risk below, not papered
over.

### What `claude --help` actually shows

The installed CLI (checked directly, not from memory) has three flags that
matter here:

```text
-r, --resume [value]        Resume a conversation by session ID, or open
                             interactive picker with optional search term
--input-format <format>     Input format (only works with --print): "text"
                             (default), or "stream-json" (realtime streaming
                             input)
--output-format <format>    Output format (only works with --print): "text"
                             (default), "json" (single result), or
                             "stream-json" (realtime streaming)
--include-partial-messages  Include partial message chunks as they arrive
                             (only works with --print and
                             --output-format=stream-json)
--replay-user-messages      Re-emit user messages from stdin back on stdout
                             for acknowledgment (only works with
                             --input-format=stream-json and
                             --output-format=stream-json)
--fork-session               When resuming, create a new session ID instead
                             of reusing the original (use with --resume or
                             --continue)
```

Combined: `claude -p --resume <session-id> --input-format stream-json
--output-format stream-json --include-partial-messages` starts a
non-interactive (`-p`) subprocess that resumes an existing session's full
conversation history by ID, accepts a stream of JSON-encoded user-turn
messages on stdin, and emits a stream of JSON-encoded assistant-message
deltas on stdout as they are produced — including partial chunks, which is
exactly FR-11's "speak on the first complete portion" requirement at the
transport level, not something Conversation Mode has to build a polling
mechanism to approximate.

Separately, `claude agents --json --cwd <path>` prints active sessions
(interactive and background), filterable by working directory, as a JSON
array — a real, scriptable way to discover *which* session is "the user's
active session" for FR-4, rather than requiring the user to supply a session
ID by hand at call-start.

### What it does not show

Nothing in the documented CLI surface exposes a way to attach to the literal,
already-running interactive TTY process — the one the user is looking at in
their terminal — and inject input into *that same process*. `--resume`
starts a **new process** that replays the named session's conversation
history; it does not reach into an existing process. This distinction
matters and is not a minor technicality: FR-4's actual requirement is that
the call use the session's *task context* ("carrying whatever task context it
already had"), not that it literally share a process with the interactive
terminal. A new subprocess that resumes the same session ID satisfies the
stated requirement (same context) without satisfying a stricter reading
(same process) that FR-4 does not actually ask for. This ADR treats the
context-preserving reading as the correct one; the escalation below asks the
operator to confirm that reading, not just the option to implement it.

## Options considered

### A. Hook into Claude Code's own hook surface (SessionStart, UserPromptSubmit, Stop, etc.) — rejected

Vox already drives Claude Code's hook surface for a different purpose
(`plugin/hooks/hooks.json`: chimes and narration on Stop, Notification,
PreCompact, and so on). Hooks are invoked *by* Claude Code, synchronously, as
part of its own request lifecycle, in response to something already
happening inside that session — a prompt already submitted, a tool already
called. There is no hook that lets an external, unrelated process inject a
*new* user turn into an otherwise-idle session; every hook is reactive to a
session-internal event, not a request channel an outside process can push
into. `voxd` is a long-running daemon entirely outside any given Claude Code
session's process tree — there is no hook Claude Code fires that `voxd` is
in a position to catch as "please treat this text as if the user typed it."
This option does not exist as a mechanism; it was worth ruling out explicitly
because vox's own hook usage elsewhere might otherwise suggest it as an
obvious first guess.

### B. A queue the session polls — rejected

Considered because it is the shape used elsewhere for asynchronous work
(e.g. this project's own mission/beads infrastructure). Nothing in Claude
Code's documented surface lets a running interactive session poll an
external queue, file, or socket mid-conversation for new work to do — the
interactive loop is driven by TTY input only. Building this would mean
modifying Claude Code itself to add a polling capability it does not have;
vox is a client of Claude Code, not a fork of it, and that is out of scope
for this project regardless of how it phases.

### C. Run the agent inside `voxd`'s own process space — rejected

Considered because it would sidestep subprocess-lifecycle concerns entirely.
Rejected because it means `voxd` (a Python daemon) hosting a second,
independent instance of the agent loop, disconnected from the interactive
session the user is actually looking at — which defeats FR-4's actual point
(the call uses the task context the user was already in, not a parallel one
that happens to also be an agent). It would also require reimplementing tool
execution, permission handling, and context management that already exist
in the `claude` binary — the same "one engine, don't duplicate it" reasoning
`punt-kit/standards/architecture.md` applies everywhere else in this
organization, applied here to a different codebase's engine rather than
vox's own.

### D. A headless `claude -p --resume` subprocess per human turn, stream-json in both directions — recommended

The mechanism the investigation above actually supports: `voxd` discovers
the user's active session ID once, at call start, via `claude agents --json
--cwd <path>` (filtered to the directory the call started in); for each
human turn, `voxd` spawns `claude -p --resume <id> --input-format
stream-json --output-format stream-json --include-partial-messages`,
writes one JSON user-message object to the subprocess's stdin, and reads a
stream of JSON assistant-message-delta objects from stdout as they arrive —
feeding directly into the sentence-streamed synthesis pipeline
(`docs/conversation-mode-prd.tex`, "Sentence-streamed synthesis: one
pipeline, not two"). No custom IPC protocol is needed; the "IPC" the PRD's
open question asked about is exactly this subprocess's stdin/stdout, already
speaking a documented JSON wire format. This also fits `voxd`'s existing
async/sync boundary concern (Spike 3's "dominant unsolved engineering
problem"): a subprocess spawned and streamed via
`asyncio.create_subprocess_exec` is a natural, idiomatic async operation —
this mechanism does not add a *second*, harder boundary on top of the one
Spike 3 already named, it is an instance of the same one.

## Open risk this document does not resolve

**Concurrent-resume safety is unverified.** If the user's interactive
session is genuinely still open in their terminal at the same moment `voxd`
resumes that same session ID headlessly for a call turn, it is not known
from this investigation whether Claude Code's session storage tolerates two
processes reading/appending the same session's history concurrently, or
whether one dispatch clobbers, corrupts, or silently forks from the other.
This is exactly the kind of thing a spike answers, not a design document —
it is named here as the load-bearing precondition option D depends on,
and Slice 1b (the earliest implementation slice) must verify it against a
real running interactive session before any other part of option D is
built. If it turns out unsafe, the fallback within option D is narrower, not
a different option entirely: require the human to not have the interactive
terminal actively mid-turn during a call, or detect and refuse to start a
call when the target session shows signs of concurrent interactive use — a
product question, not an architecture one, and one this document defers to
whoever runs that spike.

## Escalation

**This is an operator-reserved decision, not one this design mission
resolves on its own authority** — per `punt-labs/CLAUDE.md`'s
design-review-before-implementation gate, and per
`docs/conversation-mode-prd.tex` Chapter 2's own explicit statement that "no
recommendation is offered" for this fork pending investigation. That
investigation is what this document is. The recommendation is option D
above (headless `claude -p --resume` subprocess per turn, `stream-json`
input and output, session discovery via `claude agents --json`) — presented
as a recommendation, not a fait accompli.

**Decision needed from the operator before Slice 1b (implementation)
dispatches:**

1. Confirm or overrule option D as the session-attach mechanism.
2. Confirm the reading of FR-4 this ADR relies on — "the same task
   context," realized as a new subprocess resuming the same session ID —
   rather than a stricter "the same OS process" reading that no documented
   Claude Code surface supports at all. If the stricter reading is what FR-4
   actually intends, no option investigated here satisfies it, and that is
   itself a decision the operator needs to make explicitly, not one this
   document can resolve by picking the option that happens to be
   implementable.
3. Confirm that Slice 1b's first concrete task is a spike verifying
   concurrent-resume safety against a real running interactive session,
   before any of option D's plumbing is built on top of an unverified
   assumption.

Do not proceed to implementing the real (non-fake) session-attach mechanism
until this ratification lands. The `SessionAttach` Protocol and its fake
(`src/punt_vox/voxd/conversation_mode/session_attach.py`,
`tests/conversation_mode/_session_attach_fakes.py`) are deliberately shaped
to be implementation-agnostic — the protocol's one operation
(`send_turn`, returning a chunked reply stream) is satisfied equally by
option D or by any option the operator instead selects — so this
ratification does not block the rest of Slice 1a's deliverable, only Slice
1b.
