"""Tests for ``tai_mcp_ssh.ssh``.

asyncssh is faked in-process so we exercise the pool's wiring without
opening real sockets.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import keyring.errors
import pytest

from tai_mcp_ssh.audit import AuditLog
from tai_mcp_ssh.config import Host
from tai_mcp_ssh.errors import (
    HostNotAllowed,
    HostUnreachable,
    KeychainUnavailable,
    TmuxMissing,
)
from tai_mcp_ssh.ssh import ConnectionPool

# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


@dataclass
class FakeProcess:
    exit_status: int = 0
    stdout: str = ""
    stderr: str = ""


@dataclass
class FakeSSH:
    run_handler: Callable[[str], FakeProcess] = field(
        default_factory=lambda: lambda _cmd: FakeProcess()
    )
    run_calls: list[str] = field(default_factory=list)
    closed: bool = False

    async def run(self, command: str, *, check: bool = False, **_: Any) -> FakeProcess:
        self.run_calls.append(command)
        return self.run_handler(command)

    async def start_sftp_client(self) -> Any:
        return object()

    def close(self) -> None:
        self.closed = True

    async def wait_closed(self) -> None:
        return None


@dataclass
class FakeConnectFactory:
    """Records connect() kwargs and hands back FakeSSH instances."""

    run_handler: Callable[[str], FakeProcess] | None = None
    connect_calls: list[dict[str, Any]] = field(default_factory=list)
    connections: list[FakeSSH] = field(default_factory=list)

    async def __call__(self, **kwargs: Any) -> Any:
        self.connect_calls.append(kwargs)
        conn = FakeSSH(run_handler=self.run_handler or (lambda _cmd: FakeProcess()))
        self.connections.append(conn)
        return conn


def _default_handler(command: str) -> FakeProcess:
    """Handler that satisfies _ensure_ready for a healthy remote."""
    if command == "command -v tmux":
        return FakeProcess(0, "/usr/bin/tmux\n", "")
    if command == "echo $HOME":
        return FakeProcess(0, "/home/pi\n", "")
    if command.startswith("mkdir -p -m 0700"):
        return FakeProcess(0, "", "")
    if "find " in command and "-delete -print" in command:
        return FakeProcess(0, "", "")  # nothing to sweep
    return FakeProcess(0, "", "")


async def _flush_background_tasks() -> None:
    """Let create_task'd sweeps run to completion before we assert audits."""
    # asyncio.sleep(0) yields the loop; several yields lets the sweep
    # run its single conn.run() + record() chain.
    for _ in range(5):
        await asyncio.sleep(0)


def _audit_records(audit_root: Path, host: str) -> list[dict[str, Any]]:
    today = datetime.now(UTC).date().isoformat()
    f = audit_root / host / f"{today}.jsonl"
    if not f.exists():
        return []
    return [json.loads(line) for line in f.read_text().splitlines()]


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


async def test_get_unknown_alias_rejected(tmp_path: Path) -> None:
    audit = AuditLog(root=tmp_path)
    pool = ConnectionPool(hosts={}, audit=audit, connect=FakeConnectFactory())
    with pytest.raises(HostNotAllowed):
        await pool.get("nonexistent")
    audit.close()


async def test_get_opens_and_caches(tmp_path: Path) -> None:
    audit = AuditLog(root=tmp_path)
    factory = FakeConnectFactory(run_handler=_default_handler)
    pool = ConnectionPool(
        hosts={"pi": Host(alias="pi", host="192.168.1.42")},
        audit=audit,
        connect=factory,
    )

    a = await pool.get("pi")
    b = await pool.get("pi")
    assert a is b
    assert len(factory.connect_calls) == 1  # only one connect
    await pool.close_all()
    audit.close()


