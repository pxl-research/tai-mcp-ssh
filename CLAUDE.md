# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

An MCP stdio server that lets an LLM administer a small allowlisted fleet of Linux hosts over SSH. Every command runs inside a persistent remote `tmux` session, and output goes to a log file on the remote host. Tool responses return only trimmed head and tail slices so the LLM's token budget isn't burned. Sudo and other interactive prompts are handed off to the human operator through `tmux attach`. Passwords are never stored on disk or in config.

## Commands

All tooling runs through `uv` (Python 3.11+). Run `uv sync` once to create `.venv/` from `uv.lock`.

```sh
uv run pytest                                   # full suite; enforces --cov-fail-under=85
uv run pytest tests/unit/test_sessions.py::test_name --no-cov   # single test, skip coverage gate
uv run ruff check . && uv run ruff format .     # lint + format (line length 100)
uv run mypy src                                 # strict mode
uv run pre-commit run --all-files               # commit hooks: ruff, ruff-format, mypy, hygiene
uv run tai-mcp-ssh serve                        # start MCP server on stdio
uv run tai-mcp-ssh hosts add|list|remove|test <alias>
uv run tai-mcp-ssh audit tail
```

There is no hosted CI. `uv run pre-commit install` installs both the commit hooks and a pre-push hook that runs the full `pytest` suite.

When you run a subset of tests, pass `--no-cov`. Otherwise the 85% coverage gate fails the run.

## Spec-driven workflow (OpenSpec)

The behavioural contract lives in `openspec/specs/<capability>/spec.md`. The capabilities are `remote-sessions`, `output-capture`, `file-transfer`, `host-inventory`, `audit-log`, and `cli-tool`. A new feature normally follows this sequence:

1. Propose it as a change under `openspec/changes/<name>/` (proposal, design, spec deltas, tasks).
2. Implement it.
3. Archive it, which promotes the deltas into the canonical specs.

The `/opsx:*` commands and `openspec-*` skills in `.claude/` drive this workflow. Check the relevant spec before you change behaviour, and update the spec when behaviour changes.

Commit messages use an uppercase type prefix: `FEAT:`, `FIX:`, `DOCS:`, `SPEC:`, `PROPOSE:`, `TEST:`, `PERF:`. Reference the issue or PR number where relevant.

## Architecture

Layering, from outermost to innermost:

`server.py` → `SessionManager` (`sessions.py`) / `TransferManager` (`transfer.py`) → `ConnectionPool` / `Connection` (`ssh.py`) → asyncssh

`AuditLog` (`audit.py`) is shared by every layer.

