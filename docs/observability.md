# Observability

## Audit log

Every tool call writes one JSON line into a per-host, per-UTC-day file:

- Linux: `~/.local/state/tai-mcp-ssh/audit/<host>/YYYY-MM-DD.jsonl`
- macOS: `~/Library/Logs/tai-mcp-ssh/audit/<host>/YYYY-MM-DD.jsonl`

Non-host events (MCP startup, allowlist rejections where the host alias isn't recognised, retention-sweep summaries) live under `audit/_system/`. Files older than 90 days are deleted on MCP startup; configurable via an `[audit] retention_days = N` section in `hosts.toml`.

Inspect with:

```sh
uv run tai-mcp-ssh audit tail -n 20 --pretty
uv run tai-mcp-ssh audit tail --host pi-living -n 100

# or directly with shell tools
tail -f ~/.local/state/tai-mcp-ssh/audit/pi-living/$(date -u +%F).jsonl | jq
cat ~/.local/state/tai-mcp-ssh/audit/pi-living/*.jsonl | jq 'select(.exit != 0)'
```

Each line carries `ts`, `tool`, `host`, `session`, `cmd`, `exit`, `duration_ms`, `stdout_bytes`, `log_id`, the LLM's `reason` (if supplied), and `sha256` for file transfers.

## Live observability

Because every action happens in a named `tmux` pane, you can attach at any time and watch the LLM work:

```sh
ssh pi-living -t tmux attach -t tai-mcp/default
```

Detach with `Ctrl-B D`.
