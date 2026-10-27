# remote-sessions Specification

## Purpose
Give the LLM long-lived shell sessions on managed hosts that survive across multiple tool calls. Each session is a named tmux session on the remote (`tai/<name>`), identified by a composite `<host>/<name>` ID and addressed via `session_run`, `session_wait`, `session_list`, and `session_kill`. tmux preserves cwd, environment, exported variables, function definitions, and any backgrounded children between calls — the foundation the LLM needs to drive a real interactive workflow. Completion is detected via per-command sentinels; known interactive prompts (sudo, hostkey, apt confirmations) are recognised at the pane tail so the LLM can hand off cleanly when human input is required; transport failures (peer reboot, network drop) are surfaced as a typed `host-unreachable` error and the next call transparently reconnects.

## Requirements

### Requirement: Composite session identifier
Sessions SHALL be identified by a composite ID of the form `<host>/<name>`, where `<host>` matches an allowlist alias and `<name>` is a session label chosen by the LLM (default `"default"`). The host SHALL be derivable from the session ID without any additional lookup.

#### Scenario: Session ID parsing
- **WHEN** a tool receives `session_id = "pi-living/build"`
- **THEN** the MCP SHALL parse `host = "pi-living"` and `name = "build"`
- **AND** verify the host is in the allowlist before any further action

#### Scenario: Malformed session ID
- **WHEN** a session ID lacks a `/` separator or has an empty host or name part
- **THEN** the tool call SHALL fail with a descriptive error

### Requirement: tmux-backed sessions auto-create on first use
On the remote host, each session SHALL correspond to a tmux session named `tai/<name>`. Before sending each command, `session_run` SHALL ensure the named tmux session exists, creating it if absent, on first use or after it was killed outside `session_kill`.

#### Scenario: First call to a new session
- **WHEN** the LLM calls `session_run("pi-living/default", "pwd")` and no tmux session `tai/default` exists on `pi-living`
- **THEN** the MCP SHALL create the tmux session and then send the command
- **AND** the audit log entry SHALL reflect the session was newly created

#### Scenario: tmux session vanished between calls
- **WHEN** `pi-living/default` has completed a previous command
- **AND** the tmux session `tai/default` has since been killed outside `session_kill` (by an operator or the idle-session reaper)
- **AND** the LLM calls `session_run("pi-living/default", "pwd")`
- **THEN** the MCP SHALL recreate the tmux session and run the command
- **AND** the call SHALL NOT fail because the session was missing

#### Scenario: tmux not installed on the remote
- **WHEN** the remote host does not have `tmux` available on PATH
- **THEN** the tool call SHALL fail with an error indicating the missing dependency and recommended install command
- **AND** no tmux session SHALL be created and no command SHALL be sent

### Requirement: Sentinel-based completion detection
After sending a command into the tmux pane, the MCP SHALL append a unique sentinel of the form `printf '\n__TAI_DONE__%d__<run_id>__\n' "$?"` (or an equivalent shell-safe construction). The sentinel SHALL start on its own line — preceded by a leading newline — so that line-anchored pattern matching cannot miss it when the user's command finishes without a trailing newline. Completion SHALL be detected by observing the sentinel in pane output. The exit code SHALL be extracted from the sentinel.

#### Scenario: Command completes within timeout
- **WHEN** the sentinel `__TAI_DONE__0__01JV3N…__` appears in the pane within the timeout window
- **THEN** the tool SHALL return `status: "done"`, `exit: 0`
- **AND** the captured output SHALL exclude the sentinel line itself

#### Scenario: Per-run sentinel uniqueness
- **WHEN** two commands are run in succession in the same pane
- **THEN** each SHALL use a sentinel containing its own `run_id`
- **AND** completion detection for the second command SHALL NOT match the first command's sentinel

### Requirement: Interactive-prompt detection
While polling the pane for the completion sentinel, the MCP SHALL also detect known interactive prompts at the pane tail. When detected, the tool SHALL return immediately without waiting for the timeout.

