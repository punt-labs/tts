# Design: Remote voxd Connectivity

**Author:** Raymond H (rmh)
**Date:** 2026-05-12
**Mission:** m-2026-05-12-009
**Status:** SHIPPED (v4.8.0, 2026-05-12) — `VOXD_HOST`/`VOXD_PORT`/`VOXD_TOKEN` (client) and `VOXD_BIND` (server) are live in `client.py`, `doctor.py`, `voxd/daemon.py`, and the `service/` modules. User guide: [`docs/guide-remote-setup.md`](../guide-remote-setup.md). The launchd details below predate the DES-038 LaunchDaemon→LaunchAgent move (v4.9.0) and are retained as the original design record.

## Problem

voxd binds to `127.0.0.1:8421` and clients discover port/token from
local files (`~/.punt-labs/vox/run/serve.port`, `serve.token`). SSH
reverse tunnels have already proven the protocol works across hosts.
Two barriers remain: the client hardcodes localhost, and the server
only binds localhost.

## Design

Three client-side env vars, one server-side env var. When client env
vars are set, file-based discovery is bypassed entirely.

### Env Vars

| Var | Side | Default | Purpose |
|-----|------|---------|---------|
| `VOXD_HOST` | client | `127.0.0.1` | WebSocket host for voxd |
| `VOXD_PORT` | client | read `serve.port` file | WebSocket port for voxd |
| `VOXD_TOKEN` | client | read `serve.token` file | Auth token for voxd |
| `VOXD_BIND` | server | `127.0.0.1` | Address voxd binds to |

### Client Resolution Order

In `client.py`, at the top of the module:

```python
def _env_host() -> str:
    """Return VOXD_HOST or default."""
    return os.environ.get("VOXD_HOST", "127.0.0.1")

def _env_port() -> int | None:
    """Return VOXD_PORT as int, or None to fall back to file."""
    raw = os.environ.get("VOXD_PORT")
    if raw is None:
        return None
    try:
        return int(raw)
    except ValueError:
        logger.warning("VOXD_PORT=%r is not an integer, ignoring", raw)
        return None

def _env_token() -> str | None:
    """Return VOXD_TOKEN, or None to fall back to file."""
    return os.environ.get("VOXD_TOKEN")
```

Resolution for each parameter follows the same pattern -- env var
wins, file-based discovery is the fallback:

| Parameter | 1st (env var) | 2nd (file) | 3rd (hardcoded) |
|-----------|---------------|------------|------------------|
| host | `VOXD_HOST` | -- | `127.0.0.1` |
| port | `VOXD_PORT` | `serve.port` | error (daemon not running) |
| token | `VOXD_TOKEN` | `serve.token` | `None` (no auth) |

When `VOXD_HOST` is set to a remote address, `VOXD_PORT` and
`VOXD_TOKEN` should also be set -- the file-based discovery only
makes sense for a local daemon. The client does not enforce this; it
simply follows the resolution order above. If the user sets
`VOXD_HOST=remote-box` but not `VOXD_PORT`, the client will try to
read `serve.port` from the local filesystem, which will either contain
the local daemon's port or be absent (raising `VoxdConnectionError`
with a clear message).

### Server Bind Address

In `voxd.py`, the `main()` function already accepts `--host` via
typer. Use typer's `envvar=` parameter to wire `VOXD_BIND` as the
env-var source for `--host`, giving standard CLI precedence for free:

```python
DEFAULT_HOST = "127.0.0.1"

@cli.callback(invoke_without_command=True)
def main(
    port: int = typer.Option(DEFAULT_PORT, "--port", "-p", help="Listen port"),
    host: str = typer.Option(DEFAULT_HOST, "--host", envvar="VOXD_BIND", help="Listen host"),
) -> None:
    ...
    config = uvicorn.Config(app, host=host, port=port, ...)
```

Precedence: `--host` (explicit CLI flag) > `VOXD_BIND` (env var) >
`DEFAULT_HOST` (`127.0.0.1`). Typer handles this natively -- no
manual `os.environ.get()` needed in `main()`.

When binding to a non-localhost address, voxd logs a WARNING:

```python
if host not in ("127.0.0.1", "::1"):
    logger.warning(
        "Binding to %s -- voxd is accessible from the network. "
        "Ensure VOXD_TOKEN is set on all clients.",
        host,
    )
```

Access logging stays enabled, but the token must be stripped from
logged URIs. The auth token appears in the WebSocket upgrade request
as `?token=<value>`. A uvicorn log filter redacts the query string
from access log lines:

