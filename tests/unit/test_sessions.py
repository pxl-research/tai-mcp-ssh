"""Tests for ``tai_mcp_ssh.sessions``.

The :class:`SessionManager` is exercised end-to-end against a fake
``ConnectionPool`` whose remote shell is simulated by an in-process
"log file" that the polling loop reads via ``cat``.
"""

from __future__ import annotations

import asyncio
import json
import re
import shlex
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from tai_mcp_ssh.audit import AuditLog
from tai_mcp_ssh.errors import HostNotAllowed, HostUnreachable
from tai_mcp_ssh.sessions import (
    SessionManager,
    _detect_prompt,
    _extract_output,
    _extract_partial_output,
    _slice_output,
    parse_session_id,
)

# ---------------------------------------------------------------------------
# Fake connection + pool
# ---------------------------------------------------------------------------


@dataclass
class FakeProc:
    exit_status: int = 0
    stdout: str | bytes = ""
    stderr: str = ""


class FakeConnection:
    """Imitates :class:`tai_mcp_ssh.ssh.Connection` for SessionManager tests.

    Records every ``run()`` call, tracks an in-process per-pane "log file"
    so the manager's polling loop sees realistic content, and lets each
    test programme what `cat`/`tail` returns on demand.
    """

    def __init__(self, host: str, home_dir: str = "/home/pi") -> None:
        self._alias = host
        self._home = home_dir
        self.run_calls: list[str] = []
        # path -> log content
        self.log_files: dict[str, str] = {}
        # extra handlers per command pattern (for prompt simulation, etc.)
        self.handlers: list[tuple[re.Pattern[str], Callable[[str], FakeProc]]] = []

    @property
    def alias(self) -> str:
        return self._alias

    @property
    def home_dir(self) -> str:
        return self._home

    def add_handler(self, pattern: str, handler: Callable[[str], FakeProc]) -> None:
        self.handlers.append((re.compile(pattern), handler))

    def set_log(self, path: str, content: str) -> None:
        self.log_files[path] = content

    def append_log(self, path: str, content: str) -> None:
        self.log_files[path] = self.log_files.get(path, "") + content

    async def run(
        self, command: str, *, check: bool = False, encoding: str | None = "utf-8"
    ) -> FakeProc:
        self.run_calls.append(command)

        for pattern, handler in self.handlers:
            if pattern.search(command):
                return handler(command)

        if command.startswith("cat "):
            # Extract quoted path; fall back to unquoted.
            stripped = command[len("cat ") :].strip()
            if stripped.startswith("'") and stripped.endswith("'"):
                path = stripped[1:-1]
            else:
                path = stripped
            return FakeProc(0, self.log_files.get(path, ""), "")

        return FakeProc(0, "", "")


class FakePool:
    def __init__(self, conns: dict[str, FakeConnection]) -> None:
        self.conns = conns

    async def get(self, alias: str) -> FakeConnection:
        if alias not in self.conns:
            raise HostNotAllowed(alias)
        return self.conns[alias]


def _audit_records(audit_root: Path, host: str) -> list[dict[str, Any]]:
    today = datetime.now(UTC).date().isoformat()
    f = audit_root / host / f"{today}.jsonl"
    if not f.exists():
        return []
    return [json.loads(line) for line in f.read_text().splitlines()]


def _make_log(log_id: str, *, output: str, exit_code: int = 0) -> str:
    """Mimic what pipe-pane captures for a completed command."""
    return (
        f"pi@host:~ $ echo __TAI_START__{log_id}__; cmd; echo __TAI_DONE__$?__{log_id}__\n"
        f"__TAI_START__{log_id}__\n"
        f"{output}"
        + ("" if output.endswith("\n") else "\n")
        + f"__TAI_DONE__{exit_code}__{log_id}__\n"
        f"pi@host:~ $ \n"
    )


# ---------------------------------------------------------------------------
# Pure-helper tests
# ---------------------------------------------------------------------------


def test_parse_session_id_ok() -> None:
    assert parse_session_id("pi/default") == ("pi", "default")
    assert parse_session_id("ubuntu-vps/build") == ("ubuntu-vps", "build")


def test_parse_session_id_rejects_no_slash() -> None:
    with pytest.raises(ValueError, match="expected"):
        parse_session_id("pi-default")


