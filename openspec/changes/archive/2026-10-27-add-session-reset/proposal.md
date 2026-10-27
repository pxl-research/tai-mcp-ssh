## Why

When a session's command sends successfully but its DONE sentinel never arrives
(`exec sh` mid-session, a manually-corrupted log, a transport blip that truncated the
log without killing tmux), `_SessionState.log_id` stays set and every subsequent
`session_run` returns `busy` forever. The only escape is `session_kill`, which
destroys the tmux pane's accumulated cwd, environment, and backgrounded children —
the exact state tmux-backed sessions exist to preserve. There is no lighter-weight
recovery affordance (issue #5).

## What Changes

- Add a `session_reset(session_id)` MCP tool that abandons tracking of the current
  in-flight command and returns the session to idle **without** touching the remote
  tmux pane. It clears local session state (the existing `_clear_state` primitive) and
  emits a `session_reset` audit record. The next `session_run` reuses the same live
  pane.
- `session_reset` on an idle or unknown session is a no-op that reports
  `reset: false`; on a session with an in-flight command it reports `reset: true`.
- The tool surface grows from 7 tools to 8.
- **Not** in scope: `force=True` on `session_run` (a possible future ergonomic
  shorthand, noted in design) and auto-abandon in `_poll` (a stall-timeout heuristic
  the issue itself flags as hard to tune — quiet `apt update` looks identical to a
  wedged session). Both are deliberately deferred; `session_reset` is the explicit,
  auditable, low-risk primitive that unblocks recovery on its own.

## Capabilities

### New Capabilities
<!-- none -->

### Modified Capabilities
- `remote-sessions`: adds a requirement that a session stuck with an in-flight
  command can be returned to idle via `session_reset` without tearing down the tmux
  pane, complementing the existing `session_kill`. The `busy` serialization
  requirement is unchanged; `session_reset` is the documented way out of it that
  preserves pane state.

## Impact

- Code: `src/tai_mcp_ssh/sessions.py` (`SessionManager.reset()` — take the per-session
  lock, audit, `_clear_state`); `src/tai_mcp_ssh/server.py` (`session_reset` tool spec
  + dispatch). No change to the connection pool, transfer, or audit modules beyond a
  new tool/event name.
- Reuse: `_clear_state` already performs the exact idle teardown (clears `log_id`,
  buffers, decoder, offsets); the per-session `asyncio.Lock` already serializes
  `run`/`wait`; the audit `record()` path already handles arbitrary event names.
- Behavioral note (documented in the tool description, not a code risk): `reset`
  abandons *local tracking* only — if the command is genuinely still executing
  remotely, a later `session_run` writes into a still-busy shell and output can
  interleave. This is the deliberate tradeoff vs. `session_kill`; the description
  steers callers to use it when a command is believed wedged, not merely slow.
- Tests: extend `tests/unit/test_sessions.py` (reuse the existing fakes) — reset on a
  busy session returns to idle and is reusable; reset on idle/unknown is a `false`
  no-op; reset audits one record; reset does not call `tmux kill-session`.
  `tests/unit/test_server.py` — dispatch routes `session_reset` and the tool count
  becomes 8.