async def test_key_auth_connect_kwargs(tmp_path: Path) -> None:
    audit = AuditLog(root=tmp_path)
    factory = FakeConnectFactory(run_handler=_default_handler)
    ssh_config = tmp_path / "ssh_config"
    ssh_config.touch()  # gated on existence by `_build_connect_kwargs`
    pool = ConnectionPool(
        hosts={
            "pi": Host(
                alias="pi",
                host="192.168.1.42",
                user="pi",
                port=2222,
                auth="key",
                identity_file="~/.ssh/pi_ed25519",
            )
        },
        audit=audit,
        connect=factory,
        ssh_config=ssh_config,
    )
    await pool.get("pi")
    kw = factory.connect_calls[0]
    assert kw["host"] == "192.168.1.42"
    assert kw["username"] == "pi"
    assert kw["port"] == 2222
    assert kw["client_keys"] == ["~/.ssh/pi_ed25519"]
    assert "password" not in kw
    assert kw["config"] == [str(ssh_config)]
    await pool.close_all()
    audit.close()


async def test_keepalive_kwargs_sent_to_asyncssh(tmp_path: Path) -> None:
    # Regression for #6: surface a dead transport within ~90s (3 × 30s)
    # so the pool's eviction path runs instead of hanging on kernel TCP
    # timeout.
    audit = AuditLog(root=tmp_path)
    factory = FakeConnectFactory(run_handler=_default_handler)
    pool = ConnectionPool(
        hosts={"pi": Host(alias="pi")},
        audit=audit,
        connect=factory,
    )
    await pool.get("pi")
    kw = factory.connect_calls[0]
    assert kw["keepalive_interval"] == 30
    assert kw["keepalive_count_max"] == 3
    await pool.close_all()
    audit.close()


async def test_missing_ssh_config_is_tolerated(tmp_path: Path) -> None:
    # Regression for #3: a non-existent ssh_config must not crash connect;
    # we skip the kwarg and let asyncssh use its own defaults.
    audit = AuditLog(root=tmp_path)
    factory = FakeConnectFactory(run_handler=_default_handler)
    pool = ConnectionPool(
        hosts={"pi": Host(alias="pi")},
        audit=audit,
        connect=factory,
        ssh_config=tmp_path / "does-not-exist",
    )
    await pool.get("pi")
    kw = factory.connect_calls[0]
    assert "config" not in kw
    await pool.close_all()
    audit.close()


async def test_key_auth_without_identity_file_omits_client_keys(
    tmp_path: Path,
) -> None:
    audit = AuditLog(root=tmp_path)
    factory = FakeConnectFactory(run_handler=_default_handler)
    pool = ConnectionPool(
        hosts={"pi": Host(alias="pi")},  # delegates everything to ssh_config
        audit=audit,
        connect=factory,
    )
    await pool.get("pi")
    kw = factory.connect_calls[0]
    # When no identity_file, we let asyncssh pick (agent + standard keys).
    assert "client_keys" not in kw
    # Alias is passed as host so ssh_config can resolve HostName.
    assert kw["host"] == "pi"
    await pool.close_all()
    audit.close()


async def test_password_auth_resolves_keyring_and_disables_keys(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "tai_mcp_ssh.ssh.keyring.get_password",
        lambda service, account: "s3cret",
    )

    audit = AuditLog(root=tmp_path)
    factory = FakeConnectFactory(run_handler=_default_handler)
    pool = ConnectionPool(
        hosts={
            "vps": Host(
                alias="vps",
                host="1.2.3.4",
                user="admin",
                auth="password",
                password_ref="keychain://tai-mcp-ssh/vps",
            )
        },
        audit=audit,
        connect=factory,
    )
    await pool.get("vps")
    kw = factory.connect_calls[0]
    assert kw["password"] == "s3cret"
    assert kw["client_keys"] == ()
    assert kw.get("gss_auth") is False
    await pool.close_all()
    audit.close()


async def test_password_missing_in_keyring_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "tai_mcp_ssh.ssh.keyring.get_password",
        lambda service, account: None,
    )

    audit = AuditLog(root=tmp_path)
    pool = ConnectionPool(
        hosts={
            "vps": Host(
                alias="vps",
                host="1.2.3.4",
                auth="password",
                password_ref="keychain://tai-mcp-ssh/vps",
            )
        },
        audit=audit,
        connect=FakeConnectFactory(),
    )
    with pytest.raises(KeychainUnavailable, match="no entry"):
        await pool.get("vps")
    audit.close()