def test_parse_session_id_rejects_empty_parts() -> None:
    with pytest.raises(ValueError):
        parse_session_id("/default")
    with pytest.raises(ValueError):
        parse_session_id("pi/")


def test_parse_session_id_rejects_unsafe_name() -> None:
    # Shell metacharacters in the name part would be interpolated into
    # remote tmux commands. Reject at the boundary.
    with pytest.raises(ValueError, match="shell metacharacters"):
        parse_session_id("pi/foo;touch")
    with pytest.raises(ValueError, match="shell metacharacters"):
        parse_session_id("pi/$(whoami)")
    with pytest.raises(ValueError, match="shell metacharacters"):
        parse_session_id("pi/with space")


def test_extract_output_strips_markers_and_prompt() -> None:
    content = _make_log("01J", output="hello\nworld")
    assert _extract_output(content, "01J") == "hello\nworld"


def test_extract_output_handles_empty_body() -> None:
    content = _make_log("01J", output="")
    # Output stripped to nothing — extraction returns "" (no body lines).
    assert _extract_output(content, "01J") == ""


def test_extract_output_returns_empty_when_no_markers() -> None:
    assert _extract_output("garbage with no markers", "01J") == ""


def test_done_sentinel_matches_after_no_trailing_newline() -> None:
    # Regression for #7. The production wrapper now emits the DONE marker
    # via `printf '\n__TAI_DONE__%d__<id>__\n' "$?"`, so even when the
    # user command's last output line lacks a trailing newline (curl,
    # printf without \n, echo -n) the marker still lands at column 0
    # and the line-anchored regex / _extract_output both succeed.
    log_id = "01J"
    content = (
        f"__TAI_START__{log_id}__\n"
        '{"version":"0.23.3"}'  # no trailing newline (curl-style)
        f"\n__TAI_DONE__0__{log_id}__\n"
    )
    done_re = re.compile(rf"^__TAI_DONE__(\d+)__{re.escape(log_id)}__\s*$", re.MULTILINE)
    m = done_re.search(content)
    assert m is not None
    assert m.group(1) == "0"
    # _extract_output returns the body line(s). The printf's leading \n
    # leaves a single empty line before DONE, which is harmless tail noise.
    assert _extract_output(content, log_id).startswith('{"version":"0.23.3"}')


def test_extract_partial_output_after_start_only() -> None:
    content = "prompt $ ...\n__TAI_START__abc__\npartial line 1\npartial line 2"
    assert _extract_partial_output(content, "abc") == "partial line 1\npartial line 2"


def test_detect_prompt_sudo() -> None:
    content = "Working...\n[sudo] password for pi: "
    match = _detect_prompt(content)
    assert match is not None
    status, prompt = match
    assert status == "needs_password"
    assert prompt.startswith("[sudo] password")


def test_detect_prompt_apt_confirm() -> None:
    content = "...\nDo you want to continue? [Y/n] "
    match = _detect_prompt(content)
    assert match is not None
    assert match[0] == "needs_input"


def test_detect_prompt_yesno_hostkey() -> None:
    content = "Are you sure you want to continue connecting (yes/no/[fingerprint])? "
    match = _detect_prompt(content)
    assert match is not None
    assert match[0] == "needs_input"


def test_detect_prompt_ignores_midstream_match() -> None:
    # `[Y/n]` mid-output (not at the end) must NOT classify as a prompt.
    content = "Some help text mentioning [Y/n] options.\nMore output here\nfinal line\n"
    assert _detect_prompt(content) is None


def test_slice_output_inline_when_small() -> None:
    head, tail, truncated = _slice_output("short output\n")
    assert head == "short output\n"
    assert tail == ""
    assert truncated is False


def test_slice_output_truncates_when_long() -> None:
    huge = "\n".join(f"line {i}" for i in range(500)) + "\n"
    head, tail, truncated = _slice_output(huge)
    assert truncated is True
    assert head.startswith("line 0")
    assert "line 499" in tail


# ---------------------------------------------------------------------------
# SessionManager behaviour
# ---------------------------------------------------------------------------


async def test_run_unknown_host_propagates(tmp_path: Path) -> None:
    audit = AuditLog(root=tmp_path)
    sm = SessionManager(FakePool({}), audit)  # type: ignore[arg-type]
    with pytest.raises(HostNotAllowed):
        await sm.run("ghost/default", "ls")
    audit.close()