- **`server.py`**: thin MCP wiring around a `Services` bundle that holds config, audit, pool, and managers (all injectable for tests). `tool_specs()` defines the 8 tools. Keep tool descriptions terse because they are sent on every LLM turn. `dispatch()` routes calls to the managers. The `hosts` tool re-reads `hosts.toml` on each call (`reload_hosts_from_disk`). If the file is malformed, it keeps the old in-memory allowlist and evicts pooled connections for hosts that were removed or changed.
- **Audit invariant: every host action, and every rejected or failed call, produces exactly one audit record.** Calls that only read or leave local state unchanged (`session_list`, and `session_wait`/`session_reset` on an idle session) write none. Managers record ordinary outcomes themselves. `_dispatch_and_audit` records early rejections (`HostNotAllowed`, `TmuxMissing`, `KeychainUnavailable`) as `rejected` and any other exception as `error`. If a manager has already written a richer record before raising, it sets `exc.audited = True` to suppress the duplicate. Internal events use `_`-prefixed tool names (`_session_create`, `_tmux_check`, `_hosts_reload`, `_remote_sweep`). Audit files are written to `<audit_dir>/<host>/<UTC-date>.jsonl`, and secret-looking keys are scrubbed.
- **`sessions.py`**:
  - **Session IDs and tmux targets.** Session IDs have the form `<host>/<name>` and map to the remote tmux session `tai/<name>`. `name` is restricted to `[A-Za-z0-9_-]+` because it is interpolated into shell commands. Always target tmux with the exact-match helpers (`=tai/<name>`, `=tai/<name>:` for panes). A bare `-t tai/foo` falls back to prefix matching and can hit `tai/foobar`.
  - **Command wrapping.** Each run wraps the command as `echo __TAI_START__<ulid>__; eval '<cmd>'; printf '\n__TAI_DONE__%d__<ulid>__\n' "$?"`. Before every command, the first round trip ensures the tmux session exists (`has-session || new-session`, which echoes `__TAI_CREATED__` so `_session_create` is audited). A session killed outside `session_kill` therefore heals itself. That round trip then points `tmux pipe-pane` at `~/.tai-ssh/logs/<ulid>.log` and sends the wrapped command with `send-keys -l`. `eval` keeps cd/export/source effects in the persistent shell. The leading `\n` keeps the DONE marker at column 0.
  - **Polling.** `_poll` reads the log incrementally (`tail -c +offset` plus a stateful UTF-8 decoder), and each poll scans only the newly read text. Its checks run in this order:
    1. The DONE sentinel, which is the only trusted completion signal.
    2. Prompt patterns anchored at the end of the buffer, which return `needs_password` or `needs_input` with an `attach_hint`.
    3. The timeout, which returns `still_running`.
  - **Session state.** A session with an active `log_id` is busy, and `run` returns `busy` instead of queueing. `reset` clears this local state without touching tmux. `kill` tears down the tmux session. `HostUnreachable` drops local session state (`_forget`).
  - **Locks.** Each session has its own `asyncio.Lock`, and locks are never removed. Read session state *under* the lock to avoid TOCTOU races.
- **`ssh.py`**:
  - **Connection lifecycle.** The pool opens one cached connection per host alias, lazily and under a per-alias lock. It evicts a connection that `Connection.run` marked dead after a transport error (`DEAD_CONN_ERRORS` → `HostUnreachable`), so the next call reconnects.
  - **First-connect bootstrap.** On first connect the pool checks that tmux exists, resolves `$HOME`, and runs `mkdir ~/.tai-ssh/logs`.
  - **Background maintenance.** `get()` schedules a fire-and-forget `_maintain` task at most once per hour per host (throttled with a monotonic clock that survives eviction). The task sweeps old remote logs and reaps idle `tai/*`/`tai-mcp/*` sessions (`select_idle_sessions` + a `pgrep` guard against background children). Each step audits its own failures and never raises.
  - **Connect targets and passwords.** The host alias is passed as the asyncssh connect target so `~/.ssh/config` applies. Password auth resolves `keychain://<service>/<account>` through `keyring` at connect time only.
- **`config.py`**: loads `hosts.toml` (`~/.config/tai-mcp-ssh/`, XDG-aware). Keys like `password`, `token`, or `secret` at any nesting depth are rejected. A missing file means an empty allowlist.
- **`transfer.py`**: SFTP put/get runs as the SSH user and streams a sha256. `get` is confined to the downloads directory unless `allow_outside` is set. A permission failure on `put` raises `TransferDenied` with a stage-and-move hint (`put` to /tmp, then `sudo mv`).
- **`paths.py`**: the single place for host-side path logic. Audit logs go to `~/Library/Logs` on macOS and the XDG state dir on Linux.
- **`errors.py`**: domain exceptions, all subclasses of `TaiMcpSshError`, mapped to MCP errors only at the server boundary.

## Testing conventions

Tests are unit-only under `tests/unit/`, and pytest-asyncio runs in `asyncio_mode = "auto"`. There is no `conftest.py`. Each test module defines its own fakes, such as `FakeConnection`/`FakePool` in `test_sessions.py` (a scripted remote shell with in-memory log files), `FakeConnectFactory` in `test_ssh.py`, and the fake SFTP client in `test_transfer.py`. Managers accept these through constructor injection: `connect=`, `now=`, `poll_interval=`, and the `Services(...)` kwargs. Isolate filesystem state with `tmp_path` and XDG env-var monkeypatching. Tests never open real SSH connections.
