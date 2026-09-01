# COE: Linux silent playback (2026-04-06)

**Status:** Resolved 2026-04-07 (one day after writing).
**Host:** okinos (Linux, `<user>`, espeak-ng provider, no API keys).
**Date opened:** 2026-04-06.

## Resolution

All four threads in this postmortem are now closed:

- **Observability failure** (the "real problem" called out at the top): fixed in PR #158. `voxd._play_audio` now captures the spawn command, audio session env, exit code, elapsed wall time, file size, and full stderr; startup logs the full process environment; synthesis fails fast on 0-byte output; the health WebSocket exposes `audio_env`, `player_binary`, and `last_playback`. Merged as part of v4.1.0.
- **Audio session env vars missing from systemd unit** (`XDG_RUNTIME_DIR`, `PULSE_SERVER`, `DBUS_SESSION_BUS_ADDRESS`): fixed in PR #157, merged in v4.1.0. The install command now captures these at install time and injects them into the systemd unit, with a `/run/user/<uid>` fallback when sudo strips the env.
- **Stale `voxd` binary baked into systemd ExecStart** (the hypothesis section at the bottom): confirmed and fixed in PR #162. `_voxd_exec_args()` now resolves from `Path(sys.executable).parent / "voxd"` instead of `shutil.which("voxd")`, eliminating the class of bugs where an old `uv tool install` binary on `$PATH` wins over the current install.
- **Underlying architectural regression** (per-user state stranded at FHS paths): fixed in PR #162. `voxd` state is back under `~/.punt-labs/vox/`, the install runs as the user and scopes `sudo` to 3–5 subprocess calls per platform, and pre-v3 keys at `~/.punt-labs/vox/keys.env` load again automatically with no migration needed.

Retained in `docs/coes/` as institutional memory: the five-whys, the observability critique, the investigation protocol, the list of hypotheses checked and ruled out. These are still useful reading for anyone debugging silent-playback failures in a daemon.

---

## Original content (from the day the issue was open)

## The real problem (read this first)

Three rounds of "fixes" have shipped without anyone ever observing what voxd is doing on the user's machine. That's a fundamental observability failure in vox itself, not a one-off diagnostic accident. **Before fixing the audio bug, fix the logging — without it, the next bug will burn the same cycle.**

The core failure is that voxd's INFO-level logs say `Playback start: x.mp3` and `Playback done: x.mp3` and stop there. They tell you nothing actionable:

- They don't say which command was spawned (afplay vs ffplay).
- They don't say what env vars the subprocess inherited (XDG_RUNTIME_DIR, PULSE_SERVER, DBUS_SESSION_BUS_ADDRESS, DISPLAY).
- They don't say the subprocess's exit code.
- They don't capture the subprocess's stderr — `_play_audio` actively discards it via `asyncio.subprocess.DEVNULL`.
- They don't say how long the subprocess actually ran. A 0ms run means it crashed immediately; the log claims "Playback done."
- They don't say which voxd binary is running (the conda one or the .local one).
- They don't say which systemd unit was loaded.

The 5-whys that should have been done on day one:

1. Why don't I know if audio is actually playing? Because the log only says "start" and "done."
2. Why does it only say "start" and "done"? Because we suppressed stderr to avoid noise.
3. Why did we suppress stderr without capturing it elsewhere? Because we treated playback as fire-and-forget.
4. Why is fire-and-forget OK? It isn't — playback is the entire product. Every silent failure is a P0.
5. Why don't we know it's silently failing in production? Because voxd has no metric, no error log, no health signal that distinguishes "played" from "subprocess exited with code 1 immediately."

**Fix the observability first. Then fix the audio bug. The audio bug should fall out of the new logs in five minutes.**

## What needs to change in voxd (do this first)

### 1. `_play_audio` must log everything

In `src/punt_vox/voxd.py`, the `_play_audio` function currently spawns the player with stderr piped to DEVNULL and only logs "Playback start" / "Playback done". It must:

- Drop `-loglevel quiet` from the ffplay invocation so the player produces real diagnostics.
- Capture stderr via PIPE instead of discarding it.
- Capture exit code and elapsed time.
- Log the audio-related env vars that voxd's process actually has (XDG_RUNTIME_DIR, PULSE_SERVER, DBUS_SESSION_BUS_ADDRESS, DISPLAY, WAYLAND_DISPLAY, HOME, USER).
- Log the file path and file size of the MP3 being played. A zero-byte file is a synthesis bug masquerading as a playback bug.
- On non-zero return code, log at ERROR level with all of the above plus the captured stderr.
- On success, log at INFO level with the elapsed time and any stderr the player produced (ffplay still prints a stream summary even with quiet off).

