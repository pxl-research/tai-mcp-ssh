## ADDED Requirements

### Requirement: Host-inventory reloads are audited
Every reload of `hosts.toml` triggered by a `hosts` MCP tool invocation SHALL produce exactly one `_hosts_reload` audit record under `audit/_system/`. The record SHALL include integer counts `added`, `removed`, and `changed` describing the diff applied to the in-memory allowlist, even when all counts are zero. If the reload failed (malformed TOML, validation error, IO error), the record SHALL carry `status: "error"` with a descriptive error field; on success it SHALL carry `status: "ok"`.

#### Scenario: Successful reload audited with diff counts
- **WHEN** the LLM calls `hosts()` and the on-disk file differs from the in-memory allowlist by 1 added alias, 0 removed, and 2 changed
- **THEN** a `_hosts_reload` record SHALL be appended to `audit/_system/<today>.jsonl`
- **AND** the record SHALL include `"added": 1`, `"removed": 0`, `"changed": 2`, and `"status": "ok"`

#### Scenario: No-op reload still audited
- **WHEN** the LLM calls `hosts()` and `hosts.toml` is identical to the in-memory allowlist
- **THEN** a `_hosts_reload` record SHALL still be written with all counts equal to `0`
- **AND** the record SHALL carry `"status": "ok"`

#### Scenario: Failed reload audited
- **WHEN** the LLM calls `hosts()` and `hosts.toml` cannot be parsed or fails validation
- **THEN** a `_hosts_reload` record SHALL be appended with `"status": "error"` and a descriptive error field
- **AND** the in-memory allowlist SHALL remain unchanged (per the host-inventory spec)
