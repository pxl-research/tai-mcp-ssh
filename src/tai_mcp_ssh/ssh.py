"""asyncssh connection pool with first-connect bootstrap.

Opens one ``asyncssh.SSHClientConnection`` per allowlisted host and caches
it for reuse. On the first ``get(alias)`` for a host the pool verifies
that ``tmux`` is installed on the remote and ensures ``~/.tai-ssh/logs/``
exists. While the host is in use the pool also runs best-effort background
maintenance — a sweep of stale remote log files and a reap of idle
``tai/*`` tmux sessions — at most once per hour per host.

Password auth resolves the ``keychain://`` reference once at connect
time; the resolved string is dropped immediately after the handshake.
"""

from __future__ import annotations

import asyncio
import contextlib
import re
import shlex
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import asyncssh
import keyring
import keyring.errors

from tai_mcp_ssh.audit import AuditLog
from tai_mcp_ssh.config import DEFAULT_SESSION_IDLE_TIMEOUT_HOURS, Host
from tai_mcp_ssh.errors import (
    ConfigError,
    HostNotAllowed,
    HostUnreachable,
    KeychainUnavailable,
    TmuxMissing,
)

_KEYCHAIN_PREFIX = "keychain://"
_REMOTE_LOG_DIR = "~/.tai-ssh/logs"

# Background maintenance (log sweep + idle-session reap) runs at most this
# often per host. A long-lived MCP connects once, so "on first connect" alone
# would effectively never re-run.
_MAINTENANCE_INTERVAL_S = 3600.0

# Session reaping. Only our own sessions are candidates: `tai/*`, plus the
# legacy `tai-mcp/*` prefix left behind by versions before the rename.
_REAP_NAME_RE = re.compile(r"^(?:tai|tai-mcp)/")
# A pane running one of these is sitting at a prompt; anything else (rsync,
# sudo waiting for a password, an editor) means the session is in use.
_IDLE_SHELLS = frozenset({"bash", "zsh", "sh", "dash", "fish"})
# Remote clock first, so idle age never depends on local/remote clock skew.
_LIST_PANES_CMD = (
    "date +%s; tmux list-panes -a -F "
    "'#{session_name}\t#{session_attached}\t#{session_activity}"
    "\t#{pane_current_command}\t#{pane_pid}'"
)

# Exceptions that indicate the SSH transport is gone (peer reboot, network
# drop, sshd kill). Caught at the Connection boundary (and by transfer.py
# around SFTP write/read loops) so the pool can evict the dead conn.
DEAD_CONN_ERRORS: tuple[type[BaseException], ...] = (
    asyncssh.ConnectionLost,
    asyncssh.DisconnectError,
    asyncssh.ChannelOpenError,
    OSError,
    EOFError,
)


def _as_str(value: bytes | str | None) -> str:
    """Coerce asyncssh's bytes|str|None stdout/stderr to a plain str."""
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value


# An asyncssh.connect-shaped callable; injectable so tests can supply a fake.
ConnectFn = Callable[..., Awaitable[asyncssh.SSHClientConnection]]


