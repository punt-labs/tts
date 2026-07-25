# Remote voxd Setup

Play audio on your local machine from a remote host. Two approaches:
direct network or SSH tunnel. Both use the same env vars.

## When you need this

You SSH from machine A (with speakers) to machine B (headless server,
cloud VM, or second workstation). You run Claude Code on B. When Claude
finishes a task, you want to hear it on A.

## What plays where

This setup is for **playback that voxd drives**: `vox say`, task
notifications, chimes, and `/music` all play on the daemon host (machine A) —
that is the whole point, so you hear your agent on the machine with speakers.
`vox rec new`, `vox rec play`, and `vox rec get` also work against a remote
daemon — see [Recording and playback over a remote daemon](#recording-and-playback-over-a-remote-daemon)
below.

## Quick decision

- **Same LAN** (both machines on your home/office network) → Direct network (simpler, persistent)
- **Different networks** (home + cloud VM, home + office, any setup where B can't route to A) → SSH tunnel (works across NAT, firewalls, and network boundaries)

---

## Approach 1: Direct Network

Machine A runs voxd on a network interface. Machine B connects directly.

### On machine A (speakers)

**1. Install vox and start the daemon with network binding:**

```bash
# Install
curl -fsSL https://raw.githubusercontent.com/punt-labs/vox/main/install.sh | sh

# Install daemon with network binding
export VOXD_BIND=0.0.0.0
vox daemon install
```

`VOXD_BIND=0.0.0.0` is baked into the service unit at install time.
The daemon will bind to all interfaces on every start.

**2. macOS firewall (macOS only):**

When voxd first binds to a network interface, macOS should prompt you
to allow the Python process (shown as `python3` or `python3.X`) to
accept incoming connections. Click **Allow**.

If you missed the prompt or it didn't appear:

1. Open **System Settings → Network → Firewall → Options**
2. Ensure "Block all incoming connections" is **off**
3. Find `python3.14` in the list and set it to **Allow**

Linux: no firewall changes needed on most systems. If `ufw` is active:
`sudo ufw allow 8421/tcp`.

**3. Get the auth token:**

```bash
cat ~/.punt-labs/vox/run/serve.token
```

Save this value — you'll need it on machine B. The token is stable
across daemon restarts (generated once at install time, persisted).

**4. Verify the daemon is reachable from B:**

```bash
# From machine B:
curl http://<A-ip>:8421/health
# Should return: {"status":"ok",...}
```

### On machine B (remote host)

**1. Install vox (CLI only, no daemon):**

```bash
curl -fsSL https://raw.githubusercontent.com/punt-labs/vox/main/install.sh | sh
```

Skip `vox daemon install` — you don't need a local daemon.

**2. Configure env vars in `.envrc`:**

Create or edit `.envrc` in your project directory:

```bash
# Remote voxd on machine A
export VOXD_HOST=192.168.1.100   # machine A's IP
export VOXD_PORT=8421
export VOXD_TOKEN=<paste token from machine A>
```

Then `direnv allow`. Every shell in this directory will connect to A's
voxd automatically.

**3. Test:**

```bash
vox doctor    # Shows "Remote config: VOXD_HOST=..., VOXD_PORT=..., VOXD_TOKEN=***"
vox say "hello from the remote host"   # Should play on machine A
```

---

## Approach 2: SSH Tunnel

No port opening, no firewall changes. The tunnel forwards a local port
on B to voxd on A.

### On machine A (speakers)

Install vox normally. No `VOXD_BIND` needed — voxd stays on localhost.

```bash
curl -fsSL https://raw.githubusercontent.com/punt-labs/vox/main/install.sh | sh
vox daemon install
```

Get the auth token:

```bash
cat ~/.punt-labs/vox/run/serve.token
```

### On machine B (remote host)

**1. Install vox CLI (no daemon):**

```bash
curl -fsSL https://raw.githubusercontent.com/punt-labs/vox/main/install.sh | sh
```

**2. SSH with reverse tunnel:**

From machine A, open the tunnel:

```bash
ssh -R 18421:localhost:8421 machine-B
```

This maps port 18421 on B to port 8421 on A. Use 18421 (not 8421) to
avoid colliding with B's own voxd if it's running.

**3. Configure env vars in `.envrc` on B:**

```bash
# Tunnel to machine A's voxd
export VOXD_HOST=127.0.0.1
export VOXD_PORT=18421
export VOXD_TOKEN=<paste token from machine A>
```

**4. Test:**

```bash
vox say "hello through the tunnel"   # Should play on machine A
```

The tunnel dies when the SSH session ends. For persistent tunnels,
use `autossh` or an SSH config entry with `RemoteForward`.

---

## Persistent SSH tunnel (optional)

Add to `~/.ssh/config` on machine A:

```text
Host machine-B
  HostName machine-B.example.com
  RemoteForward 18421 localhost:8421
```

Now every `ssh machine-B` automatically sets up the tunnel.

For auto-reconnecting tunnels, install `autossh`:

```bash
# macOS
brew install autossh

# Ubuntu
sudo apt install autossh

# Start persistent tunnel
autossh -M 0 -f -N -R 18421:localhost:8421 machine-B
```

---

## Recording and playback over a remote daemon

The daemon is the audio host: it owns the recordings store and plays audio on
its own machine (the one with speakers). Recording and playback are coherent
whether the daemon is local or remote.

**`vox rec new`** captures speech into the daemon's store and prints the bare
store id — it does not write a file on the client, and takes no `-o`:

```bash
# On machine B (driving A's daemon):
vox rec new "the build is green"
# → a1b2c3d4e5f6.mp3   (the recording's id in the daemon store)
```

Pass `--name greeting.mp3` to store under a chosen bare filename (no
directories, no `..`). `vox rec list` enumerates the store.

**`vox rec play <id>`** plays a stored recording on the daemon host by its id —
audio comes out of A's speakers even though you ran the command on B:

```bash
vox rec play a1b2c3d4e5f6.mp3     # plays on machine A
```

(To play a local file on the machine you're sitting at, use the OS player —
`afplay`/`ffplay`. `vox rec play` is for recordings in the daemon store.)

**`vox rec get <id>`** copies a stored recording to the client, writing it into
the current directory under its store name — no `-o`, and it refuses to
overwrite an existing file:

```bash
vox rec get a1b2c3d4e5f6.mp3     # writes ./a1b2c3d4e5f6.mp3 on B
```

The bytes stream from the daemon in bounded, sha256-verified chunks, so a
recording of any size transfers in full.

The client never names a path on the daemon's filesystem: a recording is
referenced only by its store id, and the daemon confines every rec operation
to its own `0700` recordings root.

---

## Env vars reference

| Var | Side | Default | Purpose |
|-----|------|---------|---------|
| `VOXD_HOST` | client | `127.0.0.1` | WebSocket host for voxd |
| `VOXD_PORT` | client | read `serve.port` file | WebSocket port for voxd |
| `VOXD_TOKEN` | client | read `serve.token` file | Auth token for voxd |
| `VOXD_BIND` | server | `127.0.0.1` | Address voxd binds to (set at install time) |

Resolution order: explicit constructor arg > env var > file > default.

When `VOXD_HOST` is set to a remote address, also set `VOXD_PORT` and
`VOXD_TOKEN` — file-based discovery only works for a local daemon.

## Troubleshooting

**Connection timeout:** voxd isn't reachable. Check `VOXD_HOST` IP,
verify with `curl http://<ip>:8421/health`.

**Connection reset:** macOS firewall is blocking. See the firewall
section above.

**HTTP 403:** Token mismatch. Re-copy the token from machine A:
`cat ~/.punt-labs/vox/run/serve.token`. The token is stable across
restarts but changes if you delete the file or reinstall.

**`vox doctor` shows the right config but `vox say` fails:** Run
`vox doctor` on machine A too — confirm the daemon is actually running
and on the expected port.

## Security

The auth token prevents unauthorized access. It's transmitted in the
WebSocket URI query string over unencrypted `ws://`. On trusted LANs,
this is sufficient. On untrusted networks, use the SSH tunnel approach
— it encrypts everything.

`vox doctor` redacts the token value as `***` in its output.
voxd's access logs redact `?token=...` from logged URIs.
