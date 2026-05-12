## Context

This is a greenfield Python MCP server for letting LLMs administer small fleets of Linux servers (Raspberry Pi, Ubuntu) over SSH. The user maintains a handful of hosts (single digits to low double digits), trusts the LLM to drive routine ops, but does not want to hand it credentials or root, and does not want verbose command output (e.g. `apt update`, `docker build`) to consume the model's context budget.

Constraints:
- Local-MCP topology only (v1): MCP runs on the same machine or LAN as the human operator, so they can `tmux attach` to a remote pane to enter sudo passwords. Remote-MCP topology is documented as out of scope.
- Python 3.11+ (for `tomllib`).
- `tmux` available on each managed host. Trivial install on Debian/Ubuntu/RPi OS.
- SSH key auth is the recommended path; password auth is supported only via OS keychain, never plaintext on disk or argv.

## Goals / Non-Goals

**Goals:**
- Minimum-surface MCP API: 7 tools, total tool description footprint kept small to preserve token budget on every LLM turn.
- Output capture by default: command stdout/stderr lands in a remote log file; tool responses return head + tail slices when output is large.
- Persistent shell state per session, addressed by `<host>/<name>` composite IDs.
- Clean sudo handoff: detect the password prompt within milliseconds, surface a structured `needs_password` response with an `attach_hint`, and let the user complete the prompt via `tmux attach`.
- Rich, append-only JSONL audit trail: every tool invocation produces one line capturing timestamps, exit codes, byte counts, log_ids, and optional LLM-supplied `reason`.
- Operator CLI for host management, audit inspection, and serving the MCP.

**Non-Goals:**
- Multi-tenant / multi-user MCP. Single user, single machine.
- Remote-MCP topology (MCP running on a bastion, user on a laptop elsewhere). Sudo handoff design assumes the user can SSH directly to the managed host.
- Sudo-over-SFTP or any form of "MCP smuggling root into file transfer". `put` runs as the SSH user; root-owned destinations use stage-and-move.
- A web UI for the audit log (a future skin over the JSONL file; `tail -f | jq` is the v1 experience).
- Windows remote hosts. Linux/macOS only on the remote side.
- Storing or processing the user's sudo password inside the MCP process at any time.

## Decisions

### 1. tmux-backed unified session model (no separate fire-and-forget lane)

Every command runs inside a tmux session on the managed host. Sessions are named `tai-mcp/<name>` and addressed externally by composite ID `<host>/<name>`. Sessions auto-create on first `session_run`.

**Why a single lane**: an earlier draft had a fast stateless `run` plus a `run_sudo` that used tmux. Collapsing both into tmux-backed sessions buys: persistent shell state (cwd, env, venv activation) without LLM chaining gymnastics; a natural home for long-running commands; the sudo handoff "for free"; and live observability — the operator can `tmux attach` and literally watch the LLM work.

**Cost accepted**: `tmux` must be installed on every managed host (trivial), and "command finished" must be detected via a sentinel marker rather than coming for free from `ssh exec`.

**Alternative considered**: stateless `ssh exec` per command, with tmux only when the LLM explicitly opts in. Rejected because it doubled the tool surface, complicated audit (two record shapes), and the LLM doesn't actually benefit from statelessness — it has to re-state context every call regardless.

### 2. Sentinel-based completion detection

After sending a command into a tmux pane, the MCP appends `; echo __TAI_DONE__$?__<run_id>__` and polls `tmux capture-pane -p` (or `pipe-pane` capture) until that marker appears. The exit code and run ID are extracted from the marker line, then everything between the previous prompt and the marker is treated as the command's output.

**Why**: prompt detection across distros, shells, and PS1 variations is fragile. A sentinel with the run_id avoids ambiguity even if multiple commands have run in the pane.

**Alternative considered**: `tmux pipe-pane` to stream output to a file and grep that file for a marker. Equivalent reliability, a bit more setup, used as a fallback if pane capture proves lossy under heavy output.

### 3. Interactive-prompt detection by pattern matching

Between polls, the MCP also scans the pane tail for known interactive prompts:

- `[sudo] password for <user>:`
- `Password:`
- `(yes/no)?` (ssh hostkey)
- apt's `Do you want to continue? [Y/n]`
- `Are you sure? [y/N]`