class Connection:
    """Live SSH connection to one managed host plus first-connect bootstrap state.

    Thin wrapper around ``asyncssh.SSHClientConnection`` exposing only what
    sessions/transfer need. The pool builds these; callers get them via
    :meth:`ConnectionPool.get`.
    """

    def __init__(
        self,
        host: Host,
        ssh_conn: asyncssh.SSHClientConnection,
        audit: AuditLog,
    ) -> None:
        self._host = host
        self._conn = ssh_conn
        self._audit = audit
        self._tmux_path: str | None = None
        self._home_dir: str | None = None
        self._dead = False

    @property
    def alias(self) -> str:
        return self._host.alias

    @property
    def host(self) -> Host:
        return self._host

    @property
    def tmux_path(self) -> str | None:
        return self._tmux_path

    @property
    def dead(self) -> bool:
        """True once a transport-dead error was observed on this connection."""
        return self._dead

    def mark_dead(self) -> None:
        """Flag this connection as transport-dead so the pool evicts it on next get()."""
        self._dead = True

    @property
    def home_dir(self) -> str:
        """Absolute path of the remote user's home dir. Set during _ensure_ready."""
        if self._home_dir is None:
            raise RuntimeError(f"{self.alias}: home_dir not resolved (Connection not ready)")
        return self._home_dir

    async def run(
        self, command: str, *, check: bool = False, encoding: str | None = "utf-8"
    ) -> asyncssh.SSHCompletedProcess:
        """Execute a one-shot command. Use sessions.py for stateful operations.

        ``encoding=None`` returns raw ``bytes`` stdout/stderr; callers that need
        exact byte accounting (the session log reader) rely on this.
        """
        if self._dead:
            raise HostUnreachable(f"{self.alias}: connection already dead")
        try:
            return await self._conn.run(command, check=check, encoding=encoding)
        except DEAD_CONN_ERRORS as exc:
            self._dead = True
            raise HostUnreachable(f"{self.alias}: {exc}") from exc

    async def start_sftp(self) -> asyncssh.SFTPClient:
        if self._dead:
            raise HostUnreachable(f"{self.alias}: connection already dead")
        try:
            return await self._conn.start_sftp_client()
        except DEAD_CONN_ERRORS as exc:
            self._dead = True
            raise HostUnreachable(f"{self.alias}: {exc}") from exc

    async def close(self) -> None:
        self._conn.close()
        await self._conn.wait_closed()


