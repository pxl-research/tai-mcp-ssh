## MODIFIED Requirements

### Requirement: `hosts()` MCP tool surface
The MCP server SHALL expose a `hosts()` tool that returns the configured allowlist with secrets redacted. The tool SHALL reload `hosts.toml` from disk on every invocation so that aliases added, removed, or modified in the file while the MCP is running are visible to the LLM without restarting the server. The reload SHALL atomically swap the in-memory allowlist and evict cached SSH connections for aliases that were removed or whose connection parameters changed; cached connections for unchanged aliases SHALL be preserved.

#### Scenario: hosts() lists entries
- **WHEN** the LLM calls `hosts()`
- **THEN** the tool SHALL return an array of objects with fields `alias`, `host`, `user`, `port`, `auth`
- **AND** no field whose name or value contains a password or keychain reference SHALL be included

#### Scenario: Newly added host visible without restart
- **WHEN** the operator runs `tai-mcp-ssh hosts add new-vps` while the MCP is serving
- **AND** the LLM subsequently calls `hosts()`
- **THEN** the response SHALL include `new-vps` in the returned list
- **AND** a subsequent `session_run("new-vps/default", ...)` SHALL NOT raise `HostNotAllowed`

#### Scenario: Removed host evicted from connection pool
- **WHEN** an alias that previously had an open cached SSH connection is removed from `hosts.toml`
- **AND** the LLM subsequently calls `hosts()`
- **THEN** the response SHALL NOT include the removed alias
- **AND** the cached connection for that alias SHALL be closed and dropped from the pool
- **AND** a subsequent `session_run("<removed>/default", ...)` SHALL raise `HostNotAllowed`

#### Scenario: Changed connection params take effect
- **WHEN** an alias's `host`, `user`, `port`, `auth`, `identity_file`, or `password_ref` is edited in `hosts.toml`
- **AND** the LLM subsequently calls `hosts()`
- **THEN** the cached connection for that alias SHALL be closed and dropped
- **AND** the next tool call against that alias SHALL open a fresh connection using the new parameters

#### Scenario: Malformed hosts.toml does not clear the running allowlist
- **WHEN** `hosts.toml` is edited to introduce a parse error, a forbidden key, or other validation failure
- **AND** the LLM subsequently calls `hosts()`
- **THEN** the running allowlist SHALL remain unchanged (no aliases are silently dropped)
- **AND** the reload failure SHALL be recorded in the audit log
- **AND** the `hosts()` response SHALL reflect the previously valid allowlist

#### Scenario: In-flight calls not disturbed by a concurrent reload
- **WHEN** a long-running `session_run` against alias `pi` is in flight
- **AND** `pi` is unchanged in `hosts.toml`
- **AND** the LLM calls `hosts()` while the `session_run` is still polling
- **THEN** the in-flight `session_run` SHALL continue using its cached connection
- **AND** the reload SHALL NOT close or evict that connection
