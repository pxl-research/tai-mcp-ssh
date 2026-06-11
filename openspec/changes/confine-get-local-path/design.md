## Context

`TransferManager.get` (`src/tai_mcp_ssh/transfer.py`) computes its destination as
`Path(local_path).expanduser()` when `local_path` is given, or
`paths.downloads_dir(host) / Path(remote_path).name` otherwise, then
`dest.parent.mkdir(parents=True, exist_ok=True)` and streams bytes in. The
`local_path` value comes from the LLM via the `get` MCP tool. Nothing constrains it,
so the LLM can direct a download — content the *remote* host controls — onto any file
the operator's user can write.

This is asymmetric with the rest of the threat model. `session_run` already grants the
LLM a remote shell, and `put` reading a local file is inherent to uploading. But `get`
writing *outside* a known sink is a local-overwrite primitive that the operator never
opts into, and it is cheap to fence off. The project's stance is "minimum surface,
explicit allowlists, audited"; a default-confined `get` fits that.

Constraints: keep the tool surface token-cheap (descriptions ship every turn), reuse
existing primitives (`paths.downloads_dir()`, `TransferDenied`, the server's existing
rejection→audit mapping), and don't disturb `put` or the default `get` path.

## Goals / Non-Goals

**Goals:**
- `get` writes only under `downloads_dir()` by default; out-of-tree writes need an
  explicit, audited opt-in.
- Reject before opening SFTP or creating/truncating any local file.
- Zero change to the common cases: no `local_path`, or an in-tree `local_path`.

**Non-Goals:**
- Confining `put`'s *local* source path. Reading a local file is inherent to upload
  and is not an overwrite primitive; out of scope.
- Confining `get`'s *remote* path — the LLM already has full remote read via the SSH
  user and `session_run`.
- A configurable set of allowed sink directories. One root (`downloads_dir()`) plus a
  per-call opt-in is enough; a config knob can come later if a real need appears.

## Decisions

### 1. Containment via `resolve()` + `is_relative_to`, checked before any I/O
Resolve the destination and the downloads root, then require
`dest.is_relative_to(root)`. `Path.resolve()` collapses `..` and follows symlinks, so
neither `downloads/../../etc/x` nor a symlink planted inside the tree escapes the
check. The check runs at the very top of `get`, before `self._pool.get(host)` and
before `dest.parent.mkdir` / `dest.open("wb")`, so a rejected call opens no SSH
connection and never creates or truncates a local file.

- **Resolve subtlety:** the destination may not exist yet, but `resolve()` on a
  non-existent path still normalises it (Python 3.11+ no longer requires the path to
  exist). The downloads root is resolved the same way so the comparison is symlink-
  consistent on both sides (e.g. macOS `/var` → `/private/var`).
- **Alternative considered:** string `startswith` on the un-resolved paths. Rejected —
  it misses `..` traversal and symlinks, which is the whole point.

### 2. `allow_outside: bool = False` opt-in, surfaced on the tool
A new optional boolean on `TransferManager.get` and the `get` MCP tool input schema.
Default `False` preserves the safe behavior; an operator who genuinely wants to land a
file elsewhere passes `True`. The flag is the single audited escape hatch.

- **Why a per-call flag over a global config:** the decision is per-download
  ("yes, write this one to `/etc/...`"), not a standing posture. A flag keeps the
  choice at the call site and in the audit record. One extra optional boolean is a
  negligible addition to the tool description.
- **Alternative considered:** silently redirect out-of-tree paths into the downloads
  dir. Rejected — surprising, and hides operator intent.

### 3. Reuse `TransferDenied` and the existing rejection plumbing
`get`'s permission-denied path already constructs a `TransferDenied`, sets
`denied.audited = True`, writes a `status="rejected"` audit record, and re-raises;
the server boundary maps `TransferDenied` to an MCP error. The containment rejection
reuses exactly this shape — one audit record, no secret leakage, a message naming the
downloads root and the `allow_outside` opt-in. No new exception type, no new server
mapping.

## Risks / Trade-offs

- **[Behavioral break for existing out-of-tree `get` callers]** → Documented as a
  deliberate change in the proposal; the fix is a one-word `allow_outside=True`. The
  default and in-tree cases — the overwhelming majority — are untouched.
- **[`resolve()` touches the filesystem (symlink walk)]** → Negligible for a single
  destination path, and it runs once per `get` before the transfer. Worth it for
  correct `..`/symlink handling.
- **[Opt-in could become a habit the LLM sets reflexively]** → It is audited every
  time, and the default stays safe; an operator reviewing the log sees exactly when an
  out-of-tree write happened and why (via `reason`).

## Open Questions

- Should `allow_outside` writes additionally require the path to be absolute (reject
  ambiguous relative paths)? Leaning no — `resolve()` already anchors relative paths to
  cwd deterministically — but flag for review during implementation.
