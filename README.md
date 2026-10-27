# tai-mcp-ssh

A small, opinionated [MCP](https://modelcontextprotocol.io) server that lets an LLM admin a handful of Linux servers (Raspberry Pis, Ubuntu VPSes) over SSH — without handing it your credentials, and without burning your token budget on `apt update` output.

## What it does

- Exposes 8 MCP tools to an LLM: `hosts`, `session_list`, `session_run`, `session_wait`, `session_kill`, `session_reset`, `put`, `get` — see the [tool reference](./docs/tools.md).
- Runs every command inside a named `tmux` session on the remote host, so shell state (cwd, env, activated venvs) persists across calls.
- Captures all output to a log file on the remote (`~/.tai-ssh/logs/<log_id>.log`). Tool responses return head + tail slices; the LLM uses plain `tail`/`grep`/`cat` against the log path to read more.
- Detects sudo prompts and other interactive prompts the LLM cannot answer. Hands off to you via `tmux attach`, then resumes once you detach.
- `session_reset` recovers a wedged command without killing the pane; `session_kill` tears the pane down.
- Tells the LLM to close the sessions it opens, and cleans up `tai/*` sessions left idle at a prompt for 48 hours (configurable).
- Keeps a JSONL audit log of every tool call: command, exit code, byte count, and (optional) LLM-supplied reason.

## What it does NOT do

- It does not store your sudo password, ever. SSH passwords (if used) live in the OS keychain, never on disk.
- It does not let the LLM reach hosts you haven't explicitly added to the allowlist.
- It does not stream raw command output back to the LLM by default — large outputs are trimmed and only the slice the LLM asks for is returned.
- It does not run as a hosted/bastion service (yet). See [Topology](#topology).

## Tech stack

- **Python 3.11+**, fully `asyncio`, packaged with [`hatchling`](https://hatch.pypa.io/) and managed with [`uv`](https://docs.astral.sh/uv/)
- [`mcp`](https://pypi.org/project/mcp/) — official MCP Python SDK (low-level server, stdio transport)
- [`asyncssh`](https://asyncssh.readthedocs.io/) — SSH connections and SFTP
- [`keyring`](https://pypi.org/project/keyring/) — OS keychain for password-auth hosts
- [`click`](https://click.palletsprojects.com/) — CLI; [`tomli-w`](https://pypi.org/project/tomli-w/) — writing `hosts.toml`; [`python-ulid`](https://pypi.org/project/python-ulid/) — log IDs
- `tmux` on each remote host for persistent sessions
- Dev: `pytest` (+ `pytest-asyncio`, `pytest-cov`), `ruff`, `mypy --strict`, `pre-commit`

## Requirements

- Python 3.11+ and [`uv`](https://docs.astral.sh/uv/) on the machine running the MCP (`brew install uv` or `curl -LsSf https://astral.sh/uv/install.sh | sh`)
- `tmux` installed on each managed remote host
- SSH access to those hosts (key auth strongly preferred)

## Installation

> This project is currently an MVP for internal / educational use at PXL Smart ICT and is not published to PyPI. The intended install path is `git clone` + `uv sync`.

```sh
git clone https://github.com/pxl-research/tai-mcp-ssh.git
cd tai-mcp-ssh
uv sync
```

This creates `.venv/` and installs all runtime + dev dependencies from `uv.lock`. No global pollution.

## Configuration

Add each host the LLM may reach to the allowlist:

```sh
uv run tai-mcp-ssh hosts add pi-living
# interactive prompts for host, user, auth method, identity file
# passwords (if needed) are captured via getpass and stored in the OS keychain

uv run tai-mcp-ssh hosts test pi-living
# checks: connect, whoami, tmux present, log dir writable, end-to-end latency
```

Host config lives at `~/.config/tai-mcp-ssh/hosts.toml`. Key-auth hosts can piggyback on your existing `~/.ssh/config` — just list the alias. Edits are picked up by the running server on the next `hosts` tool call. See [docs/configuration.md](./docs/configuration.md) for the full file format, defaults, and local paths.

## Running

The server speaks MCP over stdio; your MCP client launches it. Configure the client to start it via `uv` so deps resolve from this checkout:

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

Other CLI commands:

```sh
uv run tai-mcp-ssh --help
uv run tai-mcp-ssh hosts list | remove <alias>
uv run tai-mcp-ssh audit tail -n 20 --pretty
```

### Topology

**The MCP and the human operator are assumed to be on the same machine or LAN as the managed host.** The sudo handoff asks you to `ssh <host> -t tmux attach -t tai/<name>` in a separate terminal, which requires you to reach the host directly. Running the MCP on a remote bastion while you sit elsewhere is out of scope for v1. If you need bastion-style operation, configure `NOPASSWD` sudoers for the commands the LLM uses so the handoff is rarely needed (see [docs/sudo-handoff.md](./docs/sudo-handoff.md)).

## Documentation

- [Configuration](./docs/configuration.md) — `hosts.toml` format, keychain secrets, SSH behaviour, local paths.
- [MCP tool reference](./docs/tools.md) — tool arguments, result shape, statuses, output trimming.
- [The sudo handoff](./docs/sudo-handoff.md) — the `tmux attach` flow for interactive prompts, plus `NOPASSWD` sudoers for routine ops.
- [File transfer](./docs/file-transfer.md) — staging root-owned files via `put` + `sudo mv`, and the downloads-dir confinement `get` enforces.
- [Observability](./docs/observability.md) — the JSONL audit log and attaching to a live `tmux` pane to watch the LLM work.
- [Development](./docs/development.md) — tests, linting, pre-commit and pre-push hooks, and the OpenSpec workflow.

The behavioural contract lives under `openspec/specs/` (one capability per directory). In-flight changes — proposed but not yet promoted into the canonical specs — live under `openspec/changes/`.

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
tests/unit/           pytest suite (fakes, no real SSH)
openspec/             specs and change proposals
docs/                 how-to guides and reference
```

## License

[PolyForm Noncommercial License 1.0.0](https://polyformproject.org/licenses/noncommercial/1.0.0). Free for personal, hobby, research, educational, and other noncommercial use. Commercial use requires a separate license — open an issue if you need one. Full text in [`LICENSE`](./LICENSE).
