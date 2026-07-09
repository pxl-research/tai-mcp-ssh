# tai-mcp-ssh

A small, opinionated [MCP](https://modelcontextprotocol.io) server that lets an LLM admin a handful of Linux servers (Raspberry Pis, Ubuntu VPSes) over SSH — without handing it your credentials, and without burning your token budget on `apt update` output.

The current behavioural contract lives under `openspec/specs/` (one capability per directory). In-flight changes — proposed but not yet promoted into the canonical specs — live under `openspec/changes/`.

## What it does

- Exposes 8 MCP tools to an LLM: `hosts`, `session_list`, `session_run`, `session_wait`, `session_kill`, `session_reset`, `put`, `get`.
- `session_reset` is the lightweight escape hatch for a wedged command: it returns the session to idle without killing the tmux pane (cwd/env/children survive), unlike `session_kill` which tears the pane down.
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

## Topology

**The MCP and the human operator are assumed to be on the same machine or LAN as the managed host.** The sudo handoff works by asking you to `ssh <host> -t tmux attach -t tai-mcp/<name>` in a separate terminal — that requires you to be able to reach the host directly. Running the MCP on a remote bastion while you sit elsewhere is out of scope for v1; the handoff would have nowhere to land. If you need bastion-style operation, configure `NOPASSWD` sudoers for the commands the LLM uses (see below) so the handoff is rarely needed.

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

## Documentation

Deeper how-to guides live under [`docs/`](./docs/):

- [The sudo handoff](./docs/sudo-handoff.md) — the `tmux attach` flow for interactive prompts, plus configuring `NOPASSWD` sudoers to avoid it for routine ops.
- [File transfer](./docs/file-transfer.md) — staging root-owned files via `put` + `sudo mv`, and the downloads-dir confinement `get` enforces.
- [Observability](./docs/observability.md) — the JSONL audit log and attaching to a live `tmux` pane to watch the LLM work.

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
  errors.py           shared exception types

openspec/             change proposals and specs
docs/                 how-to guides (sudo handoff, file transfer, observability)
```

## License

[PolyForm Noncommercial License 1.0.0](https://polyformproject.org/licenses/noncommercial/1.0.0). Free for personal, hobby, research, educational, and other noncommercial use. Commercial use requires a separate license — open an issue if you need one. Full text in [`LICENSE`](./LICENSE).