#### Scenario: Sudo password prompt detected
- **WHEN** the pane tail matches `[sudo] password for <user>:` or `Password:`
- **THEN** the tool SHALL return `status: "needs_password"`
- **AND** the response SHALL include `attach_hint` with a command of the form `ssh <host> -t tmux attach -t tai/<name>`
- **AND** the response SHALL include the matched `prompt` string
- **AND** the audit log entry SHALL record `needs_password: true`

#### Scenario: Generic interactive prompt detected
- **WHEN** the pane tail matches an `apt`-style `[Y/n]` confirmation, a `(yes/no)?` hostkey prompt, or an `Are you sure? [y/N]` prompt
- **THEN** the tool SHALL return `status: "needs_input"` with the matched `prompt` and an `attach_hint`

#### Scenario: Pattern at start-of-output is ignored
- **WHEN** a matching prompt-like substring appears in the middle of regular command output (not at the pane tail awaiting input)
- **THEN** the MCP SHALL NOT classify it as an interactive prompt
- **AND** SHALL continue polling

### Requirement: Timeout fallback
If neither the completion sentinel nor a recognised prompt appears within the per-call `timeout` (default 30 seconds), the tool SHALL return `status: "still_running"` and return captured output collected so far.

#### Scenario: Long-running command exceeds timeout
- **WHEN** a build command is still emitting output when the timeout elapses
- **THEN** the tool SHALL return `status: "still_running"` with current head/tail/bytes
- **AND** the audit log SHALL record the partial status

### Requirement: `session_wait` resumes a session
The MCP SHALL expose a `session_wait(session_id, timeout?)` tool that polls the same session for further output without sending a new command. The same status outcomes (`done` / `still_running` / `needs_password` / `needs_input`) SHALL apply.

#### Scenario: Resume after sudo handoff
- **WHEN** the user has attached the tmux pane, entered the sudo password, and detached
- **AND** the LLM calls `session_wait("pi-living/default")`
- **THEN** the MCP SHALL capture continued output and return `status: "done"` (or another appropriate status)

#### Scenario: Resume on idle session
- **WHEN** the session has no in-flight command and the last command's `status` was `done`
- **THEN** `session_wait` SHALL return the last completion result immediately without polling

### Requirement: Per-session command serialization
Within a single session, the MCP SHALL serialize commands: a `session_run` against a session that is currently `still_running`, `needs_password`, or `needs_input` SHALL be rejected.

#### Scenario: Second command attempted on a busy session
- **WHEN** `session_run` is called while a previous command in the same session is awaiting completion or input
- **THEN** the tool SHALL return `status: "busy"` with a hint to call `session_wait`
- **AND** the rejection SHALL be audited

### Requirement: `session_list` and `session_kill` tools
The MCP SHALL expose `session_list()` returning all known active sessions across all hosts, and `session_kill(session_id)` to tear down a session.

#### Scenario: session_list shape
- **WHEN** the LLM calls `session_list()`
- **THEN** the tool SHALL return an array of `{session_id, host, name, created_at, last_used_at, busy}`

#### Scenario: session_kill terminates tmux session
- **WHEN** the LLM calls `session_kill("pi-living/build")`
- **THEN** the MCP SHALL execute the equivalent of `tmux kill-session -t tai/build` on `pi-living`
- **AND** the action SHALL be audited
- **AND** subsequent `session_run` calls to that session_id SHALL auto-create a fresh tmux session

### Requirement: Transparent recovery from transport failure
When the SSH transport to a managed host dies (peer reboot, network drop, sshd kill), the MCP SHALL evict the dead connection from its pool and surface a typed `host-unreachable` error to the caller. Subsequent tool calls against the same host SHALL transparently open a fresh connection.

#### Scenario: Tool call against a host that rebooted
- **WHEN** a managed host reboots while the MCP holds a cached SSH connection
- **AND** the LLM invokes any tool against that host
- **THEN** the first such call MAY fail with a `host-unreachable` error
- **AND** the dead connection SHALL be evicted from the pool
- **AND** the next tool call SHALL open a fresh connection and succeed without operator intervention

