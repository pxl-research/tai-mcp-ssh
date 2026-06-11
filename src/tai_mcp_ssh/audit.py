"""Append-only JSONL audit log.

Layout: ``<audit_dir>/<host>/<UTC-date>.jsonl``

* One folder per managed host (alias). Non-host events go to ``_system/``.
* One file per UTC calendar day. Day rollover is detected on every write,
  so a long-running process switches files at midnight without restart.
* Per-host :class:`asyncio.Lock` serialises writes within a host;
  writes against different hosts run in parallel.
* Retention sweep on startup deletes daily files older than the
  configured threshold (default 90 days).
"""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, TextIO

from tai_mcp_ssh import paths

SYSTEM_HOST = "_system"
DEFAULT_RETENTION_DAYS = 90

# Field names whose values are scrubbed before writing to disk. Cheap
# safety net for the case where calling code accidentally forwards a
# resolved keychain value through kwargs.
SECRET_KEYS: frozenset[str] = frozenset(
    {
        "password",
        "passwd",
        "secret",
        "token",
        "private_key",
        "passphrase",
        "resolved_password",
    }
)

_DATE_FILE_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})\.jsonl$")

# Reserved fields auto-filled by record(); callers may not override.
_RESERVED: frozenset[str] = frozenset({"ts", "tool", "host"})

# Best-effort scrubbing of secrets that an LLM might embed in a `cmd` string.
# Deliberately limited to two HIGH-CONFIDENCE forms to avoid corrupting
# innocent commands: the long-form `--password=VALUE` / `--password VALUE`
# flag and `keychain://` references. Ambiguous short flags like `-p` are NOT
# matched (they collide with `cp -p`, `mkdir -p`, `ssh -p`, ...). The real
# defense remains NOPASSWD sudoers + keychain, not this net.
_CMD_SECRET_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"(--password[=\s])(\S+)"),
    re.compile(r"(keychain://)(\S+)"),
]


def _redact_command(cmd: str) -> str:
    """Mask the value of known secret-bearing forms in a command string."""
    for pattern in _CMD_SECRET_PATTERNS:
        cmd = pattern.sub(r"\1<redacted>", cmd)
    return cmd


@dataclass(slots=True)
class _OpenFile:
    handle: TextIO
    iso_date: str


@dataclass(slots=True)
class _HostState:
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    file: _OpenFile | None = None


class AuditLog:
    """Asynchronous per-host JSONL writer.

    Construct one instance per process and reuse it. ``record()`` is
    safe to call from any task; concurrent calls against the same host
    serialise, calls against different hosts parallelise.
    """

    def __init__(
        self,
        root: Path | None = None,
        *,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._root = root or paths.audit_dir()
        # Injectable clock for deterministic testing of UTC rollover.
        self._now: Callable[[], datetime] = now or (lambda: datetime.now(UTC))
        self._states: dict[str, _HostState] = {}

    async def record(self, tool: str, host: str | None = None, **fields: Any) -> None:
        """Append one audit record. ``host=None`` routes to ``_system``."""
        h = host or SYSTEM_HOST
        state = self._states.setdefault(h, _HostState())
        record = self._build_record(tool, h, fields)
        async with state.lock:
            await asyncio.to_thread(self._write_locked, state, h, record)

    async def sweep_retention(self, retention_days: int = DEFAULT_RETENTION_DAYS) -> None:
        """Delete daily files older than ``retention_days`` across all host folders.

        Best-effort: filesystem errors are captured and reported in the
        summary record; never raised.
        """
        deleted, errors = await asyncio.to_thread(self._sweep_files_sync, retention_days)
        if deleted or errors:
            await self.record(
                "_sweep",
                host=SYSTEM_HOST,
                deleted_by_host=deleted,
                errors=errors,
                retention_days=retention_days,
            )

    def close(self) -> None:
        """Close every cached file handle. Call once at shutdown."""
        for state in self._states.values():
            if state.file is not None:
                state.file.handle.close()
                state.file = None

    # Internals -------------------------------------------------------------

    def _build_record(self, tool: str, host: str, fields: dict[str, Any]) -> dict[str, Any]:
        ts = self._now().isoformat(timespec="milliseconds").replace("+00:00", "Z")
        record: dict[str, Any] = {"ts": ts, "tool": tool, "host": host}
        for k, v in fields.items():
            if k in _RESERVED:
                continue
            if k in SECRET_KEYS:
                record[k] = "<redacted>"
            elif k == "cmd" and isinstance(v, str):
                record[k] = _redact_command(v)
            else:
                record[k] = v
        return record

    def _write_locked(self, state: _HostState, host: str, record: dict[str, Any]) -> None:
        today = self._now().date().isoformat()
        if state.file is None or state.file.iso_date != today:
            if state.file is not None:
                state.file.handle.close()
            host_dir = self._root / host
            host_dir.mkdir(parents=True, mode=0o700, exist_ok=True)
            handle = (host_dir / f"{today}.jsonl").open("a", encoding="utf-8")
            state.file = _OpenFile(handle=handle, iso_date=today)

        line = json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n"
        state.file.handle.write(line)
        state.file.handle.flush()

    def _sweep_files_sync(self, retention_days: int) -> tuple[dict[str, int], list[str]]:
        deleted_by_host: dict[str, int] = {}
        errors: list[str] = []
        if not self._root.exists():
            return deleted_by_host, errors

        cutoff_ordinal = self._now().date().toordinal() - retention_days

        for host_dir in self._root.iterdir():
            if not host_dir.is_dir():
                continue
            count = 0
            for f in host_dir.iterdir():
                if not f.is_file():
                    continue
                m = _DATE_FILE_RE.match(f.name)
                if not m:
                    continue
                try:
                    file_date = date.fromisoformat(m.group(1))
                except ValueError:
                    continue
                if file_date.toordinal() < cutoff_ordinal:
                    try:
                        f.unlink()
                        count += 1
                    except OSError as exc:
                        errors.append(f"{f.name}: {exc}")
            if count:
                deleted_by_host[host_dir.name] = count

        return deleted_by_host, errors
