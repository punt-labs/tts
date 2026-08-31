# vox-04qy — steering spike harness

Can an external process deliver a mid-run user turn into a launched Mode B
session, with confirmed receipt and characterized mid-turn semantics? Two
arms: pi's RPC `steer` verb over subprocess pipes, and the Claude Code TUI
in tmux via send-keys with the hook store as the delivery receipt. The
plan is `PLAN.md`; the verdicts and evidence citations are `REPORT.md`.

Throwaway spike code: nothing here ships, nothing touches `src/`. Scripts
are self-contained PEP 723 uv scripts; run them **from this directory** so
sibling imports resolve.

## Files

| File | Role |
|------|------|
| `rpc_protocol.py` | Arm 1 wire objects: `RpcCommand`, `RpcEvent`, stamped `Transcript` |
| `rpc_session.py` | Arm 1 process driver: `PiSpec` argv + `PiRpcSession` over Popen pipes |
| `steer_analysis.py` | Arm 1 verdict numbers: send stamps, first-after events, timelines |
| `run_arm1.py` | Arm 1 live runner: midturn steer / idle steer / follow_up contrast |
| `stamp.py` | Ledger core + `Sanitizer` (copied verbatim from vox-73y7) |
| `hook_store.py` | Stub voxd store, JSON-RPC over loopback WebSocket (73y7 copy) |
| `relay_stamp.py` | Sender-side `relay_seq`/`relay_start_ns` stamper (73y7 copy) |
| `wiring.py` | Hook wiring + `steer-inject-v1` profile (73y7 copy, no Bash) |
| `scratch.py` | Scratch project + isolated config + relay deposit (73y7 copy) |
| `launcher.py` | tmux fork, prefix `vox04qy`, + literal/paste send primitives |
| `ledger_watch.py` | Blocking receipt checks over the growing ledger |
| `stubs.py` | Sentinel `vox`/`vox-panel` stand-ins, first on the fork's PATH |
| `run_arm2.py` | Arm 2 live runner: the five-case send-keys matrix |
| `teardown.py` | Idempotent teardown with a pgrep pass before any kill (juhw copy) |
| `results/` | Committed sanitized evidence per run |
| `REPORT.md` | Per-arm verdicts + mid-turn semantics |

`test_*.py` are the colocated offline pins; none spawns pi or claude.

## Prerequisites

- `pi` (0.84.4+), `claude`, `tmux`, `mcp-proxy`, `uv`, `git` on `PATH`.
- Arm 1: an Anthropic API key in the environment (`direnv exec ../../`).
- Arm 2: file-based Claude credentials at `~/.claude/.credentials.json`;
  the run copies them (mode 0600) into the throwaway config dir and the
  teardown deletes the copy.

## Running

```sh
cd spikes/vox-04qy-steering
direnv exec ../../ uv run pytest .                        # offline pins
direnv exec ../../ uv run run_arm1.py --steer-style plain
direnv exec ../../ uv run run_arm1.py --steer-style adversarial
direnv exec ../../ uv run run_arm2.py
direnv exec ../../ uv run teardown.py                     # manual cleanup
```

## Isolation and bounds

- Everything a live run creates lives under `~/.cache/vox04qy-scratch/`
  — outside the repo, unenabled, removed (and verified removed) by the
  runner's own teardown and by `teardown.py`.
- Sentinel `vox`/`vox-panel` stubs resolve first on both children's PATH;
  an invocation would be recorded evidence, never a real panel. Both live
  runs recorded zero.
- Arm 1's pi is tool-restricted to `read,grep,find,ls`; Arm 2's profile
  denies Bash and all network tools. Claude forks: one per run, launcher
  cap 2.
- Zero ElevenLabs involvement. Committed evidence is path- and
  credential-sanitized at persist time (`stamp.Sanitizer`).
