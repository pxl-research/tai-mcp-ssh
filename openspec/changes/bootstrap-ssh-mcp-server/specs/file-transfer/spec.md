## ADDED Requirements

### Requirement: `put` and `get` use SFTP over the existing SSH connection
The MCP SHALL expose `put(host, local_path, remote_path, reason?)` and `get(host, remote_path, local_path?, reason?)` tools that transfer files via SFTP over the same SSH connection used for sessions. No separate authentication SHALL be required.

#### Scenario: put uploads a file
- **WHEN** the LLM calls `put("pi-living", "/Users/me/script.sh", "/home/pi/script.sh")`
- **THEN** the MCP SHALL transfer the file via SFTP using the connection authenticated for `pi-living`
- **AND** the response SHALL include `bytes` and `sha256` of the transferred content

#### Scenario: get downloads a file
- **WHEN** the LLM calls `get("pi-living", "/var/log/syslog")` with no `local_path`
- **THEN** the MCP SHALL download to a sensible local path (default: `~/.local/state/tai-mcp-ssh/downloads/<host>/<basename>`)
- **AND** the response SHALL include `bytes`, `sha256`, and the actual `local_path`

### Requirement: No sudo path inside `put`/`get`
The `put` and `get` tools SHALL execute as the SSH user only. The system SHALL NOT provide any privilege-escalation mechanism inside file transfer.

#### Scenario: put to a root-owned path fails cleanly
- **WHEN** `put` targets a path the SSH user cannot write (e.g., `/etc/nginx/nginx.conf`)
- **THEN** the tool SHALL return a permission error with guidance to use stage-and-move via `session_run` + `sudo mv`
- **AND** the failure SHALL be audited

#### Scenario: Stage-and-move documented pattern
- **WHEN** the LLM needs to install a config file to `/etc/nginx/nginx.conf`
- **THEN** the recommended pattern SHALL be: `put` to `/tmp/nginx.conf.new`, then `session_run` with `sudo mv /tmp/nginx.conf.new /etc/nginx/nginx.conf`
- **AND** both operations SHALL be independently audited

### Requirement: Allowlist applies to file transfer
`put` and `get` SHALL reject any host not present in `hosts.toml`, identically to session tools.

#### Scenario: put to unlisted host rejected
- **WHEN** `put` is called with a host not in the allowlist
- **THEN** the call SHALL fail with the same allowlist error used by `session_run`
- **AND** no SFTP connection SHALL be attempted

### Requirement: Transfer integrity recorded
For each successful `put` or `get`, the audit record SHALL include the byte count and SHA-256 hash of the transferred bytes.

#### Scenario: sha256 in audit
- **WHEN** a `put` completes successfully
- **THEN** the audit line for that call SHALL include `"sha256"` with the hex digest of the bytes written to the remote
- **AND** the same digest SHALL be returned in the tool response
