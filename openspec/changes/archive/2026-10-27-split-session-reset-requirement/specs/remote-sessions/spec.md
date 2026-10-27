# Spec Delta

## MODIFIED Requirements

### Requirement: `session_reset` recovers a stuck session without killing the pane
The MCP SHALL expose a `session_reset(session_id)` tool that returns a session with an
in-flight command to idle by clearing local session bookkeeping, **without** tearing
down the remote tmux pane. This provides a lighter-weight recovery than `session_kill`
for the case where a command sent successfully but its completion sentinel will never
arrive (for example `exec sh` mid-session, or a corrupted/truncated log).

#### Scenario: Reset a busy session returns it to idle
- **WHEN** a session has an in-flight command (it would return `busy` to `session_run`)
- **AND** the LLM calls `session_reset(session_id)`
- **THEN** the tool SHALL clear the in-flight command state and return `reset: true`
- **AND** the remote tmux pane SHALL NOT be killed
- **AND** a subsequent `session_run` against the same `session_id` SHALL be accepted
  (no longer `busy`) and reuse the existing pane
- **AND** the reset SHALL be audited

#### Scenario: Reset an idle or unknown session is a no-op
- **WHEN** `session_reset` is called on a session with no in-flight command, or on a
  `session_id` the MCP has no local state for
- **THEN** the tool SHALL return `reset: false`
- **AND** no tmux session SHALL be killed

#### Scenario: Reset does not require a reachable host
- **WHEN** `session_reset` is called while the host is unreachable
- **THEN** the local in-flight command state SHALL still be cleared and `reset: true`
  returned (when there was an in-flight command)
- **AND** the call SHALL NOT fail with a `host-unreachable` error

## ADDED Requirements

### Requirement: `session_reset` preserves the tmux pane
The tmux pane — its cwd, environment, and any backgrounded children — SHALL survive a
`session_reset`, so a subsequent `session_run` reuses the same live pane.

#### Scenario: Shell state survives a reset
- **WHEN** `session_run("pi-living/default", "cd /srv/app")` completed earlier
- **AND** a later command in that session is stuck and the LLM calls `session_reset("pi-living/default")`
- **THEN** a following `session_run("pi-living/default", "pwd")` SHALL print `/srv/app`
- **AND** no tmux session SHALL have been created or killed by the reset

### Requirement: `session_reset` works without a live connection
`session_reset` SHALL NOT require a live SSH connection to the host, since it mutates
only local state.

#### Scenario: Reset opens no connection
- **WHEN** a session has an in-flight command and `session_reset` is called for it
- **THEN** the reset SHALL complete without opening or using an SSH connection to the host

### Requirement: `session_reset` is audited
A `session_reset` that clears an in-flight command SHALL be audited as a
`session_reset` event recording the host, the `session_id`, and the `log_id` of the
abandoned command.

#### Scenario: Audit record for a reset
- **WHEN** `session_reset("pi-living/default")` clears an in-flight command with `log_id` `01JV3N…`
- **THEN** an audit record with `"tool":"session_reset"`, `"host":"pi-living"`,
  `"session":"pi-living/default"` and `"log_id":"01JV3N…"` SHALL be written
