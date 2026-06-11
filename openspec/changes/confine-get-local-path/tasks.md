## 1. Containment in TransferManager.get

- [x] 1.1 Add `allow_outside: bool = False` (keyword-only) to `TransferManager.get` in `src/tai_mcp_ssh/transfer.py`.
- [x] 1.2 At the top of `get`, compute `dest` as today, then resolve both `dest` and `paths.downloads_dir()` with `Path.resolve()` and check `dest.is_relative_to(root)`. Do this **before** `self._pool.get(host)` and before any `mkdir` / file open.
- [x] 1.3 On an out-of-tree `dest` with `allow_outside` false, write a `status="rejected"` audit record (host, remote_path, resolved local_path, reason), then raise a `TransferDenied` whose message names the downloads root and the `allow_outside` opt-in; set `denied.audited = True` so the server boundary doesn't double-audit (mirror the existing permission-denied branch).
- [x] 1.4 When `allow_outside` is true and the path is out-of-tree, proceed with the transfer and record in the success audit that the write was outside the downloads tree (e.g. an `outside=true` field).

## 2. Wire through the MCP tool

- [x] 2.1 Add an optional `allow_outside` boolean to the `get` tool `inputSchema` in `tool_specs()` (`src/tai_mcp_ssh/server.py`), with a one-line description; keep it token-cheap.
- [x] 2.2 In `dispatch()`, pass `allow_outside=args.get("allow_outside", False)` through to `svc.transfer.get(...)`.

## 3. Tests

- [x] 3.1 `tests/unit/test_transfer.py` (reuse `FakeSFTPClient`/`FakePool`): default `local_path` (omitted) downloads under `downloads_dir(host)` — unchanged behavior.
- [x] 3.2 An explicit `local_path` that resolves inside the downloads tree is allowed and reported back.
- [x] 3.3 An out-of-tree `local_path` without opt-in raises `TransferDenied`, writes one `rejected` audit record, opens no SFTP connection, and creates no local file.
- [x] 3.4 The same out-of-tree path with `allow_outside=True` succeeds and is audited as an outside write.
- [x] 3.5 Traversal/symlink guard: `downloads_dir()/../escape` is rejected (proves `resolve()` is used, not a string prefix check).
- [x] 3.6 `tests/unit/test_server.py`: a `get` dispatch forwards `allow_outside` to the transfer manager.

## 4. Docs, lint, validate

- [x] 4.1 README: note that `get` writes under the downloads dir by default and that out-of-tree destinations need `allow_outside`.
- [x] 4.2 `uv run pytest -q` (coverage stays >= 80%), `uv run ruff check src tests`, `uv run mypy src`, `uv run pre-commit run --all-files` — all clean.
- [x] 4.3 `uv run openspec validate confine-get-local-path --strict` passes.

## 5. Wrap up

- [ ] 5.1 Commit on this feature branch with a `FEAT:` message referencing the review finding.
- [ ] 5.2 Open PR; on merge, `/opsx:archive confine-get-local-path` to sync the file-transfer delta into the canonical specs.