The current implementation actively destroys diagnostic evidence. Stop doing that.

### 2. voxd startup must log its environment

At voxd startup, before the server binds, log a single record containing:

- pid, uid, gid, cwd
- The path to the voxd binary (`sys.executable`) and module file
- A snapshot of: PATH, XDG_RUNTIME_DIR, PULSE_SERVER, DBUS_SESSION_BUS_ADDRESS, DISPLAY, WAYLAND_DISPLAY, HOME, USER, LANG

This makes one log line answer "is voxd running with the right env vars" without anyone needing to run `systemctl show` or `cat /proc/<pid>/environ`.

### 3. Synthesis must log the result file size

In `_synthesize_to_file`, after the synthesize call, log: provider name, output path, file size in bytes. A zero-byte synthesis result is the bug we'd never see otherwise.

### 4. voxd should run a sentinel playback at startup

After binding, voxd should attempt to play 100ms of silence (or just probe whether the player binary can connect to the audio server). On failure, log a clear ERROR: "voxd cannot reach audio output — check XDG_RUNTIME_DIR, PulseAudio, ALSA permissions" with the captured stderr from the probe. This becomes a canary so users know within seconds of starting voxd whether it can produce sound at all, instead of finding out 20 minutes later when their first chime is silent.

### 5. The healthcheck endpoint must report audio session state

`_handle_health` and `_health_route` must include:

- The current values of XDG_RUNTIME_DIR, PULSE_SERVER, DBUS_SESSION_BUS_ADDRESS as voxd sees them.
- Whether ffplay (or afplay on macOS) is on PATH.
- The result of the most recent playback attempt (success/failure, exit code, last error message).

Then `vox doctor` exposes this and the next time someone says "vox isn't working", the doctor output already contains the answer.

## Once observability is fixed, gather this data

Run on okinos in this order. Paste every output verbatim back into the issue.

### A. Daemon state

```bash
sudo systemctl status voxd | head -20
sudo systemctl show voxd --no-pager --property=Environment,RuntimeDirectory,FragmentPath,ActiveState,SubState,ExecMainStartTimestamp
sudo cat /etc/systemd/system/voxd.service
```

We need to know:

- Is voxd actually running (not "loaded but failed")?
- What's the unit's `Environment=` line — does it include XDG_RUNTIME_DIR?
- When was it last started? If it's older than the last `daemon install`, the install didn't restart it.
- Which fragment path is loaded (the unit on disk vs. a drop-in override)?

### B. Which voxd binary is actually running, and what env does it have

This is the smoking gun that should have been the first thing checked:

```bash
ps -ef | grep '[v]oxd'
sudo ls -la /proc/$(pgrep -f 'bin/voxd')/exe
sudo cat /proc/$(pgrep -f 'bin/voxd')/environ | tr '\0' '\n' | sort
```

The third command shows the **actual environment of the running voxd process**, byte-for-byte. If XDG_RUNTIME_DIR is not in that output, the systemd unit fix didn't take effect for this process — either because the unit wasn't reloaded, the service wasn't restarted, or the unit on disk doesn't have what we think it does.

### C. voxd's own log

```bash
sudo tail -200 /var/log/vox/voxd.log
```

After the observability fixes above are deployed, this log alone should answer:

- Is voxd starting up cleanly?
- What env vars did it see at startup?
- What command did it spawn for playback?
- What did that command return?
- What did it print to stderr?

### D. Reproduce a single playback with full visibility

```bash
sudo truncate -s 0 /var/log/vox/voxd.log
vox unmute "test playback one"
sudo cat /var/log/vox/voxd.log
```

### E. Independent audio sanity check

Does ffplay work AT ALL from the user's shell, with the exact MP3 voxd produces?

```bash
ls -la ~/vox-output/
ffplay -nodisp -autoexit ~/vox-output/<some-recent-file>.mp3
```

Does it work as the same user voxd runs as, in the same way voxd would call it?

```bash
sudo -u <user> env -i HOME=/home/<user> PATH=/usr/bin:/bin XDG_RUNTIME_DIR=/run/user/1000 ffplay -nodisp -autoexit /tmp/test.mp3
```

