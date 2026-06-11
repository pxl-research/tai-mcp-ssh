## ADDED Requirements

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
