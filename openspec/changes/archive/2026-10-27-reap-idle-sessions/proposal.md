# Proposal

## Why

Remote `tai/*` tmux sessions pile up because nothing ever closes them. Models rarely call `session_kill`, even with the new server instructions, and there is no server-side backstop. A tracked session whose tmux session has disappeared — killed by hand today, or by a cleanup tomorrow — also breaks: `session_run` only creates the tmux session the first time a name is used, so every later command fails until someone calls `session_kill`.

## What Changes

- **Idle session reaping.** The MCP periodically kills remote tmux sessions named `tai/*` (and legacy `tai-mcp/*`, for the transition after the prefix rename) that meet all of these conditions:
  - idle for longer than a configured timeout
  - sitting at a plain shell prompt
  - not attached by a human
- **New global setting** in `hosts.toml`: `[sessions] idle_timeout_hours`. The default is `48`, and `0` disables reaping. There is no per-host override.
- **Periodic trigger.** The reaper runs in the background whenever a tool call obtains a connection to a host, at most once per hour per host. Previously, cleanup ran only on the first connect, which almost never recurs on a long-lived server. The existing remote log retention sweep moves to the same throttled trigger.
- **Self-healing sessions.** Before every command, `session_run` makes sure the tmux session exists and recreates it if it has disappeared. A vanished session no longer leaves its `session_id` stuck.
- Each reaper run is audited as `_session_sweep`.

Out of scope:
- scoping session names per client or project
- an operator CLI for sessions
- killing sessions on server shutdown or after each command

## Capabilities

### New Capabilities

None.

### Modified Capabilities

- `remote-sessions`:
  - Adds a requirement for idle-session reaping and the `[sessions].idle_timeout_hours` setting.
  - Changes the auto-create requirement so the tmux session is ensured before every command, not only on first use.
- `output-capture`: the log retention sweep runs on the same throttled per-host trigger (at most hourly), not only on the initial connect.

## Impact

- **Code:**
  - `config.py`: new `SessionSettings` dataclass and parsing of `[sessions]`.
  - `ssh.py`: throttled per-host maintenance in `ConnectionPool.get`, the reaper, and the log sweep rescheduling.
  - `sessions.py`: ensure the session exists inside the `_send_command` round trip.
  - `server.py`: pass the setting through `Services`, and update it on `hosts.toml` reload.
- **Remote hosts:** one extra `tmux list-panes` call per host per hour while the MCP is in use. Sessions idle at a prompt for longer than 48 h are killed. Their shell state (cwd, env) is lost; their log files remain.
- **Docs:** `docs/configuration.md` (new setting), `docs/tools.md` (cleanup behaviour), `docs/observability.md` (new audit event).
- No new dependencies. No changes to the MCP tool surface.
