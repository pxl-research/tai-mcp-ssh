# file-transfer Specification

## Purpose
Let the LLM move files between the operator's machine and managed hosts via SFTP-backed `put` / `get` tools that ride the existing SSH connection — no separate authentication, no separate allowlist. Privilege escalation is deliberately *not* part of the transfer path: a `put` to a root-owned destination fails cleanly with guidance to use the documented stage-and-move pattern (`put` to `/tmp`, then `session_run` with `sudo mv`), so any privileged write still goes through the audited session surface.
## Requirements
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

### Requirement: `get` confines its local destination to the downloads directory
`get` SHALL write the downloaded file only under the downloads directory
(`paths.downloads_dir()`, i.e. `~/.local/state/tai-mcp-ssh/downloads/` or its macOS
equivalent) unless the caller explicitly opts out. The destination SHALL be resolved
(symlinks and `..` segments collapsed) and checked for containment **before** any
SFTP connection is opened or any bytes are written. A resolved destination outside
the downloads tree SHALL be rejected with a `TransferDenied` error unless the call
sets `allow_outside = true`. The default destination
(`downloads_dir(host)/<basename>` when `local_path` is omitted) and any `local_path`
that resolves inside the downloads tree SHALL continue to work unchanged.

#### Scenario: Default destination is allowed
- **WHEN** the LLM calls `get("pi-living", "/var/log/syslog")` with no `local_path`
- **THEN** the file SHALL download to `downloads_dir("pi-living")/syslog`
- **AND** no containment rejection SHALL occur

#### Scenario: Explicit in-tree path is allowed
- **WHEN** `local_path` is supplied and resolves to a location inside the downloads tree
- **THEN** the download SHALL proceed and the response SHALL report that `local_path`

#### Scenario: Out-of-tree path rejected by default
- **WHEN** `get` is called with a `local_path` that resolves outside the downloads tree (for example `~/.ssh/authorized_keys`) and `allow_outside` is not set
- **THEN** the call SHALL fail with a `TransferDenied` error naming the downloads root and the `allow_outside` opt-in
- **AND** no SFTP connection SHALL be opened and no local file SHALL be created or truncated
- **AND** the rejection SHALL be audited with `status: "rejected"`

#### Scenario: Out-of-tree path allowed with explicit opt-in
- **WHEN** `get` is called with an out-of-tree `local_path` and `allow_outside = true`
- **THEN** the download SHALL proceed to that path
- **AND** the audit record SHALL reflect that the write was outside the downloads tree

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
