## 1. Pool eviction primitive

- [ ] 1.1 Add `ConnectionPool.update_hosts(self, new_hosts: dict[str, Host], evict: set[str]) -> None` in `src/tai_mcp_ssh/ssh.py`. Swap `self._hosts = new_hosts` then `await self._evict(alias)` for each alias in `evict`. Reuses existing `_evict`.
- [ ] 1.2 Unit test in `tests/unit/test_ssh.py`: removed alias's cached connection is closed and dropped from `_connections`/`_ready`; unchanged alias's cached connection survives; added alias is reachable on next `get()` without an extra round trip.
- [ ] 1.3 Unit test: `update_hosts` with an empty evict set is a no-op on connections but does swap the allowlist.

## 2. Services reload helper

- [ ] 2.1 Add `Services.reload_hosts_from_disk(self) -> tuple[int, int, int]` in `src/tai_mcp_ssh/server.py`. Returns `(added, removed, changed)`. Calls `load_config()`, diffs against `self.config.hosts` using `Host.__eq__` (frozen dataclass), computes `evict = removed ∪ changed`, swaps `self.config` and calls `await self.pool.update_hosts(new_hosts, evict)`.
- [ ] 2.2 On `load_config()` failure, leave `self.config` and `self.pool._hosts` untouched and re-raise so the dispatch path can audit the failure.

## 3. Dispatch routing

- [ ] 3.1 In `dispatch()` (`server.py`), when `name == "hosts"`, call `await svc.reload_hosts_from_disk()` before building the return list. Wrap in try/except: on success, audit `_hosts_reload` with `status="ok"` and the diff counts; on failure, audit `_hosts_reload` with `status="error"` and `error=str(exc)`, then fall back to returning the *current* (pre-failure) allowlist.
- [ ] 3.2 Update the `hosts` entry in `tool_specs()` description to mention the side effect: e.g. `"List SSH hosts the LLM may reach. Reloads hosts.toml from disk on each call; use this to pick up newly-added/changed hosts without restarting."`

## 4. Audit + dispatch tests

- [ ] 4.1 Unit test in `tests/unit/test_server.py`: a `hosts` call writes exactly one `_hosts_reload` record to `audit/_system/<today>.jsonl` with `status="ok"` and the expected diff counts.
- [ ] 4.2 Unit test: a `hosts` call when `hosts.toml` is malformed records `_hosts_reload` with `status="error"`, leaves the in-memory allowlist unchanged, and the returned list reflects the pre-failure state.
- [ ] 4.3 Unit test: a `hosts` call when the file is identical to the in-memory state records `_hosts_reload` with all counts equal to `0`.

## 5. End-to-end coverage

- [ ] 5.1 Integration-style test in `tests/unit/test_server.py` (or new `test_reload.py`) using the existing fakes: write a hosts.toml, build Services, call `hosts`, edit the file to add an alias, call `hosts` again — assert the new alias is present and a `session_run` against it does not raise `HostNotAllowed`.
- [ ] 5.2 Test that an in-flight call against an unchanged alias is not disturbed: hold a coroutine inside `_poll`, trigger `hosts` reload, assert the polling task's cached connection survives.

## 6. Lint, types, validate

- [ ] 6.1 `uv run pytest -q` — all green, coverage ≥ 80%.
- [ ] 6.2 `uv run ruff check src/ tests/` and `uv run mypy src/` — clean.
- [ ] 6.3 `uv run openspec validate reload-hosts-on-call --strict` — passes.

## 7. Manual smoke

- [ ] 7.1 With the MCP serving, run `tai-mcp-ssh hosts add smoke-new-host` (key auth, dummy host) — does not restart the MCP.
- [ ] 7.2 In the connected LLM, call the `hosts` tool — verify `smoke-new-host` is in the response and an audit `_hosts_reload` record exists.
- [ ] 7.3 Edit `hosts.toml` to remove `smoke-new-host`. Call `hosts` again. Verify the alias is gone and `session_run("smoke-new-host/x", "true")` raises `HostNotAllowed`.
- [ ] 7.4 Edit `hosts.toml` to introduce a TOML syntax error. Call `hosts`. Verify the response is the pre-failure list and an audit `_hosts_reload` with `status="error"` is recorded. Restore the file.

## 8. Wrap up

- [ ] 8.1 Commit on a feature branch with a `FEAT:` message referencing #9.
- [ ] 8.2 Open PR; on merge, `/opsx:archive reload-hosts-on-call` to promote the deltas into the canonical specs.
- [ ] 8.3 Close issue #9 with a reference to the commit/PR.