```python
class _TokenRedactFilter(logging.Filter):
    _TOKEN_RE = re.compile(r"\?token=[^\s\"']+")

    def filter(self, record: logging.LogRecord) -> bool:
        if hasattr(record, "msg") and isinstance(record.msg, str):
            record.msg = self._TOKEN_RE.sub("?token=REDACTED", record.msg)
        return True
```

Applied to uvicorn's access logger at startup. This preserves
operational visibility (connection counts, status codes, timing)
while preventing token leakage to log files.

### Service Unit Changes

In `service.py`, when `VOXD_BIND` is set at install time, bake it
into the service unit as an environment variable. The voxd binary
reads `VOXD_BIND` at runtime via `typer.Option(..., envvar="VOXD_BIND")`
-- no `--host` flag needs to be passed in the exec args.

**systemd** (`_systemd_unit_content`):

```python
def _systemd_unit_content(user: str) -> str:
    ...
    bind = os.environ.get("VOXD_BIND")
    if bind is not None:
        # Add to env_block alongside PATH and audio vars
        env_block lines += f'Environment="VOXD_BIND={bind}"'
```

**launchd** (`_launchd_plist_content`):

```python
def _launchd_plist_content(user: str) -> str:
    ...
    bind = os.environ.get("VOXD_BIND")
    # Add to EnvironmentVariables dict if set:
    #   <key>VOXD_BIND</key>
    #   <string>{escaped_bind}</string>
```

This follows the existing pattern: `PATH` and audio vars are already
baked into service units from the installing shell's environment.

## Changed Files

### `src/punt_vox/client.py`

1. Add `import os` to stdlib imports.
2. Add `_env_host()`, `_env_port()`, `_env_token()` module-level
   helpers (9 lines total, shown above).
3. Change `VoxClient.__init__` default for `host` from `"127.0.0.1"`
   to `_env_host()`.
4. In `_resolve_port()`: check `_env_port()` before reading
   `serve.port`. If `self._explicit_port` is set (caller passed
   `port=`), it still wins.
5. In `_resolve_token()`: check `_env_token()` before reading
   `serve.token`. If `self._explicit_token` is set, it still wins.
6. Change `VoxClientSync.__init__` default for `host` from
   `"127.0.0.1"` to `_env_host()`.

Specifically, the resolution in `_resolve_port` becomes:

```python
def _resolve_port(self) -> int:
    if self._port is not None:
        return self._port
    env = _env_port()
    if env is not None:
        return env
    port = read_port_file()
    if port is None:
        msg = "voxd port file not found. Is the daemon running? Start it with: voxd"
        raise VoxdConnectionError(msg)
    return port
```

And `_resolve_token`:

```python
def _resolve_token(self) -> str | None:
    if self._token is not None:
        return self._token
    env = _env_token()
    if env is not None:
        return env
    return read_token_file()
```

### `src/punt_vox/voxd.py`

1. Add `envvar="VOXD_BIND"` to the `--host` typer Option declaration.
   This gives `--host > VOXD_BIND > DEFAULT_HOST` precedence natively.
   No manual `os.environ.get()` call needed.
2. Add a `logger.warning()` when `host` is not `127.0.0.1` or `::1`
   (non-localhost bind).
3. Add a `_TokenRedactFilter` log filter that strips `?token=...`
   from access log messages. Apply it to uvicorn's access logger at
   startup. Access logging stays enabled for operational visibility.
   (~12 lines changed)

### `src/punt_vox/service.py`

1. In `_systemd_unit_content()`: after building `audio_lines`, append
   a `VOXD_BIND` environment line if the var is set in the installing
   shell. (4 lines)
2. In `_launchd_plist_content()`: add a `<key>VOXD_BIND</key>` entry
   to the `EnvironmentVariables` dict if set. (5 lines)

### `src/punt_vox/__main__.py`

1. In the `doctor` command: after probing the daemon, display any
   active `VOXD_*` env var overrides in the output. Example:

   ```text
   OK  Daemon: running on port 8421 (provider: elevenlabs, version 4.7.5)
       (via VOXD_HOST=192.168.1.100, VOXD_PORT=8421)
   ```

   Check `VOXD_HOST`, `VOXD_PORT`, `VOXD_TOKEN` and append a
   parenthetical line listing those that are set. (~8 lines)

All other CLI commands that construct `VoxClientSync()` with no
arguments will pick up the env-var defaults through the updated
`__init__` signatures -- no changes needed there.

### `src/punt_vox/hooks.py`

No changes needed. `_make_client()` returns `VoxClientSync()` with no
arguments -- env var defaults flow through automatically.