async def test_run_done_returns_full_output(tmp_path: Path) -> None:
    fc = FakeConnection("pi")
    audit = AuditLog(root=tmp_path)
    sm = SessionManager(FakePool({"pi": fc}), audit, poll_interval=0.01)  # type: ignore[arg-type]

    # As soon as a `cat <log_path>` is requested, return a complete log.
    # We don't know the log_id ahead of time, so we hook on cat and synthesise.
    captured_log_id: dict[str, str] = {}

    def cat_handler(cmd: str) -> FakeProc:
        # cmd looks like: cat '/home/pi/.tai-ssh/logs/<id>.log'
        m = re.search(r"/logs/([0-9A-Z]+)\.log", cmd)
        if not m:
            return FakeProc(0, "", "")
        log_id = m.group(1)
        captured_log_id["id"] = log_id
        return FakeProc(0, _make_log(log_id, output="hello\nworld"), "")

    fc.add_handler(r"^tail -c ", cat_handler)

    result = await sm.run("pi/default", "echo hello && echo world")
    assert result.status == "done"
    assert result.exit == 0
    assert result.head == "hello\nworld"
    assert result.tail == ""
    assert result.truncated is False
    assert result.log_id == captured_log_id["id"]
    assert result.log_path.endswith(f"/logs/{captured_log_id['id']}.log")
    audit.close()


async def test_run_creates_tmux_session_idempotently(tmp_path: Path) -> None:
    fc = FakeConnection("pi")
    fc.add_handler(
        r"^tail -c ",
        lambda cmd: FakeProc(
            0,
            _make_log(
                re.search(r"/logs/([0-9A-Z]+)\.log", cmd).group(1),  # type: ignore[union-attr]
                output="",
            ),
            "",
        ),
    )
    audit = AuditLog(root=tmp_path)
    sm = SessionManager(FakePool({"pi": fc}), audit, poll_interval=0.01)  # type: ignore[arg-type]

    await sm.run("pi/default", "ls")
    # has-session check is part of the create command.
    create_calls = [c for c in fc.run_calls if "tmux has-session" in c]
    assert len(create_calls) == 1
    assert "tmux new-session -d -s tai-mcp/default" in create_calls[0]
    audit.close()


async def test_run_wraps_user_command_in_eval(tmp_path: Path) -> None:
    # Regression for #4 (parse-error sentinel survival) and for the
    # `bash -c` regression caught in PR review: deferring the user
    # command via `eval` keeps a parse error from stripping the outer
    # DONE echo AND preserves shell state (cd/source/export/aliases)
    # across runs, since eval runs in the current shell — unlike
    # `bash -c`, which would fork a child.
    fc = FakeConnection("pi")
    fc.add_handler(
        r"^tail -c ",
        lambda cmd: FakeProc(
            0,
            _make_log(
                re.search(r"/logs/([0-9A-Z]+)\.log", cmd).group(1),  # type: ignore[union-attr]
                output="",
            ),
            "",
        ),
    )
    audit = AuditLog(root=tmp_path)
    sm = SessionManager(FakePool({"pi": fc}), audit, poll_interval=0.01)  # type: ignore[arg-type]

    await sm.run("pi/default", "echo (oops)")
    send_keys = next(c for c in fc.run_calls if "tmux send-keys" in c and " -l " in c)
    # Recover the literal typed into the pane (the arg after `-l`).
    args = shlex.split(send_keys)
    typed = args[args.index("-l") + 1]
    # Outer sentinels survive a parse error in the user command.
    assert "echo __TAI_START__" in typed
    # DONE marker is emitted via `printf '\n...\n'` so it always lands
    # at start-of-line (regression for #7), regardless of whether the
    # user command's last output had a trailing newline.
    assert "printf '\\n__TAI_DONE__%d__" in typed
    assert '"$?"' in typed
    # User command goes through eval (current shell — state persists).
    # No child-shell wrapper anywhere.
    assert "eval 'echo (oops)'" in typed
    assert "bash -c" not in typed
    audit.close()


