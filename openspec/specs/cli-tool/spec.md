# cli-tool Specification

## Purpose
Provide the operator-facing surface for installing, configuring, and running the MCP server. A single `tai-mcp-ssh` binary exposes the subcommands needed for initial setup (`hosts add` / `hosts test`), day-to-day maintenance (`hosts list` / `hosts remove`), forensic review (`audit tail`), and starting the server itself (`serve`). The CLI is the only path that touches secrets directly — interactive prompts capture passwords and write them to the OS keychain so the rest of the system never sees plaintext.
## Requirements
### Requirement: Single binary `tai-mcp-ssh` with subcommands
The project SHALL install a single console entry point `tai-mcp-ssh` exposing the subcommands: `hosts add`, `hosts list`, `hosts remove`, `hosts test`, `audit tail`, and `serve`.

#### Scenario: `serve` starts the MCP over stdio
- **WHEN** the user runs `tai-mcp-ssh serve`
- **THEN** the process SHALL start the MCP server speaking the MCP protocol over stdin/stdout
- **AND** SHALL exit cleanly on EOF or SIGINT

### Requirement: `hosts add` is interactive and never accepts secrets on argv
The `hosts add <alias>` subcommand SHALL prompt the user interactively for connection details and for the password (when password auth is selected) via `getpass`. Passwords SHALL NOT be accepted as command-line arguments, environment variables (other than for testing), or read from stdin without a TTY.

#### Scenario: Interactive password capture
- **WHEN** the user runs `tai-mcp-ssh hosts add my-vps` and selects password auth
- **THEN** the CLI SHALL prompt with `getpass`, hiding input
- **AND** the entered password SHALL be stored via the `keyring` library at service `tai-mcp-ssh`, account `<alias>`
- **AND** the password SHALL NOT be echoed, logged, or written to `hosts.toml`

#### Scenario: Argv-supplied password rejected
- **WHEN** the user attempts to pass a password via a CLI flag
- **THEN** the CLI SHALL refuse and emit an error explaining that secrets must be supplied interactively

#### Scenario: Updates `hosts.toml`
- **WHEN** `hosts add` completes successfully
- **THEN** `~/.config/tai-mcp-ssh/hosts.toml` SHALL contain a new `[hosts.<alias>]` table with the captured fields and (for password auth) a `password_ref = "keychain://tai-mcp-ssh/<alias>"`

### Requirement: `hosts list` redacts secrets
The `hosts list` subcommand SHALL print the configured hosts in a human-readable form. No password, no keychain value, and no `password_ref` raw value SHALL be printed in clear; password-auth entries SHALL be indicated by a literal `(keychain)` marker or equivalent.

#### Scenario: Mixed auth listing
- **WHEN** the user runs `tai-mcp-ssh hosts list`
- **THEN** key-auth entries SHALL show `auth=key` and the resolved `identity_file` (path only)
- **AND** password-auth entries SHALL show `auth=password (keychain)` without revealing the secret

### Requirement: `hosts remove` deletes the keychain entry
The `hosts remove <alias>` subcommand SHALL remove the entry from `hosts.toml` AND delete the corresponding keychain entry (if any). The user SHALL be prompted to confirm.

#### Scenario: Removes keychain
- **WHEN** the user confirms removal of a password-auth host
- **THEN** the keychain entry for `tai-mcp-ssh/<alias>` SHALL be deleted via `keyring.delete_password`
- **AND** the TOML entry SHALL be removed
- **AND** no error SHALL be raised if the keychain entry was already absent

### Requirement: `hosts test` verifies end-to-end setup
The `hosts test <alias>` subcommand SHALL connect to the host, run `whoami`, verify `tmux` is on PATH, verify `~/.tai-ssh/logs/` can be created and written to, and report latency. The result SHALL summarize each check.

#### Scenario: All checks pass
- **WHEN** the user runs `tai-mcp-ssh hosts test pi-living` against a properly configured host
- **THEN** the CLI SHALL print a success line per check (connect, whoami, tmux, log dir) and an end-to-end latency

#### Scenario: tmux missing
- **WHEN** the remote does not have `tmux` installed
- **THEN** the relevant check SHALL fail with a non-zero exit and a recommended install command (e.g. `sudo apt install tmux`)

### Requirement: `audit tail` reads the JSONL audit log
The `audit tail` subcommand SHALL print the last N audit records (default 20), optionally filtered by `--host`, `--session`, or `--tool`. Output SHALL be one record per line in JSON form by default, with an optional human-readable mode (`--pretty`).

#### Scenario: Basic tail
- **WHEN** the user runs `tai-mcp-ssh audit tail -n 5`
- **THEN** the CLI SHALL print the last 5 lines of the audit JSONL file as-is

#### Scenario: Filtered tail
- **WHEN** the user runs `tai-mcp-ssh audit tail --host pi-living -n 50`
- **THEN** the CLI SHALL print up to 50 of the most recent records whose `host` field equals `pi-living`