async def test_keyring_error_raises_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _raise(*_: Any, **__: Any) -> None:
        raise keyring.errors.KeyringError("no backend")

    monkeypatch.setattr("tai_mcp_ssh.ssh.keyring.get_password", _raise)

    audit = AuditLog(root=tmp_path)
    pool = ConnectionPool(
        hosts={
            "vps": Host(
                alias="vps",
                host="1.2.3.4",
                auth="password",
                password_ref="keychain://tai-mcp-ssh/vps",
            )
        },
        audit=audit,
        connect=FakeConnectFactory(),
    )
    with pytest.raises(KeychainUnavailable, match="keychain access failed"):
        await pool.get("vps")
    audit.close()


async def test_ensure_ready_runs_tmux_check_and_mkdir(tmp_path: Path) -> None:
    audit = AuditLog(root=tmp_path)
    factory = FakeConnectFactory(run_handler=_default_handler)
    pool = ConnectionPool(
        hosts={"pi": Host(alias="pi", host="h")},
        audit=audit,
        connect=factory,
    )
    conn = await pool.get("pi")
    fake = factory.connections[0]
    assert "command -v tmux" in fake.run_calls
    assert "echo $HOME" in fake.run_calls
    assert any(c.startswith("mkdir -p -m 0700") for c in fake.run_calls)
    assert conn.tmux_path == "/usr/bin/tmux"
    assert conn.home_dir == "/home/pi"
    await _flush_background_tasks()
    records = _audit_records(tmp_path, "pi")
    tools = {r["tool"] for r in records}
    assert "_tmux_check" in tools
    assert "_logdir_check" in tools
    await pool.close_all()
    audit.close()


async def test_tmux_missing_raises_and_audits(tmp_path: Path) -> None:
    def handler(command: str) -> FakeProcess:
        if command == "command -v tmux":
            return FakeProcess(exit_status=1, stdout="", stderr="not found")
        return FakeProcess()

    audit = AuditLog(root=tmp_path)
    factory = FakeConnectFactory(run_handler=handler)
    pool = ConnectionPool(
        hosts={"pi": Host(alias="pi", host="h")},
        audit=audit,
        connect=factory,
    )
    with pytest.raises(TmuxMissing):
        await pool.get("pi")
    records = _audit_records(tmp_path, "pi")
    missing = [r for r in records if r["tool"] == "_tmux_check"]
    assert missing and missing[0]["status"] == "missing"
    audit.close()


async def test_remote_sweep_records_deleted_count(tmp_path: Path) -> None:
    def handler(command: str) -> FakeProcess:
        if command == "command -v tmux":
            return FakeProcess(0, "/usr/bin/tmux\n", "")
        if command == "echo $HOME":
            return FakeProcess(0, "/home/pi\n", "")
        if command.startswith("mkdir"):
            return FakeProcess(0, "", "")
        if "find " in command:
            assert "-mtime +7" in command  # default retention
            return FakeProcess(0, "old1.log\nold2.log\nold3.log\n", "")
        return FakeProcess()

    audit = AuditLog(root=tmp_path)
    factory = FakeConnectFactory(run_handler=handler)
    pool = ConnectionPool(
        hosts={"pi": Host(alias="pi", host="h")},
        audit=audit,
        connect=factory,
    )
    await pool.get("pi")
    await _flush_background_tasks()
    records = _audit_records(tmp_path, "pi")
    sweeps = [r for r in records if r["tool"] == "_remote_sweep"]
    assert sweeps and sweeps[0]["deleted"] == 3
    assert sweeps[0]["retention_days"] == 7
    assert sweeps[0]["status"] == "ok"
    await pool.close_all()
    audit.close()