async def test_run_preserves_shell_state_via_eval(tmp_path: Path) -> None:
    # `cd /tmp` followed by `pwd` is the canonical test: with a
    # child-shell wrapper the `cd` wouldn't affect the parent and `pwd`
    # would show $HOME instead of /tmp. We can't run a real shell here,
    # so the assertion is structural: both commands go through `eval`
    # in the current shell, with no subshell or `bash -c` wrapping.
    fc = FakeConnection("pi")
    fc.add_handler(
        r"^tail -c ",
        lambda cmd: FakeProc(
            0,
            _make_log(
                re.search(r"/logs/([0-9A-Z]+)\.log", cmd).group(1),  # type: ignore[union-attr]
                output="",
            ),
            "",
        ),
    )
    audit = AuditLog(root=tmp_path)
    sm = SessionManager(FakePool({"pi": fc}), audit, poll_interval=0.01)  # type: ignore[arg-type]

    await sm.run("pi/default", "cd /tmp")
    await sm.run("pi/default", "pwd")

    sends = [c for c in fc.run_calls if "tmux send-keys" in c and " -l " in c]
    assert len(sends) == 2
    for send_keys in sends:
        typed = shlex.split(send_keys)[shlex.split(send_keys).index("-l") + 1]
        assert "eval " in typed
        assert "bash -c" not in typed
        assert "( " not in typed and "(set " not in typed  # no subshell wrap
    audit.close()


async def test_run_does_not_recreate_session(tmp_path: Path) -> None:
    fc = FakeConnection("pi")
    fc.add_handler(
        r"^tail -c ",
        lambda cmd: FakeProc(
            0,
            _make_log(
                re.search(r"/logs/([0-9A-Z]+)\.log", cmd).group(1),  # type: ignore[union-attr]
                output="ok",
            ),
            "",
        ),
    )
    audit = AuditLog(root=tmp_path)
    sm = SessionManager(FakePool({"pi": fc}), audit, poll_interval=0.01)  # type: ignore[arg-type]

    await sm.run("pi/default", "true")
    await sm.run("pi/default", "true")
    create_calls = [c for c in fc.run_calls if "tmux has-session" in c]
    assert len(create_calls) == 1
    audit.close()


async def test_run_returns_busy_when_session_locked(tmp_path: Path) -> None:
    fc = FakeConnection("pi")
    # cat returns NO completion — keep the first run polling.
    fc.add_handler(r"^tail -c ", lambda _cmd: FakeProc(0, "", ""))

    audit = AuditLog(root=tmp_path)
    sm = SessionManager(FakePool({"pi": fc}), audit, poll_interval=0.05)  # type: ignore[arg-type]

    # Kick off the first run; it'll spin without completing.
    first = asyncio.create_task(sm.run("pi/default", "long-task", timeout=0.5))
    # Give it a tick to set state.
    await asyncio.sleep(0.02)
    # Concurrent second run must see busy.
    busy = await sm.run("pi/default", "another")
    assert busy.status == "busy"

    # Let the first one time out → still_running.
    first_result = await first
    assert first_result.status == "still_running"
    audit.close()


async def test_run_returns_needs_password_on_sudo_prompt(tmp_path: Path) -> None:
    fc = FakeConnection("pi")
    fc.add_handler(
        r"^tail -c ",
        lambda cmd: FakeProc(
            0,
            "starting...\n[sudo] password for pi: ",
            "",
        ),
    )
    audit = AuditLog(root=tmp_path)
    sm = SessionManager(FakePool({"pi": fc}), audit, poll_interval=0.01)  # type: ignore[arg-type]

    result = await sm.run("pi/default", "sudo apt update", reason="patch")
    assert result.status == "needs_password"
    assert result.prompt is not None and result.prompt.startswith("[sudo]")
    assert result.attach_hint is not None
    assert "tmux attach -t tai-mcp/default" in result.attach_hint
    audit.close()


async def test_run_times_out_returns_still_running(tmp_path: Path) -> None:
    fc = FakeConnection("pi")
    # Empty log; no sentinel, no prompt. Manager must hit timeout.
    fc.add_handler(r"^tail -c ", lambda _cmd: FakeProc(0, "", ""))

    audit = AuditLog(root=tmp_path)
    sm = SessionManager(FakePool({"pi": fc}), audit, poll_interval=0.05)  # type: ignore[arg-type]

    result = await sm.run("pi/default", "long", timeout=0.2)
    assert result.status == "still_running"
    assert result.exit is None
    audit.close()


