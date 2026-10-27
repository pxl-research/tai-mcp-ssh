## Context

Strict validation rejects requirement descriptions over 500 characters. Three requirements bundle several behaviours into one paragraph.

## Decisions

- **MODIFIED + ADDED, not RENAMED.** Each original requirement keeps its header, so existing references stay valid, and its description is shortened to the core behaviour. Each behaviour that moves out becomes an ADDED requirement.
- **REMOVED + ADDED for `hosts()` MCP tool surface.** OpenSpec rejects a MODIFIED block that drops any of the original's scenarios, even if they reappear elsewhere. Five of its six scenarios test reloading, so the requirement is replaced by three new ones, and the listing requirement gets a more specific name.
- **Scenarios follow the behaviour they test.** No scenario is dropped. Scenarios that tested a moved behaviour move with it.
- **New scenarios only where a requirement would have none.** "Audit log root location" gets a Linux scenario and a macOS scenario, both matching `paths.audit_dir()`. "`get` checks containment before any I/O" gets a symlink/`..` scenario, matching `TransferManager.get`'s resolve-then-check order. The `allow_outside` opt-in stays in the core `get` requirement, since it is the exception to that rule.

## Risks / Trade-offs

None. Behaviour is unchanged.