If matched, the tool returns immediately with `status: "needs_password"` (sudo) or `"needs_input"` (others), plus an `attach_hint` of the form `ssh <host> -t tmux attach -t tai-mcp/<name>`. The LLM relays this to the user. After the user completes the prompt and detaches, the LLM calls `session_wait` to capture continued output.

**Why patterns + timeout (not timeout alone)**: pattern matching gives sub-second response to a stuck password prompt, instead of waiting for the full `timeout` window every time. Timeout is still the safety net for genuinely long-running commands → `status: "still_running"`.

### 4. Output capture lives on the remote, not in the MCP

Every command is wrapped to tee its combined stdout+stderr to `~/.tai-ssh/logs/<log_id>.log` on the remote. `log_id` is a ULID generated by the MCP and embedded in both the wrapper and the audit record. Tool responses return `{head, tail, bytes, truncated, log_id}`; when output is small (≤ threshold, e.g. 4 KB or 50 lines), `truncated` is false and full output is inline.

**Why on remote, not in MCP**: keeps the LLM's path to "read the full log" as simple as `session_run("host/default", "tail -200 ~/.tai-ssh/logs/<log_id>.log")` — no extra tool, full power of shell tooling (grep/awk/wc), and the log fetch itself is audited because it goes through `session_run`. The MCP avoids buffering arbitrarily large outputs in its own memory.

**Cleanup**: a stale-log sweep deletes logs older than 7 days from `~/.tai-ssh/logs/` on every connect, capped to avoid pathological cases. Default retention is configurable per host in `hosts.toml`.

### 5. JSONL audit log with rich fields

Path: `~/.local/state/tai-mcp-ssh/audit.jsonl` (XDG state dir; macOS may use `~/Library/Logs/tai-mcp-ssh/audit.jsonl` — picked at install time, documented).

One JSON object per line. Fields:

```
ts, tool, session, host, user, cmd, reason, exit, status,
duration_ms, stdout_bytes, stderr_bytes, log_id, truncated,
needs_password, llm_session_id?, sha256? (for put/get)
```

**Why JSONL not plain log**: greppable, `jq`-able, append-only, line-oriented for tailing. A future web UI is a thin skin over the file.

**Rotation**: append-only with size-based rotation at 64 MB (`audit.jsonl` → `audit.jsonl.1`, etc.). Not implemented in v1; the file is allowed to grow and the user can rotate manually. Tracked as follow-up.

### 6. Composite session IDs `<host>/<name>`

Session IDs include the host so the LLM (and the audit log, and stack traces) can immediately tell which host a session lives on, and so the `host` parameter doesn't appear on every session tool.

**Alternative considered**: opaque ULID returned by a separate `session_open(host)` tool. Rejected: adds a tool, adds a round trip, makes audit lines less readable, and adds an indirection that buys nothing.

### 7. Host inventory in TOML; secrets in OS keychain

`~/.config/tai-mcp-ssh/hosts.toml` is the explicit allowlist of hosts the LLM may reach. Each entry references a `~/.ssh/config` alias when possible (so SSH key auth, ports, jumphosts come for free), or specifies `host`/`user`/`auth` inline.

Password auth resolves through `keychain://tai-mcp-ssh/<alias>`. The keychain reference is the only secret-related string ever written to TOML. The OS keychain is accessed via the `keyring` Python library (works on macOS Keychain, Linux libsecret, Windows Credential Manager).

**Why TOML over YAML/JSON**: no indentation footguns, native `tomllib` in 3.11+, comments allowed for human edits. A SQLite DB was considered (atomic, queryable) but rejected as overkill for a dozen entries and worse for emergency hand-editing.

**Why keychain over an encrypted file**: existing OS facility the user already trusts, no key-management for us, no extra dependency beyond `keyring`.

### 8. `asyncssh` over shelling out to `ssh`/`scp`

The MCP uses `asyncssh` for all SSH and SFTP operations.

**Why**: native password auth (no `sshpass` gymnastics, no argv leakage), built-in connection multiplexing, `~/.ssh/config` parsing included, native SFTP for `put`/`get`, and a coherent async API that pairs naturally with the Python MCP SDK.

**Cost**: one more dependency. Accepted in line with the user's stated preference: simplicity & elegance > absolute minimal dep count.

### 9. CLI binary `tai-mcp-ssh` with subcommands

```
tai-mcp-ssh hosts add <alias>     interactive (getpass, never argv)
tai-mcp-ssh hosts list            redacts secrets
tai-mcp-ssh hosts remove <alias>  also wipes keychain entry
tai-mcp-ssh hosts test <alias>    runs whoami, reports latency
tai-mcp-ssh audit tail [-n] [--host X]
tai-mcp-ssh serve                 starts the MCP over stdio
```

