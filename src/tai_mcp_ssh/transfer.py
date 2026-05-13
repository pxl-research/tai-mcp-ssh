"""SFTP put/get over the existing SSH connection pool.

`put` and `get` ride the SSH connection authenticated by ``ssh.py``; no
separate handshake. Both run as the SSH user — root-owned destinations
use the documented stage-and-move pattern (put to ``/tmp``, then
``session_run`` with ``sudo mv``).
"""

from __future__ import annotations

import hashlib
import os
import shlex
from dataclasses import dataclass
from pathlib import Path

import asyncssh

from tai_mcp_ssh import paths
from tai_mcp_ssh.audit import AuditLog
from tai_mcp_ssh.errors import TransferDenied
from tai_mcp_ssh.ssh import ConnectionPool

_CHUNK_BYTES = 64 * 1024

# asyncssh exposes SFTP status codes as integers on SFTPError.code.
# 3 is FX_PERMISSION_DENIED per RFC draft-ietf-secsh-filexfer.
_SFTP_PERMISSION_DENIED = 3


def _stage_and_move_hint(host: str, remote_path: str) -> str:
    """One-line recipe shown when a `put` to a write-protected destination fails."""
    name = Path(remote_path).name
    staging = f"/tmp/{name}"
    # shlex.quote on the shell paths; !r on the outer Python literals so
    # Python chooses non-colliding quotes if either path contains '.
    shell_cmd = f"sudo mv {shlex.quote(staging)} {shlex.quote(remote_path)}"
    return (
        f"the SSH user cannot write {host}:{remote_path}. "
        f"Use stage-and-move: put({host!r}, <local>, {staging!r}) "
        f"then session_run({host + '/default'!r}, {shell_cmd!r})."
    )


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
        try:
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
        except asyncssh.SFTPError as exc:
            if exc.code != _SFTP_PERMISSION_DENIED:
                # Not a permissions issue — let the server boundary audit it
                # with status=error and surface the real cause to the caller.
                raise
            await self._audit.record(
                "put",
                host=host,
                local_path=str(local),
                remote_path=remote_path,
                status="rejected",
                error=str(exc),
            )
            denied = TransferDenied(_stage_and_move_hint(host, remote_path))
            denied.audited = True  # type: ignore[attr-defined]
            raise denied from exc

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
        try:
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
        except asyncssh.SFTPError as exc:
            if exc.code != _SFTP_PERMISSION_DENIED:
                raise
            await self._audit.record(
                "get",
                host=host,
                remote_path=remote_path,
                local_path=str(dest),
                status="rejected",
                error=str(exc),
            )
            shell_cmd = f"sudo cat {shlex.quote(remote_path)} > /tmp/..."
            denied = TransferDenied(
                f"the SSH user cannot read {host}:{remote_path}. "
                f"Either ensure read permissions or stage it via "
                f"session_run({host + '/default'!r}, {shell_cmd!r}) "
                f"first and `get` from /tmp."
            )
            denied.audited = True  # type: ignore[attr-defined]
            raise denied from exc

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