async def test_run_recovers_after_failed_send(tmp_path: Path) -> None:
    # A non-fatal failure during _send_command (e.g. tmux command returns
    # non-zero because the pane was killed externally) must NOT leave the
    # session wedged `busy`; the next run() should reuse it as idle.
    fc = FakeConnection("pi")
    fail = {"on": True}

    def pipe_pane_handler(_cmd: str) -> FakeProc:
        if fail["on"]:
            raise RuntimeError("pipe-pane failed")
        return FakeProc(0, "", "")

    fc.add_handler(r"tmux pipe-pane", pipe_pane_handler)
    # Second run (after we flip fail off) completes normally.
    fc.add_handler(
        r"^tail -c ",
        lambda cmd: FakeProc(
            0,
            _make_log(
                re.search(r"/logs/([0-9A-Z]+)\.log", cmd).group(1),  # type: ignore[union-attr]
                output="ok",
            ),
            "",
        ),
    )

    audit = AuditLog(root=tmp_path)
    sm = SessionManager(FakePool({"pi": fc}), audit, poll_interval=0.01)  # type: ignore[arg-type]

    with pytest.raises(RuntimeError):
        await sm.run("pi/default", "first")

    # Session must be recoverable, not stuck busy.
    fail["on"] = False
    result = await sm.run("pi/default", "second")
    assert result.status == "done"
    assert result.exit == 0
    audit.close()


async def test_read_log_offset_exact_across_split_multibyte(tmp_path: Path) -> None:
    # Regression: the byte-addressed `tail -c +N` offset must advance by the
    # exact bytes read, even when a multibyte char (é = 0xC3 0xA9) is split
    # across two polls. A fake that honours the offset would lose bytes if the
    # offset drifted (the old code re-encoded a lossy-decoded str).
    fc = FakeConnection("pi")
    calls = {"n": 0}

    def tail_handler(cmd: str) -> FakeProc:
        log_id = re.search(r"/logs/([0-9A-Z]+)\.log", cmd).group(1)  # type: ignore[union-attr]
        n = int(re.search(r"tail -c \+(\d+)", cmd).group(1))  # type: ignore[union-attr]
        start = f"__TAI_START__{log_id}__\n".encode()
        done = f"__TAI_DONE__0__{log_id}__\n".encode()
        full = start + b"caf\xc3\xa9 done\n" + done
        calls["n"] += 1
        # Poll 1: file only written up to the first byte of é (split point).
        available = full[: len(start) + 4] if calls["n"] == 1 else full
        return FakeProc(0, available[n - 1 :], "")  # honour the byte offset

    fc.add_handler(r"^tail -c ", tail_handler)

    audit = AuditLog(root=tmp_path)
    sm = SessionManager(FakePool({"pi": fc}), audit, poll_interval=0.01)  # type: ignore[arg-type]

    result = await sm.run("pi/default", "printf 'café done\\n'", timeout=1.0)
    assert result.status == "done"
    assert result.exit == 0
    # Output reconstructs exactly — no dropped \xa9, no drift.
    assert "café done" in result.head
    assert result.bytes == len("café done".encode())
    audit.close()


async def test_wait_idle_session_returns_done(tmp_path: Path) -> None:
    fc = FakeConnection("pi")
    audit = AuditLog(root=tmp_path)
    sm = SessionManager(FakePool({"pi": fc}), audit, poll_interval=0.01)  # type: ignore[arg-type]
    result = await sm.wait("pi/default")
    assert result.status == "done"
    assert result.bytes == 0
    audit.close()


async def test_wait_resumes_after_sudo_handoff(tmp_path: Path) -> None:
    fc = FakeConnection("pi")
    audit = AuditLog(root=tmp_path)
    sm = SessionManager(FakePool({"pi": fc}), audit, poll_interval=0.01)  # type: ignore[arg-type]

    # Phase 1: cat returns a sudo-prompt log.
    fc.add_handler(
        r"^tail -c ",
        lambda _cmd: FakeProc(0, "starting...\n[sudo] password for pi: ", ""),
    )
    r1 = await sm.run("pi/default", "sudo whoami")
    assert r1.status == "needs_password"
    log_id = r1.log_id

    # Phase 2: replace handler with a completed log.
    fc.handlers.clear()
    fc.add_handler(
        r"^tail -c ",
        lambda _cmd: FakeProc(0, _make_log(log_id, output="root", exit_code=0), ""),
    )
    r2 = await sm.wait("pi/default")
    assert r2.status == "done"
    assert r2.exit == 0
    assert r2.head == "root"
    audit.close()


