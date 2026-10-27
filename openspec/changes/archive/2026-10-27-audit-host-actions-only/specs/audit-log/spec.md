## RENAMED Requirements

- FROM: `### Requirement: Every tool invocation produces exactly one audit record`
- TO: `### Requirement: Every host action produces exactly one audit record`

## MODIFIED Requirements

### Requirement: Every host action produces exactly one audit record
The MCP SHALL write exactly one audit record for every tool invocation that acts on a managed host: running or polling a command, killing a session, resetting an in-flight command, or transferring a file. Every invocation that is rejected or fails SHALL also produce exactly one record, including failures before any remote action. Invocations that only read local state or leave it unchanged need not produce a record.

#### Scenario: Rejected call audited
- **WHEN** a tool call is rejected because the host alias is not in the allowlist
- **THEN** an audit record SHALL still be written with `status: "rejected"` and a descriptive error field, under `audit/_system/`
- **AND** the record SHALL NOT include any secret material

#### Scenario: Long-running call produces one record per turn
- **WHEN** a `session_run` returns `still_running` and is subsequently resumed via `session_wait`
- **THEN** the initial `session_run` SHALL produce one record (with `status: "still_running"`)
- **AND** each `session_wait` SHALL produce its own record
- **AND** all records related to the same underlying command SHALL share the same `log_id`

#### Scenario: Local-only calls need no record
- **WHEN** the LLM calls `session_list()`, or calls `session_wait` or `session_reset` on a session that has no command in flight
- **THEN** the call MAY complete without writing an audit record
- **AND** if the call is rejected or fails, it SHALL still produce one record
