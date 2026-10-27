# Configuration

## Host allowlist (`hosts.toml`)

The LLM can only reach hosts listed in `hosts.toml`. The file lives at:

- `$XDG_CONFIG_HOME/tai-mcp-ssh/hosts.toml`
- `~/.config/tai-mcp-ssh/hosts.toml` if `XDG_CONFIG_HOME` is unset (on macOS too)

If the file is missing or empty, the allowlist is empty, so the server still starts cleanly. The `hosts` MCP tool re-reads the file on every call, which means you can edit it without restarting the server. If an edit leaves the file malformed, the server keeps using the last valid allowlist and records the error in the audit log.

You would normally manage the file with the CLI rather than by hand:

```sh
uv run tai-mcp-ssh hosts add <alias> [--host H] [--user U] [--port 22] [--auth key|password] [--identity-file PATH]
uv run tai-mcp-ssh hosts list
uv run tai-mcp-ssh hosts test <alias>
uv run tai-mcp-ssh hosts remove <alias> [-y]
```

`hosts add` needs an interactive terminal and prompts for any options you leave out. For password auth it stores the password in the OS keychain and writes only a reference to it into the file. `hosts remove` also deletes the matching keychain entry.

### Example

```toml
[hosts.pi-living]          # key auth via an existing ~/.ssh/config alias
auth = "key"

[hosts.vps]
host = "203.0.113.10"
user = "admin"
port = 2222
auth = "key"
identity_file = "/Users/me/.ssh/vps.pem"
log_retention_days = 14

[hosts.lab-box]
host = "lab.local"
user = "pi"
auth = "password"
password_ref = "keychain://tai-mcp-ssh/lab-box"

[audit]
retention_days = 90

[sessions]
idle_timeout_hours = 48
```

### Host fields

| Key | Default | Notes |
|---|---|---|
| `host` | the alias | IP or DNS name. Leave it out to use the matching `Host` block in `~/.ssh/config`. |
| `user` | from `~/.ssh/config` | Remote username. |
| `port` | `22` | |
| `auth` | `"key"` | Either `"key"` or `"password"`. |
| `identity_file` | — | A specific private key. Applies to key auth only. |
| `password_ref` | — | Required when `auth = "password"`. Must have the form `keychain://<service>/<account>`. |
| `log_retention_days` | `7` | Remote command logs older than this are deleted in the background while the MCP is in use, at most once per hour per host. |

The `[audit] retention_days` setting (default `90`) controls how long local audit files are kept. See [observability.md](./observability.md).

### Session cleanup

`[sessions] idle_timeout_hours` (default `48`, `0` disables it) is a global setting; there is no per-host override. Remote tmux sessions named `tai/*` are cleaned up automatically. Sessions with the old `tai-mcp/*` prefix are also included. A session is killed only when all of these hold:

- Its last activity is older than `idle_timeout_hours`.
- Its pane is sitting at a plain shell prompt (`bash`, `zsh`, `sh`, `dash`, or `fish`). A running job, or a `sudo` prompt waiting for a handoff, keeps the session alive.
- No one is attached to it.
- Its shell has no background child processes, such as a job started with `cmd &`.

The cleanup never touches tmux sessions with other names. It runs in the background while the MCP is in use, at most once per hour per host. Every run is recorded in the audit log as `_session_sweep`. If the LLM later reuses a session that was cleaned up, the server recreates it on the next `session_run`, so the call still succeeds. The session's previous shell state (working directory, environment) is lost.

### Secrets are rejected

The server refuses to load the file if any key named `password`, `passwd`, `secret`, `token`, `private_key`, or `passphrase` appears at any nesting depth. Secrets belong in the OS keychain, which is accessed through [`keyring`](https://pypi.org/project/keyring/). The keychain reference is resolved only at connect time, and the resolved password is never written to disk or to the audit log.

## SSH behaviour

- The host alias is passed to asyncssh as the connection target, so the matching `~/.ssh/config` block (HostName, User, Port, IdentityFile, ProxyJump, …) applies. Any value set in `hosts.toml` overrides the corresponding `~/.ssh/config` value.
- Each host gets one cached connection per server process. A connection that dies (for example after a reboot or network drop) is detected through 30-second keepalives and reopened automatically on the next tool call.
- The first time the server connects to a host, it checks that `tmux` is installed and creates `~/.tai-ssh/logs/` with mode `0700`.
- While a host is in use, the server runs background maintenance at most once per hour per host: it deletes old remote logs and cleans up idle sessions (see [Session cleanup](#session-cleanup)).

## Local paths

| What | Linux | macOS |
|---|---|---|
| Host allowlist | `~/.config/tai-mcp-ssh/hosts.toml` | same |
| Audit log | `~/.local/state/tai-mcp-ssh/audit/` | `~/Library/Logs/tai-mcp-ssh/audit/` |
| `get` downloads | `~/.local/state/tai-mcp-ssh/downloads/<host>/` | same |

The `XDG_CONFIG_HOME` and `XDG_STATE_HOME` environment variables override the base directories. On macOS, the audit log location ignores them.
