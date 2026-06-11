"""MCP stdio server exposing the 7-tool surface.

The MCP integration is intentionally thin: tool inputs are dispatched to
the underlying managers (:class:`SessionManager`, :class:`TransferManager`,
:class:`ConnectionPool`) and results are JSON-encoded into a single
``TextContent`` block.

Audit records for ordinary calls are emitted by the managers themselves;
this module only audits early rejections (allowlist miss, malformed
session id, tmux missing, keychain unavailable) so every tool invocation
still produces exactly one record.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import asdict, is_dataclass
from typing import Any

import mcp.server.stdio
import mcp.types as mtypes
from mcp.server.lowlevel import NotificationOptions, Server
from mcp.server.models import InitializationOptions

from tai_mcp_ssh.audit import AuditLog
from tai_mcp_ssh.config import Config, load_config
from tai_mcp_ssh.errors import (
    HostNotAllowed,
    KeychainUnavailable,
    TaiMcpSshError,
    TmuxMissing,
)
from tai_mcp_ssh.sessions import SessionManager
from tai_mcp_ssh.ssh import ConnectionPool
from tai_mcp_ssh.transfer import TransferManager

SERVER_NAME = "tai-mcp-ssh"
SERVER_VERSION = "0.1.0"


def tool_specs() -> list[mtypes.Tool]:
    """Return the 7 tool specs.

    Descriptions are deliberately concise — they're sent on every LLM turn,
    so verbosity here is paid for repeatedly.
    """
    return [
        mtypes.Tool(
            name="hosts",
            description=(
                "List SSH hosts the LLM may reach. Re-reads hosts.toml on each call; "
                "call again to pick up newly-added/changed/removed entries."
            ),
            inputSchema={
                "type": "object",
                "properties": {},
                "additionalProperties": False,
            },
        ),
        mtypes.Tool(
            name="session_list",
            description="List active tmux-backed sessions across hosts.",
            inputSchema={
                "type": "object",
                "properties": {},
                "additionalProperties": False,
            },
        ),
        mtypes.Tool(
            name="session_run",
            description=(
                "Run a shell command in a persistent tmux session on the remote host. "
                "Returns trimmed output plus log_path so you can read more via "
                "`cat`/`tail`/`grep`. If status='needs_password' or 'needs_input', "
                "tell the user to run the attach_hint command, complete the prompt, "
                "and detach with Ctrl-B D; then call session_wait."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "session_id": {
                        "type": "string",
                        "description": "<host>/<name>, e.g. 'pi-living/default'.",
                    },
                    "command": {"type": "string"},
                    "reason": {
                        "type": "string",
                        "description": "Optional one-line audit reason.",
                    },
                    "timeout": {
                        "type": "number",
                        "description": "Seconds to poll before returning still_running.",
                        "default": 30,
                    },
                },
                "required": ["session_id", "command"],
                "additionalProperties": False,
            },
        ),
        mtypes.Tool(
            name="session_wait",
            description=(
                "Resume polling an in-flight session_run (sudo handoff or "
                "long-running command). Same return shape as session_run."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "session_id": {"type": "string"},
                    "timeout": {"type": "number", "default": 30},
                },
                "required": ["session_id"],
                "additionalProperties": False,
            },
        ),
        mtypes.Tool(
            name="session_kill",
            description="Tear down a tmux session on the remote.",
            inputSchema={
                "type": "object",
                "properties": {"session_id": {"type": "string"}},
                "required": ["session_id"],
                "additionalProperties": False,
            },
        ),
        mtypes.Tool(
            name="put",
            description=(
                "Upload a local file to the remote via SFTP. Runs as the SSH "
                "user. For root-owned destinations use stage-and-move: put to "
                "/tmp/<name>, then session_run with `sudo mv ...`."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "host": {"type": "string"},
                    "local_path": {"type": "string"},
                    "remote_path": {"type": "string"},
                    "reason": {"type": "string"},
                },
                "required": ["host", "local_path", "remote_path"],
                "additionalProperties": False,
            },
        ),
        mtypes.Tool(
            name="get",
            description=(
                "Download a remote file via SFTP. Defaults local_path to "
                "~/.local/state/tai-mcp-ssh/downloads/<host>/<basename>."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "host": {"type": "string"},
                    "remote_path": {"type": "string"},
                    "local_path": {"type": "string"},
                    "reason": {"type": "string"},
                    "allow_outside": {"type": "boolean"},
                },
                "required": ["host", "remote_path"],
                "additionalProperties": False,
            },
        ),
    ]


class Services:
    """Bundle of long-lived services shared by every tool handler.

    Instantiated once per ``serve_stdio`` call. Tests can construct one
    explicitly with custom managers.
    """

    def __init__(
        self,
        *,
        config: Config | None = None,
        audit: AuditLog | None = None,
        pool: ConnectionPool | None = None,
        sessions: SessionManager | None = None,
        transfer: TransferManager | None = None,
    ) -> None:
        self.config = config or load_config()
        self.audit = audit or AuditLog()
        self.pool = pool or ConnectionPool(self.config.hosts, self.audit)
        self.sessions = sessions or SessionManager(self.pool, self.audit)
        self.transfer = transfer or TransferManager(self.pool, self.audit)

    async def close(self) -> None:
        await self.pool.close_all()
        self.audit.close()

    async def reload_hosts_from_disk(self) -> tuple[int, int, int]:
        """Re-read ``hosts.toml`` and reconcile with the in-memory allowlist.

        Returns ``(added, removed, changed)`` counts. Raises ``ConfigError`` /
        ``OSError`` etc. from :func:`load_config` if the file is malformed; the
        in-memory state is left untouched so the caller can fall back to it.
        """
        new_cfg = load_config()  # may raise
        old = self.config.hosts
        new = new_cfg.hosts
        added = new.keys() - old.keys()
        removed = old.keys() - new.keys()
        changed = {a for a in old.keys() & new.keys() if old[a] != new[a]}
        self.config = new_cfg
        await self.pool.update_hosts(new, evict=removed | changed)
        return (len(added), len(removed), len(changed))


async def dispatch(svc: Services, name: str, args: dict[str, Any]) -> Any:
    """Resolve a tool call against the bundled services.

    Returns a JSON-serialisable result. Raises domain exceptions which
    :func:`call_tool` audits and re-raises.
    """
    if name == "hosts":
        # Re-read hosts.toml so config edits made while the MCP is running
        # become visible without a restart. Fail soft: if the file is now
        # malformed we keep the previous in-memory allowlist and surface
        # the failure in the audit log.
        try:
            added, removed, changed = await svc.reload_hosts_from_disk()
            await svc.audit.record(
                "_hosts_reload",
                host=None,
                status="ok",
                added=added,
                removed=removed,
                changed=changed,
            )
        except Exception as exc:  # noqa: BLE001 — every reload audited
            # Counts are zero on failure: no diff was applied because the
            # in-memory state is left untouched. Including them keeps the
            # `_hosts_reload` record schema uniform across ok/error.
            await svc.audit.record(
                "_hosts_reload",
                host=None,
                status="error",
                added=0,
                removed=0,
                changed=0,
                error=f"{type(exc).__name__}: {exc}",
            )
        return [
            {
                "alias": h.alias,
                "host": h.host,
                "user": h.user,
                "port": h.port,
                "auth": h.auth,
            }
            for h in svc.config.hosts.values()
        ]
    if name == "session_list":
        return svc.sessions.list_sessions()
    if name == "session_run":
        return await svc.sessions.run(
            args["session_id"],
            args["command"],
            reason=args.get("reason"),
            timeout=float(args.get("timeout", 30)),
        )
    if name == "session_wait":
        return await svc.sessions.wait(
            args["session_id"],
            timeout=float(args.get("timeout", 30)),
        )
    if name == "session_kill":
        return await svc.sessions.kill(args["session_id"])
    if name == "put":
        return await svc.transfer.put(
            args["host"],
            args["local_path"],
            args["remote_path"],
            reason=args.get("reason"),
        )
    if name == "get":
        return await svc.transfer.get(
            args["host"],
            args["remote_path"],
            args.get("local_path"),
            reason=args.get("reason"),
            allow_outside=args.get("allow_outside", False),
        )
    raise ValueError(f"unknown tool: {name}")


def to_jsonable(obj: Any) -> Any:
    """Convert dataclasses / nested containers to plain JSON-friendly values."""
    if is_dataclass(obj) and not isinstance(obj, type):
        return {k: to_jsonable(v) for k, v in asdict(obj).items()}
    if isinstance(obj, list):
        return [to_jsonable(x) for x in obj]
    if isinstance(obj, dict):
        return {k: to_jsonable(v) for k, v in obj.items()}
    return obj


def _host_from_args(name: str, args: dict[str, Any]) -> str | None:
    if "host" in args:
        host = args.get("host")
        return host if isinstance(host, str) else None
    sid = args.get("session_id")
    if isinstance(sid, str) and "/" in sid:
        return sid.split("/", 1)[0]
    return None


async def _dispatch_and_audit(services: Services, name: str, args: dict[str, Any]) -> Any:
    """Dispatch a tool call and ensure exactly one audit record per call.

    Three failure modes:

    - ``HostNotAllowed`` / ``TmuxMissing`` / ``KeychainUnavailable``: domain
      rejections not yet recorded by a manager — audited as ``rejected``.
    - Other ``TaiMcpSshError``: audited as ``error`` unless the raising
      manager already recorded a richer entry and set ``exc.audited = True``.
    - Anything else (``ValueError`` from ``parse_session_id``, unknown-tool
      dispatch, …): audited as ``error`` with a typed prefix so the
      every-call invariant holds even for validation paths we didn't
      enumerate above.

    All branches re-raise; the caller maps to MCP error responses.
    """
    try:
        return await dispatch(services, name, args)
    except (HostNotAllowed, TmuxMissing, KeychainUnavailable) as exc:
        await services.audit.record(
            name,
            host=_host_from_args(name, args),
            status="rejected",
            error=str(exc),
        )
        raise
    except TaiMcpSshError as exc:
        if not getattr(exc, "audited", False):
            await services.audit.record(
                name,
                host=_host_from_args(name, args),
                status="error",
                error=str(exc),
            )
        raise
    except Exception as exc:  # noqa: BLE001 — invariant: every call audited
        await services.audit.record(
            name,
            host=_host_from_args(name, args),
            status="error",
            error=f"{type(exc).__name__}: {exc}",
        )
        raise


def build_server(services: Services) -> Server:
    """Wire the dispatch logic into a low-level MCP :class:`Server`."""
    server: Server = Server(SERVER_NAME)

    # The mcp SDK's decorators are untyped; ignore the resulting mypy noise
    # on those two lines rather than `# type: ignore`-ing the function bodies.
    @server.list_tools()  # type: ignore[no-untyped-call,untyped-decorator]
    async def _list_tools() -> list[mtypes.Tool]:
        return tool_specs()

    @server.call_tool()  # type: ignore[untyped-decorator]
    async def _call_tool(name: str, arguments: dict[str, Any] | None) -> list[mtypes.TextContent]:
        args = arguments or {}
        result = await _dispatch_and_audit(services, name, args)
        return [mtypes.TextContent(type="text", text=json.dumps(to_jsonable(result)))]

    return server


async def serve_stdio() -> None:
    """Start the MCP server over stdio. Blocks until the client disconnects."""
    services = Services()
    # Run the audit retention sweep once before accepting tool calls.
    await services.audit.sweep_retention(services.config.audit.retention_days)
    server = build_server(services)
    try:
        async with mcp.server.stdio.stdio_server() as (read, write):
            await server.run(
                read,
                write,
                InitializationOptions(
                    server_name=SERVER_NAME,
                    server_version=SERVER_VERSION,
                    capabilities=server.get_capabilities(
                        notification_options=NotificationOptions(),
                        experimental_capabilities={},
                    ),
                ),
            )
    finally:
        await services.close()


def main() -> None:
    """Entry point used by `tai-mcp-ssh serve`."""
    asyncio.run(serve_stdio())