async def test_kill_terminates_session_and_clears_state(tmp_path: Path) -> None:
    fc = FakeConnection("pi")
    fc.add_handler(
        r"^tail -c ",
        lambda cmd: FakeProc(
            0,
            _make_log(
                re.search(r"/logs/([0-9A-Z]+)\.log", cmd).group(1),  # type: ignore[union-attr]
                output="x",
            ),
            "",
        ),
    )
    audit = AuditLog(root=tmp_path)
    sm = SessionManager(FakePool({"pi": fc}), audit, poll_interval=0.01)  # type: ignore[arg-type]

    await sm.run("pi/default", "ok")
    result = await sm.kill("pi/default")
    assert result["killed"] is True
    assert any("tmux kill-session -t tai-mcp/default" in c for c in fc.run_calls)
    assert sm.list_sessions() == []

    records = _audit_records(tmp_path, "pi")
    kills = [r for r in records if r["tool"] == "session_kill"]
    assert kills and kills[0]["killed"] is True
    audit.close()


async def test_list_sessions_shape(tmp_path: Path) -> None:
    fc = FakeConnection("pi")
    fc.add_handler(
        r"^tail -c ",
        lambda cmd: FakeProc(
            0,
            _make_log(
                re.search(r"/logs/([0-9A-Z]+)\.log", cmd).group(1),  # type: ignore[union-attr]
                output="hi",
            ),
            "",
        ),
    )
    audit = AuditLog(root=tmp_path)
    sm = SessionManager(FakePool({"pi": fc}), audit, poll_interval=0.01)  # type: ignore[arg-type]

    await sm.run("pi/build", "ok")
    rows = sm.list_sessions()
    assert len(rows) == 1
    r = rows[0]
    assert r["session_id"] == "pi/build"
    assert r["host"] == "pi"
    assert r["name"] == "build"
    assert r["busy"] is False
    assert r["created_at"].endswith("Z")
    audit.close()


async def test_poll_uses_incremental_tail_not_full_cat(tmp_path: Path) -> None:
    # Each poll must fetch only bytes after state.log_offset via
    # `tail -c +<offset+1>`, never re-shipping the whole file.
    fc = FakeConnection("pi")
    captured_cmds: list[str] = []

    def handler(cmd: str) -> FakeProc:
        captured_cmds.append(cmd)
        m = re.search(r"/logs/([0-9A-Z]+)\.log", cmd)
        if not m:
            return FakeProc(0, "", "")
        return FakeProc(0, _make_log(m.group(1), output="ok"), "")

    fc.add_handler(r"^tail -c ", handler)

    audit = AuditLog(root=tmp_path)
    sm = SessionManager(FakePool({"pi": fc}), audit, poll_interval=0.01)  # type: ignore[arg-type]

    await sm.run("pi/default", "echo ok")
    log_reads = [c for c in captured_cmds if c.startswith("tail -c ")]
    assert log_reads, "expected at least one tail -c read"
    # First read starts from byte 1 (whole file from offset 0).
    assert log_reads[0].startswith("tail -c +1 ")
    # `cat <path>` must never appear — that's the regression we're guarding.
    assert not any(c.startswith("cat ") for c in captured_cmds)
    audit.close()


async def test_audit_records_session_run_with_reason(tmp_path: Path) -> None:
    fc = FakeConnection("pi")
    fc.add_handler(
        r"^tail -c ",
        lambda cmd: FakeProc(
            0,
            _make_log(
                re.search(r"/logs/([0-9A-Z]+)\.log", cmd).group(1),  # type: ignore[union-attr]
                output="hi",
                exit_code=0,
            ),
            "",
        ),
    )
    audit = AuditLog(root=tmp_path)
    sm = SessionManager(FakePool({"pi": fc}), audit, poll_interval=0.01)  # type: ignore[arg-type]

    await sm.run("pi/default", "uname -a", reason="check kernel")
    records = _audit_records(tmp_path, "pi")
    runs = [r for r in records if r["tool"] == "session_run"]
    assert runs
    last = runs[-1]
    assert last["cmd"] == "uname -a"
    assert last["reason"] == "check kernel"
    assert last["status"] == "done"
    assert last["exit"] == 0
    audit.close()


