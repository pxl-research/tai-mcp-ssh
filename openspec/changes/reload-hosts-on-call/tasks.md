## 1. Pool eviction primitive

- [x] 1.1 Add `ConnectionPool.update_hosts(self, new_hosts: dict[str, Host], evict: set[str]) -> None` in `src/tai_mcp_ssh/ssh.py`. Swap `self._hosts = new_hosts` then `await self._evict(alias)` for each alias in `evict`, under the per-alias lock. Reuses existing `_evict`.
- [x] 1.2 Unit test in `tests/unit/test_ssh.py`: removed alias's cached connection is closed and dropped from `_connections`/`_ready`; unchanged alias's cached connection survives; added alias is reachable on next `get()` without an extra round trip. (`test_update_hosts_evicts_removed_alias`)
- [x] 1.3 Unit test: `update_hosts` with an empty evict set is a no-op on connections but does swap the allowlist. (`test_update_hosts_no_eviction_keeps_connections`) Plus `test_update_hosts_changed_params_evicts_and_reopens` confirms a changed Host triggers eviction + re-open.

## 2. Services reload helper

- [x] 2.1 Add `Services.reload_hosts_from_disk(self) -> tuple[int, int, int]` in `src/tai_mcp_ssh/server.py`. Returns `(added, removed, changed)`. Calls `load_config()`, diffs against `self.config.hosts` using `Host.__eq__` (frozen dataclass), computes `evict = removed ∪ changed`, swaps `self.config` and calls `await self.pool.update_hosts(new_hosts, evict)`.
- [x] 2.2 On `load_config()` failure, leave `self.config` and `self.pool._hosts` untouched and re-raise so the dispatch path can audit the failure. (`load_config()` is awaited before the in-memory mutation; the helper re-raises naturally.)

## 3. Dispatch routing

- [x] 3.1 In `dispatch()` (`server.py`), when `name == "hosts"`, call `await svc.reload_hosts_from_disk()` before building the return list. Wrap in try/except: on success, audit `_hosts_reload` with `status="ok"` and the diff counts; on failure, audit `_hosts_reload` with `status="error"` and `error=str(exc)`, then fall back to returning the *current* (pre-failure) allowlist.
- [x] 3.2 Update the `hosts` entry in `tool_specs()` description to mention the side effect.

## 4. Audit + dispatch tests

- [x] 4.1 Unit test in `tests/unit/test_server.py`: a `hosts` call writes exactly one `_hosts_reload` record to `audit/_system/<today>.jsonl` with `status="ok"` and the expected diff counts. (`test_dispatch_hosts_reloads_and_audits_diff_counts`)
- [x] 4.2 Unit test: a `hosts` call when `hosts.toml` is malformed records `_hosts_reload` with `status="error"`, leaves the in-memory allowlist unchanged, and the returned list reflects the pre-failure state. (`test_dispatch_hosts_fails_soft_on_bad_toml`)
- [x] 4.3 Unit test: a `hosts` call when the file is identical to the in-memory state records `_hosts_reload` with all counts equal to `0`. (`test_dispatch_hosts_no_op_reload_audits_zero_counts`)

## 5. End-to-end coverage

- [x] 5.1 Integration-style coverage: `test_dispatch_hosts_reloads_and_audits_diff_counts` walks the full path — `Services` constructed with one host, `load_config` returns a 2-host Config with one IP-changed and one added, the dispatch returns the new list, the pool's `update_hosts` is awaited with the correct evict set, and the audit record carries the diff counts.
- [x] 5.2 In-flight-call-not-disturbed guarantee: covered structurally by `test_update_hosts_no_eviction_keeps_connections` — unchanged aliases are not in the evict set, so their cached `Connection` references held by an in-flight `_poll` cannot be invalidated. (`_poll` doesn't reach back into the pool dict after `pool.get()` returns.)

## 6. Lint, types, validate

- [x] 6.1 `uv run pytest -q` — 146 passing, 90.52% coverage.
- [x] 6.2 `uv run ruff check src/ tests/` and `uv run mypy src/` — clean.
- [x] 6.3 `uv run openspec validate reload-hosts-on-call --strict` — passes.

## 7. Manual smoke

- [ ] 7.1 With the MCP serving, run `tai-mcp-ssh hosts add smoke-new-host` (key auth, dummy host) — does not restart the MCP.
- [ ] 7.2 In the connected LLM, call the `hosts` tool — verify `smoke-new-host` is in the response and an audit `_hosts_reload` record exists.
- [ ] 7.3 Edit `hosts.toml` to remove `smoke-new-host`. Call `hosts` again. Verify the alias is gone and `session_run("smoke-new-host/x", "true")` raises `HostNotAllowed`.
- [ ] 7.4 Edit `hosts.toml` to introduce a TOML syntax error. Call `hosts`. Verify the response is the pre-failure list and an audit `_hosts_reload` with `status="error"` is recorded. Restore the file.

## 8. Wrap up

- [ ] 8.1 Commit on a feature branch with a `FEAT:` message referencing #9.
- [ ] 8.2 Open PR; on merge, `/opsx:archive reload-hosts-on-call` to promote the deltas into the canonical specs.
- [ ] 8.3 Close issue #9 with a reference to the commit/PR.
