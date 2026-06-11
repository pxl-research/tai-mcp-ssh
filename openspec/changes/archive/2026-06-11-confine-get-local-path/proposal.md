## Why

The `get` MCP tool writes a downloaded remote file to whatever local path the LLM
supplies, via `Path(local_path).expanduser()` in `TransferManager.get`
(`src/tai_mcp_ssh/transfer.py`), with no containment check. Because the MCP runs
with the operator's privileges, this is an arbitrary local-file **overwrite**
primitive: a downloaded file (whose contents the remote host controls) can be
written over `~/.ssh/authorized_keys`, `~/.bashrc`, a crontab, or any other file the
operator can write. A code review flagged this as the one finding that changes the
behavioral contract rather than a straight bug fix, so it goes through the proposal
flow instead of being patched silently.

## What Changes

- `get()` resolves the destination and requires it to live under
  `paths.downloads_dir()` by default. A resolved path outside that tree is rejected
  with a `TransferDenied` error pointing at the opt-in — **before** any SSH
  connection is opened or bytes are written.
- A new optional `allow_outside: bool = False` parameter (surfaced on the `get` MCP
  tool and `TransferManager.get`) lets the operator deliberately write outside the
  downloads tree when they mean to. The rejection and the opt-in are both audited.
- **BREAKING (behavioral):** existing `get()` calls that pass an absolute or
  otherwise out-of-tree `local_path` and rely on it being written start failing
  unless `allow_outside=True` is set. The common cases are unaffected: omitting
  `local_path` (defaults under `downloads_dir(host)/<basename>`) and passing a path
  that already resolves inside the downloads tree both behave exactly as today.

## Capabilities

### New Capabilities
<!-- none -->

### Modified Capabilities
- `file-transfer`: adds a requirement that `get()` confines its local destination to
  the downloads directory by default and only writes outside it under an explicit
  opt-in. `put` is unchanged (reading local files is inherent to upload and is not in
  scope here).

## Impact

- Code: `src/tai_mcp_ssh/transfer.py` (`TransferManager.get` — resolve + containment
  check + `allow_outside` param); `src/tai_mcp_ssh/server.py` (`get` tool input schema
  gains an optional `allow_outside` boolean and the dispatch passes it through). No
  change to `put`, sessions, or the connection pool.
- Reuse: `paths.downloads_dir()` already computes the confinement root;
  `TransferDenied` already exists in `errors.py` and is already mapped to a rejected
  audit record + MCP error at the server boundary.
- Tests: extend `tests/unit/test_transfer.py` using the existing
  `FakeSFTPClient`/`FakePool` fakes — out-of-tree path rejected without opt-in,
  allowed with `allow_outside=True`, and the default / in-tree paths unchanged.
- Public surface: the `get` return shape is unchanged; only a new optional input
  field and a new rejection path are added. `hosts.toml`, dependencies, and on-disk
  layout are untouched.
