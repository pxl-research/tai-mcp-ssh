## 1. Project scaffolding

- [ ] 1.1 Create `pyproject.toml` (PEP 621, Python 3.11+, project name `tai-mcp-ssh`, author "PXL Smart ICT", `license = { file = "LICENSE" }`, build backend `hatchling`) with runtime deps: `mcp`, `asyncssh`, `keyring`, `click`, `tomli-w`, `python-ulid`
- [ ] 1.2 Add dev deps under `[dependency-groups].dev`: `pytest`, `pytest-asyncio`, `ruff`, `mypy`, `pre-commit`
- [ ] 1.3 Add console entry point `tai-mcp-ssh = tai_mcp_ssh.cli:main` in `pyproject.toml`
- [ ] 1.4 Create package layout `src/tai_mcp_ssh/{__init__.py, cli.py, server.py, config.py, audit.py, sessions.py, transfer.py, ssh.py, paths.py}` with stub implementations sufficient for `uv run tai-mcp-ssh --help` to succeed
- [ ] 1.5 Run `uv sync` to create `.venv` and generate `uv.lock`; commit `uv.lock` (treats deps as reproducible for teammates)
- [ ] 1.6 Verify `README.md` and `LICENSE` are in place (already created during the design phase); confirm `.gitignore` covers `.venv/` and `uv` caches
- [ ] 1.7 Add `[tool.ruff]` and `[tool.mypy]` sections to `pyproject.toml` (target Python 3.11, ruff `line-length = 100`, enable common rule sets `E`, `F`, `I`, `UP`, `B`, `SIM`; mypy `strict = true` on `src/`)
- [ ] 1.8 Activate pre-commit hooks locally: `uv run pre-commit install`; smoke-test with `uv run pre-commit run --all-files`
- [ ] 1.9 CI placeholder: GitHub Actions workflow that runs `uv sync --frozen` and then `uv run pre-commit run --all-files` (which exercises ruff, ruff-format, mypy, and hygiene hooks in one step) plus `uv run pytest`

## 2. Configuration and paths

- [ ] 2.1 Implement `paths.py`: resolve XDG config / state dirs with macOS-specific overrides (`~/Library/Logs/tai-mcp-ssh/` for audit)
- [ ] 2.2 Implement `config.py`: read `hosts.toml` via `tomllib`, validate schema (`host`, `user`, `port`, `auth`, `identity_file`, `password_ref`, `log_retention_days`), expose `load_hosts()` and `Host` dataclass
- [ ] 2.3 Reject TOML files containing any literal password-like field; raise a clear error
- [ ] 2.4 Implement TOML writes via `tomli-w` for use by the CLI `hosts add/remove`
- [ ] 2.5 Unit tests covering: missing file, empty file, key-auth entry, password-auth entry, plaintext-password rejection, malformed entries

## 3. Audit log

- [ ] 3.1 Implement `audit.py`: append-only JSONL writer with line-atomic writes (single `write` of the serialized line + `\n`)
- [ ] 3.2 Provide `record(tool, host, **fields)` helper that auto-fills `ts` (ISO-8601 UTC), resolves the target file as `audit/<host>/<UTC-date>.jsonl` (defaulting `host = "_system"` for non-host events), and merges caller fields
- [ ] 3.3 Lazily create per-host folders (`audit/<host>/`) with mode `0700` on first write to that host
- [ ] 3.4 Detect UTC date rollover on every write so a long-running process correctly switches to the next day's file without restart
- [ ] 3.5 Maintain a per-host open-file-handle cache keyed by `(host, date)` so we don't reopen on every record; close stale handles after rollover
- [ ] 3.6 Per-host `asyncio.Lock` for serialized writes within a host; writes to different hosts run in parallel without contention
- [ ] 3.7 Implement startup retention sweep: delete files in `audit/*/` whose filename-date is older than `retention_days` (default 90); record the sweep summary as a `_system` event; failures audited and non-fatal
- [ ] 3.8 Read `[audit].retention_days` (optional) from `hosts.toml` via `config.py`
- [ ] 3.9 Add a redaction helper that scrubs known-secret fields before write (`password`, `password_ref` resolved to the secret, etc.)
- [ ] 3.10 Unit tests: same-host writes serialize, cross-host writes parallelize, UTC midnight rollover switches files, retention sweep deletes only old files, host folder lazy creation, secret redaction, `_system` fallback for missing host

## 4. SSH connection layer

- [ ] 4.1 Implement `ssh.py`: per-host `asyncssh` connection pool keyed by alias; reuse a single open connection per host
- [ ] 4.2 Key auth: delegate to `~/.ssh/config` via `asyncssh`'s `config` argument
- [ ] 4.3 Password auth: resolve `keychain://` reference via `keyring`, pass to `asyncssh.connect(password=...)`, drop the local reference after handshake
- [ ] 4.4 Detect `tmux` presence on first connect (`command -v tmux`); cache the result per connection
- [ ] 4.5 Ensure `~/.tai-ssh/logs/` exists with mode `0700` on first connect; record the housekeeping action in the audit log
- [ ] 4.6 Implement log retention sweep (`find ~/.tai-ssh/logs -mtime +N -delete` equivalent via SFTP listing); best-effort, audited
- [ ] 4.7 Unit tests with `asyncssh` mocked: connect resolves config, password auth retrieves keychain, tmux-missing path

