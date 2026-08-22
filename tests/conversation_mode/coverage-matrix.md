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
| FR-1 | Start a call with one action; tell the user what's needed if unconfigured | 2 | `tests/test_cli_call.py` (`test_start_requires_the_script_option`, `test_run_call_speaks_the_reply_and_holds_the_lock_only_while_active`) |
| FR-2 | End a call explicitly, or automatically after a bounded timeout | 1 | `tests/conversation_mode/test_call_state.py`, `tests/conversation_mode/test_call_session.py` (`test_hangup_returns_to_idle`, `test_timeout_from_listening_returns_to_idle`) |
| FR-3 | Every call-state transition is communicated audibly | 1 | `tests/conversation_mode/test_call_session.py` (`test_start_speaks_the_listening_cue`, `test_full_round_trip_speaks_the_agent_s_reply`) -- listening/reply-ready cues only this slice; barge-in's cue is Slice 2a+ |
| FR-4 | One human, one active agent session, for the call's lifetime | 2 | `tests/conversation_mode/test_session_discovery.py` (never auto-picks among multiple candidates) |
| FR-5 | Detect when the human has finished a turn | 1 | `tests/conversation_mode/test_turn_detector.py` (`test_continuous_speech_then_gap_ends_turn`) |
| FR-6 | Do not end a turn on a thinking pause, cough, or incidental noise | 1 | `tests/conversation_mode/test_turn_detector.py` (`test_brief_within_word_dip_does_not_reset_the_run`, `test_a_cough_shorter_than_min_speech_does_not_end_a_turn`) |
| FR-7 | Do not treat steady background noise as speech | 1 | `tests/conversation_mode/test_turn_detector.py` (`test_steady_room_noise_never_signals_speech`) |
| FR-7a | Without headphones, do not treat the agent's own voice as human speech | | P2, not in scope for this slice |
| FR-8 | Let the human interrupt the agent; stop speech promptly on interruption | | Barge-in detection is Slice 2a+ territory; `CallSession.barge_in`/`BargeIn` command exist and are exercised in `test_call_actor.py`'s state-transition tests, but no detector drives them yet |
| FR-9 | Do not discard what the human said while interrupting | | Same as FR-8; the `CaptureDuringWait`/pending-addendum mechanics are covered by `test_call_state.py`, not yet wired to a live detector |
| FR-10 | Without headphones, distinguish genuine interruption from acoustic echo | | P2, not in scope for this slice |
| FR-11 | Begin speaking on the reply's first complete portion | | This slice speaks the reply as one non-streamed block (per mission scope); `ClaudeSessionAttach` streams stream-json internally but `CallSession` does not yet act per-chunk |
| FR-12 | Play a presence signal while the agent is working | | Slice 2a/4 territory |
| FR-13 | The microphone remains available throughout the agent's turn | | Real capture is deferred alongside the real STT provider (see `src/punt_vox/commands/call.py`'s module docstring) |
| FR-14 | Support ElevenLabs for STT and TTS on macOS | | Real STT provider deferred (`src/punt_vox/providers/` locked by another open mission); TTS already ships via existing `mic:unmute`/`vox say` |
| FR-14a | Support at least one fully local provider on macOS and Linux | | P2, not in scope for this slice |
| FR-15 | Let a user configure the Conversation Mode provider | | Provider selection for Conversation Mode is not built this slice (no real STT provider to select) |
| FR-16 | No cloud account/network required for a complete call on a local provider | | P2, not in scope for this slice |
| FR-17 | Account for platform-specific local-provider setup cost | | P2, not in scope for this slice |
| FR-18 | Fail to start the call with a clear reason if the provider is unreachable | 2 | `tests/conversation_mode/test_session_discovery.py` (`test_no_active_sessions_returns_empty_tuple`, `test_nonzero_exit_raises_session_discovery_error`) -- session-attach's fail-closed path; provider-unreachable proper (ElevenLabs STT) is deferred with the real provider |
| FR-19 | Never fabricate on ambiguous audio; ask the human to repeat | 1 | `tests/conversation_mode/test_stt_provider.py` (`test_low_confidence_final_event_must_not_be_acted_on`), `tests/conversation_mode/test_call_session.py` (`test_low_confidence_transcript_asks_the_human_to_repeat`) |
| FR-21 | Provider-agnostic interfaces for capture/recognition/turn-taking/synthesis | 1 | `tests/conversation_mode/test_stt_provider.py` (`test_fake_satisfies_the_stt_provider_protocol`), `tests/conversation_mode/test_session_attach_fake.py` (Slice 1a) |
| FR-22 | Require a minimum sustained-speech duration before treating audio as an interruption | | Barge-in is Slice 2a+ territory; this slice's `TurnDetector` proves the underlying accumulated-run model FR-22's mechanism reuses (see `docs/conversation-mode-prd.tex` S:design-termination), not the interruption-specific longer sustain threshold |

## Non-Functional Requirements

| ID | Description (short) | Tier | Test path |
|----|----------------------|------|-----------|
| NFR-1 | Interruption latency: detection + termination, additive | | Barge-in is Slice 2a+ territory |
| NFR-2 | Time to first spoken word, flat regardless of reply length | | Not measured this slice; the reply is spoken as one non-streamed block |
| NFR-3 | Platform and provider breadth (Phase 2 scope) | | P2, not in scope for this slice |
| NFR-4 | No audio or transcript leaves the machine in a fully local configuration | | P2, not in scope for this slice |
| NFR-5 | No regression to vox's behavior outside an active call | 1/2 | Full existing suite (unrelated to Conversation Mode) passes unchanged alongside this slice's additions |
| NFR-6 | Every call-state cue is audible; no cue depends on a visual/text channel | 1 | `tests/conversation_mode/test_call_session.py` (`test_start_speaks_the_listening_cue`) -- listening/reply-ready cues speak through the injected `SpeakFn`, matching `mic:unmute`'s call shape |
