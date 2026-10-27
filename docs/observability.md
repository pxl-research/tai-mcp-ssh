# Observability

## Audit log

Every tool call writes one JSON line into a per-host, per-UTC-day file:

- Linux: `~/.local/state/tai-mcp-ssh/audit/<host>/YYYY-MM-DD.jsonl`
- macOS: `~/Library/Logs/tai-mcp-ssh/audit/<host>/YYYY-MM-DD.jsonl`

Events that aren't tied to a host go under `audit/_system/`. These include `hosts.toml` reloads (`_hosts_reload`), audit-retention sweep summaries (`_sweep`), and rejections where no host could be determined. Audit files older than 90 days are deleted when the MCP server starts. You can change this with an `[audit] retention_days = N` section in `hosts.toml` (see [configuration.md](./configuration.md)).

Inspect with:

```sh
uv run tai-mcp-ssh audit tail -n 20 --pretty
uv run tai-mcp-ssh audit tail --host pi-living -n 100
uv run tai-mcp-ssh audit tail --session pi-living/default --tool session_run

# or directly with shell tools (Linux paths shown)
tail -f ~/.local/state/tai-mcp-ssh/audit/pi-living/$(date -u +%F).jsonl | jq
cat ~/.local/state/tai-mcp-ssh/audit/pi-living/*.jsonl | jq 'select(.tool == "_session_sweep" and (.killed | length) > 0)'
cat ~/.local/state/tai-mcp-ssh/audit/pi-living/*.jsonl | jq 'select(.exit != 0)'
```

Every record contains `ts`, `tool`, and `host`. Other fields depend on the tool. Command records add `session`, `cmd`, `reason`, `status`, `exit`, `duration_ms`, `stdout_bytes`, `log_id`, and `truncated`. File-transfer records add `local_path`, `remote_path`, `bytes`, `sha256`, and `status`. Internal events have tool names that start with `_`, such as `_session_create`, `_tmux_check`, and `_remote_sweep`. The hourly idle-session cleanup writes one `_session_sweep` record per host per run, with `idle_timeout_hours`, the `killed` session names (often empty), and a `status`. Fields whose names look like secrets are written as `<redacted>`, and so are `--password=…` and `keychain://…` values inside `cmd`.

## Live observability

Because every action happens in a named `tmux` pane, you can attach at any time and watch the LLM work:

```sh
ssh pi-living -t tmux attach -t tai/default
```

Detach with `Ctrl-B D`.
