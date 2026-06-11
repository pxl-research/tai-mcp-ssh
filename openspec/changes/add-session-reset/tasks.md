## 1. `SessionManager.reset`

- [x] 1.1 Add `async def reset(self, session_id: str) -> dict[str, bool]` to `SessionManager` in `src/tai_mcp_ssh/sessions.py`. Parse the session_id (reuse `parse_session_id`), look up `self._sessions.get(session_id)`.
- [x] 1.2 If the state is `None` or `state.log_id is None`, return `{"reset": False}` without auditing a clear (idle/unknown is a no-op). Do not open a connection.
- [x] 1.3 Otherwise take the per-session lock (`self._locks.setdefault(session_id, asyncio.Lock())`), audit a `session_reset` record (host, session, log_id), then call `self._clear_state(state)` and return `{"reset": True}`. Do NOT call `tmux kill-session` and do NOT require a live SSH connection.

## 2. Wire through the MCP tool

- [x] 2.1 Add a `session_reset` tool to `tool_specs()` in `src/tai_mcp_ssh/server.py`: required `session_id` (string), `additionalProperties: false`, and a token-cheap description that states it abandons tracking of the current command without killing the pane and steers callers to use `session_wait` first if unsure whether the command is merely slow.
- [x] 2.2 In `dispatch()`, route `session_reset` to `svc.sessions.reset(args["session_id"])`.

## 3. Tests

- [x] 3.1 `tests/unit/test_sessions.py` (reuse existing fakes): reset on a busy session clears state, returns `{"reset": True}`, audits one `session_reset` record, and a following `session_run` is accepted (not `busy`) — proving the pane was reused, not recreated.
- [x] 3.2 Reset on an idle session and on an unknown `session_id` both return `{"reset": False}` and write no `session_reset` audit record.
- [x] 3.3 Reset does not invoke `tmux kill-session` (assert via the fake connection that no kill command was sent).
- [x] 3.4 Reset on a busy session whose host is unreachable still clears local state and returns `{"reset": True}` without raising `host-unreachable`.
- [x] 3.5 `tests/unit/test_server.py`: `session_reset` dispatch forwards to `sessions.reset`; the tool count assertion updates from 7 to 8.

## 4. Docs, lint, validate

- [x] 4.1 README: add `session_reset` to the tool list (currently "7 MCP tools") and one line on when to use it vs. `session_kill`.
- [x] 4.2 `uv run pytest -q` (coverage stays >= 85%), `uv run ruff check src tests`, `uv run mypy src`, `uv run pre-commit run --all-files` — all clean.
- [x] 4.3 `uv run openspec validate add-session-reset --strict` passes.

## 5. Wrap up

- [ ] 5.1 Commit on a feature branch with a `FEAT:` message referencing issue #5.
- [ ] 5.2 Open PR; on merge, `/opsx:archive add-session-reset` to sync the remote-sessions delta into the canonical specs.