async def test_remote_sweep_uses_custom_retention(tmp_path: Path) -> None:
    def handler(command: str) -> FakeProcess:
        if command == "command -v tmux":
            return FakeProcess(0, "/usr/bin/tmux\n", "")
        if command == "echo $HOME":
            return FakeProcess(0, "/home/pi\n", "")
        if command.startswith("mkdir"):
            return FakeProcess()
        if "find " in command:
            assert "-mtime +30" in command
            return FakeProcess(0, "", "")
        return FakeProcess()

    audit = AuditLog(root=tmp_path)
    factory = FakeConnectFactory(run_handler=handler)
    pool = ConnectionPool(
        hosts={"pi": Host(alias="pi", host="h", log_retention_days=30)},
        audit=audit,
        connect=factory,
    )
    await pool.get("pi")
    await _flush_background_tasks()
    await pool.close_all()
    audit.close()


async def test_dead_transport_raises_host_unreachable_and_evicts(tmp_path: Path) -> None:
    """After a transport-dead error, the conn is marked dead and dropped on next get()."""
    audit = AuditLog(root=tmp_path)

    call_count = {"n": 0}

    def handler(command: str) -> FakeProcess:
        if command == "command -v tmux":
            return FakeProcess(0, "/usr/bin/tmux\n", "")
        if command == "echo $HOME":
            return FakeProcess(0, "/home/pi\n", "")
        if command.startswith("mkdir -p -m 0700") or "-delete -print" in command:
            return FakeProcess(0, "", "")
        # The first "real" run after _ensure_ready raises a transport-dead error.
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise ConnectionResetError("peer rebooted")
        return FakeProcess(0, "", "")

    factory = FakeConnectFactory(run_handler=handler)
    pool = ConnectionPool(
        hosts={"pi": Host(alias="pi", host="192.168.1.42")},
        audit=audit,
        connect=factory,
    )

    conn = await pool.get("pi")
    with pytest.raises(HostUnreachable):
        await conn.run("anything")
    assert conn.dead is True

    # Next get() must evict the dead conn and open a fresh one.
    conn2 = await pool.get("pi")
    assert conn2 is not conn
    assert conn2.dead is False
    # Sanity: a follow-up call on the fresh conn works.
    result = await conn2.run("anything")
    assert result.exit_status == 0
    audit.close()


async def test_open_failure_raises_host_unreachable(tmp_path: Path) -> None:
    """Initial connect() failure surfaces as HostUnreachable, not raw OSError."""
    audit = AuditLog(root=tmp_path)

    async def bad_connect(**_: Any) -> Any:
        raise OSError("network unreachable")

    pool = ConnectionPool(
        hosts={"pi": Host(alias="pi", host="192.168.1.42")},
        audit=audit,
        connect=bad_connect,
    )
    with pytest.raises(HostUnreachable, match="network unreachable"):
        await pool.get("pi")
    audit.close()


async def test_ensure_ready_failure_evicts_from_cache(tmp_path: Path) -> None:
    """If bootstrap dies mid-handshake, the cache must not retain the half-open conn."""
    audit = AuditLog(root=tmp_path)

    def handler(command: str) -> FakeProcess:
        # _ensure_ready's first call is `command -v tmux`; blow up there.
        if command == "command -v tmux":
            raise ConnectionResetError("peer rebooted")
        return FakeProcess(0, "", "")

    factory = FakeConnectFactory(run_handler=handler)
    pool = ConnectionPool(
        hosts={"pi": Host(alias="pi", host="192.168.1.42")},
        audit=audit,
        connect=factory,
    )
    with pytest.raises(HostUnreachable):
        await pool.get("pi")
    # Second attempt must build a brand-new connection (factory called twice).
    factory.run_handler = _default_handler
    await pool.get("pi")
    assert len(factory.connect_calls) == 2
    audit.close()


async def test_close_all_closes_underlying_connections(tmp_path: Path) -> None:
    audit = AuditLog(root=tmp_path)
    factory = FakeConnectFactory(run_handler=_default_handler)
    pool = ConnectionPool(
        hosts={
            "a": Host(alias="a", host="ha"),
            "b": Host(alias="b", host="hb"),
        },
        audit=audit,
        connect=factory,
    )
    await pool.get("a")
    await pool.get("b")
    await pool.close_all()
    assert all(c.closed for c in factory.connections)
    audit.close()