class ConnectionPool:
    """Per-process pool of open SSH connections keyed by host alias.

    Connections are opened lazily and reused across tool calls. A per-alias
    ``asyncio.Lock`` serialises opens so two concurrent ``get(alias)``
    don't double-open. The "ready" check (tmux + log dir) runs once per
    alias on first successful open.
    """

    def __init__(
        self,
        hosts: dict[str, Host],
        audit: AuditLog,
        *,
        connect: ConnectFn | None = None,
        ssh_config: Path | None = None,
        idle_timeout_hours: int = DEFAULT_SESSION_IDLE_TIMEOUT_HOURS,
        monotonic: Callable[[], float] | None = None,
    ) -> None:
        self._hosts = hosts
        self._audit = audit
        self._idle_timeout_hours = idle_timeout_hours
        self._monotonic = monotonic or time.monotonic
        self._connect: ConnectFn = connect or asyncssh.connect
        self._ssh_config = ssh_config or (Path.home() / ".ssh" / "config")
        self._connections: dict[str, Connection] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._ready: set[str] = set()
        # Monotonic time of the last maintenance run per alias. Survives
        # eviction on purpose, so a reconnect storm can't re-trigger it.
        self._last_maintenance: dict[str, float] = {}
        # Strong refs to in-flight fire-and-forget maintenance tasks so the
        # event loop doesn't garbage-collect them mid-run (only a weak ref is
        # held otherwise). Each task removes itself on completion.
        self._maintenance_tasks: set[asyncio.Task[None]] = set()

    async def get(self, alias: str) -> Connection:
        """Return an open, bootstrap-checked :class:`Connection` for ``alias``.

        Evicts and re-opens transparently if the cached connection was marked
        dead by a previous transport error (peer reboot, network drop).
        """
        if alias not in self._hosts:
            raise HostNotAllowed(alias)

        lock = self._locks.setdefault(alias, asyncio.Lock())
        async with lock:
            conn = self._connections.get(alias)
            if conn is not None and conn.dead:
                await self._evict(alias)
                conn = None
            if conn is None:
                conn = await self._open(alias)
                self._connections[alias] = conn
            if alias not in self._ready:
                try:
                    await self._ensure_ready(conn)
                except Exception:
                    # Bootstrap failed (likely transport died mid-handshake);
                    # don't leave a half-initialised conn in the cache.
                    await self._evict(alias)
                    raise
                self._ready.add(alias)
            self._schedule_maintenance(conn)
            return conn

    async def close_all(self) -> None:
        for conn in self._connections.values():
            # Best-effort teardown — swallow individual close errors so one
            # bad connection doesn't strand the rest.
            with contextlib.suppress(Exception):
                await conn.close()
        self._connections.clear()
        self._ready.clear()

    async def update_hosts(
        self,
        new_hosts: dict[str, Host],
        evict: set[str],
        *,
        idle_timeout_hours: int | None = None,
    ) -> None:
        """Swap the in-memory allowlist and close cached connections for ``evict``.

        Called from :meth:`Services.reload_hosts_from_disk` when the user edits
        ``hosts.toml`` at runtime. The per-alias lock is acquired before each
        eviction so a concurrent ``get(alias)`` doesn't race with the close
        (matches the contract that ``_evict`` is always called under-lock).
        ``idle_timeout_hours``, when given, applies from the next maintenance run.
        """
        self._hosts = new_hosts
        if idle_timeout_hours is not None:
            self._idle_timeout_hours = idle_timeout_hours
        for alias in evict:
            lock = self._locks.setdefault(alias, asyncio.Lock())
            async with lock:
                await self._evict(alias)

    # Internals -------------------------------------------------------------

    async def _evict(self, alias: str) -> None:
        """Drop a cached connection and its ready-state; best-effort close."""
        conn = self._connections.pop(alias, None)
        self._ready.discard(alias)
        if conn is not None:
            with contextlib.suppress(Exception):
                await conn.close()

    async def _open(self, alias: str) -> Connection:
        host = self._hosts[alias]
        kwargs = self._build_connect_kwargs(host)
        if host.auth == "password":
            kwargs["password"] = await self._resolve_password(host)
        try:
            ssh_conn = await self._connect(**kwargs)
        except DEAD_CONN_ERRORS as exc:
            raise HostUnreachable(f"{alias}: {exc}") from exc
        # password (if any) goes out of scope here.
        return Connection(host, ssh_conn, self._audit)

    def _build_connect_kwargs(self, host: Host) -> dict[str, Any]:
        # Pass the alias as the connect target so asyncssh can apply the
        # matching ~/.ssh/config Host block (HostName, Port, User, etc.).
        # Inline values from hosts.toml override the config when present.
        kwargs: dict[str, Any] = {
            "host": host.host or host.alias,
            # Surface a dead transport within ~90s (3 × 30s) so the pool's
            # eviction path runs instead of hanging on kernel TCP timeout.
            "keepalive_interval": 30,
            "keepalive_count_max": 3,
        }
        # asyncssh raises FileNotFoundError if `config` points at a missing
        # file. Treat a non-existent ssh_config as "no config" rather than
        # a hard error so first-run / minimal setups work.
        if self._ssh_config.is_file():
            kwargs["config"] = [str(self._ssh_config)]
        if host.user is not None:
            kwargs["username"] = host.user
        if host.port != 22:
            kwargs["port"] = host.port

        if host.auth == "key":
            if host.identity_file is not None:
                kwargs["client_keys"] = [host.identity_file]
        else:  # password — skip publickey attempts so we don't burn time on agent/keys
            kwargs["client_keys"] = ()
            kwargs["gss_auth"] = False

        return kwargs

    async def _resolve_password(self, host: Host) -> str:
        if host.password_ref is None:
            raise ConfigError(f"{host.alias}: password_ref is required for password auth")
        if not host.password_ref.startswith(_KEYCHAIN_PREFIX):
            raise ConfigError(f"{host.alias}: password_ref must use the keychain:// scheme")
        service_account = host.password_ref[len(_KEYCHAIN_PREFIX) :]
        service, _, account = service_account.partition("/")
        if not service or not account:
            raise ConfigError(
                f"{host.alias}: malformed password_ref {host.password_ref!r}; "
                f"expected keychain://<service>/<account>"
            )

        try:
            password = await asyncio.to_thread(keyring.get_password, service, account)
        except keyring.errors.KeyringError as exc:
            raise KeychainUnavailable(f"{host.alias}: keychain access failed: {exc}") from exc

        if password is None:
            raise KeychainUnavailable(
                f"{host.alias}: no entry in keychain for {host.password_ref}; "
                f"run `tai-mcp-ssh hosts add {host.alias}` to store the password."
            )
        return password

    async def _ensure_ready(self, conn: Connection) -> None:
        """Run first-connect bootstrap: tmux check and log dir mkdir."""
        # 1. tmux presence — must succeed; everything else depends on tmux.
        result = await conn.run("command -v tmux", check=False)
        stdout = _as_str(result.stdout).strip()
        if result.exit_status != 0 or not stdout:
            await self._audit.record("_tmux_check", host=conn.alias, status="missing")
            raise TmuxMissing(
                f"{conn.alias}: tmux is not installed on the remote. "
                f"Install with `sudo apt install tmux` (Debian/Ubuntu/RPi OS) "
                f"or equivalent for your distro."
            )
        conn._tmux_path = stdout
        await self._audit.record("_tmux_check", host=conn.alias, status="ok", tmux_path=stdout)

        # 2. Resolve $HOME so callers can build absolute log paths per spec.
        home_result = await conn.run("echo $HOME", check=True)
        home = _as_str(home_result.stdout).strip()
        if not home:
            raise RuntimeError(f"{conn.alias}: could not resolve $HOME on the remote")
        conn._home_dir = home

        # 3. Remote log dir.
        await conn.run(f"mkdir -p -m 0700 {_REMOTE_LOG_DIR}", check=True)
        await self._audit.record("_logdir_check", host=conn.alias, status="ok")

    def _schedule_maintenance(self, conn: Connection) -> None:
        """Fire-and-forget :meth:`_maintain` if the last run is over an hour old.

        Called under the per-alias lock, and the timestamp is taken before the
        task starts, so concurrent ``get()`` calls never schedule twice.
        """
        now = self._monotonic()
        last = self._last_maintenance.get(conn.alias)
        if last is not None and now - last < _MAINTENANCE_INTERVAL_S:
            return
        self._last_maintenance[conn.alias] = now
        task = asyncio.create_task(self._maintain(conn))
        self._maintenance_tasks.add(task)
        task.add_done_callback(self._maintenance_tasks.discard)

    async def _maintain(self, conn: Connection) -> None:
        """Best-effort housekeeping; each step audits its own failures and
        never raises, so one failing step can't skip the other."""
        await self._sweep_remote_logs(conn, conn.host.log_retention_days)
        await self._reap_idle_sessions(conn, self._idle_timeout_hours)

    async def _sweep_remote_logs(self, conn: Connection, retention_days: int) -> None:
        """Delete remote ``~/.tai-ssh/logs/*.log`` files older than ``retention_days``.

        Best-effort: failures audit and return; never raised. Runs from
        :meth:`_maintain`.
        """
        try:
            cmd = (
                f"find {_REMOTE_LOG_DIR} -type f -name '*.log' "
                f"-mtime +{retention_days} -delete -print"
            )
            result = await conn.run(cmd, check=False)
            stdout = _as_str(result.stdout)
            deleted = len(stdout.strip().splitlines()) if stdout else 0
            await self._audit.record(
                "_remote_sweep",
                host=conn.alias,
                retention_days=retention_days,
                deleted=deleted,
                status="ok" if result.exit_status == 0 else "partial",
            )
        except Exception as exc:  # noqa: BLE001 — best-effort, never raise
            await self._audit.record(
                "_remote_sweep",
                host=conn.alias,
                retention_days=retention_days,
                status="error",
                error=str(exc),
            )

    async def _reap_idle_sessions(self, conn: Connection, idle_timeout_hours: int) -> None:
        """Kill idle ``tai/*`` (and legacy ``tai-mcp/*``) tmux sessions.

        A session is a candidate only when it is unattached, every pane sits
        at a plain shell prompt, and its last activity is older than the
        timeout (see :func:`select_idle_sessions`). Each candidate is killed
        only if ``pgrep`` positively reports no child processes under its
        pane shells, so background jobs (``cmd &``) are never SIGHUP'd.
        Best-effort: failures audit and return; never raised.
        """
        if idle_timeout_hours <= 0:
            return
        try:
            result = await conn.run(_LIST_PANES_CMD, check=False)
            stdout = _as_str(result.stdout)
            if result.exit_status != 0:
                stderr = _as_str(result.stderr)
                if "no server running" not in stderr and "error connecting" not in stderr:
                    raise RuntimeError(f"tmux list-panes failed: {stderr.strip()}")
                stdout = ""  # no tmux server -> no sessions to reap
            candidates = select_idle_sessions(stdout, idle_timeout_hours)
            killed: list[str] = []
            if candidates:
                kill = await conn.run(_kill_command(candidates), check=False)
                names = {name for name, _ in candidates}
                killed = [n for n in _as_str(kill.stdout).splitlines() if n in names]
            await self._audit.record(
                "_session_sweep",
                host=conn.alias,
                idle_timeout_hours=idle_timeout_hours,
                killed=killed,
                status="ok",
            )
        except Exception as exc:  # noqa: BLE001 — best-effort, never raise
            await self._audit.record(
                "_session_sweep",
                host=conn.alias,
                idle_timeout_hours=idle_timeout_hours,
                killed=[],
                status="error",
                error=str(exc),
            )


