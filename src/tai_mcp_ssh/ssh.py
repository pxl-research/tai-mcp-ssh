"""asyncssh connection pool with first-connect bootstrap.

Opens one ``asyncssh.SSHClientConnection`` per allowlisted host and caches
it for reuse. On the first ``get(alias)`` for a host the pool verifies
that ``tmux`` is installed on the remote, ensures ``~/.tai-ssh/logs/``
exists, and kicks off a best-effort sweep of stale remote log files.

Password auth resolves the ``keychain://`` reference once at connect
time; the resolved string is dropped immediately after the handshake.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import asyncssh
import keyring
import keyring.errors

from tai_mcp_ssh.audit import AuditLog
from tai_mcp_ssh.config import Host
from tai_mcp_ssh.errors import (
    ConfigError,
    HostNotAllowed,
    HostUnreachable,
    KeychainUnavailable,
    TmuxMissing,
)

_KEYCHAIN_PREFIX = "keychain://"
_REMOTE_LOG_DIR = "~/.tai-ssh/logs"

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
    ) -> None:
        self._hosts = hosts
        self._audit = audit
        self._connect: ConnectFn = connect or asyncssh.connect
        self._ssh_config = ssh_config or (Path.home() / ".ssh" / "config")
        self._connections: dict[str, Connection] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._ready: set[str] = set()
        # Strong refs to in-flight fire-and-forget sweep tasks so the event
        # loop doesn't garbage-collect them mid-run (only a weak ref is held
        # otherwise). Each task removes itself on completion.
        self._sweep_tasks: set[asyncio.Task[None]] = set()

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
            return conn

    async def close_all(self) -> None:
        for conn in self._connections.values():
            # Best-effort teardown — swallow individual close errors so one
            # bad connection doesn't strand the rest.
            with contextlib.suppress(Exception):
                await conn.close()
        self._connections.clear()
        self._ready.clear()

    async def update_hosts(self, new_hosts: dict[str, Host], evict: set[str]) -> None:
        """Swap the in-memory allowlist and close cached connections for ``evict``.

        Called from :meth:`Services.reload_hosts_from_disk` when the user edits
        ``hosts.toml`` at runtime. The per-alias lock is acquired before each
        eviction so a concurrent ``get(alias)`` doesn't race with the close
        (matches the contract that ``_evict`` is always called under-lock).
        """
        self._hosts = new_hosts
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
        """Run first-connect bootstrap: tmux check, log dir mkdir, sweep schedule."""
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

        # 4. Best-effort remote-log retention sweep, fire-and-forget. Keep a
        # strong reference until the task finishes so it isn't GC'd mid-run.
        task = asyncio.create_task(self._sweep_remote_logs(conn, conn.host.log_retention_days))
        self._sweep_tasks.add(task)
        task.add_done_callback(self._sweep_tasks.discard)

    async def _sweep_remote_logs(self, conn: Connection, retention_days: int) -> None:
        """Delete remote ``~/.tai-ssh/logs/*.log`` files older than ``retention_days``.

        Best-effort: failures audit and return; never raised. Runs as a
        background task scheduled from :meth:`_ensure_ready`.
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