### `src/punt_vox/server.py`

No changes needed. `_voxd_client()` returns `VoxClientSync()` with no
arguments -- same reasoning.

### Tests

- `tests/test_client.py`: parametrized tests for env var resolution
  (`monkeypatch.setenv`). Verify VOXD_HOST, VOXD_PORT, VOXD_TOKEN
  override defaults. Verify invalid VOXD_PORT is ignored with warning.
- `tests/test_service.py`: verify VOXD_BIND is baked into generated
  systemd unit content and launchd plist content when set.
- `tests/test_cli.py`: verify `vox doctor` reports active VOXD_*
  env var overrides in its output.
- `tests/test_voxd.py` (or inline): verify non-localhost bind emits
  a WARNING log.

## What Does NOT Change

- **WebSocket protocol.** No message format changes. The `ws://` URI
  scheme, JSON message types, auth token query parameter -- all
  identical.
- **Token generation.** voxd still generates and writes `serve.token`
  on startup. Local clients still read it from the file. Remote
  clients override it with `VOXD_TOKEN`.
- **Port file.** voxd still writes `serve.port` after bind. Local
  clients still read it.
- **Default behavior.** With no env vars set, behavior is identical to
  today: bind `127.0.0.1:8421`, discover port/token from files.
- **No new dependencies.** `os.environ.get` is stdlib.
- **No mcp-proxy integration.** Vox does not use mcp-proxy today and
  this design does not add it. The MCP server (`vox mcp`) runs via
  stdio and connects to voxd as a local client. This design makes the
  voxd connection configurable; the MCP transport is orthogonal.
- **`service.py` install flow.** No new sudo calls, no new files. The
  only change is an optional extra `Environment=` line in the
  generated unit content.

## Security

**Network-exposed voxd** -- when `VOXD_BIND=0.0.0.0`, voxd listens on
all interfaces. The existing token-based auth is the security boundary:

1. **Token is required.** Every WebSocket connection must present the
   token via `?token=<value>` query parameter. Connections without a
   valid token are rejected before any message processing.

2. **Token is random.** Generated by `secrets.token_urlsafe(32)` on
   first startup, stored at `~/.punt-labs/vox/run/serve.token` with
   mode 0700 on the parent directory.

3. **Token transmission.** The remote client receives the token
   out-of-band (the user copies it, sets `VOXD_TOKEN` in
   `.envrc`). It is transmitted in the WebSocket URI query string over
   unencrypted `ws://`.

4. **No TLS.** voxd does not terminate TLS. For remote use over
   untrusted networks, an SSH tunnel provides encryption (same pattern
   already validated in testing). The token prevents unauthorized
   access; the tunnel prevents eavesdropping.

5. **Threat model.** An attacker on the network can observe the token
   in cleartext if no tunnel is used. Mitigation: document that
   `VOXD_BIND=0.0.0.0` without an SSH tunnel exposes the token and
   should only be used on trusted LANs. This matches quarry's model
   (quarry uses TLS with self-signed certs for remote; vox is
   lower-stakes -- audio playback, not data access).

6. **No new attack surface on default config.** With no env vars set,
   voxd binds `127.0.0.1` only. Network exposure is strictly opt-in.

## Alignment

### Quarry pattern

Quarry uses `mcp-proxy` with a TOML config file
(`~/.punt-labs/mcp-proxy/quarry.toml`) that stores the WebSocket URL
and auth headers. This design does not adopt that pattern because vox
has a simpler topology: the vox MCP server (`vox mcp`) is a thin
client of voxd, not a standalone daemon. The mcp-proxy pattern is
appropriate when the MCP server itself is the daemon (quarry, lux).
For vox, the connection that needs remote configuration is
`VoxClient -> voxd`, not `Claude Code -> vox mcp`.

The env var names follow quarry's conventions (product-prefixed,
uppercase, underscore-separated). The resolution order (env var >
file > default) matches quarry's client-side pattern.

### Lux mcp-proxy-proposal

The lux proposal introduces `luxd` as a session hub daemon with
`mcp-proxy` bridging stdio to WebSocket. Vox's architecture is
different: voxd is a pure audio server, not an MCP server. The
`vox mcp` process is a FastMCP stdio server that happens to use voxd
as a backend. Making voxd remotely accessible solves the cross-host
problem without the proxy layer.

If vox later adopts mcp-proxy (for MCP server lifecycle benefits), the
env vars introduced here remain correct -- they configure the
`VoxClient -> voxd` connection, which is independent of the
`Claude Code -> vox mcp` transport.
