## Context

`SessionManager` tracks each session's in-flight command in `_SessionState.log_id`
(`src/tai_mcp_ssh/sessions.py`). While that field is set, `run()` returns `busy`
(serialization, `remote-sessions` spec). It is cleared in exactly three places today:
on completion/`_poll` resolution, on a failed `_send_command` (the wedge-fix
roll-back), and on `host-unreachable` via `_forget`. None of these covers a command
that *sent fine but never produces its DONE sentinel* — `exec sh` mid-session, a
corrupted/truncated log, a transport blip that didn't kill tmux. In that state the
session is `busy` forever and only `session_kill` (which destroys the tmux pane)
recovers it.

The teardown needed is already implemented: `_clear_state(state)` resets `log_id`,
`log_path`, `command`, `reason`, `started_at`, the log buffer/offset/decoder, and
`scan_pos` — leaving the `_SessionState` in the registry as idle. The per-session
`asyncio.Lock` that `run`/`wait` use to serialize access also already exists. So the
escape hatch is a thin, auditable wrapper over primitives that are present and tested.

Constraints: keep the MCP tool surface token-cheap (descriptions ship every turn);
reuse `_clear_state`, the per-session lock, and the existing audit `record()` path;
do not change the `busy` semantics or any existing tool.

## Goals / Non-Goals

**Goals:**
- A `session_reset(session_id)` tool that returns a wedged session to idle without
  killing the tmux pane, so cwd/env/children survive.
- Auditable as its own `session_reset` event.
- A no-op (`reset: false`) on idle/unknown sessions; `reset: true` when it actually
  cleared an in-flight command.

**Non-Goals:**
- `force=True` on `session_run` — a convenience shorthand for "reset then run". Sound,
  but it overloads `run` semantics and buries recovery inside a run event; hold it as a
  possible follow-on once `session_reset` exists.
- Auto-abandon in `_poll` based on a log-size stall timer. The issue itself flags the
  tuning problem (a quiet `apt update` is indistinguishable from a wedge); the false-
  abandon risk outweighs the benefit. Explicitly out of scope.
- Any remote signalling (no `C-c`, no `tmux send-keys` interrupt). `reset` is purely
  local bookkeeping; killing or signalling the remote command is what `session_kill`
  is for.

## Decisions

### 1. Reuse `_clear_state` under the per-session lock
`reset()` parses the session_id, looks up the state, and — if there is an in-flight
command — takes the same per-session `asyncio.Lock` as `run`/`wait` before calling
`_clear_state`. The lock prevents a race with a concurrent `_poll`/`run` mutating the
same state. No new teardown logic; `_clear_state` is the single source of truth for
"return this session to idle."

- **Alternative considered:** a standalone reset that re-implements the field resets.
  Rejected — it would drift from `_clear_state` the moment a new `_SessionState` field
  is added (exactly the lockstep bug we just de-duplicated for the sentinel regex).

### 2. Local-only; do not touch the remote pane
`reset` deliberately leaves the tmux pane and any running command alone. This is the
whole point of the feature (preserve pane state), and it is the key behavioral
difference from `session_kill`. The tradeoff — if the command is genuinely still
running, a later `session_run` interleaves with it — is surfaced in the tool
description ("use when a command is believed wedged, not merely slow; call
`session_wait` first if you're unsure"). Encoding judgment about "is it really stuck"
into the tool would be a heuristic we explicitly rejected (see Non-Goals).

### 3. No connection required for the common case
Unlike `run`/`wait`, `reset` does not need a live SSH connection — it only mutates
local state. It SHALL NOT open or require a connection, so a session wedged *because*
the host is flaky can still be reset. (`host-unreachable` is already handled for the
other tools via `_forget`; reset's local-only nature sidesteps it entirely.)

### 4. Return shape mirrors `session_kill`
`{"reset": bool}`, paralleling `session_kill`'s `{"killed": bool}` — `true` when an
in-flight command was cleared, `false` when the session was already idle or unknown.
Symmetry keeps the tool surface predictable.

## Risks / Trade-offs

- **[Output interleaving if the command was not actually wedged]** → Documented in the
  tool description; `session_wait` remains the first-line "is it still going?" check.
  Inherent to any non-destructive escape hatch; the alternative (`session_kill`) is
  strictly more destructive.
- **[LLM reaches for `reset` reflexively instead of waiting]** → It is audited every
  time, so misuse is visible in the log; and `reset: false` on an idle session makes a
  premature call cheap and obvious.
- **[Lock acquisition on a session whose `_poll` is mid-flight]** → That is the point
  of taking the lock; reset waits its turn rather than racing. A `_poll` holding the
  lock for the full timeout means reset blocks up to that timeout — acceptable, and no
  worse than any other serialized call.

## Open Questions

- Should `session_list`'s `busy` flag gain a companion `last_reset_at`, or is the
  audit record sufficient? Leaning audit-only (no state-shape churn) — flag for review.