# ---------------------------------------------------------------------------
# Host-unreachable recovery (peer-reboot bug)
# ---------------------------------------------------------------------------


class FlakyPool:
    """Pool that returns a healthy conn, then raises HostUnreachable, then heals."""

    def __init__(self, healthy: FakeConnection, fail_until: int) -> None:
        self._conn = healthy
        self._fail_until = fail_until
        self.calls = 0

    async def get(self, alias: str) -> FakeConnection:
        self.calls += 1
        if self.calls <= self._fail_until:
            raise HostUnreachable(f"{alias}: peer rebooted")
        return self._conn


async def test_run_clears_local_state_on_host_unreachable(tmp_path: Path) -> None:
    """If the host vanishes mid-run, the registry must not keep a ghost busy entry."""
    healthy = FakeConnection("pi")
    healthy.add_handler(
        r"^tail -c ",
        lambda cmd: FakeProc(
            0,
            _make_log(
                re.search(r"/logs/([0-9A-Z]+)\.log", cmd).group(1),  # type: ignore[union-attr]
                output="ok",
            ),
            "",
        ),
    )
    pool = FlakyPool(healthy, fail_until=0)  # start healthy
    audit = AuditLog(root=tmp_path)
    sm = SessionManager(pool, audit, poll_interval=0.01)  # type: ignore[arg-type]

    # First call registers the session locally.
    await sm.run("pi/default", "ok")
    assert sm.list_sessions(), "session should be registered after first run"

    # Now the host goes away.
    pool._fail_until = 99  # type: ignore[attr-defined]
    with pytest.raises(HostUnreachable):
        await sm.run("pi/default", "next")

    # The registry must be cleared so subsequent calls aren't stuck on a ghost.
    assert sm.list_sessions() == []
    audit.close()


async def test_kill_succeeds_when_host_unreachable(tmp_path: Path) -> None:
    """session_kill must always clean up local state, even with a dead host."""
    healthy = FakeConnection("pi")
    healthy.add_handler(
        r"^tail -c ",
        lambda cmd: FakeProc(
            0,
            _make_log(
                re.search(r"/logs/([0-9A-Z]+)\.log", cmd).group(1),  # type: ignore[union-attr]
                output="ok",
            ),
            "",
        ),
    )
    pool = FlakyPool(healthy, fail_until=0)
    audit = AuditLog(root=tmp_path)
    sm = SessionManager(pool, audit, poll_interval=0.01)  # type: ignore[arg-type]

    await sm.run("pi/default", "ok")
    assert sm.list_sessions()

    # Host drops out before we kill.
    pool._fail_until = 99  # type: ignore[attr-defined]
    result = await sm.kill("pi/default")

    assert result == {"killed": False}
    assert sm.list_sessions() == []  # local state cleared regardless

    records = _audit_records(tmp_path, "pi")
    kills = [r for r in records if r["tool"] == "session_kill"]
    assert kills and kills[-1]["killed"] is False
    audit.close()


async def test_wait_clears_local_state_on_host_unreachable(tmp_path: Path) -> None:
    """session_wait must also surface HostUnreachable and forget the ghost session."""
    healthy = FakeConnection("pi")
    pool = FlakyPool(healthy, fail_until=99)  # host already gone
    audit = AuditLog(root=tmp_path)
    sm = SessionManager(pool, audit, poll_interval=0.01)  # type: ignore[arg-type]

    # Pre-seed a fake registered session so wait() has something to clean up.
    from tai_mcp_ssh.sessions import _SessionState  # local import keeps test isolated

    now = datetime.now(UTC)
    sm._sessions["pi/default"] = _SessionState(  # type: ignore[attr-defined]
        session_id="pi/default",
        host="pi",
        name="default",
        created_at=now,
        last_used_at=now,
        log_id="01J",
        log_path="/home/pi/.tai-ssh/logs/01J.log",
    )

    with pytest.raises(HostUnreachable):
        await sm.wait("pi/default")
    assert sm.list_sessions() == []
    audit.close()


