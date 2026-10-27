# Proposal

## Why

The `session_reset` requirement synced from `add-session-reset` (#17) packs four behaviors into one description that is longer than 500 characters. As a result, `openspec validate remote-sessions --strict` fails with a warning.

## What Changes

- Shorten the existing requirement's description to its core behavior: the tool returns a stuck session to idle without killing the pane. Its header and all three scenarios stay unchanged.
- Move each of the other behaviors into its own requirement with its own scenario:
  - the pane survives the reset
  - the reset needs no live connection
  - the reset is audited
- No behavior change and no code change. The wording of the moved behaviors is kept.

## Capabilities

### New Capabilities

None.

### Modified Capabilities

- `remote-sessions`: the `session_reset` requirement is split into four smaller requirements, with the same meaning.

## Impact

Spec text only (`openspec/specs/remote-sessions/spec.md`). No code, tests, or docs change.
