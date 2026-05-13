"""tmux-backed remote sessions with sentinel completion and prompt detection.

Composite session IDs ``<host>/<name>``. On first use a tmux session named
``tai-mcp/<name>`` is created on the remote host. Each command is wrapped
with start/done markers; pane output is piped to
``~/.tai-ssh/logs/<log_id>.log`` and polled for the completion sentinel or
known interactive prompts (sudo, hostkey, apt-confirm, ...).
"""

from __future__ import annotations

import asyncio
import re
import shlex
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal

from ulid import ULID

from tai_mcp_ssh.audit import AuditLog
from tai_mcp_ssh.ssh import Connection, ConnectionPool, _as_str

_TMUX_PREFIX = "tai-mcp"

# Output-slice thresholds (per output-capture spec).
_FULL_THRESHOLD_LINES = 50
_FULL_THRESHOLD_BYTES = 4096
_HEAD_MAX_LINES = 50
_HEAD_MAX_BYTES = 2048
_TAIL_MAX_LINES = 50
_TAIL_MAX_BYTES = 2048

# Prompt patterns anchored at end-of-content (\Z) so mid-stream literal
# matches don't false-positive. Order matters only when one prompt happens
# to be a suffix of another (none do here).
_PROMPT_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\[sudo\] password for [^:\n]+:[ \t]*\Z"), "needs_password"),
    (re.compile(r"(?:^|\n)Password:[ \t]*\Z"), "needs_password"),
    (
        re.compile(
            r"Are you sure you want to continue connecting "
            r"\(yes/no(?:/\[fingerprint\])?\)\?[ \t]*\Z"
        ),
        "needs_input",
    ),
    (re.compile(r"\[Y/n\][ \t]*\Z", re.IGNORECASE), "needs_input"),
    (re.compile(r"\[y/N\][ \t]*\Z", re.IGNORECASE), "needs_input"),
]

Status = Literal["done", "still_running", "needs_password", "needs_input", "busy"]


@dataclass(frozen=True, slots=True)
class RunResult:
    """Shape returned by ``session_run`` / ``session_wait``."""

    session_id: str
    status: Status
    head: str
    tail: str
    bytes: int
    truncated: bool
    log_id: str
    log_path: str
    exit: int | None = None
    attach_hint: str | None = None
    prompt: str | None = None


@dataclass(slots=True)
class _SessionState:
    session_id: str
    host: str
    name: str
    created_at: datetime
    last_used_at: datetime
    # Active-command fields — None when the session is idle.
    log_id: str | None = None
    log_path: str | None = None
    started_at: datetime | None = None
    command: str | None = None
    reason: str | None = None


def parse_session_id(session_id: str) -> tuple[str, str]:
    """Split ``<host>/<name>`` into its parts. Raises ``ValueError`` if malformed."""
    if "/" not in session_id:
        raise ValueError(f"Invalid session_id {session_id!r}: expected '<host>/<name>'")
    host, _, name = session_id.partition("/")
    if not host or not name:
        raise ValueError(f"Invalid session_id {session_id!r}: host and name must both be non-empty")
    return host, name


