# audit-log Specification

## Purpose
TBD - created by promoting change `bootstrap-ssh-mcp-server`. Update Purpose after archive.
## Requirements
### Requirement: Audit log directory layout
The system SHALL store audit records under a per-host directory structure rooted at the state directory. On Linux the root SHALL be `$XDG_STATE_HOME/tai-mcp-ssh/audit/` when `XDG_STATE_HOME` is set, otherwise `~/.local/state/tai-mcp-ssh/audit/`. On macOS the root SHALL be `~/Library/Logs/tai-mcp-ssh/audit/`. Each managed host SHALL have its own subdirectory named after the allowlist alias (for example `audit/pi-living/`). Non-host events SHALL be written to a reserved `audit/_system/` subdirectory.

#### Scenario: First write to a new host creates its folder
- **WHEN** the MCP records its first audit event for `pi-living`
- **THEN** `audit/pi-living/` SHALL be created with mode `0700` if missing
- **AND** the audit file inside SHALL be opened in append mode

#### Scenario: System-level events use _system
- **WHEN** the MCP records an event that does not pertain to a specific managed host (for example MCP startup, config-load error, retention-sweep summary, or an allowlist rejection where the requested host alias is not in `hosts.toml`)
- **THEN** the record SHALL be written under `audit/_system/`
- **AND** the record SHALL carry `host = "_system"` so the schema remains uniform

### Requirement: One file per UTC day per host
Audit records for a given host SHALL be appended to a file named `YYYY-MM-DD.jsonl` (UTC date) inside that host's audit folder. On the first record of a new UTC day, the writer SHALL switch to a new file without restarting the process.

#### Scenario: Day rollover during operation
- **WHEN** the wall clock crosses midnight UTC during MCP operation
- **THEN** the next audit record for a given host SHALL be written to `audit/<host>/<new-date>.jsonl`
- **AND** the previous day's file SHALL remain unchanged
- **AND** no records SHALL be duplicated across the boundary

#### Scenario: First record after restart on the same day
- **WHEN** the MCP process restarts and produces its first audit record for `pi-living` on the same calendar day as a prior run
- **THEN** the record SHALL be appended to the existing `audit/pi-living/YYYY-MM-DD.jsonl` file rather than overwriting it

### Requirement: Retention sweep
The MCP SHALL delete audit files older than the configured retention period from all host subdirectories on startup. The default retention period SHALL be 90 days. The value SHALL be configurable via an `[audit].retention_days` setting in `hosts.toml`.

#### Scenario: Old files deleted on startup
- **WHEN** the MCP starts and finds files in `audit/<host>/` whose filename date is older than `today - retention_days`
- **THEN** those files SHALL be deleted
- **AND** the count of deleted files per host SHALL be recorded as a `_system` housekeeping event

#### Scenario: Custom retention value
- **WHEN** `hosts.toml` contains `[audit]` with `retention_days = 30`
- **THEN** the sweep SHALL use 30 days instead of the default 90

#### Scenario: Sweep failure does not abort startup
- **WHEN** the retention sweep fails for any reason (for example permission denied on a file)
- **THEN** the failure SHALL be recorded as a `_system` event with the error message
- **AND** the MCP SHALL continue serving

### Requirement: Every tool invocation produces exactly one audit record
The MCP SHALL write one audit record per tool invocation, including invocations that fail before any remote action.

#### Scenario: Rejected call audited
- **WHEN** a tool call is rejected because the host alias is not in the allowlist
- **THEN** an audit record SHALL still be written with `status: "rejected"` and a descriptive error field, under `audit/_system/`
- **AND** the record SHALL NOT include any secret material

#### Scenario: Long-running call produces one record per turn
- **WHEN** a `session_run` returns `still_running` and is subsequently resumed via `session_wait`
- **THEN** the initial `session_run` SHALL produce one record (with `status: "still_running"`)
- **AND** each `session_wait` SHALL produce its own record
- **AND** all records related to the same underlying command SHALL share the same `log_id`

### Requirement: Audit record schema
Each audit record SHALL include the fields: `ts` (ISO-8601 UTC), `tool`, `host` (always present; `"_system"` for non-host events), `session` (when applicable), `user` (when applicable), `cmd` (when applicable), `reason` (when supplied by the LLM), `exit` (when applicable), `status`, `duration_ms`, `stdout_bytes`, `stderr_bytes`, `log_id` (when applicable), `truncated`, `needs_password` (boolean, when applicable), and `sha256` (for `put`/`get`).

#### Scenario: Example `session_run` record
- **WHEN** a `session_run` for `apt update` against `pi-living` completes with exit 0 and 24 KB of output on 2026-05-12 UTC
- **THEN** the corresponding audit line SHALL include `"tool":"session_run"`, `"session":"pi-living/default"`, `"host":"pi-living"`, `"cmd":"apt update"`, `"exit":0`, `"status":"done"`, `"stdout_bytes":24112` (or similar), `"truncated":true`, and a valid `"log_id"`
- **AND** the line SHALL be appended to `audit/pi-living/2026-05-12.jsonl`

#### Scenario: Example `put` record
- **WHEN** a `put` transfers a 2 KB file to a remote path
- **THEN** the audit line SHALL include `"tool":"put"`, `"host"`, the local and remote paths, `"bytes":2048`, and a `"sha256"` of the bytes transferred

### Requirement: Audit records never contain secrets
The audit log SHALL never contain passwords, keychain values, or other authentication material in any field. Commands that interpolate secrets MUST be rejected by the system or have those secrets redacted before logging.

#### Scenario: Secret-bearing command redacted or rejected
- **WHEN** a `session_run` command appears to embed a literal password or keychain value
- **THEN** the implementation SHALL either reject the call or replace the secret with `"<redacted>"` in the audit `cmd` field
- **AND** the rejection SHALL itself be audited (without the secret)

### Requirement: Append-only durability with per-host concurrency
Audit writes SHALL be append-only and SHALL be flushed before the corresponding tool call returns. Concurrent writes targeting the same host file SHALL not interleave within a single line. Concurrent writes targeting different host files SHALL run in parallel without contention.

#### Scenario: Write-before-return ordering
- **WHEN** a tool finishes its work and prepares to return
- **THEN** the audit record SHALL be flushed (`fsync` not required, but the write SHALL be visible to a concurrent `tail -f`) before the response is sent back

#### Scenario: Concurrent writes to the same host file
- **WHEN** two tool calls targeting the same host finish in close succession
- **THEN** their audit lines SHALL each be a complete JSON object on its own line
- **AND** neither line SHALL contain fragments of the other

#### Scenario: Concurrent writes to different host files
- **WHEN** two tool calls targeting different hosts finish concurrently
- **THEN** each line SHALL be written to its own host's daily file without cross-host locking