def select_idle_sessions(listing: str, idle_timeout_hours: int) -> list[tuple[str, list[int]]]:
    """Pick reapable sessions from ``_LIST_PANES_CMD`` output.

    ``listing`` is the remote ``date +%s`` line followed by one tab-separated
    row per pane: name, attached count, session_activity (epoch), current
    command, pane pid. Returns ``(session_name, pane_pids)`` for every session
    whose name is ours, is unattached, has been idle longer than the timeout,
    and whose panes are *all* at a shell prompt. Malformed rows are ignored.
    """
    lines = listing.splitlines()
    if not lines:
        return []
    try:
        now = int(lines[0].strip())
    except ValueError:
        return []
    cutoff = now - idle_timeout_hours * 3600

    sessions: dict[str, tuple[bool, list[int]]] = {}
    for line in lines[1:]:
        parts = line.split("\t")
        if len(parts) != 5:
            continue
        name, attached, activity, command, pid = parts
        if not _REAP_NAME_RE.match(name):
            continue
        try:
            attached_n, activity_ts, pane_pid = int(attached), int(activity), int(pid)
        except ValueError:
            continue
        # Login shells report as `-bash`.
        at_prompt = command.lstrip("-") in _IDLE_SHELLS
        idle = attached_n == 0 and activity_ts < cutoff and at_prompt
        ok, pids = sessions.get(name, (True, []))
        sessions[name] = (ok and idle, [*pids, pane_pid])
    return [(name, pids) for name, (ok, pids) in sessions.items() if ok]


def _kill_command(candidates: list[tuple[str, list[int]]]) -> str:
    """One shell line that kills each candidate whose pane shells have no
    children, echoing the names it killed.

    ``pgrep -P`` exits 1 only when it positively found no children; any other
    status (children found, pgrep missing or failing) skips the session.
    """
    parts = []
    for name, pids in candidates:
        no_children = " && ".join(
            f"{{ pgrep -P {pid} >/dev/null 2>&1; [ $? -eq 1 ]; }}" for pid in pids
        )
        parts.append(
            f"{no_children} && tmux kill-session -t {shlex.quote('=' + name)} "
            f"&& printf '%s\\n' {shlex.quote(name)}"
        )
    return "; ".join(parts)