# ---------------------------------------------------------------------------
# session_reset — lightweight recovery without killing the pane (issue #5)
# ---------------------------------------------------------------------------


def _wedged_connection() -> FakeConnection:
    """A connection whose log never yields a DONE sentinel, so a run() against
    it lands in `still_running` with `log_id` still set (an in-flight session)."""
    fc = FakeConnection("pi")
    fc.add_handler(r"^tail -c ", lambda _cmd: FakeProc(0, "", ""))
    return fc


async def test_reset_busy_session_returns_to_idle_and_reuses_pane(tmp_path: Path) -> None:
    fc = _wedged_connection()
    audit = AuditLog(root=tmp_path)
    sm = SessionManager(FakePool({"pi": fc}), audit, poll_interval=0.01)  # type: ignore[arg-type]

    first = await sm.run("pi/default", "wedged", timeout=0.1)
    assert first.status == "still_running"

    result = await sm.reset("pi/default")
    assert result == {"reset": True}

    # Next run is accepted (not busy) and REUSES the pane: exactly one tmux
    # create across both runs, and the pane is never killed.
    second = await sm.run("pi/default", "again", timeout=0.1)
    assert second.status != "busy"
    assert len([c for c in fc.run_calls if "tmux has-session" in c]) == 1
    assert not any("tmux kill-session" in c for c in fc.run_calls)
    audit.close()


async def test_reset_audits_one_record(tmp_path: Path) -> None:
    fc = _wedged_connection()
    audit = AuditLog(root=tmp_path)
    sm = SessionManager(FakePool({"pi": fc}), audit, poll_interval=0.01)  # type: ignore[arg-type]

    await sm.run("pi/default", "wedged", timeout=0.1)
    await sm.reset("pi/default")

    resets = [r for r in _audit_records(tmp_path, "pi") if r["tool"] == "session_reset"]
    assert len(resets) == 1
    assert resets[0]["session"] == "pi/default"
    assert resets[0]["log_id"]
    audit.close()


async def test_reset_idle_session_is_noop(tmp_path: Path) -> None:
    fc = FakeConnection("pi")
    fc.add_handler(
        r"^tail -c ",
        lambda cmd: FakeProc(
            0,
            _make_log(
                re.search(r"/logs/([0-9A-Z]+)\.log", cmd).group(1),  # type: ignore[union-attr]
                output="ok",
            ),
            "",
        ),
    )
    audit = AuditLog(root=tmp_path)
    sm = SessionManager(FakePool({"pi": fc}), audit, poll_interval=0.01)  # type: ignore[arg-type]

    done = await sm.run("pi/default", "ok")
    assert done.status == "done"  # session is now idle; log_id cleared

    result = await sm.reset("pi/default")
    assert result == {"reset": False}
    assert not [r for r in _audit_records(tmp_path, "pi") if r["tool"] == "session_reset"]
    audit.close()


async def test_reset_unknown_session_is_noop(tmp_path: Path) -> None:
    audit = AuditLog(root=tmp_path)
    sm = SessionManager(FakePool({}), audit)  # type: ignore[arg-type]
    result = await sm.reset("ghost/default")
    assert result == {"reset": False}
    assert _audit_records(tmp_path, "ghost") == []
    audit.close()


async def test_reset_works_when_host_unreachable(tmp_path: Path) -> None:
    # reset opens no connection, so a session wedged *because* the host went
    # flaky can still be cleared.
    class _HealThenDiePool:
        def __init__(self, conn: FakeConnection, die_after: int) -> None:
            self._conn = conn
            self._die_after = die_after
            self.calls = 0

        async def get(self, alias: str) -> FakeConnection:
            self.calls += 1
            if self.calls > self._die_after:
                raise HostUnreachable(f"{alias}: gone")
            return self._conn

    fc = _wedged_connection()
    pool = _HealThenDiePool(fc, die_after=1)
    audit = AuditLog(root=tmp_path)
    sm = SessionManager(pool, audit, poll_interval=0.01)  # type: ignore[arg-type]

    await sm.run("pi/default", "wedged", timeout=0.1)  # one get() -> calls == 1
    assert pool.calls == 1

    result = await sm.reset("pi/default")  # host now unreachable
    assert result == {"reset": True}
    assert pool.calls == 1  # reset opened no connection
    audit.close()
