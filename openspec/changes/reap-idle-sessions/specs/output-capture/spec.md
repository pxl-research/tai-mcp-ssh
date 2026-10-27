# Spec Delta

## MODIFIED Requirements

### Requirement: Log retention sweep
The MCP SHALL delete log files older than the configured retention period (default 7 days, per-host overridable via `log_retention_days` in `hosts.toml`) from `~/.tai-ssh/logs/` when a tool call obtains a connection to a host, at most once per hour per host, including the initial connect. The sweep SHALL be best-effort and SHALL NOT block command execution.

#### Scenario: Old logs removed on connect
- **WHEN** the MCP connects to a host with files older than the retention period in `~/.tai-ssh/logs/`
- **THEN** those files SHALL be deleted
- **AND** the count of deleted files SHALL be recorded in the audit log as a `housekeeping` event

#### Scenario: Old logs removed on a long-lived server
- **WHEN** the MCP has stayed connected to a host for longer than the retention period
- **AND** a tool call uses that host more than one hour after the previous sweep
- **THEN** the sweep SHALL run again and delete log files that have since aged past the retention period

#### Scenario: Sweep failure does not abort command
- **WHEN** the retention sweep fails (e.g., permission denied)
- **THEN** the failure SHALL be logged to the audit log
- **AND** the connection and subsequent command SHALL proceed normally
