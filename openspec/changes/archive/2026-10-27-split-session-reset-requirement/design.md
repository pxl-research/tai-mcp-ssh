# Design

## Context

This change restructures the spec only. See proposal.md for the reason.

## Decisions

- The split follows OpenSpec's procedure for long requirements:
  - Under MODIFIED, the original header and every scenario stay as they are, and only the description is cut down to one behavior.
  - Each behavior that was removed comes back as an ADDED requirement with its own scenario.
- The audit requirement covers only resets that clear an in-flight command. That is what the original text and the code specify (the record holds host, session and log_id). This change deliberately doesn't specify the no-op case.

## Risks / Trade-offs

- [Risk] Rewording could shift meaning → the moved sentences are kept almost word for word.