class SessionManager:
    """Coordinates tmux-backed sessions across hosts.

    One instance per process. Construct with the shared ``ConnectionPool``
    and ``AuditLog``.
    """

    def __init__(
        self,
        pool: ConnectionPool,
        audit: AuditLog,
        *,
        now: Callable[[], datetime] | None = None,
        poll_interval: float = 0.2,
    ) -> None:
        self._pool = pool
        self._audit = audit
        self._now = now or (lambda: datetime.now(UTC))
        self._poll_interval = poll_interval
        self._sessions: dict[str, _SessionState] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    # Public API ------------------------------------------------------------

    async def run(
        self,
        session_id: str,
        command: str,
        *,
        reason: str | None = None,
        timeout: float = 30.0,
    ) -> RunResult:
        host, name = parse_session_id(session_id)
        conn = await self._pool.get(host)

        lock = self._locks.setdefault(session_id, asyncio.Lock())
        async with lock:
            state = self._sessions.get(session_id)
            if state is not None and state.log_id is not None:
                # Session is mid-command — refuse with `busy` and let the
                # caller decide whether to wait or kill.
                return self._busy_result(state)

            if state is None:
                state = await self._create_session(conn, session_id, host, name)
                self._sessions[session_id] = state

            log_id = str(ULID())
            log_path = f"{conn.home_dir}/.tai-ssh/logs/{log_id}.log"
            state.log_id = log_id
            state.log_path = log_path
            state.command = command
            state.reason = reason
            state.started_at = self._now()
            state.last_used_at = state.started_at

            await self._send_command(conn, name, log_id, log_path, command)

        return await self._poll(conn, state, timeout, tool="session_run")

    async def wait(self, session_id: str, *, timeout: float = 30.0) -> RunResult:
        host, _ = parse_session_id(session_id)
        conn = await self._pool.get(host)

        state = self._sessions.get(session_id)
        if state is None or state.log_id is None:
            return self._idle_result(session_id)

        return await self._poll(conn, state, timeout, tool="session_wait")

    async def kill(self, session_id: str) -> dict[str, bool]:
        host, name = parse_session_id(session_id)
        conn = await self._pool.get(host)

        result = await conn.run(f"tmux kill-session -t {_TMUX_PREFIX}/{name}", check=False)
        killed = result.exit_status == 0
        self._sessions.pop(session_id, None)
        self._locks.pop(session_id, None)
        await self._audit.record("session_kill", host=host, session=session_id, killed=killed)
        return {"killed": killed}

    def list_sessions(self) -> list[dict[str, Any]]:
        return [
            {
                "session_id": s.session_id,
                "host": s.host,
                "name": s.name,
                "created_at": _iso(s.created_at),
                "last_used_at": _iso(s.last_used_at),
                "busy": s.log_id is not None,
            }
            for s in self._sessions.values()
        ]

    # Internals -------------------------------------------------------------

    async def _create_session(
        self, conn: Connection, session_id: str, host: str, name: str
    ) -> _SessionState:
        # Idempotent: succeeds whether the tmux session exists from a prior
        # MCP run or not. Without `has-session` first, `new-session` would
        # error on re-attach.
        tmux_target = f"{_TMUX_PREFIX}/{name}"
        await conn.run(
            f"tmux has-session -t {tmux_target} 2>/dev/null "
            f"|| tmux new-session -d -s {tmux_target}",
            check=True,
        )
        now = self._now()
        await self._audit.record("_session_create", host=host, session=session_id)
        return _SessionState(
            session_id=session_id,
            host=host,
            name=name,
            created_at=now,
            last_used_at=now,
        )

    async def _send_command(
        self,
        conn: Connection,
        name: str,
        log_id: str,
        log_path: str,
        command: str,
    ) -> None:
        tmux_target = f"{_TMUX_PREFIX}/{name}"

        # Wrap with START/DONE markers. The shell prompt's echo of the typed
        # text contains the *literal* markers, so we don't confuse them with
        # the actual `echo` output: the start marker line is the bare string
        # `__TAI_START__<id>__` whereas the echoed command line begins with
        # the prompt and includes `echo __TAI_START__<id>__; ...`.
        wrapped = f"echo __TAI_START__{log_id}__; {command}; echo __TAI_DONE__$?__{log_id}__"

        # Reset pipe-pane then redirect to the new per-command log file.
        # Two `pipe-pane` invocations chained by `;` is one ssh round trip.
        pipe_cmd = f"cat >> {log_path}"
        await conn.run(
            f"tmux pipe-pane -t {tmux_target}; "
            f"tmux pipe-pane -t {tmux_target} {shlex.quote(pipe_cmd)}",
            check=True,
        )

        # Type the command literally (so semicolons/quotes survive untouched),
        # then a separate `Enter` keystroke to execute.
        await conn.run(
            f"tmux send-keys -t {tmux_target} -l {shlex.quote(wrapped)}",
            check=True,
        )
        await conn.run(f"tmux send-keys -t {tmux_target} Enter", check=True)

    async def _poll(
        self,
        conn: Connection,
        state: _SessionState,
        timeout: float,
        *,
        tool: str,
    ) -> RunResult:
        assert state.log_id is not None
        assert state.log_path is not None
        log_id = state.log_id
        log_path = state.log_path
        done_re = re.compile(rf"^__TAI_DONE__(\d+)__{re.escape(log_id)}__\s*$", re.MULTILINE)

        call_start = self._now()
        while True:
            content = await self._read_log(conn, log_path)

            # 1. Completion sentinel — the only "done" signal we trust.
            m = done_re.search(content)
            if m:
                exit_code = int(m.group(1))
                output = _extract_output(content, log_id)
                result = self._build_result(state, output, "done", exit_code=exit_code)
                await self._audit_event(state, result, tool=tool)
                self._clear_state(state)
                return result

            # 2. Interactive prompt at the tail.
            prompt_match = _detect_prompt(content)
            if prompt_match:
                status, prompt = prompt_match
                output = _extract_partial_output(content, log_id)
                result = self._build_result(
                    state,
                    output,
                    status,
                    prompt=prompt,
                    attach_hint=self._attach_hint(state),
                )
                await self._audit_event(state, result, tool=tool)
                return result

            # 3. Timeout — return what we have, mark `still_running`.
            elapsed = (self._now() - call_start).total_seconds()
            if elapsed >= timeout:
                output = _extract_partial_output(content, log_id)
                result = self._build_result(state, output, "still_running")
                await self._audit_event(state, result, tool=tool)
                return result

            await asyncio.sleep(self._poll_interval)

    async def _read_log(self, conn: Connection, log_path: str) -> str:
        result = await conn.run(f"cat {shlex.quote(log_path)}", check=False)
        if result.exit_status != 0:
            return ""
        return _as_str(result.stdout)

    def _attach_hint(self, state: _SessionState) -> str:
        return f"ssh {state.host} -t tmux attach -t {_TMUX_PREFIX}/{state.name}"

    def _build_result(
        self,
        state: _SessionState,
        output: str,
        status: Status,
        *,
        exit_code: int | None = None,
        attach_hint: str | None = None,
        prompt: str | None = None,
    ) -> RunResult:
        assert state.log_id is not None
        assert state.log_path is not None
        head, tail, truncated = _slice_output(output)
        return RunResult(
            session_id=state.session_id,
            status=status,
            head=head,
            tail=tail,
            bytes=len(output.encode("utf-8")),
            truncated=truncated,
            log_id=state.log_id,
            log_path=state.log_path,
            exit=exit_code,
            attach_hint=attach_hint,
            prompt=prompt,
        )

    def _busy_result(self, state: _SessionState) -> RunResult:
        assert state.log_id is not None
        assert state.log_path is not None
        return RunResult(
            session_id=state.session_id,
            status="busy",
            head="",
            tail="",
            bytes=0,
            truncated=False,
            log_id=state.log_id,
            log_path=state.log_path,
        )

    def _idle_result(self, session_id: str) -> RunResult:
        return RunResult(
            session_id=session_id,
            status="done",
            head="",
            tail="",
            bytes=0,
            truncated=False,
            log_id="",
            log_path="",
        )

    def _clear_state(self, state: _SessionState) -> None:
        state.log_id = None
        state.log_path = None
        state.command = None
        state.reason = None
        state.started_at = None
        state.last_used_at = self._now()

    async def _audit_event(self, state: _SessionState, result: RunResult, *, tool: str) -> None:
        duration_ms: int | None = None
        if state.started_at is not None:
            duration_ms = int((self._now() - state.started_at).total_seconds() * 1000)
        await self._audit.record(
            tool,
            host=state.host,
            session=state.session_id,
            cmd=state.command,
            reason=state.reason,
            exit=result.exit,
            status=result.status,
            duration_ms=duration_ms,
            stdout_bytes=result.bytes,
            log_id=result.log_id,
            truncated=result.truncated,
            needs_password=result.status == "needs_password",
        )


