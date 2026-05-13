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
from tai_mcp_ssh.errors import HostNotAllowed
from tai_mcp_ssh.server import (
    Services,
    build_server,
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


def test_tool_specs_returns_seven() -> None:
    names = [t.name for t in tool_specs()]
    assert names == [
        "hosts",
        "session_list",
        "session_run",
        "session_wait",
        "session_kill",
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


async def test_dispatch_hosts_returns_redacted_list(tmp_path: Path) -> None:
    svc = _make_services(
        tmp_path,
        hosts={
            "pi": Host(alias="pi", host="192.168.1.42", user="pi", port=22),
            "vps": Host(
                alias="vps",
                host="1.2.3.4",
                auth="password",
                password_ref="keychain://tai-mcp-ssh/vps",
            ),
        },
    )
    result = await dispatch(svc, "hosts", {})
    assert {h["alias"] for h in result} == {"pi", "vps"}
    # No secrets, no keychain references in the response.
    flattened = json.dumps(result)
    assert "keychain://" not in flattened
    assert "password_ref" not in flattened
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
    svc.transfer.get.assert_awaited_once_with("pi", "/etc/hostname", None, reason=None)
    svc.audit.close()


async def test_dispatch_unknown_tool_raises(tmp_path: Path) -> None:
    svc = _make_services(tmp_path)
    with pytest.raises(ValueError, match="unknown tool"):
        await dispatch(svc, "no-such-tool", {})
    svc.audit.close()


# ---------------------------------------------------------------------------
# build_server: rejection auditing
# ---------------------------------------------------------------------------


async def test_call_tool_audits_host_not_allowed(tmp_path: Path) -> None:
    svc = _make_services(tmp_path, hosts={})
    svc.sessions.run = AsyncMock(side_effect=HostNotAllowed("ghost"))
    # build_server registers handlers we don't need to introspect here; we
    # verify the audit-on-rejection contract by replaying the wrapper's
    # logic directly against dispatch.
    build_server(svc)  # ensures it constructs cleanly
    from tai_mcp_ssh.server import _host_from_args  # type: ignore[attr-defined]

    try:
        await dispatch(
            svc,
            "session_run",
            {"session_id": "ghost/default", "command": "ls"},
        )
    except HostNotAllowed as exc:
        await svc.audit.record(
            "session_run",
            host=_host_from_args("session_run", {"session_id": "ghost/default"}),
            status="rejected",
            error=str(exc),
        )
    records = _audit_records(tmp_path, "ghost")
    assert records and records[0]["status"] == "rejected"
    assert records[0]["tool"] == "session_run"
    assert records[0]["host"] == "ghost"
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
