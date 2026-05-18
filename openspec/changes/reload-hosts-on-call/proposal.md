## Why

`Services.config` and `ConnectionPool._hosts` are both loaded once at MCP server startup. Edits to `~/.config/tai-mcp-ssh/hosts.toml` made while the server is running — adding a host, changing a key path, removing a host — are invisible to the MCP until the process is restarted. Restarting an MCP stdio server is heavy: the client (Claude Code etc.) has to reattach, the user has to confirm the reconnect, and in-flight conversation context is interrupted.

The documented onboarding flow ("run `tai-mcp-ssh hosts add <alias>` then `hosts test`") explicitly expects the user to edit the file while the MCP is running. The current behavior breaks that flow: the new host is visible to the CLI but not to the MCP. Issue #9 calls this out as a workflow papercut frequent enough to warrant a fix.

## What Changes

- **`hosts` MCP tool reloads `hosts.toml` from disk on every invocation.** The natural place for the LLM to discover a newly-added host is the call where it asks "what hosts can I reach?". That call now returns a fresh view instead of the cached startup snapshot.
- **`ConnectionPool` gains a public `update_hosts(new_hosts, evict)` method** that atomically swaps the allowlist dict and best-effort closes cached connections for aliases that were removed or whose connect parameters changed. In-flight calls are not disturbed — only the *next* `pool.get(alias)` reopens with new params.
- **`Services` gains a `reload_hosts_from_disk()` helper** that reads `hosts.toml` fresh, diffs against the current allowlist, and delegates eviction to `pool.update_hosts`.
- **One new audit event `_hosts_reload`** is emitted on every reload with `added` / `removed` / `changed` counts so reloads are traceable in the audit log.
- **Tool description for `hosts` updated** to mention the side effect, so the LLM understands when to call it ("call this if a host was just added in config").

## Capabilities

### New Capabilities
<!-- none -->

### Modified Capabilities
- `host-inventory`: the `hosts` MCP tool gains a documented side effect (reload-from-disk) and the spec adds a requirement that the allowlist visible to MCP tools tracks the on-disk file across edits without a restart.
- `audit-log`: a new `_hosts_reload` system event is added to the housekeeping events the spec describes.

## Impact

- Code: `src/tai_mcp_ssh/server.py` (Services + dispatch routing for `hosts`), `src/tai_mcp_ssh/ssh.py` (`ConnectionPool.update_hosts`), `src/tai_mcp_ssh/server.py` `tool_specs()` (description update). Possibly a small helper on `config.Host` for equality comparison, depending on whether the dataclass already supports `==`.
- Tests: new unit tests in `tests/unit/test_ssh.py` (`update_hosts` evicts removed/changed aliases, leaves additions alone) and `tests/unit/test_server.py` (`hosts` dispatch reloads on call, audits one `_hosts_reload` event per call). Existing `hosts`-call tests adapt to the new behavior.
- Public surface: the `hosts` MCP tool's *return shape* is unchanged. Only its description and side effect change. No client-visible breaking change.
- Dependencies: none.
- Performance: one TOML read per `hosts` call (typically tens of bytes, not on the hot path). No effect on `session_run`/`put`/`get`/etc.
