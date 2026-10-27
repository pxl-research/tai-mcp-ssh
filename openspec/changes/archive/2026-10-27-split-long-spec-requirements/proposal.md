## Why

`openspec validate --specs --strict` fails for `audit-log`, `file-transfer` and `host-inventory`. In each spec one requirement's description is longer than 500 characters because it states several behaviours at once. With these fixed, the whole spec set passes strict validation, so any new warning stands out.

## What Changes

- `audit-log`: "Audit log directory layout" keeps the per-host and `_system` subdirectories. The platform-specific root path moves to a new requirement, "Audit log root location".
- `file-transfer`: "`get` confines its local destination to the downloads directory" keeps the containment rule and the `allow_outside` opt-in. The resolve-before-I/O ordering moves to its own requirement.
- `host-inventory`: "`hosts()` MCP tool surface" is replaced by three requirements: the redacted listing, reload on every call, and connection eviction. A MODIFIED block may not drop scenarios, and most of its scenarios belong to the reload behaviours.

This is a restructure only. No behaviour changes, and no code or tests change.

## Capabilities

### New Capabilities

None.

### Modified Capabilities

- `audit-log`: requirement split, no behaviour change.
- `file-transfer`: requirement split, no behaviour change.
- `host-inventory`: requirement split, no behaviour change.

## Impact

Specs only.
