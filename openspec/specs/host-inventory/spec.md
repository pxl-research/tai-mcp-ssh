# host-inventory Specification

## Purpose
Define which hosts the LLM is allowed to reach and how to authenticate to each. A single TOML file (`~/.config/tai-mcp-ssh/hosts.toml`) names each reachable host by alias, mirrors what `~/.ssh/config` already knows where possible, and bars plaintext secrets — passwords must live in the OS keychain referenced by `keychain://tai-mcp-ssh/<alias>` URIs. The allowlist is the system's only gate on which machines the LLM can touch; every other capability (sessions, transfer) defers to it.
## Requirements
### Requirement: Allowlist file location and format
The system SHALL read the set of LLM-reachable hosts from a TOML file at `~/.config/tai-mcp-ssh/hosts.toml`. Only hosts present in this file SHALL be addressable by MCP tools.

#### Scenario: Tool call to an unlisted host is rejected
- **WHEN** an MCP tool is invoked with a host or session_id whose host portion is not present in `hosts.toml`
- **THEN** the call SHALL fail with an error indicating the host is not in the allowlist
- **AND** the rejection SHALL be recorded in the audit log with `exit: -1` and a descriptive error field
- **AND** no SSH connection attempt SHALL be made

#### Scenario: Empty or missing config file
- **WHEN** `hosts.toml` does not exist or contains no `[hosts.*]` tables
- **THEN** `hosts()` SHALL return an empty list
- **AND** every session/transfer tool SHALL reject every call

### Requirement: Host entry schema
Each entry under `[hosts.<alias>]` SHALL support the fields: `host` (optional, defaults to looking up the alias in `~/.ssh/config`), `user` (optional, same fallback), `port` (optional, default 22), `auth` (`"key"` or `"password"`, default `"key"`), `identity_file` (optional, key auth only), `password_ref` (required for password auth, format `keychain://tai-mcp-ssh/<alias>`), and `log_retention_days` (optional, default 7).

#### Scenario: Key-auth entry referring to ssh_config
- **WHEN** an entry contains only the alias and no inline `host`/`user`
- **THEN** the MCP SHALL resolve connection parameters from `~/.ssh/config` for that alias via `asyncssh`'s built-in config parsing

#### Scenario: Password-auth entry references the OS keychain
- **WHEN** an entry has `auth = "password"` and `password_ref = "keychain://tai-mcp-ssh/<alias>"`
- **THEN** the MCP SHALL retrieve the password via the `keyring` library at connect time
- **AND** the password SHALL NOT be retained in memory beyond the duration of the authentication handshake
- **AND** the password SHALL NOT appear in any log, error, or tool response

#### Scenario: Password-auth entry with missing keychain secret
- **WHEN** `auth = "password"` is set but the referenced keychain entry does not exist
- **THEN** connection SHALL fail with a clear error pointing to `tai-mcp-ssh hosts add <alias>`
- **AND** the failure SHALL be recorded in the audit log

### Requirement: Plaintext secrets prohibited
The system SHALL NOT accept passwords or other secrets as values in `hosts.toml`. The only secret-related string permitted is a `keychain://` reference.

#### Scenario: TOML contains a literal password field
- **WHEN** an entry contains `password = "..."` (or any non-reference secret field)
- **THEN** the MCP SHALL refuse to start (in `serve` mode) or emit a hard error (in CLI commands)
- **AND** the error message SHALL instruct the user to move the secret to the keychain

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