**Why one binary**: same code paths the server uses for connecting are exercised by `hosts test`, so the CLI doubles as a smoke test. Built with `click`.

### 10. Tool surface: 7 tools, minimal payloads

```
hosts() → [{alias, host, user, auth}]
session_list() → [{session_id, host, name, created_at, last_used_at, busy}]
session_run(session_id, command, reason?, timeout?=30) → result
session_wait(session_id, timeout?=30) → result
session_kill(session_id) → {killed: bool}
put(host, local_path, remote_path, reason?) → {bytes, sha256}
get(host, remote_path, local_path?, reason?) → {bytes, sha256, local_path}
```

`result` shape:
```
{
  status: "done" | "still_running" | "needs_password" | "needs_input",
  exit?: int,            # only when status == "done"
  head: str,             # first ~50 lines or ≤2 KB
  tail: str,             # last ~50 lines or ≤2 KB
  bytes: int,            # total bytes of output captured
  truncated: bool,
  log_id: str,
  log_path: str,         # absolute path on remote
  attach_hint?: str,     # present when status is needs_*
  prompt?: str           # the matched prompt line, when needs_*
}
```

**Why no `read_log` tool**: the LLM uses `session_run` with `tail`/`grep`/`cat` against `log_path`. One fewer tool description on every prompt, and the LLM gets the full power of shell composition for free.

## Risks / Trade-offs

- **[Pane capture loses output under bursts]** → use `tmux pipe-pane -O` to also stream to a file on the remote; the file is the source of truth for output and the pane is just for interactivity / human attach.
- **[Sentinel false positive]** if a command literally prints `__TAI_DONE__...` → include the per-run ULID in the sentinel; collision is cryptographically negligible.
- **[Sudo handoff requires the user to be on the same network as the managed host]** → documented explicitly; remote-MCP topology is non-goal for v1.
- **[Long-running commands look like "stuck on prompt"]** → pattern set is conservative (anchored to known regexes, end-of-pane only) and timeout fallback still returns `still_running` rather than mis-classifying.
- **[Keychain unavailable in headless contexts]** (e.g. user runs MCP under launchd before login on macOS, or in a server without secret service) → CLI `hosts test` detects this at setup time; documented as a known limitation. SSH key auth is the recommended escape hatch.
- **[On-remote `~/.tai-ssh/logs/` grows unbounded]** → 7-day TTL sweep on connect, configurable per host. Manual `rm` always available.
- **[Audit log grows unbounded]** → no automatic rotation in v1, documented as a follow-up. JSONL is friendly to manual rotation.
- **[Concurrent `session_run` calls against the same session]** → MCP serializes per-session: a second `session_run` to a busy session returns `{status: "busy"}` with a hint to use `session_wait`. Audit log records both attempts.
- **[tmux not installed on the remote]** → first connect attempts to detect tmux and emits a clear error with the install command. The MCP does not auto-install.

## Migration Plan

No migration: greenfield repository, no existing users, no existing data.

Rollout for new hosts:
1. User runs `tai-mcp-ssh hosts add <alias>` (or edits `~/.ssh/config` plus a minimal TOML entry).
2. User runs `tai-mcp-ssh hosts test <alias>` to verify connectivity.
3. (Recommended) User configures `NOPASSWD` sudoers entries for the small set of commands the LLM legitimately needs, so the handoff is the exception not the rule.

Rollback: remove the host from `hosts.toml` (or `tai-mcp-ssh hosts remove <alias>`); the MCP refuses to reach it. Existing tmux sessions on the remote can be killed via `ssh <host> tmux kill-server` or left to expire.

## Open Questions

- Should `put`/`get` carry an optional `session_id` so file transfers attach to a logical session for audit grouping? Default plan: no — SFTP doesn't share shell state, so binding it to a session is misleading. Revisit if audit consumers want grouping.
- ULID vs UUIDv7 for `log_id`? Both work; ULID is shorter in canonical form (26 chars) and the lexicographic-time-ordering is nice for `ls` in the remote logs directory. Going with ULID unless `python-ulid` proves troublesome.
- Should `hosts test` also probe for tmux presence and `~/.tai-ssh/logs/` writability? Default plan: yes — surface fixable setup issues during onboarding rather than on first LLM call.
