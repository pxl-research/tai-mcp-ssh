"""Tests for ``tai_mcp_ssh.transfer``.

The :class:`TransferManager` is exercised against a fake SFTP backend
that records reads/writes in-memory.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import asyncssh
import pytest

from tai_mcp_ssh.audit import AuditLog
from tai_mcp_ssh.errors import HostNotAllowed, HostUnreachable, TransferDenied
from tai_mcp_ssh.transfer import TransferManager

# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


@dataclass
class FakeSFTPFile:
    """In-memory file-like that supports async read/write + async context.

    Set ``fail_on_io`` to inject a mid-transfer exception so tests can
    exercise the dead-connection branch in :class:`TransferManager`.
    """

    storage: FakeSFTPStorage
    path: str
    mode: str
    buffer: bytearray = field(default_factory=bytearray)
    _read_pos: int = 0
    fail_on_io: BaseException | None = None

    async def __aenter__(self) -> FakeSFTPFile:
        return self

    async def __aexit__(self, *_: Any) -> None:
        if "w" in self.mode:
            self.storage.files[self.path] = bytes(self.buffer)

    async def write(self, data: bytes) -> None:
        if self.fail_on_io is not None:
            raise self.fail_on_io
        self.buffer.extend(data)

    async def read(self, n: int = -1) -> bytes:
        if self.fail_on_io is not None:
            raise self.fail_on_io
        if "r" not in self.mode:
            raise OSError("not readable")
        data = self.storage.files.get(self.path, b"")
        if n < 0:
            chunk = data[self._read_pos :]
            self._read_pos = len(data)
        else:
            chunk = data[self._read_pos : self._read_pos + n]
            self._read_pos += len(chunk)
        return chunk


@dataclass
class FakeSFTPStorage:
    files: dict[str, bytes] = field(default_factory=dict)


class FakeSFTPClient:
    def __init__(
        self,
        storage: FakeSFTPStorage,
        *,
        fail_on_io: BaseException | None = None,
    ) -> None:
        self._storage = storage
        self._fail_on_io = fail_on_io

    async def __aenter__(self) -> FakeSFTPClient:
        return self

    async def __aexit__(self, *_: Any) -> None:
        return None

    def open(self, path: str, mode: str) -> FakeSFTPFile:
        if "r" in mode and path not in self._storage.files:
            raise FileNotFoundError(path)
        return FakeSFTPFile(
            storage=self._storage, path=path, mode=mode, fail_on_io=self._fail_on_io
        )


class FakeConnection:
    def __init__(
        self,
        storage: FakeSFTPStorage,
        *,
        fail_on_io: BaseException | None = None,
    ) -> None:
        self._storage = storage
        self._fail_on_io = fail_on_io
        self.dead = False

    async def start_sftp(self) -> FakeSFTPClient:
        return FakeSFTPClient(self._storage, fail_on_io=self._fail_on_io)

    def mark_dead(self) -> None:
        self.dead = True


class FakePool:
    def __init__(self, conns: dict[str, FakeConnection]) -> None:
        self._conns = conns

    async def get(self, alias: str) -> FakeConnection:
        if alias not in self._conns:
            raise HostNotAllowed(alias)
        return self._conns[alias]


def _audit_records(audit_root: Path, host: str) -> list[dict[str, Any]]:
    today = datetime.now(UTC).date().isoformat()
    f = audit_root / host / f"{today}.jsonl"
    if not f.exists():
        return []
    return [json.loads(line) for line in f.read_text().splitlines()]


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


async def test_put_uploads_bytes_and_records_sha256(tmp_path: Path) -> None:
    storage = FakeSFTPStorage()
    pool = FakePool({"pi": FakeConnection(storage)})
    audit = AuditLog(root=tmp_path)
    tm = TransferManager(pool, audit)  # type: ignore[arg-type]

    payload = b"hello world\n" * 1024
    local = tmp_path / "src.bin"
    local.write_bytes(payload)
    expected_sha = hashlib.sha256(payload).hexdigest()

    result = await tm.put("pi", local, "/tmp/dest.bin")
    assert result.bytes == len(payload)
    assert result.sha256 == expected_sha
    assert storage.files["/tmp/dest.bin"] == payload

    records = _audit_records(tmp_path, "pi")
    puts = [r for r in records if r["tool"] == "put"]
    assert puts and puts[0]["bytes"] == len(payload)
    assert puts[0]["sha256"] == expected_sha
    audit.close()


async def test_put_missing_local_file(tmp_path: Path) -> None:
    storage = FakeSFTPStorage()
    pool = FakePool({"pi": FakeConnection(storage)})
    audit = AuditLog(root=tmp_path)
    tm = TransferManager(pool, audit)  # type: ignore[arg-type]
    with pytest.raises(FileNotFoundError):
        await tm.put("pi", tmp_path / "nonexistent", "/tmp/x")
    audit.close()


async def test_get_downloads_bytes_and_records_sha256(tmp_path: Path) -> None:
    payload = b"goodbye world\n"
    storage = FakeSFTPStorage(files={"/var/log/x": payload})
    pool = FakePool({"pi": FakeConnection(storage)})
    audit = AuditLog(root=tmp_path)
    tm = TransferManager(pool, audit)  # type: ignore[arg-type]

    dest = tmp_path / "out.bin"
    result = await tm.get("pi", "/var/log/x", dest)
    assert dest.read_bytes() == payload
    assert result.sha256 == hashlib.sha256(payload).hexdigest()
    assert result.bytes == len(payload)
    audit.close()


async def test_get_with_default_local_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Redirect downloads_dir into our tmp_path so the default destination
    # lands somewhere we can inspect.
    monkeypatch.setattr(
        "tai_mcp_ssh.transfer.paths.downloads_dir",
        lambda host: tmp_path / "downloads" / host,
    )
    payload = b"abc"
    storage = FakeSFTPStorage(files={"/etc/hostname": payload})
    pool = FakePool({"pi": FakeConnection(storage)})
    audit = AuditLog(root=tmp_path)
    tm = TransferManager(pool, audit)  # type: ignore[arg-type]

    result = await tm.get("pi", "/etc/hostname")
    assert Path(result.local_path) == tmp_path / "downloads" / "pi" / "hostname"
    assert Path(result.local_path).read_bytes() == payload
    audit.close()


class _FailingSFTPClient:
    """SFTPClient stand-in that raises a configurable SFTPError on open()."""

    def __init__(self, code: int, message: str) -> None:
        self._code = code
        self._message = message

    async def __aenter__(self) -> _FailingSFTPClient:
        return self

    async def __aexit__(self, *_: Any) -> None:
        return None

    def open(self, _path: str, _mode: str) -> FakeSFTPFile:
        raise asyncssh.SFTPError(self._code, self._message)


class _FailingConnection:
    def __init__(self, code: int = 3, message: str = "Permission denied") -> None:
        self._code = code
        self._message = message

    async def start_sftp(self) -> _FailingSFTPClient:
        return _FailingSFTPClient(self._code, self._message)


# Back-compat aliases for the permission-denied tests below.
_DenyingConnection = _FailingConnection


async def test_put_permission_denied_raises_transfer_denied(tmp_path: Path) -> None:
    pool = FakePool({"pi": _DenyingConnection()})  # type: ignore[dict-item]
    audit = AuditLog(root=tmp_path)
    tm = TransferManager(pool, audit)  # type: ignore[arg-type]
    local = tmp_path / "x"
    local.write_text("y")
    with pytest.raises(TransferDenied, match="stage-and-move") as info:
        await tm.put("pi", local, "/etc/nginx/nginx.conf", reason="rotate-creds")
    # The exception carries the `audited` marker so the server boundary
    # skips a second audit record (single-audit invariant).
    assert getattr(info.value, "audited", False) is True
    # The rejected attempt is still audited exactly once, with the
    # caller-supplied reason preserved (parity with successful puts).
    records = _audit_records(tmp_path, "pi")
    rej = [r for r in records if r["tool"] == "put" and r["status"] == "rejected"]
    assert len(rej) == 1
    assert rej[0]["reason"] == "rotate-creds"
    audit.close()


async def test_get_permission_denied_raises_transfer_denied(tmp_path: Path) -> None:
    pool = FakePool({"pi": _DenyingConnection()})  # type: ignore[dict-item]
    audit = AuditLog(root=tmp_path)
    tm = TransferManager(pool, audit)  # type: ignore[arg-type]
    with pytest.raises(TransferDenied, match="cannot read") as info:
        await tm.get("pi", "/etc/shadow", tmp_path / "out", reason="audit-check")
    assert getattr(info.value, "audited", False) is True
    records = _audit_records(tmp_path, "pi")
    rej = [r for r in records if r["tool"] == "get" and r["status"] == "rejected"]
    assert len(rej) == 1
    assert rej[0]["reason"] == "audit-check"
    audit.close()


async def test_put_transport_dead_mid_write_raises_host_unreachable(tmp_path: Path) -> None:
    # `Connection.start_sftp` is wrapped at the connection boundary,
    # but `remote_f.write` is not — so we map dead-transport errors
    # mid-write to HostUnreachable and mark the conn dead so the pool
    # evicts it on the next get().
    storage = FakeSFTPStorage()
    conn = FakeConnection(storage, fail_on_io=ConnectionResetError("peer reset"))
    pool = FakePool({"pi": conn})
    audit = AuditLog(root=tmp_path)
    tm = TransferManager(pool, audit)  # type: ignore[arg-type]
    local = tmp_path / "x"
    local.write_bytes(b"hello")
    with pytest.raises(HostUnreachable):
        await tm.put("pi", local, "/tmp/dest")
    assert conn.dead is True
    audit.close()


async def test_get_transport_dead_mid_read_raises_host_unreachable(tmp_path: Path) -> None:
    storage = FakeSFTPStorage(files={"/var/log/x": b"abc"})
    conn = FakeConnection(storage, fail_on_io=ConnectionResetError("peer reset"))
    pool = FakePool({"pi": conn})
    audit = AuditLog(root=tmp_path)
    tm = TransferManager(pool, audit)  # type: ignore[arg-type]
    with pytest.raises(HostUnreachable):
        await tm.get("pi", "/var/log/x", tmp_path / "out")
    assert conn.dead is True
    audit.close()


async def test_put_non_permission_sftp_error_propagates(tmp_path: Path) -> None:
    # Disk full, missing parent, protocol error, etc. must NOT be mapped to
    # TransferDenied (the stage-and-move hint would be wrong) and must NOT
    # write a local "rejected" audit record — the server boundary records
    # them at status=error instead.
    pool = FakePool({"pi": _FailingConnection(code=4, message="Failure")})  # type: ignore[dict-item]
    audit = AuditLog(root=tmp_path)
    tm = TransferManager(pool, audit)  # type: ignore[arg-type]
    local = tmp_path / "x"
    local.write_text("y")
    with pytest.raises(asyncssh.SFTPError):
        await tm.put("pi", local, "/nope/dest")
    records = _audit_records(tmp_path, "pi")
    assert not [r for r in records if r["tool"] == "put"]
    audit.close()


async def test_get_non_permission_sftp_error_propagates(tmp_path: Path) -> None:
    pool = FakePool({"pi": _FailingConnection(code=4, message="Failure")})  # type: ignore[dict-item]
    audit = AuditLog(root=tmp_path)
    tm = TransferManager(pool, audit)  # type: ignore[arg-type]
    with pytest.raises(asyncssh.SFTPError):
        await tm.get("pi", "/nope/file", tmp_path / "out")
    records = _audit_records(tmp_path, "pi")
    assert not [r for r in records if r["tool"] == "get"]
    audit.close()


async def test_put_to_unknown_host_rejected(tmp_path: Path) -> None:
    pool = FakePool({})
    audit = AuditLog(root=tmp_path)
    tm = TransferManager(pool, audit)  # type: ignore[arg-type]
    local = tmp_path / "x"
    local.write_text("y")
    with pytest.raises(HostNotAllowed):
        await tm.put("ghost", local, "/tmp/x")
    audit.close()


async def test_get_to_unknown_host_rejected(tmp_path: Path) -> None:
    pool = FakePool({})
    audit = AuditLog(root=tmp_path)
    tm = TransferManager(pool, audit)  # type: ignore[arg-type]
    with pytest.raises(HostNotAllowed):
        await tm.get("ghost", "/etc/hostname", tmp_path / "out")
    audit.close()
