## Context

`Services` loads `hosts.toml` once via `load_config()` (server.py:180) and hands the resulting `Config.hosts: dict[str, Host]` to `ConnectionPool` at construction (server.py:182, ssh.py:147). Neither is reloaded for the lifetime of the process. The CLI happily edits `hosts.toml` while `serve` is running — it's the documented onboarding path — but the running MCP has no idea.

The current public surface this change touches:
- One MCP tool: `hosts` (defined in `tool_specs()` and dispatched in `dispatch()` server.py:191).
- One audit event family: `_system` housekeeping events (audit-log spec already covers this shape).

Eviction machinery already exists for adjacent reasons: `ConnectionPool._evict` (ssh.py) drops dead connections after transport errors. The reload path can lean on the same eviction primitive instead of inventing a new one.

## Goals / Non-Goals

**Goals:**
- A `hosts.toml` edit becomes visible to the MCP on the *next* `hosts` tool call, without a server restart.
- Removed or changed aliases get their cached `Connection` closed and dropped from the pool, so the next call against them opens fresh with the new params.
- In-flight calls are not disturbed by a concurrent reload.
- Exactly one audit record per reload (`_hosts_reload` with `added`/`removed`/`changed` counts), preserving the every-call-audited invariant for `hosts`.
- Reload is cheap: one TOML read on the `hosts` path only — not on `session_run`/`put`/`get`/etc.

**Non-Goals:**
- Reloading on every tool dispatch. The reporter and we both agree that's spooky and adds a stat() to the hot path.
- Bounded-staleness time cache. Arbitrary tuning knob; the LLM-driven explicit refresh is more legible.
- inotify watcher. Linux-only, background-task complexity, overkill for a small TOML.
- Separate `refresh_hosts` MCP tool. Every conversation pays for it in the tool description budget forever; overloading `hosts` is the standard MCP idiom (cf. `kubectl get` always hitting the API).
- Hot-reloading `[audit]` settings or any other top-level Config. Out of scope for this change; only `hosts.toml`'s `[hosts.*]` tables matter for v1.

## Decisions

### D1. Reload trigger: `hosts` call only

The `hosts` MCP tool is the natural integration point — it's what the LLM calls when it needs to know which machines it can reach. Tying reload to that call gives the LLM one obvious affordance for "I just added a host, pick it up" without expanding the tool surface.

**Alternatives considered:**
- *mtime-check on every dispatch.* Rejected: symmetric across tools but adds a `stat()` to every call and produces "spooky" reloads at unrelated tool sites.
- *Time-based cache (N minutes).* Rejected: still stale for `N - ε` seconds, and `N` is a knob.
- *inotify watcher.* Rejected: Linux-only as a stdlib feature, needs a background task, overkill.
- *Separate `refresh_hosts` tool.* Rejected: every conversation pays for the extra tool description.

### D2. `ConnectionPool.update_hosts(new_hosts, evict)` is the eviction primitive

Reuses the existing `_evict(alias)` machinery (already used by the dead-connection path). The pool's public surface gains one method; the locking/closing logic stays in one place.

Signature:
```python
async def update_hosts(self, new_hosts: dict[str, Host], evict: set[str]) -> None:
    self._hosts = new_hosts
    for alias in evict:
        await self._evict(alias)
```

The caller (`Services.reload_hosts_from_disk`) computes the `evict` set from the diff. Two reasons for moving the diff out of the pool: (a) the pool doesn't need to know what "changed" means at the `Host` level, (b) the diff is cheap and easier to test as a pure function on the Services side.

### D3. Diff is `removed ∪ changed` where `changed = old[a] != new[a]`

`Host` is a `dataclass(frozen=True, slots=True)`, so `==` already does what we want. Added aliases need no eviction — there's nothing cached yet. Unchanged aliases skip eviction so an in-flight `session_run` against them keeps using its (still valid) cached connection.

### D4. Audit on every reload, even when nothing changed

One `_hosts_reload` record per `hosts` call, with `added`/`removed`/`changed` counts (each an int, can be `0`). Keeps the every-call-audited invariant trivially true and gives operators a forensic answer to "when did the LLM last see the current allowlist?".

### D5. Failure mode: keep the current Config if the new one is bad

If `load_config()` raises (TOML is malformed, forbidden key present, etc.), we **do not** clear the existing allowlist. The `hosts` call returns the current view + an audit record indicates the reload failed. This avoids the worst-case where a typo in `hosts.toml` makes the running MCP forget every host until the user fixes the file *and* calls `hosts` again.

### D6. Locking: no new lock

`ConnectionPool` already has per-alias `asyncio.Lock`s. `update_hosts` acquires the affected per-alias locks via `_evict`, which already does the right thing under contention. The `self._hosts = new_hosts` assignment is a single Python ref-swap (atomic w.r.t. the interpreter) so concurrent reads are safe without a separate guard.

## Risks / Trade-offs

- **Risk: a `hosts` call now does file I/O.** The TOML read is small (tens of bytes typically) and only on the `hosts` path. → Mitigation: do not extend this to other tools.
- **Risk: stale-after-reload — the LLM holds a previous `hosts()` result in context.** True; out of scope. The reload guarantees the *next* call sees a fresh view; we don't push updates.
- **Risk: a host removed mid-session shows up as "still authorised" to a long-running `session_run`.** True by design — in-flight calls keep their cached connection until they finish. A *new* `session_run` against the removed alias raises `HostNotAllowed` as before. → Mitigation: documented in the spec scenario.
- **Risk: TOML reload races with a CLI write.** `tomllib` reads atomically (one `open`+`read`); the CLI's `save_host` writes atomically via tempfile-rename per config.py. Worst case: we read the pre-write or post-write snapshot; never a half-write.
- **Trade-off: in-process memory cost.** None — the new dict replaces the old one.
