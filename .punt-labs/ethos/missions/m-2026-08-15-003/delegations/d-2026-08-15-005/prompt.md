Review the diff of branch `fix/vox-mwc2-launchagent` (HEAD 90d0f42, 2 commits) against main, in <repo>.

    git -C <repo> diff main..fix/vox-mwc2-launchagent

WHAT IT FIXES. 72 playback failures in one session's log, all `AudioQueueStart failed (-66681)` after a ~15s hang, across chime assets, cache files and temp synthesis files alike. Cause: voxd's LaunchAgent plist never declared `LimitLoadToSessionType`, so launchd could bootstrap it outside the Aqua graphical login session, and macOS grants CoreAudio queue access by session membership.

Two changes: (1) `LimitLoadToSessionType=Aqua` in the plist, plus two helper extractions in `plist_content`. (2) `AudioContext` replaces a `_snapshot_env` call that logged five Linux variables (`XDG_RUNTIME_DIR`, `PULSE_SERVER`, `DBUS_SESSION_BUS_ADDRESS`, `DISPLAY`, `WAYLAND_DISPLAY`) — all `<unset>` on macOS, which is why the log fired 72 times and explained nothing. macOS now records `{mgr, ppid, sid}` where `mgr` is `launchctl managername`.

WHERE I WANT YOUR ATTENTION:

1. `AudioContext` runs a SUBPROCESS (`launchctl managername`) from a diagnostic path that executes on every playback failure. Check it cannot raise, cannot hang, and cannot slow the failure path further — the bug being fixed already costs 15s per failure. Look specifically for a missing timeout on the subprocess call. A diagnostic that blocks is worse than one that says nothing.

2. The result is cached in a `ClassVar` for the daemon's lifetime, on the reasoning that the launchd manager cannot change without a re-bootstrap. Judge that: is the cache correct, is it thread-safe given playback runs under asyncio with `to_thread` in places, and does a failed first probe poison the cache permanently with `"unknown"`?

3. The plist change is the actual fix and it is one line of XML. Verify the key/value pairing is well-formed in the emitted document and that the test asserts the key is IMMEDIATELY followed by the Aqua value — a test that only greps for "Aqua" somewhere in the plist would pass with the value attached to the wrong key.

4. The Linux branch must be untouched — ffplay genuinely needs those socket variables. Confirm no regression there.

5. THE STANDING HUNT. Every defect found on the last three PRs in this repo was something GREEN that hid a problem: a fixture that made a crash unreachable, a test asserting the wrong post-condition, substring matches passing against a tuple repr, a fully-tested exception type with zero raise sites, and a spec-mock that mocked away the thing under test. Look for the next one in the seven tests this PR adds — an assertion that would still pass if the behaviour it names were broken.

Report only findings you are confident are real, each with file:line. Zero findings is a valid and useful answer.