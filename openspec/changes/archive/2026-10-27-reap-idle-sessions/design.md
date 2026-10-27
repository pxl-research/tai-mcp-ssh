# Design

## Context

- `ConnectionPool.get` (ssh.py) runs a one-time bootstrap per host alias (`_ensure_ready`). That bootstrap fires the remote log sweep as a background task. Because it runs only once, a long-lived MCP effectively never sweeps again.
- `SessionManager.run` (sessions.py) creates the tmux session only when the `session_id` is not yet in its in-memory registry. From then on it assumes the tmux session exists.
- Every tmux target is currently passed as a bare `-t tai/<name>`. When tmux finds no exact match, it falls back to prefix matching, then to fnmatch. So once `tai/foo` is gone, `-t tai/foo` resolves to `tai/foobar` if that session exists. This matters for both reaping and self-healing.
- Configuration comes from `hosts.toml` through `load_config`. `Services.reload_hosts_from_disk` re-reads it on every `hosts` tool call.

## Goals / Non-Goals

**Goals:**
- One remote round trip per reaping run in the common case (nothing to reap).
- Reaping can never kill a session that is doing work or belongs to someone else.
- Self-healing adds no extra round trip to `session_run`.

**Non-Goals:**
- Keeping `SessionManager`'s in-memory registry in sync with reaped sessions. A reaped session still appears in `session_list` until its next use, which recreates it.
- Making the throttle interval configurable.

## Decisions

### 1. Reaper lives in `ConnectionPool`, beside the log sweep

The pool already owns per-host bootstrap and fire-and-forget housekeeping, including holding strong references to the tasks. Both housekeeping jobs move into one `_maintain(conn)` background task with two independent best-effort steps: log sweep, then session reap.

*Alternative:* putting the reaper in `SessionManager`. Rejected because the transfer tools also obtain connections, and the reaper doesn't need session state.

### 2. Throttle: per-alias monotonic timestamp checked in `get()`

After a successful `get()`, if `now - last_maintenance[alias] >= 3600`, the pool records `now` and schedules `_maintain`. Because the timestamp is recorded before the task starts, concurrent calls don't schedule duplicates. The timestamp survives connection eviction, so a reconnect storm can't trigger repeated sweeps. It is lost when the process restarts, so the first connect after a start always runs maintenance. The clock is injectable (`monotonic=`) for tests.

*Alternative:* a background `asyncio` timer per host. Rejected because it would keep connections busy while the MCP is idle, and it needs lifecycle management.

### 3. One remote command lists panes and the remote clock

```
date +%s; tmux list-panes -a -F '#{session_name}\t#{session_attached}\t#{session_activity}\t#{pane_current_command}\t#{pane_pid}'
```

- Idle age is computed against the **remote** `date +%s`, not local time, which avoids clock skew between the two machines.
- If no tmux server is running, `list-panes` exits non-zero with "no server running". That counts as zero sessions and is audited as `ok`.
- Session names are filtered in Python with `^(tai|tai-mcp)/`.
- Rows are grouped per session. A session is a candidate only if **every** pane qualifies. Our sessions have one pane, but a human may have split one.
- Last activity is `#{session_activity}`, a raw Unix epoch. A snapshot of a live host confirmed it tracks real use and that stale sessions sit well past 48 h.

### 4. Kill step re-checks for child processes, and only kills on a definite "none"

A shell at its prompt can still own background jobs (`cmd &`). Killing the session would send them SIGHUP. For each candidate, a second round trip runs:

```
pgrep -P <pane_pid> >/dev/null 2>&1; [ $? -eq 1 ] && tmux kill-session -t '=<name>' && echo '<name>'
```

- `pgrep` exits 1 only when it definitely found no children. If `pgrep` is missing (127) or errors, the session is skipped.
- The candidates' commands are chained into a single round trip. Names are shell-quoted.
- The stdout lines are the killed names, which go into the `_session_sweep` audit record.

*Alternative:* skipping the child check. Rejected because it contradicts the promise that backgrounded children survive between calls.

### 5. Exact-match tmux targets everywhere

All session targets use tmux's `=` exact-match prefix:
- `-t '=tai/<name>'` for session commands (`has-session`, `kill-session`)
- `-t '=tai/<name>:'` for pane commands (`pipe-pane`, `send-keys`)

This fixes the prefix-match hazard described in Context for `session_kill`, self-healing and reaping. `new-session -s` takes a literal name and needs no prefix. `attach_hint` keeps the plain name for readability; matching by prefix is harmless for a human attach.

### 6. Self-healing folded into the `pipe-pane` round trip

`_send_command`'s first `conn.run` becomes:

```
tmux has-session -t '=T' 2>/dev/null || { tmux new-session -d -s T && echo __TAI_CREATED__; }; tmux pipe-pane -t '=T:'; tmux pipe-pane -t '=T:' 'cat >> LOG'
```

- If the stdout contains `__TAI_CREATED__`, the manager records `_session_create`.
- The separate first-use `_create_session` round trip goes away, so first use costs one round trip fewer. The registry entry is still created on first use.

### 7. Config: `SessionSettings` on `Config`, pushed to the pool on reload

- `config.py` parses `[sessions]` into `SessionSettings(idle_timeout_hours: int = 48)`. A negative or non-integer value is a `ConfigError`.
- `ConnectionPool` takes `idle_timeout_hours` in its constructor. `update_hosts(...)` gains an `idle_timeout_hours` argument, so a reload takes effect on the next maintenance run.
- `0` skips the reap step. The log sweep still runs.

## Risks / Trade-offs

- **[Risk]** A quiet session that a human uses directly, without attaching, gets reaped. → Only `tai/*` names are touched, and the name is reserved for this MCP.
- **[Risk]** Busy-looking shells that are actually idle (for example `bash` running a long `read`) count as at a prompt. → After 48 h of inactivity, this is acceptable.
- **[Trade-off]** The registry can list a session that was reaped. → It heals itself on the next `session_run`. `session_kill` on it is harmless and returns `killed: false`.
- **[Risk]** `pane_current_command` reports `-bash` for login shells on some systems. → Strip a leading `-` before comparing.
- **[Risk]** `=`-prefixed targets behave differently on very old tmux versions. → Verify on the oldest tmux in use (Raspberry Pi OS / Ubuntu LTS). Exact match has existed since tmux 2.1.

## Migration Plan

No data migration. Existing `tai-mcp/*` sessions are reaped through the legacy prefix once they have been idle past the timeout. Rollback is a code revert; `[sessions]` in `hosts.toml` is ignored by older versions.

## Open Questions

- Should the legacy `tai-mcp/` match be removed in a later release, once no such sessions remain?
