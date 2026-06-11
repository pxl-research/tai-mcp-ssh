"""Tests for ``tai_mcp_ssh.audit``."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from tai_mcp_ssh.audit import SYSTEM_HOST, AuditLog


def _read_jsonl(p: Path) -> list[dict]:
    return [json.loads(line) for line in p.read_text(encoding="utf-8").splitlines()]


async def test_record_writes_to_correct_path(tmp_path: Path) -> None:
    log = AuditLog(root=tmp_path)
    await log.record("session_run", host="pi-living", cmd="ls", exit=0)
    log.close()
    today = datetime.now(UTC).date().isoformat()
    f = tmp_path / "pi-living" / f"{today}.jsonl"
    records = _read_jsonl(f)
    assert len(records) == 1
    r = records[0]
    assert r["tool"] == "session_run"
    assert r["host"] == "pi-living"
    assert r["cmd"] == "ls"
    assert r["exit"] == 0
    assert r["ts"].endswith("Z")


async def test_no_host_defaults_to_system(tmp_path: Path) -> None:
    log = AuditLog(root=tmp_path)
    await log.record("startup", note="hello")
    log.close()
    today = datetime.now(UTC).date().isoformat()
    assert (tmp_path / SYSTEM_HOST / f"{today}.jsonl").exists()


async def test_secret_keys_redacted(tmp_path: Path) -> None:
    log = AuditLog(root=tmp_path)
    await log.record(
        "session_run",
        host="pi",
        cmd="ls",
        password="hunter2",
        token="abc",
        resolved_password="leak",
    )
    log.close()
    today = datetime.now(UTC).date().isoformat()
    r = _read_jsonl(tmp_path / "pi" / f"{today}.jsonl")[0]
    assert r["password"] == "<redacted>"
    assert r["token"] == "<redacted>"
    assert r["resolved_password"] == "<redacted>"
    assert r["cmd"] == "ls"  # non-secret field preserved


async def test_cmd_secrets_redacted(tmp_path: Path) -> None:
    log = AuditLog(root=tmp_path)
    await log.record("session_run", host="pi", cmd="mysql --password=hunter2 -e 'select 1'")
    await log.record("session_run", host="pi", cmd="echo keychain://tai-mcp-ssh/pi")
    log.close()
    today = datetime.now(UTC).date().isoformat()
    records = _read_jsonl(tmp_path / "pi" / f"{today}.jsonl")
    assert "hunter2" not in records[0]["cmd"]
    assert "--password=<redacted>" in records[0]["cmd"]
    assert records[1]["cmd"] == "echo keychain://<redacted>"


async def test_cmd_redaction_leaves_innocent_commands_untouched(tmp_path: Path) -> None:
    # The redactor must not corrupt ambiguous short flags that collide with
    # secret forms (`-p` in cp/mkdir/ssh is not a password).
    log = AuditLog(root=tmp_path)
    await log.record("session_run", host="pi", cmd="cp -p a b")
    await log.record("session_run", host="pi", cmd="mkdir -p /srv/x")
    log.close()
    today = datetime.now(UTC).date().isoformat()
    records = _read_jsonl(tmp_path / "pi" / f"{today}.jsonl")
    assert records[0]["cmd"] == "cp -p a b"
    assert records[1]["cmd"] == "mkdir -p /srv/x"


async def test_reserved_ts_field_ignored(tmp_path: Path) -> None:
    # `tool` and `host` collide at the Python signature level so callers
    # can't pass them via kwargs anyway; `ts` is the one auto-filled
    # reserved field a caller could try to override.
    log = AuditLog(root=tmp_path)
    await log.record("session_run", host="pi", ts="OVERRIDE")
    log.close()
    today = datetime.now(UTC).date().isoformat()
    r = _read_jsonl(tmp_path / "pi" / f"{today}.jsonl")[0]
    assert r["ts"] != "OVERRIDE"
    assert r["ts"].endswith("Z")


async def test_concurrent_same_host_serialise(tmp_path: Path) -> None:
    log = AuditLog(root=tmp_path)
    await asyncio.gather(*(log.record("t", host="pi", i=i) for i in range(50)))
    log.close()
    today = datetime.now(UTC).date().isoformat()
    raw = (tmp_path / "pi" / f"{today}.jsonl").read_text(encoding="utf-8")
    lines = raw.splitlines()
    assert len(lines) == 50
    for line in lines:
        json.loads(line)  # raises if interleaved/malformed


async def test_concurrent_different_hosts(tmp_path: Path) -> None:
    log = AuditLog(root=tmp_path)
    await asyncio.gather(
        log.record("t", host="a", i=1),
        log.record("t", host="b", i=2),
        log.record("t", host="c", i=3),
    )
    log.close()
    today = datetime.now(UTC).date().isoformat()
    for host in ("a", "b", "c"):
        assert (tmp_path / host / f"{today}.jsonl").exists()


async def test_host_dir_mode_0700(tmp_path: Path) -> None:
    log = AuditLog(root=tmp_path)
    await log.record("t", host="pi")
    log.close()
    assert (tmp_path / "pi").stat().st_mode & 0o777 == 0o700


async def test_date_rollover_switches_files(tmp_path: Path) -> None:
    # Inject a clock so we can step from one UTC day to the next inside a
    # single test run, then assert each record landed in its own day file.
    clock = [datetime(2026, 5, 12, 23, 30, tzinfo=UTC)]
    log = AuditLog(root=tmp_path, now=lambda: clock[0])

    await log.record("t", host="pi", marker="day1")
    clock[0] = datetime(2026, 5, 13, 0, 1, tzinfo=UTC)
    await log.record("t", host="pi", marker="day2")
    log.close()

    day1_file = tmp_path / "pi" / "2026-05-12.jsonl"
    day2_file = tmp_path / "pi" / "2026-05-13.jsonl"
    assert day1_file.exists()
    assert day2_file.exists()
    assert _read_jsonl(day1_file)[0]["marker"] == "day1"
    assert _read_jsonl(day2_file)[0]["marker"] == "day2"


async def test_same_day_restart_appends(tmp_path: Path) -> None:
    log_a = AuditLog(root=tmp_path)
    await log_a.record("t", host="pi", marker="run1")
    log_a.close()

    log_b = AuditLog(root=tmp_path)
    await log_b.record("t", host="pi", marker="run2")
    log_b.close()

    today = datetime.now(UTC).date().isoformat()
    records = _read_jsonl(tmp_path / "pi" / f"{today}.jsonl")
    markers = [r["marker"] for r in records]
    assert markers == ["run1", "run2"]


async def test_sweep_deletes_old_files(tmp_path: Path) -> None:
    host_dir = tmp_path / "pi"
    host_dir.mkdir(parents=True)
    today = datetime.now(UTC).date()
    old = (today - timedelta(days=200)).isoformat()
    medium = (today - timedelta(days=50)).isoformat()
    new = today.isoformat()
    (host_dir / f"{old}.jsonl").write_text('{"x":1}\n')
    (host_dir / f"{medium}.jsonl").write_text('{"x":2}\n')
    (host_dir / f"{new}.jsonl").write_text('{"x":3}\n')

    log = AuditLog(root=tmp_path)
    await log.sweep_retention(retention_days=90)
    log.close()

    assert not (host_dir / f"{old}.jsonl").exists()
    assert (host_dir / f"{medium}.jsonl").exists()
    assert (host_dir / f"{new}.jsonl").exists()


async def test_sweep_writes_summary(tmp_path: Path) -> None:
    host_dir = tmp_path / "pi"
    host_dir.mkdir(parents=True)
    old = (datetime.now(UTC).date() - timedelta(days=200)).isoformat()
    (host_dir / f"{old}.jsonl").write_text("{}\n")

    log = AuditLog(root=tmp_path)
    await log.sweep_retention(retention_days=90)
    log.close()

    today = datetime.now(UTC).date().isoformat()
    sys_file = tmp_path / SYSTEM_HOST / f"{today}.jsonl"
    assert sys_file.exists()
    records = _read_jsonl(sys_file)
    sweeps = [r for r in records if r["tool"] == "_sweep"]
    assert len(sweeps) == 1
    assert sweeps[0]["deleted_by_host"] == {"pi": 1}
    assert sweeps[0]["retention_days"] == 90


async def test_sweep_ignores_non_date_files(tmp_path: Path) -> None:
    host_dir = tmp_path / "pi"
    host_dir.mkdir(parents=True)
    (host_dir / "README").write_text("not a date file")
    (host_dir / "rotated.jsonl.1").write_text("{}\n")

    log = AuditLog(root=tmp_path)
    await log.sweep_retention(retention_days=90)
    log.close()

    assert (host_dir / "README").exists()
    assert (host_dir / "rotated.jsonl.1").exists()


async def test_sweep_no_op_when_root_missing(tmp_path: Path) -> None:
    log = AuditLog(root=tmp_path / "does-not-exist")
    await log.sweep_retention(retention_days=90)  # no exception
    log.close()


async def test_sweep_with_no_old_files_writes_no_summary(tmp_path: Path) -> None:
    log = AuditLog(root=tmp_path)
    await log.sweep_retention(retention_days=90)
    log.close()
    # nothing to sweep, nothing to record
    assert not (tmp_path / SYSTEM_HOST).exists()
