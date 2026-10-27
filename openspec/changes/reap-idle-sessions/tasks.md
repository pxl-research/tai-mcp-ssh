# Tasks

## 1. Exact-match tmux targets and self-healing sessions

- [ ] 1.1 In `sessions.py`, add helpers for exact-match targets: `'=tai/<name>'` for session commands and `'=tai/<name>:'` for pane commands. Use them in `session_kill`, `pipe-pane` and `send-keys`. Verify that the `test_sessions.py` assertions on the tmux command strings are updated and pass.
- [ ] 1.2 Fold `has-session || { new-session && echo __TAI_CREATED__; }` into the `pipe-pane` round trip in `_send_command`. Drop the separate `_create_session` remote call, but still register state on first use. Record `_session_create` when the marker appears in stdout. Verify that a first-use test asserts exactly one `_session_create` record and one fewer `conn.run` call than before.
- [ ] 1.3 Add a test in which a previously used session's tmux session has vanished: the fake reports `has-session` as failing. Verify that `session_run` recreates it, returns `done`, and audits `_session_create`. This is the spec scenario "tmux session vanished between calls".
- [ ] 1.4 Update the `CLAUDE.md` sessions bullet: there are exact-match targets, and the session is ensured before every command. Verify with `uv run pre-commit run --all-files`.

## 2. `[sessions]` config

- [ ] 2.1 Add `SessionSettings(idle_timeout_hours: int = 48)` to `config.py`, and add it to `Config` as `sessions`. Parse `[sessions]`, rejecting non-table, non-integer or negative values with `ConfigError`. Verify with `test_config.py` cases for the default, a custom value, `0`, a negative value and a wrong type.
- [ ] 2.2 Document `[sessions] idle_timeout_hours` in the example and in a new "Session cleanup" subsection of `docs/configuration.md`. Verify that the example parses: a `load_config` test against the documented TOML snippet.

## 3. Throttled per-host maintenance in `ConnectionPool`

- [ ] 3.1 Add `idle_timeout_hours` and an injectable `monotonic` clock to the `ConnectionPool` constructor. Add `idle_timeout_hours` to `update_hosts(...)`. Wire both through `Services.__init__` and `reload_hosts_from_disk`. Verify with a `test_server.py` test that a reload with a changed value reaches the pool.
- [ ] 3.2 Replace the one-shot sweep scheduling in `_ensure_ready` with a throttle check in `get()`. If at least 3600 s have passed since the last run (or there is no previous run), record the time and schedule `_maintain(conn)`, keeping a strong task reference as today. Verify with `test_ssh.py`, using the fake clock:
  - runs on the first `get`
  - does not run on a second `get` within the hour
  - runs again after the hour
  - does not run twice under concurrent `get`s
  - survives eviction without re-running
- [ ] 3.3 `_maintain` runs the existing log sweep and then the session reap. Each step is independently best-effort: an exception in one is audited and doesn't stop the other. Verify with a test in which the log sweep raises and the reap still runs.
- [ ] 3.4 Update the log-retention wording in `docs/configuration.md` from "first connect" to "at most hourly while in use". Verify by reading the doc against the output-capture delta.

## 4. Session reaper

- [ ] 4.1 Implement the reap step. One `conn.run` with `date +%s; tmux list-panes -a -F ...` (tab-separated fields as in design §3). Parse the rows, filter `^(tai|tai-mcp)/`, group by session, strip a leading `-` from the command, and select candidates where every pane is a shell (`bash`, `zsh`, `sh`, `dash`, `fish`), the session is unattached, and `session_activity` (epoch seconds) is older than the timeout measured against the remote clock. Skip the step when `idle_timeout_hours == 0`. Verify with unit tests on a pure parse-and-select function covering each spec keep/reap scenario: stale bash, legacy prefix, `rsync`, `sudo`, attached, a non-`tai` name, and a split pane with one busy pane.
- [ ] 4.2 Kill the candidates in one chained round trip using the `pgrep -P <pid>; [ $? -eq 1 ] && tmux kill-session -t '=<name>' && echo '<name>'` guard, with names shell-quoted. Take the killed names from stdout. Verify with a fake connection test that the chained command is well-formed and that only names echoed back are reported.
- [ ] 4.3 Treat "no server running" (a non-zero `list-panes` exit) as zero sessions. Audit every run as `_session_sweep` with `idle_timeout_hours`, `killed` (a list) and `status`, and audit any exception as `status: "error"`. The step must never raise. Verify with tests for the no-server, ok and error records.
- [ ] 4.4 Describe the cleanup behaviour in `docs/tools.md` (Sessions section) and the `_session_sweep` event in `docs/observability.md`. Verify that the docs match the spec scenarios.

## 5. Integration checks

- [ ] 5.1 Run `uv run pytest` and `uv run pre-commit run --all-files`. Verify that both pass and coverage stays at or above 85%.
- [ ] 5.2 Run `openspec validate reap-idle-sessions --strict` and verify that it reports the change as valid.
- [ ] 5.3 Smoke test on a real host, using the oldest tmux in use. Verify that:
  - `=`-targets work for `has-session`, `kill-session`, `pipe-pane` and `send-keys`
  - a session killed by hand is recreated on the next `session_run`
  - with `idle_timeout_hours` temporarily set to 1 and the session backdated by waiting, an idle session is reaped and the audit shows `_session_sweep`

## Workflow follow-up

- Archive the change after review and merge, which promotes the deltas into `openspec/specs/`.