# Module-level helpers -----------------------------------------------------


def _iso(ts: datetime) -> str:
    return ts.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _extract_output(content: str, log_id: str) -> str:
    """Output strictly between the START and DONE markers (exclusive)."""
    start_marker = f"__TAI_START__{log_id}__"
    done_pattern = re.compile(rf"^__TAI_DONE__(\d+)__{re.escape(log_id)}__\s*$")
    lines = content.splitlines()
    start_idx: int | None = None
    end_idx: int | None = None
    for i, line in enumerate(lines):
        stripped = line.rstrip("\r")
        if start_idx is None and stripped == start_marker:
            start_idx = i + 1
        elif start_idx is not None and done_pattern.match(stripped):
            end_idx = i
            break
    if start_idx is None or end_idx is None:
        return ""
    return "\n".join(lines[start_idx:end_idx])


def _extract_partial_output(content: str, log_id: str) -> str:
    """Output collected so far when no DONE marker has appeared yet."""
    start_marker = f"__TAI_START__{log_id}__"
    lines = content.splitlines()
    for i, line in enumerate(lines):
        if line.rstrip("\r") == start_marker:
            return "\n".join(lines[i + 1 :])
    return ""


def _detect_prompt(content: str) -> tuple[Status, str] | None:
    """Return ``(status, matched prompt)`` if the *tail* of content matches a known prompt."""
    tail = content[-500:]
    for pattern, status in _PROMPT_PATTERNS:
        m = pattern.search(tail)
        if m:
            # Status is statically one of the prompt statuses; mypy can't see
            # that from the local tuple type.
            return status, m.group(0).strip()  # type: ignore[return-value]
    return None


def _slice_output(output: str) -> tuple[str, str, bool]:
    """Return ``(head, tail, truncated)`` per spec output-capture thresholds."""
    lines = output.splitlines(keepends=True)
    if len(lines) <= _FULL_THRESHOLD_LINES and len(output) <= _FULL_THRESHOLD_BYTES:
        return output, "", False
    head = "".join(lines[:_HEAD_MAX_LINES])[:_HEAD_MAX_BYTES]
    tail_text = "".join(lines[-_TAIL_MAX_LINES:])
    tail = tail_text[-_TAIL_MAX_BYTES:]
    return head, tail, True
