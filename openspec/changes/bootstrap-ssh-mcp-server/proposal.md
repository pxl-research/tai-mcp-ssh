## Why

We want LLMs to safely admin small fleets of Linux servers (Raspberry Pis, Ubuntu VPSes) without handing over credentials or letting verbose command output blow the token budget. Existing MCP-for-SSH offerings either expose too much (full shell, no audit), too little (one-shot exec without state), or burn tokens streaming raw output. None of them solve the sudo-password problem cleanly.

This change scaffolds a small, opinionated MCP server purpose-built for this workflow: persistent shell sessions per host, MCP-managed output capture, structured audit trail, and a tmux-attach handoff for privileged operations the LLM cannot perform itself.

## What Changes

- New Python MCP server (`tai-mcp-ssh`) exposing 7 tools to LLM clients: `hosts`, `session_list`, `session_run`, `session_wait`, `session_kill`, `put`, `get`.
- Sessions are tmux-backed on the remote, addressed by composite IDs `<host>/<name>`, auto-created on first use. State (cwd, env, activated venv) persists across calls within a session.
- All command output is auto-tee'd to `~/.tai-ssh/logs/<log_id>.log` on the remote. Tool responses return only head + tail slices above a size threshold; the LLM uses standard shell tools (`tail`, `grep`, `cat`) via `session_run` to read full logs.
- Sudo and other interactive prompts are detected via pane pattern-matching. When detected, the tool returns `needs_password` / `needs_input` plus an `attach_hint` instructing the user to `tmux attach` the session, complete the prompt, and detach. The LLM resumes via `session_wait`.
- Host inventory in TOML at `~/.config/tai-mcp-ssh/hosts.toml`. SSH key auth preferred and resolved via `~/.ssh/config`; password auth supported with secrets stored in the OS keychain via `keyring` (never plaintext on disk, never on argv).
- File transfer via SFTP (`put`/`get`) over the existing SSH connection. Root-owned destinations use the documented stage-and-move idiom (put to `/tmp`, `session_run` with `sudo mv`).
- Append-only JSONL audit log at `~/.local/state/tai-mcp-ssh/audit.jsonl`, capturing timestamp, tool, session, command, exit code, duration, byte counts, log_id, optional `reason`, and sha256 for transfers.
- CLI binary `tai-mcp-ssh` for operator workflow: `hosts add/list/remove/test`, `audit tail`, `serve`. Adding a host with password auth uses an interactive `getpass` flow and stores via keychain.

## Capabilities

### New Capabilities
- `host-inventory`: TOML-backed allowlist of hosts the LLM may reach, with SSH key or keychain-referenced password auth, managed via CLI.
- `remote-sessions`: tmux-backed persistent shell sessions per `<host>/<name>` over asyncssh, with sentinel-based completion detection and interactive-prompt detection that surfaces a user handoff path.
- `output-capture`: per-command on-remote log files named by `log_id`, head/tail slicing in tool responses, retention/cleanup policy for stale logs.
- `audit-log`: append-only JSONL recording every tool invocation with rich structured fields and sha256 for file transfers.
- `file-transfer`: SFTP `put` / `get` over the existing SSH channel, with documented stage-and-move pattern for privileged destinations.
- `cli-tool`: `tai-mcp-ssh` command line for managing hosts, tailing the audit log, and serving the MCP.

### Modified Capabilities
<!-- none; this is a greenfield project -->

## Impact

- New repository content under `src/tai_mcp_ssh/` (Python package) plus `pyproject.toml`, `README.md`, and tests.
- New runtime dependencies: `mcp`, `asyncssh`, `keyring`, `click` (or `typer`), `tomli-w`. TOML reads use stdlib `tomllib` (Python 3.11+).
- New on-disk surfaces (per user): `~/.config/tai-mcp-ssh/hosts.toml`, `~/.local/state/tai-mcp-ssh/audit.jsonl`, OS keychain entries under service name `tai-mcp-ssh`.
- New on-remote surface (per managed host): `~/.tai-ssh/logs/` directory and named tmux sessions prefixed `tai-mcp/`. Requires `tmux` installed on each managed host.
- Operational assumption: MCP runs on the same network as the user (so they can `tmux attach` for sudo handoff). Remote-MCP topology is out of scope for v1 and documented as such.
- Python 3.11+ required (for `tomllib` and modern asyncio).
