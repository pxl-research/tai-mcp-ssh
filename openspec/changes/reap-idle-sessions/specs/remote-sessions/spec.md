# Spec Delta

## MODIFIED Requirements

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

## ADDED Requirements

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
