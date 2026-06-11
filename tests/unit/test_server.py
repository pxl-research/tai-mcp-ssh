"""Tests for ``tai_mcp_ssh.server`` — dispatch + audit-on-rejection.

The MCP SDK wrapper itself isn't tested here (it's a thin shim around
:func:`dispatch`); we test that dispatch routes to the right manager and
that domain rejections still produce one audit record.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from tai_mcp_ssh.audit import AuditLog
from tai_mcp_ssh.config import AuditSettings, Config, Host
from tai_mcp_ssh.errors import ConfigError, HostNotAllowed, TransferDenied
from tai_mcp_ssh.server import (
    Services,
    _dispatch_and_audit,
    dispatch,
    to_jsonable,
    tool_specs,
)
from tai_mcp_ssh.sessions import RunResult


def _audit_records(audit_root: Path, host: str) -> list[dict[str, Any]]:
    today = datetime.now(UTC).date().isoformat()
    f = audit_root / host / f"{today}.jsonl"
    if not f.exists():
        return []
    return [json.loads(line) for line in f.read_text().splitlines()]


def _make_services(
    audit_root: Path,
    hosts: dict[str, Host] | None = None,
) -> Services:
    cfg = Config(hosts=hosts or {}, audit=AuditSettings())
    audit = AuditLog(root=audit_root)
    return Services(
        config=cfg,
        audit=audit,
        pool=MagicMock(),
        sessions=MagicMock(),
        transfer=MagicMock(),
    )


# ---------------------------------------------------------------------------
# Tool specs
# ---------------------------------------------------------------------------


def test_tool_specs_returns_eight() -> None:
    names = [t.name for t in tool_specs()]
    assert names == [
        "hosts",
        "session_list",
        "session_run",
        "session_wait",
        "session_kill",
        "session_reset",
        "put",
        "get",
    ]


def test_tool_specs_required_fields_match_design() -> None:
    schemas = {t.name: t.inputSchema for t in tool_specs()}
    assert schemas["session_run"]["required"] == ["session_id", "command"]
    assert schemas["session_wait"]["required"] == ["session_id"]
    assert schemas["put"]["required"] == ["host", "local_path", "remote_path"]
    assert schemas["get"]["required"] == ["host", "remote_path"]


# ---------------------------------------------------------------------------
# Dispatch routing
# ---------------------------------------------------------------------------


async def test_dispatch_hosts_returns_redacted_list(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    hosts = {
        "pi": Host(alias="pi", host="192.168.1.42", user="pi", port=22),
        "vps": Host(
            alias="vps",
            host="1.2.3.4",
            auth="password",
            password_ref="keychain://tai-mcp-ssh/vps",
        ),
    }
    svc = _make_services(tmp_path, hosts=hosts)
    # `hosts` dispatch reloads from disk; pin load_config so this test
    # doesn't depend on the developer's real ~/.config/tai-mcp-ssh/hosts.toml.
    monkeypatch.setattr(
        "tai_mcp_ssh.server.load_config",
        lambda: Config(hosts=hosts, audit=AuditSettings()),
    )
    svc.pool.update_hosts = AsyncMock()
    result = await dispatch(svc, "hosts", {})
    assert {h["alias"] for h in result} == {"pi", "vps"}
    # No secrets, no keychain references in the response.
    flattened = json.dumps(result)
    assert "keychain://" not in flattened
    assert "password_ref" not in flattened
    svc.audit.close()


# ---------------------------------------------------------------------------
# dispatch "hosts" reload behavior (issue #9 / reload-hosts-on-call)
# ---------------------------------------------------------------------------


async def test_dispatch_hosts_reloads_and_audits_diff_counts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    initial = {"pi": Host(alias="pi", host="192.168.1.42")}
    after_edit = {
        "pi": Host(alias="pi", host="10.0.0.42"),  # IP changed
        "vps": Host(alias="vps", host="1.2.3.4"),  # added
    }
    svc = _make_services(tmp_path, hosts=initial)
    monkeypatch.setattr(
        "tai_mcp_ssh.server.load_config",
        lambda: Config(hosts=after_edit, audit=AuditSettings()),
    )
    svc.pool.update_hosts = AsyncMock()

    result = await dispatch(svc, "hosts", {})

    # Returned list reflects the freshly-reloaded config, not the original.
    assert {h["alias"] for h in result} == {"pi", "vps"}
    # update_hosts was called with the new dict and the eviction set
    # (changed alias only — additions need no eviction).
    svc.pool.update_hosts.assert_awaited_once_with(after_edit, evict={"pi"})
    # One _hosts_reload audit record with the diff counts.
    records = _audit_records(tmp_path, "_system")
    reloads = [r for r in records if r["tool"] == "_hosts_reload"]
    assert len(reloads) == 1
    assert reloads[0]["status"] == "ok"
    assert reloads[0]["added"] == 1
    assert reloads[0]["removed"] == 0
    assert reloads[0]["changed"] == 1
    svc.audit.close()


async def test_dispatch_hosts_fails_soft_on_bad_toml(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    initial = {"pi": Host(alias="pi", host="192.168.1.42")}
    svc = _make_services(tmp_path, hosts=initial)

    def boom() -> Config:
        raise ConfigError("malformed hosts.toml")

    monkeypatch.setattr("tai_mcp_ssh.server.load_config", boom)
    svc.pool.update_hosts = AsyncMock()  # must not be called on failure

    result = await dispatch(svc, "hosts", {})

    # Pre-failure list is returned; in-memory allowlist is untouched.
    assert {h["alias"] for h in result} == {"pi"}
    assert svc.config.hosts == initial
    svc.pool.update_hosts.assert_not_awaited()
    # The failure is recorded as a _hosts_reload error.
    records = _audit_records(tmp_path, "_system")
    reloads = [r for r in records if r["tool"] == "_hosts_reload"]
    assert len(reloads) == 1
    assert reloads[0]["status"] == "error"
    assert "ConfigError" in reloads[0]["error"]
    # Schema must match the success branch: counts present, all zero on failure.
    assert reloads[0]["added"] == 0
    assert reloads[0]["removed"] == 0
    assert reloads[0]["changed"] == 0
    svc.audit.close()


async def test_dispatch_hosts_no_op_reload_audits_zero_counts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    hosts = {"pi": Host(alias="pi", host="192.168.1.42")}
    svc = _make_services(tmp_path, hosts=hosts)
    monkeypatch.setattr(
        "tai_mcp_ssh.server.load_config",
        lambda: Config(hosts=hosts, audit=AuditSettings()),
    )
    svc.pool.update_hosts = AsyncMock()

    await dispatch(svc, "hosts", {})

    # No-op reload: empty evict set, all counts zero, still audited.
    svc.pool.update_hosts.assert_awaited_once_with(hosts, evict=set())
    records = _audit_records(tmp_path, "_system")
    reloads = [r for r in records if r["tool"] == "_hosts_reload"]
    assert len(reloads) == 1
    assert reloads[0]["status"] == "ok"
    assert reloads[0]["added"] == 0
    assert reloads[0]["removed"] == 0
    assert reloads[0]["changed"] == 0
    svc.audit.close()


async def test_services_reload_hosts_from_disk_returns_diff_counts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Direct unit test of the helper: confirms the diff math without going
    # through dispatch + audit.
    initial = {
        "pi": Host(alias="pi", host="192.168.1.42"),
        "vps": Host(alias="vps", host="1.2.3.4"),
    }
    after = {
        "pi": Host(alias="pi", host="192.168.1.42"),  # unchanged
        "newbox": Host(alias="newbox", host="9.9.9.9"),  # added
        # vps removed
    }
    svc = _make_services(tmp_path, hosts=initial)
    monkeypatch.setattr(
        "tai_mcp_ssh.server.load_config",
        lambda: Config(hosts=after, audit=AuditSettings()),
    )
    svc.pool.update_hosts = AsyncMock()

    added, removed, changed = await svc.reload_hosts_from_disk()
    assert (added, removed, changed) == (1, 1, 0)
    svc.pool.update_hosts.assert_awaited_once_with(after, evict={"vps"})
    # In-memory config now reflects the reloaded one.
    assert svc.config.hosts == after
    svc.audit.close()


async def test_dispatch_session_list_delegates(tmp_path: Path) -> None:
    svc = _make_services(tmp_path)
    svc.sessions.list_sessions = MagicMock(return_value=[{"session_id": "pi/x"}])
    result = await dispatch(svc, "session_list", {})
    assert result == [{"session_id": "pi/x"}]
    svc.audit.close()


async def test_dispatch_session_run_passes_args(tmp_path: Path) -> None:
    svc = _make_services(tmp_path)
    expected = RunResult(
        session_id="pi/default",
        status="done",
        head="ok",
        tail="",
        bytes=2,
        truncated=False,
        log_id="01J",
        log_path="/home/pi/.tai-ssh/logs/01J.log",
        exit=0,
    )
    svc.sessions.run = AsyncMock(return_value=expected)
    result = await dispatch(
        svc,
        "session_run",
        {
            "session_id": "pi/default",
            "command": "uname -a",
            "reason": "check kernel",
            "timeout": 15,
        },
    )
    assert result is expected
    svc.sessions.run.assert_awaited_once_with(
        "pi/default", "uname -a", reason="check kernel", timeout=15.0
    )
    svc.audit.close()


async def test_dispatch_session_wait_passes_args(tmp_path: Path) -> None:
    svc = _make_services(tmp_path)
    svc.sessions.wait = AsyncMock()
    await dispatch(svc, "session_wait", {"session_id": "pi/x", "timeout": 5})
    svc.sessions.wait.assert_awaited_once_with("pi/x", timeout=5.0)
    svc.audit.close()


async def test_dispatch_session_kill(tmp_path: Path) -> None:
    svc = _make_services(tmp_path)
    svc.sessions.kill = AsyncMock(return_value={"killed": True})
    result = await dispatch(svc, "session_kill", {"session_id": "pi/x"})
    assert result == {"killed": True}
    svc.audit.close()


async def test_dispatch_session_reset(tmp_path: Path) -> None:
    svc = _make_services(tmp_path)
    svc.sessions.reset = AsyncMock(return_value={"reset": True})
    result = await dispatch(svc, "session_reset", {"session_id": "pi/x"})
    assert result == {"reset": True}
    svc.sessions.reset.assert_awaited_once_with("pi/x")
    svc.audit.close()


async def test_dispatch_put_passes_args(tmp_path: Path) -> None:
    svc = _make_services(tmp_path)
    svc.transfer.put = AsyncMock()
    await dispatch(
        svc,
        "put",
        {"host": "pi", "local_path": "/x", "remote_path": "/y", "reason": "deploy"},
    )
    svc.transfer.put.assert_awaited_once_with("pi", "/x", "/y", reason="deploy")
    svc.audit.close()


async def test_dispatch_get_with_default_local_path(tmp_path: Path) -> None:
    svc = _make_services(tmp_path)
    svc.transfer.get = AsyncMock()
    await dispatch(svc, "get", {"host": "pi", "remote_path": "/etc/hostname"})
    svc.transfer.get.assert_awaited_once_with(
        "pi", "/etc/hostname", None, reason=None, allow_outside=False
    )
    svc.audit.close()


async def test_dispatch_get_forwards_allow_outside(tmp_path: Path) -> None:
    svc = _make_services(tmp_path)
    svc.transfer.get = AsyncMock()
    await dispatch(
        svc,
        "get",
        {"host": "pi", "remote_path": "/x", "local_path": "/tmp/y", "allow_outside": True},
    )
    svc.transfer.get.assert_awaited_once_with("pi", "/x", "/tmp/y", reason=None, allow_outside=True)
    svc.audit.close()


async def test_dispatch_unknown_tool_raises(tmp_path: Path) -> None:
    svc = _make_services(tmp_path)
    with pytest.raises(ValueError, match="unknown tool"):
        await dispatch(svc, "no-such-tool", {})
    svc.audit.close()


# ---------------------------------------------------------------------------
# build_server: rejection auditing
# ---------------------------------------------------------------------------


async def test_dispatch_and_audit_records_host_not_allowed_as_rejected(tmp_path: Path) -> None:
    # Exercises the actual production audit branch — no logic replay.
    svc = _make_services(tmp_path, hosts={})
    svc.sessions.run = AsyncMock(side_effect=HostNotAllowed("ghost"))
    with pytest.raises(HostNotAllowed):
        await _dispatch_and_audit(
            svc,
            "session_run",
            {"session_id": "ghost/default", "command": "ls"},
        )
    records = _audit_records(tmp_path, "ghost")
    assert records and records[0]["status"] == "rejected"
    assert records[0]["tool"] == "session_run"
    assert records[0]["host"] == "ghost"
    svc.audit.close()


async def test_dispatch_and_audit_records_unexpected_value_error(tmp_path: Path) -> None:
    # ValueError from dispatch (e.g. unknown tool) must still produce one
    # audit record at status=error with a typed prefix.
    svc = _make_services(tmp_path)
    with pytest.raises(ValueError):
        await _dispatch_and_audit(svc, "no-such-tool", {})
    records = _audit_records(tmp_path, "_system")
    assert records and records[0]["status"] == "error"
    assert records[0]["error"].startswith("ValueError")
    svc.audit.close()


async def test_dispatch_and_audit_skips_audit_when_marker_set(tmp_path: Path) -> None:
    # Managers that record richer fields locally (e.g. TransferManager
    # logging a rejected put with local/remote paths) set `exc.audited`
    # so the server doesn't double-record.
    svc = _make_services(tmp_path, hosts={"pi": Host(alias="pi")})
    exc = TransferDenied("nope")
    exc.audited = True  # type: ignore[attr-defined]
    svc.transfer.put = AsyncMock(side_effect=exc)
    with pytest.raises(TransferDenied):
        await _dispatch_and_audit(
            svc, "put", {"host": "pi", "local_path": "/x", "remote_path": "/y"}
        )
    records = _audit_records(tmp_path, "pi")
    assert not records  # nothing recorded by the server — manager owned it
    svc.audit.close()


async def test_dispatch_and_audit_records_taimcpssh_error_without_marker(tmp_path: Path) -> None:
    svc = _make_services(tmp_path, hosts={"pi": Host(alias="pi")})
    svc.transfer.put = AsyncMock(side_effect=TransferDenied("no marker"))
    with pytest.raises(TransferDenied):
        await _dispatch_and_audit(
            svc, "put", {"host": "pi", "local_path": "/x", "remote_path": "/y"}
        )
    records = _audit_records(tmp_path, "pi")
    assert records and records[0]["status"] == "error"
    assert records[0]["tool"] == "put"
    svc.audit.close()


def test_host_from_args_session_id() -> None:
    from tai_mcp_ssh.server import _host_from_args  # type: ignore[attr-defined]

    assert _host_from_args("session_run", {"session_id": "pi/x"}) == "pi"
    assert _host_from_args("put", {"host": "vps"}) == "vps"
    assert _host_from_args("hosts", {}) is None


# ---------------------------------------------------------------------------
# to_jsonable
# ---------------------------------------------------------------------------


def test_to_jsonable_unwraps_dataclasses() -> None:
    r = RunResult(
        session_id="pi/x",
        status="done",
        head="h",
        tail="",
        bytes=1,
        truncated=False,
        log_id="01J",
        log_path="/p",
        exit=0,
    )
    out = to_jsonable(r)
    assert isinstance(out, dict)
    assert out["session_id"] == "pi/x"
    assert out["status"] == "done"
    assert out["exit"] == 0
    # Serialises cleanly.
    json.dumps(out)


def test_to_jsonable_passes_through_primitives() -> None:
    assert to_jsonable({"a": [1, 2], "b": "ok"}) == {"a": [1, 2], "b": "ok"}
    assert to_jsonable(None) is None


# ---------------------------------------------------------------------------
# Sanity: rejected calls still go through _system if no host can be inferred
# ---------------------------------------------------------------------------


async def test_unparseable_session_id_audited_to_system(tmp_path: Path) -> None:
    svc = _make_services(tmp_path)
    svc.sessions.run = AsyncMock(side_effect=ValueError("bad id"))
    try:
        await dispatch(svc, "session_run", {"session_id": "no-slash", "command": "x"})
    except ValueError:
        await svc.audit.record("session_run", host=None, status="rejected", error="bad id")
    records = _audit_records(tmp_path, "_system")
    assert records and records[0]["host"] == "_system"
    svc.audit.close()
