"""SFTP put/get over the existing SSH connection pool.

`put` and `get` ride the SSH connection authenticated by ``ssh.py``; no
separate handshake. Both run as the SSH user — root-owned destinations
use the documented stage-and-move pattern (put to ``/tmp``, then
``session_run`` with ``sudo mv``).
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from pathlib import Path

from tai_mcp_ssh import paths
from tai_mcp_ssh.audit import AuditLog
from tai_mcp_ssh.ssh import ConnectionPool

_CHUNK_BYTES = 64 * 1024


@dataclass(frozen=True, slots=True)
class TransferResult:
    bytes: int
    sha256: str
    local_path: str
    remote_path: str


class TransferManager:
    """SFTP put/get wrapper with sha256 streaming and audit emission."""

    def __init__(self, pool: ConnectionPool, audit: AuditLog) -> None:
        self._pool = pool
        self._audit = audit

    async def put(
        self,
        host: str,
        local_path: str | os.PathLike[str],
        remote_path: str,
        *,
        reason: str | None = None,
    ) -> TransferResult:
        """Upload ``local_path`` to ``remote_path`` on ``host`` via SFTP."""
        local = Path(local_path).expanduser().resolve()
        if not local.is_file():
            raise FileNotFoundError(f"local path is not a regular file: {local}")

        # Stream the file through sha256 and SFTP in one pass.
        digest = hashlib.sha256()
        size = local.stat().st_size

        conn = await self._pool.get(host)
        async with (
            await conn.start_sftp() as sftp,
            sftp.open(remote_path, "wb") as remote_f,
        ):
            with local.open("rb") as local_f:
                while True:
                    chunk = local_f.read(_CHUNK_BYTES)
                    if not chunk:
                        break
                    digest.update(chunk)
                    await remote_f.write(chunk)

        sha = digest.hexdigest()
        await self._audit.record(
            "put",
            host=host,
            local_path=str(local),
            remote_path=remote_path,
            bytes=size,
            sha256=sha,
            reason=reason,
            status="done",
        )
        return TransferResult(
            bytes=size, sha256=sha, local_path=str(local), remote_path=remote_path
        )

    async def get(
        self,
        host: str,
        remote_path: str,
        local_path: str | os.PathLike[str] | None = None,
        *,
        reason: str | None = None,
    ) -> TransferResult:
        """Download ``remote_path`` from ``host`` via SFTP."""
        dest = (
            Path(local_path).expanduser()
            if local_path is not None
            else paths.downloads_dir(host) / Path(remote_path).name
        )
        dest.parent.mkdir(parents=True, exist_ok=True)

        digest = hashlib.sha256()
        size = 0

        conn = await self._pool.get(host)
        async with (
            await conn.start_sftp() as sftp,
            sftp.open(remote_path, "rb") as remote_f,
        ):
            with dest.open("wb") as local_f:
                while True:
                    chunk = await remote_f.read(_CHUNK_BYTES)
                    if not chunk:
                        break
                    # asyncssh returns str|bytes; SFTP binary mode yields bytes.
                    data = chunk if isinstance(chunk, bytes) else chunk.encode()
                    digest.update(data)
                    local_f.write(data)
                    size += len(data)

        sha = digest.hexdigest()
        await self._audit.record(
            "get",
            host=host,
            remote_path=remote_path,
            local_path=str(dest),
            bytes=size,
            sha256=sha,
            reason=reason,
            status="done",
        )
        return TransferResult(
            bytes=size,
            sha256=sha,
            local_path=str(dest),
            remote_path=remote_path,
        )