Does it work with NO XDG_RUNTIME_DIR? (proves whether that's actually the issue)

```bash
sudo -u <user> env -i HOME=/home/<user> PATH=/usr/bin:/bin ffplay -nodisp -autoexit /tmp/test.mp3
```

If both succeed, XDG_RUNTIME_DIR was a wrong hypothesis. If only the first succeeds, the hypothesis was right but the systemd unit isn't actually injecting it.

## What's already been tried (don't repeat these)

1. **PR #154 (merged, in v4.0.3)**: Fixed `VoiceNotFoundError: en` — espeak provider now discovers installed voices instead of assuming hardcoded defaults. This was real and it fixed a real bug, but it wasn't the playback issue.

2. **PR #157 (merged, on main, unreleased)**: Added XDG_RUNTIME_DIR, PULSE_SERVER, DBUS_SESSION_BUS_ADDRESS to the systemd unit at install time, with a `/run/user/<uid>` fallback when sudo strips the env. Added `RuntimeDirectory=vox`. The user installed from main, ran `sudo PATH="$PATH" /home/<user>/miniconda3/bin/vox daemon install`, the install reported success — but **we never verified whether the new unit actually has the env vars in it**, and we never looked at `/proc/<pid>/environ` of the running voxd. We assumed.

3. **What we know works**: `espeak-ng "Hello"` from the user shell plays audio. The user IS able to play audio on this machine. The issue is voxd specifically.

4. **What we know fails**: `vox unmute "Hello"` prints "Hello" to stdout but produces no audio. No error from the CLI. The hook log shows requests reaching voxd successfully (chime requests, no `voxd not running` warnings after the daemon was reinstalled). But the hook log is the user-side log, not voxd's log — we've never actually looked at voxd's side.

## The hypothesis I want tested first

The systemd unit fix in PR #157 may have worked correctly — the unit on disk may have `XDG_RUNTIME_DIR=/run/user/1000` in it — but the running voxd process is **the old one from before the install**, because:

- The user has TWO voxd binaries: `/home/<user>/.local/bin/voxd` (from `uv tool install punt-vox==4.0.3`) and `/home/<user>/miniconda3/bin/voxd` (from `pip install punt-vox@git+...@main`).
- The systemd unit's `ExecStart` was generated by the new code, but `shutil.which("voxd")` under sudo (with `secure_path`) found the `.local/bin` voxd first.
- The `_systemd_install` flow does call `systemctl stop` then `systemctl enable --now`, which should restart, but we have no proof it actually picked up the new unit content vs. continuing to run the old process. `daemon-reload` was issued; `restart` was not issued explicitly.

`/proc/<pid>/environ` will tell us the truth in one command. That's why it's the most important thing in section B above.

## If the env vars ARE in the running process and audio still fails

Then XDG_RUNTIME_DIR was a wrong hypothesis. The next places to look:

1. **Does voxd's user have audio group membership?** `groups <user>` should include `audio`. If voxd runs as `<user>` but `<user>` isn't in `audio`, ALSA device access fails.
2. **Is PulseAudio/PipeWire actually running for the user?** `pgrep -u <user> pulseaudio` or `pgrep -u <user> pipewire`. If neither is running, there's nothing to connect TO.
3. **Is the PulseAudio cookie readable?** `ls -la /run/user/1000/pulse/`. The systemd-managed voxd needs read access to this directory.
4. **Does ffplay work when invoked by voxd's exact env?** Section E above answers this.

## Restart the daemon explicitly before testing anything

Whatever the next test is, run this first. Don't trust that `daemon install` restarted it:

```bash
sudo systemctl daemon-reload
sudo systemctl restart voxd
sudo systemctl status voxd | head -10
sudo cat /proc/$(pgrep -f 'bin/voxd')/environ | tr '\0' '\n' | grep -E 'XDG|PULSE|DBUS|PATH'
```

If XDG_RUNTIME_DIR is not in that output, the unit on disk doesn't have it — go check `/etc/systemd/system/voxd.service` and report what's actually in it. If it IS in the output, the env var fix took effect and the bug is somewhere else (audio group, no PulseAudio, ffplay returning non-zero, etc.).

## Sequence of work

1. Open a PR with the observability changes from "What needs to change in voxd" above. Do not skip section 1 (the play_audio rewrite) — that's the most important.
2. Merge it.
3. Cut v4.0.4 with both the observability changes and the systemd env vars from PR #157.
4. Install v4.0.4 on okinos. Restart the daemon explicitly. Reproduce. The voxd log should now answer the question without any guessing.
5. Whatever the log says, fix the actual root cause.

The reason observability comes before the bug fix: even if the audio fix itself works, we can't confirm it works without the logging. And if it doesn't work, we'll be back here in two days with the same blind diagnosis problem. The logging investment compounds; the next bug pays for it instantly.
