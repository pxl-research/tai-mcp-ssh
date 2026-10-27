## Why

The audit-log spec requires exactly one record for every tool invocation. The code doesn't do that. Three calls write no record:
- `session_list`
- `session_wait` on a session with no command running
- `session_reset` on a session with no command running

None of these touch a remote host or change anything there. The audit log exists so operators can see what happened on a host, so these calls don't need a record. This change makes the spec match the code.

## What Changes

- The requirement is renamed from "Every tool invocation produces exactly one audit record" to "Every host action produces exactly one audit record".
- That requirement now covers every call that acts on a managed host, plus every call that is rejected or fails.
- It explicitly exempts `session_list`, and `session_wait` or `session_reset` when no command is running. These calls only read local state or leave it unchanged.
- The Purpose section and the docs that repeat the old wording (`README.md`, `docs/observability.md`, `CLAUDE.md`, the `server.py` docstrings) are updated to match.

No behaviour change.

## Capabilities

### New Capabilities

None.

### Modified Capabilities

- `audit-log`: narrows the "one record per call" guarantee to host actions, rejections and failures.

## Impact

Specs and docs only. No code or test changes.