## 5. Remote sessions (tmux)

- [ ] 5.1 Implement `sessions.py`: per-host registry of active sessions keyed by `<host>/<name>`
- [ ] 5.2 Auto-create tmux session on first `session_run` (`tmux new-session -d -s tai-mcp/<name>`)
- [ ] 5.3 Send commands via `tmux send-keys -t tai-mcp/<name> '<cmd>; echo __TAI_DONE__$?__<run_id>__' Enter`
- [ ] 5.4 Use `tmux pipe-pane -O 'cat >> ~/.tai-ssh/logs/<log_id>.log'` per command so output is captured to file regardless of pane capture timing
- [ ] 5.5 Poll for completion: tail the log file (small `tail`s over SFTP or SSH exec) every ~200ms looking for sentinel
- [ ] 5.6 Implement interactive-prompt detection: regex set for sudo / `Password:` / `(yes/no)?` / apt-Y/n / `Are you sure?` matched at tail
- [ ] 5.7 Implement timeout fallback returning `still_running`
- [ ] 5.8 Implement per-session serialization: reject second `session_run` while session is non-idle, returning `status: "busy"`
- [ ] 5.9 Implement `session_wait`: resume polling without sending a new command; same status outcomes
- [ ] 5.10 Implement `session_kill`: `tmux kill-session -t tai-mcp/<name>`; clear registry entry
- [ ] 5.11 Implement `session_list`: enumerate registry across all hosts with metadata
- [ ] 5.12 Build the response shape (head/tail/bytes/truncated/log_id/log_path/attach_hint/prompt) per spec
- [ ] 5.13 Unit tests with a fake remote: sentinel detection, prompt detection, busy rejection, kill, list

## 6. File transfer

- [ ] 6.1 Implement `transfer.py`: `put` and `get` over `asyncssh` SFTP using the existing pooled connection
- [ ] 6.2 Compute SHA-256 streamed during transfer; record `bytes` and `sha256`
- [ ] 6.3 Allowlist check shared with sessions (refuse unlisted host)
- [ ] 6.4 Default `local_path` for `get` derives `~/.local/state/tai-mcp-ssh/downloads/<host>/<basename>`
- [ ] 6.5 Surface SFTP permission errors with a guidance message pointing to the stage-and-move pattern
- [ ] 6.6 Unit tests: put/get round-trip, sha256 correctness, permission-denied surface, default local path

## 7. MCP server wiring

- [ ] 7.1 Implement `server.py` using the `mcp` SDK: register the seven tools (`hosts`, `session_list`, `session_run`, `session_wait`, `session_kill`, `put`, `get`)
- [ ] 7.2 Define tool input schemas with minimal fields and concise descriptions (token-budget conscious)
- [ ] 7.3 Wire every tool through the audit log: even rejected calls produce a record
- [ ] 7.4 Map internal exceptions to MCP error responses without leaking secrets
- [ ] 7.5 Implement stdio transport entry point invoked from `cli.py serve`

## 8. CLI

- [ ] 8.1 Implement `cli.py` with `click`, top-level group `tai-mcp-ssh`
- [ ] 8.2 `hosts add <alias>`: interactive prompts (host, user, port, auth selector); `getpass` for password; refuse any password supplied via argv or non-TTY stdin; write TOML; store keychain
- [ ] 8.3 `hosts list`: read TOML, print in tabular form, redact secrets, mark password-auth as `(keychain)`
- [ ] 8.4 `hosts remove <alias>`: confirm, delete TOML entry, delete keychain entry (ignore if absent)
- [ ] 8.5 `hosts test <alias>`: connect, run `whoami`, check tmux, check log dir writability, print latency and per-check status
- [ ] 8.6 `audit tail [-n N] [--host X] [--session Y] [--tool Z] [--pretty]`: stream the JSONL file with optional filters and pretty-printing
- [ ] 8.7 `serve`: launch `server.py` over stdio
- [ ] 8.8 CLI integration tests using `click.testing.CliRunner`

## 9. Documentation

- [ ] 9.1 README: install instructions, prerequisite list (tmux on remote, Python 3.11+, ssh setup)
- [ ] 9.2 README: step-by-step host onboarding via `hosts add` and `hosts test`
- [ ] 9.3 README: the sudo-handoff flow, with the exact `ssh ... -t tmux attach -t tai-mcp/<name>` command and the Ctrl-B D detach hint
- [ ] 9.4 README: recommended `NOPASSWD` sudoers snippets for common LLM admin tasks
- [ ] 9.5 README: stage-and-move pattern for root-owned file installs
- [ ] 9.6 README: document the local-MCP topology assumption and that remote-MCP is out of scope for v1
- [ ] 9.7 README: how to inspect and rotate the audit log

## 10. End-to-end smoke

- [ ] 10.1 Manual test against a real Raspberry Pi or VM: full flow from `hosts add` to `session_run` to a sudo handoff and back
- [ ] 10.2 Manual test of `put` + `sudo mv` stage-and-move
- [ ] 10.3 Manual test of long-running command + `session_wait` resume
- [ ] 10.4 Verify audit log contents reflect each step accurately, no secrets present
