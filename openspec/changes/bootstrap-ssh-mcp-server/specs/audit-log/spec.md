## ADDED Requirements

### Requirement: JSONL audit log file location
The system SHALL append one JSON object per line to an audit file. On Linux the path SHALL be `~/.local/state/tai-mcp-ssh/audit.jsonl`. On macOS the path SHALL be `~/Library/Logs/tai-mcp-ssh/audit.jsonl`. The parent directory SHALL be created if missing.

#### Scenario: First write creates the directory
- **WHEN** the MCP records its first audit event and the state directory does not exist
- **THEN** the directory SHALL be created with mode `0700`
- **AND** the file SHALL be opened in append mode

### Requirement: Every tool invocation produces exactly one audit record
The MCP SHALL write one audit record per tool invocation, including invocations that fail before any remote action.

#### Scenario: Rejected call audited
- **WHEN** a tool call is rejected because the host is not in the allowlist
- **THEN** an audit record SHALL still be written with `status: "rejected"` and a descriptive error field
- **AND** the record SHALL NOT include any secret material

#### Scenario: Long-running call produces one record
- **WHEN** a `session_run` returns `still_running` and is subsequently resumed via `session_wait`
- **THEN** the initial `session_run` SHALL produce one record (with `status: "still_running"`)
- **AND** each `session_wait` SHALL produce its own record
- **AND** all records related to the same underlying command SHALL share the same `log_id`

### Requirement: Audit record schema
Each audit record SHALL include the fields: `ts` (ISO-8601 UTC), `tool`, `session` (when applicable), `host`, `user`, `cmd` (when applicable), `reason` (when supplied by the LLM), `exit` (when applicable), `status`, `duration_ms`, `stdout_bytes`, `stderr_bytes`, `log_id` (when applicable), `truncated`, `needs_password` (boolean, when applicable), and `sha256` (for `put`/`get`).

#### Scenario: Example `session_run` record
- **WHEN** a `session_run` for `apt update` completes with exit 0 and 24 KB of output
- **THEN** the corresponding audit line SHALL include `"tool":"session_run"`, `"session":"pi-living/default"`, `"cmd":"apt update"`, `"exit":0`, `"status":"done"`, `"stdout_bytes":24112` (or similar), `"truncated":true`, and a valid `"log_id"`

#### Scenario: Example `put` record
- **WHEN** a `put` transfers a 2 KB file to a remote path
- **THEN** the audit line SHALL include `"tool":"put"`, `"host"`, the local and remote paths, `"bytes":2048`, and a `"sha256"` of the bytes transferred

### Requirement: Audit records never contain secrets
The audit log SHALL never contain passwords, keychain values, or other authentication material in any field. Commands that interpolate secrets MUST be rejected by the system or have those secrets redacted before logging.

#### Scenario: Secret-bearing command rejected
- **WHEN** a `session_run` command appears to embed a literal password or keychain value
- **THEN** the implementation SHALL either reject the call or replace the secret with `"<redacted>"` in the audit `cmd` field
- **AND** the rejection SHALL itself be audited (without the secret)

### Requirement: Append-only durability
Audit writes SHALL be append-only and SHALL be flushed before the corresponding tool call returns. Concurrent writes from multiple tool invocations SHALL not interleave within a single line.

#### Scenario: Write-before-return ordering
- **WHEN** a tool finishes its work and prepares to return
- **THEN** the audit record SHALL be flushed (`fsync` not required, but the write SHALL be visible to a concurrent `tail -f`) before the response is sent back

#### Scenario: Concurrent writes
- **WHEN** two tool calls finish in close succession
- **THEN** their audit lines SHALL each be a complete JSON object on its own line
- **AND** neither line SHALL contain fragments of the other
