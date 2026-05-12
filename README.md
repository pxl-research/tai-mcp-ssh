# tai-mcp-ssh

A small, opinionated [MCP](https://modelcontextprotocol.io) server that lets an LLM admin a handful of Linux servers (Raspberry Pis, Ubuntu VPSes) over SSH — without handing it your credentials, and without burning your token budget on `apt update` output.

> **Status**: design complete, implementation not started. See `openspec/changes/bootstrap-ssh-mcp-server/` for the full spec.

## What it does

- Exposes 7 MCP tools to an LLM: `hosts`, `session_list`, `session_run`, `session_wait`, `session_kill`, `put`, `get`.
- Runs every command inside a named `tmux` session on the remote host, so shell state (cwd, env, activated venvs) persists across calls.
- Captures all output to a log file on the remote (`~/.tai-ssh/logs/<log_id>.log`). Tool responses return head + tail slices; the LLM uses plain `tail`/`grep`/`cat` against the log path to read more.
- Detects sudo prompts and other interactive prompts the LLM cannot answer. Hands off to you via `tmux attach`, then resumes once you detach.
- Keeps a JSONL audit log of every command, exit code, byte count, and (optional) LLM-supplied reason.

## What it does NOT do

- It does not store your sudo password, ever.
- It does not let the LLM reach hosts you haven't explicitly added to the allowlist.
- It does not stream raw command output back to the LLM by default — large outputs are trimmed and only the slice the LLM asks for is returned.
- It does not run as a hosted/bastion service (yet). The MCP and the human operator are assumed to be on the same machine or LAN, because sudo handoff uses `tmux attach`.

## Requirements

- Python 3.11+ on the machine running the MCP
- [`uv`](https://docs.astral.sh/uv/) (`brew install uv` or `curl -LsSf https://astral.sh/uv/install.sh | sh`)
- `tmux` installed on each managed remote host
- SSH access to those hosts (key auth strongly preferred)

## Development setup

Clone the repo, then:

```sh
uv sync
```

This creates `.venv/` and installs all runtime + dev dependencies from `uv.lock`. No global pollution.

Common dev commands:

```sh
uv run tai-mcp-ssh --help
uv run pytest
uv run ruff check .
uv run mypy src
```

### Pre-commit hooks

Activate once per clone:

```sh
uv run pre-commit install
```

From then on every `git commit` runs:

- `ruff` (lint with `--fix`) and `ruff-format`
- `mypy` (via `uv run mypy src` — sees the same deps as your venv)
- hygiene: trailing whitespace, EOF newline, TOML/YAML syntax, merge-conflict markers, large files (>500 KB), private SSH key detection, line-ending normalisation

Run them manually against the whole tree (useful before opening a PR):

```sh
uv run pre-commit run --all-files
```

Hook versions are pinned in `.pre-commit-config.yaml`. Bump them with `uv run pre-commit autoupdate` when you want.

> **Note**: this project is currently an MVP for internal / educational use at PXL Smart ICT and is not published to PyPI. The intended install path is `git clone` + `uv sync`.

## Add a host

```sh
uv run tai-mcp-ssh hosts add pi-living
# interactive prompts for host, user, auth method, etc.
# passwords (if needed) are captured via getpass and stored in the OS keychain
```

Then verify:

```sh
uv run tai-mcp-ssh hosts test pi-living
# checks: connect, whoami, tmux present, log dir writable, end-to-end latency
```

Host config lives at `~/.config/tai-mcp-ssh/hosts.toml`. Key-auth hosts can piggyback on your existing `~/.ssh/config` — just list the alias.

## Wire into your MCP client

The server speaks MCP over stdio. Configure your MCP client to launch it via `uv` so deps resolve from this checkout:

```json
{
  "mcpServers": {
    "tai-mcp-ssh": {
      "command": "uv",
      "args": [
        "--directory", "/absolute/path/to/tai-mcp-ssh",
        "run", "tai-mcp-ssh", "serve"
      ]
    }
  }
}
```

`uv --directory` tells uv to operate as if it were in that directory regardless of where the MCP client launches it from. Edits to the source are picked up on the next launch — no rebuild step.

## The sudo handoff

When the LLM runs a command that needs a password, the MCP detects the prompt within milliseconds and returns something like:

```json
{
  "status": "needs_password",
  "attach_hint": "ssh pi-living -t tmux attach -t tai-mcp/default",
  "prompt": "[sudo] password for pi:"
}
```

The LLM will tell you what to do. The flow:

1. Run the `attach_hint` command in your own terminal.
2. Type your sudo password into the live `tmux` pane.
3. Detach with `Ctrl-B` then `D`.
4. Tell the LLM to continue. It will call `session_wait` and pick up where it left off.

**Tip**: for routine LLM admin work, configuring `NOPASSWD` `sudoers` entries for the specific commands you trust (e.g. `systemctl restart nginx`, `apt update`) sidesteps the handoff entirely and is more auditable.

## Stage-and-move for root-owned files

`put` runs as the SSH user, not root. To install a config file into `/etc/...`:

```
1. put("pi-living", "nginx.conf", "/tmp/nginx.conf.new")
2. session_run("pi-living/default", "sudo mv /tmp/nginx.conf.new /etc/nginx/nginx.conf")
```

Both halves are independently audited.

## Audit log

Every tool call writes one JSON line to:

- Linux: `~/.local/state/tai-mcp-ssh/audit.jsonl`
- macOS: `~/Library/Logs/tai-mcp-ssh/audit.jsonl`

Inspect with:

```sh
tai-mcp-ssh audit tail -n 20 --pretty
tai-mcp-ssh audit tail --host pi-living -n 100
# or: tail -f ~/.local/state/tai-mcp-ssh/audit.jsonl | jq
```

Each line carries `ts`, `tool`, `session`, `host`, `cmd`, `exit`, `duration_ms`, `stdout_bytes`, `log_id`, the LLM's `reason` (if supplied), and `sha256` for file transfers.

## Live observability

Because every action happens in a named `tmux` pane, you can attach at any time and watch the LLM work:

```sh
ssh pi-living -t tmux attach -t tai-mcp/default
```

Detach with `Ctrl-B D`.

## Project layout

```
src/tai_mcp_ssh/      Python package
  cli.py              tai-mcp-ssh CLI entry point
  server.py           MCP server (stdio)
  sessions.py         tmux-backed session manager
  transfer.py         SFTP put/get
  ssh.py              asyncssh connection pool
  config.py           hosts.toml load/save
  audit.py            JSONL audit log
  paths.py            XDG / macOS path resolution

openspec/             change proposals and specs
```

## License

[PolyForm Noncommercial License 1.0.0](https://polyformproject.org/licenses/noncommercial/1.0.0). Free for personal, hobby, research, educational, and other noncommercial use. Commercial use requires a separate license — open an issue if you need one. Full text in [`LICENSE`](./LICENSE).
