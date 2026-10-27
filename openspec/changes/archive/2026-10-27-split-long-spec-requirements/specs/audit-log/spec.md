## MODIFIED Requirements

### Requirement: Audit log directory layout
The system SHALL store audit records in one subdirectory per managed host under the audit root (see "Audit log root location"). Each subdirectory SHALL be named after the allowlist alias (for example `audit/pi-living/`). Non-host events SHALL be written to a reserved `audit/_system/` subdirectory.

#### Scenario: First write to a new host creates its folder
- **WHEN** the MCP records its first audit event for `pi-living`
- **THEN** `audit/pi-living/` SHALL be created with mode `0700` if missing
- **AND** the audit file inside SHALL be opened in append mode

#### Scenario: System-level events use _system
- **WHEN** the MCP records an event that does not pertain to a specific managed host (for example MCP startup, config-load error, retention-sweep summary, or an allowlist rejection where the requested host alias is not in `hosts.toml`)
- **THEN** the record SHALL be written under `audit/_system/`
- **AND** the record SHALL carry `host = "_system"` so the schema remains uniform

## ADDED Requirements

### Requirement: Audit log root location
The audit root SHALL be a platform-specific state directory. On Linux it SHALL be `$XDG_STATE_HOME/tai-mcp-ssh/audit/` when `XDG_STATE_HOME` is set, otherwise `~/.local/state/tai-mcp-ssh/audit/`. On macOS it SHALL be `~/Library/Logs/tai-mcp-ssh/audit/`.

#### Scenario: Linux honours XDG_STATE_HOME
- **WHEN** the MCP runs on Linux with `XDG_STATE_HOME=/data/state`
- **THEN** audit records SHALL be written under `/data/state/tai-mcp-ssh/audit/`

#### Scenario: macOS uses Library/Logs
- **WHEN** the MCP runs on macOS
- **THEN** audit records SHALL be written under `~/Library/Logs/tai-mcp-ssh/audit/`
- **AND** `XDG_STATE_HOME` SHALL NOT affect the audit root
