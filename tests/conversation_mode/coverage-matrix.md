# Conversation Mode Coverage Matrix

Maps every functional and non-functional requirement in
[`conversation-mode-prd.tex`](../../docs/conversation-mode-prd.tex) Chapter 1
to the test tier and test path that cover it, per Chapter 3's coverage
requirement 2 ("every requirement maps to at least one test at a named
tier"). Scaffolded empty in Slice 1a (vox-gs9u.1) so later slices fill rows
in as they close, rather than starting an empty matrix from zero.

Tiers follow `TESTING.md` / `punt-kit/standards/python.md`'s four-tier
pyramid plus the new Tier 5 this feature's test plan adds (Chapter 3,
"Testing tiers"):

| Tier | What it tests | Runs in CI |
|------|----------------|------------|
| 1. Unit | Pure logic: detectors, the call state machine, segmenter policies | Yes |
| 2. Integration | Cross-component wiring, provider protocol conformance | Yes |
| 3. Subprocess/E2E | Wire protocol, real provider calls, regression harnesses | Optional |
| 4. SDK | End-to-end with a real Claude Code session | No |
| 5. Live audio, human-judged | Does it sound right | No, by construction |

A requirement checkable only by ear (for example NFR-6's "does this sound
right," FR-12's presence-signal naturalness) maps to Tier 5, not a missing
row — Chapter 3 names this explicitly so Tier 5 rows are not mistaken for
gaps.

## Functional Requirements

| ID | Description (short) | Tier | Test path |
|----|----------------------|------|-----------|
| FR-1 | Start a call with one action; tell the user what's needed if unconfigured | | |
| FR-2 | End a call explicitly, or automatically after a bounded timeout | | |
| FR-3 | Every call-state transition is communicated audibly | | |
| FR-4 | One human, one active agent session, for the call's lifetime | | |
| FR-5 | Detect when the human has finished a turn | | |
| FR-6 | Do not end a turn on a thinking pause, cough, or incidental noise | | |
| FR-7 | Do not treat steady background noise as speech | | |
| FR-7a | Without headphones, do not treat the agent's own voice as human speech | | |
| FR-8 | Let the human interrupt the agent; stop speech promptly on interruption | | |
| FR-9 | Do not discard what the human said while interrupting | | |
| FR-10 | Without headphones, distinguish genuine interruption from acoustic echo | | |
| FR-11 | Begin speaking on the reply's first complete portion | | |
| FR-12 | Play a presence signal while the agent is working | | |
| FR-13 | The microphone remains available throughout the agent's turn | | |
| FR-14 | Support ElevenLabs for STT and TTS on macOS | | |
| FR-14a | Support at least one fully local provider on macOS and Linux | | |
| FR-15 | Let a user configure the Conversation Mode provider | | |
| FR-16 | No cloud account/network required for a complete call on a local provider | | |
| FR-17 | Account for platform-specific local-provider setup cost | | |
| FR-18 | Fail to start the call with a clear reason if the provider is unreachable | | |
| FR-19 | Never fabricate on ambiguous audio; ask the human to repeat | | |
| FR-21 | Provider-agnostic interfaces for capture/recognition/turn-taking/synthesis | | |
| FR-22 | Require a minimum sustained-speech duration before treating audio as an interruption | | |

## Non-Functional Requirements

| ID | Description (short) | Tier | Test path |
|----|----------------------|------|-----------|
| NFR-1 | Interruption latency: detection + termination, additive | | |
| NFR-2 | Time to first spoken word, flat regardless of reply length | | |
| NFR-3 | Platform and provider breadth (Phase 2 scope) | | |
| NFR-4 | No audio or transcript leaves the machine in a fully local configuration | | |
| NFR-5 | No regression to vox's behavior outside an active call | | |
| NFR-6 | Every call-state cue is audible; no cue depends on a visual/text channel | | |