#### Scenario: Session bookkeeping cleared on transport loss
- **WHEN** `session_run` or `session_wait` raises a `host-unreachable` error
- **THEN** any local session state associated with that `session_id` SHALL be cleared (the remote tmux server is gone by definition)
- **AND** the next `session_run` against the same `session_id` SHALL create a fresh tmux session

#### Scenario: session_kill on an unreachable host
- **WHEN** `session_kill` is invoked but the host is unreachable
- **THEN** the local session bookkeeping SHALL be cleared regardless
- **AND** the response SHALL indicate the remote tmux session was not confirmed killed (`killed: false`)
- **AND** the call SHALL be audited

### Requirement: Idle session reaping
The MCP SHALL kill remote tmux sessions named `tai/*` or legacy `tai-mcp/*` that have had no activity for longer than `idle_timeout_hours`, whose pane is at a plain shell prompt (`bash`, `zsh`, `sh`, `dash`, or `fish`), and to which no client is attached. Reaping SHALL be best-effort, SHALL NOT block or fail the triggering tool call, and SHALL NOT touch tmux sessions with any other name.

#### Scenario: Stale idle session is reaped
- **WHEN** `tai/build` on `pi-living` has been idle for 50 hours at a `bash` prompt with no attached client
- **AND** reaping runs for `pi-living` with `idle_timeout_hours = 48`
- **THEN** the tmux session `tai/build` SHALL be killed

#### Scenario: Legacy-prefixed session is reaped
- **WHEN** `tai-mcp/default` has been idle for longer than the timeout at a shell prompt with no attached client
- **THEN** it SHALL be killed

#### Scenario: Quiet long-running job is kept
- **WHEN** `tai/sync` has been idle for longer than the timeout but its pane is running `rsync`
- **THEN** it SHALL NOT be killed

#### Scenario: Pending sudo handoff is kept
- **WHEN** `tai/default` has been idle for longer than the timeout and its pane is running `sudo` at a password prompt
- **THEN** it SHALL NOT be killed

#### Scenario: Attached session is kept
- **WHEN** a session meets the idle and prompt conditions but a client is attached to it
- **THEN** it SHALL NOT be killed

#### Scenario: Unrelated tmux sessions are never touched
- **WHEN** the remote user has an idle tmux session named `work`
- **THEN** reaping SHALL NOT kill it

#### Scenario: Reaping failure does not affect the tool call
- **WHEN** reaping fails on a host (for example, the tmux server is not running or the command errors)
- **THEN** the failure SHALL be audited
- **AND** the tool call that triggered reaping SHALL proceed normally

### Requirement: Idle timeout setting
The idle timeout SHALL be configured globally by `[sessions].idle_timeout_hours` in `hosts.toml`, with a default of 48 hours. A value of `0` SHALL disable reaping. There SHALL be no per-host override.

#### Scenario: Default timeout
- **WHEN** `hosts.toml` has no `[sessions]` table
- **THEN** reaping SHALL use an idle timeout of 48 hours

#### Scenario: Reaping disabled
- **WHEN** `hosts.toml` contains `[sessions]` with `idle_timeout_hours = 0`
- **THEN** no session SHALL be reaped

#### Scenario: Setting changed while the MCP runs
- **WHEN** the operator changes `idle_timeout_hours` in `hosts.toml` and the `hosts` tool reloads the file
- **THEN** subsequent reaping runs SHALL use the new value

### Requirement: Periodic per-host maintenance trigger
Reaping SHALL run in the background when a tool call obtains a connection to a host, at most once per hour per host, including on the first connection after MCP start.

#### Scenario: Throttled re-run on a long-lived server
- **WHEN** the MCP has been running for a day and the LLM calls `session_run` against `pi-living` every few minutes
- **THEN** reaping SHALL have run against `pi-living` about once per hour, not on every call and not only once at first connect

#### Scenario: Reaping is audited
- **WHEN** reaping runs against a host
- **THEN** a `_session_sweep` audit record SHALL be written for that host with the names of the killed sessions (possibly empty) and the timeout used
