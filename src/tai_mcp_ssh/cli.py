"""``tai-mcp-ssh`` command-line interface.

Subcommands:
  hosts {add,list,remove,test} — manage the host allowlist
  audit tail                   — read the JSONL audit log
  serve                        — start the MCP over stdio
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path
from typing import Any

import click
import keyring
import keyring.errors

from tai_mcp_ssh import paths
from tai_mcp_ssh.audit import AuditLog
from tai_mcp_ssh.config import (
    Host,
    delete_host,
    load_config,
    save_host,
)
from tai_mcp_ssh.errors import HostNotAllowed, KeychainUnavailable, TmuxMissing
from tai_mcp_ssh.ssh import ConnectionPool

_KEYRING_SERVICE = "tai-mcp-ssh"


@click.group(help="tai-mcp-ssh: MCP server for remote Linux admin via SSH.")
@click.version_option()
def main() -> None:
    pass


# ---------------------------------------------------------------------------
# `hosts` subcommands
# ---------------------------------------------------------------------------


@main.group(help="Manage the host allowlist.")
def hosts() -> None:
    pass


@hosts.command("add", help="Add a host interactively. Passwords prompted via getpass.")
@click.argument("alias")
@click.option(
    "--host",
    "host_addr",
    default=None,
    help="IP or DNS name (skipped if using ~/.ssh/config alias).",
)
@click.option("--user", "user", default=None, help="Remote username.")
@click.option("--port", "port", default=22, type=int, show_default=True)
@click.option(
    "--auth",
    type=click.Choice(["key", "password"]),
    default=None,
    help="Auth method. Prompted if not supplied.",
)
@click.option(
    "--identity-file",
    "identity_file",
    default=None,
    help="Optional path to a specific SSH private key (key auth only).",
)
def hosts_add(
    alias: str,
    host_addr: str | None,
    user: str | None,
    port: int,
    auth: str | None,
    identity_file: str | None,
) -> None:
    if not sys.stdin.isatty():
        raise click.ClickException(
            "hosts add must be run from an interactive terminal (secrets are captured via getpass)."
        )

    if host_addr is None:
        host_addr = (
            click.prompt(
                "Host (IP or DNS; leave blank to use ssh_config alias)",
                default="",
                show_default=False,
            )
            or None
        )
    if user is None:
        user = (
            click.prompt("User (leave blank to use ssh_config)", default="", show_default=False)
            or None
        )
    if auth is None:
        auth = click.prompt("Auth", type=click.Choice(["key", "password"]), default="key")

    password_ref: str | None = None
    if auth == "password":
        password = click.prompt("Password", hide_input=True, confirmation_prompt=False)
        try:
            keyring.set_password(_KEYRING_SERVICE, alias, password)
        except keyring.errors.KeyringError as exc:
            raise click.ClickException(f"failed to store password in keychain: {exc}") from exc
        password_ref = f"keychain://{_KEYRING_SERVICE}/{alias}"

    h = Host(
        alias=alias,
        host=host_addr,
        user=user,
        port=port,
        auth=auth,  # type: ignore[arg-type]
        identity_file=identity_file,
        password_ref=password_ref,
    )
    save_host(h)
    click.echo(f"Added [hosts.{alias}] to {paths.hosts_toml()}.")
    if auth == "password":
        click.echo(f"Stored password in keychain at {password_ref}.")
    if click.confirm("Test connection now?", default=True):
        _run_hosts_test(alias)


@hosts.command("list", help="List configured hosts (secrets redacted).")
def hosts_list() -> None:
    cfg = load_config()
    if not cfg.hosts:
        click.echo("No hosts configured. Add one with `tai-mcp-ssh hosts add <alias>`.")
        return
    for h in cfg.hosts.values():
        auth_label = f"{h.auth}"
        if h.auth == "password":
            auth_label = "password (keychain)"
        details = []
        if h.host:
            details.append(f"host={h.host}")
        if h.user:
            details.append(f"user={h.user}")
        if h.port != 22:
            details.append(f"port={h.port}")
        if h.identity_file:
            details.append(f"identity_file={h.identity_file}")
        click.echo(f"{h.alias}  [{auth_label}]  " + "  ".join(details))


@hosts.command("remove", help="Remove a host. Wipes its keychain entry too.")
@click.argument("alias")
@click.option("--yes", "-y", is_flag=True, help="Skip confirmation prompt.")
def hosts_remove(alias: str, yes: bool) -> None:
    cfg = load_config()
    if alias not in cfg.hosts:
        raise click.ClickException(f"no host with alias {alias!r}")
    if not yes and not click.confirm(f"Remove host {alias!r}?", default=False):
        click.echo("Aborted.")
        return
    delete_host(alias)
    try:
        keyring.delete_password(_KEYRING_SERVICE, alias)
    except keyring.errors.PasswordDeleteError:
        # No keychain entry — fine for key-auth hosts.
        pass
    except keyring.errors.KeyringError as exc:
        click.echo(f"warning: keychain entry removal failed: {exc}", err=True)
    click.echo(f"Removed [hosts.{alias}].")


@hosts.command("test", help="Verify connectivity and remote prerequisites for one host.")
@click.argument("alias")
def hosts_test(alias: str) -> None:
    _run_hosts_test(alias)


def _run_hosts_test(alias: str) -> None:
    cfg = load_config()
    if alias not in cfg.hosts:
        raise click.ClickException(f"no host with alias {alias!r}")
    audit = AuditLog()
    pool = ConnectionPool({alias: cfg.hosts[alias]}, audit)
    start = time.monotonic()
    try:
        asyncio.run(_probe(pool, alias))
    except HostNotAllowed as exc:
        raise click.ClickException(f"host not in allowlist: {exc}") from exc
    except TmuxMissing as exc:
        raise click.ClickException(str(exc)) from exc
    except KeychainUnavailable as exc:
        raise click.ClickException(str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        raise click.ClickException(f"connect failed: {exc}") from exc
    finally:
        audit.close()
    elapsed_ms = (time.monotonic() - start) * 1000
    click.echo("  connect ........ ok")
    click.echo("  whoami / tmux .. ok")
    click.echo("  log dir ........ ok")
    click.echo(f"  latency ........ {elapsed_ms:.0f} ms")


async def _probe(pool: ConnectionPool, alias: str) -> None:
    conn = await pool.get(alias)
    # `get` already runs `command -v tmux`, `echo $HOME`, and `mkdir -p`,
    # so reaching this point with no exception means all four checks passed.
    await conn.run("whoami", check=True)
    await pool.close_all()


# ---------------------------------------------------------------------------
# `audit tail`
# ---------------------------------------------------------------------------


@main.group(help="Read the audit log.")
def audit() -> None:
    pass


@audit.command("tail", help="Print the most recent audit records.")
@click.option("-n", "n", default=20, show_default=True, help="Records to return.")
@click.option("--host", "host_filter", default=None, help="Filter by host alias.")
@click.option("--session", "session_filter", default=None, help="Filter by session_id.")
@click.option("--tool", "tool_filter", default=None, help="Filter by tool name.")
@click.option("--pretty", is_flag=True, help="Pretty-print each record.")
def audit_tail(
    n: int,
    host_filter: str | None,
    session_filter: str | None,
    tool_filter: str | None,
    pretty: bool,
) -> None:
    root = paths.audit_dir()
    if not root.exists():
        click.echo(f"No audit log yet at {root}.")
        return

    if host_filter:
        host_dirs: list[Path] = [root / host_filter]
    else:
        host_dirs = [d for d in root.iterdir() if d.is_dir()]

    records: list[dict[str, Any]] = []
    for d in host_dirs:
        if not d.exists():
            continue
        # Walk daily files newest-first so we stop once we have enough.
        for f in sorted(d.glob("*.jsonl"), reverse=True):
            for line in reversed(f.read_text(encoding="utf-8").splitlines()):
                try:
                    r = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if session_filter and r.get("session") != session_filter:
                    continue
                if tool_filter and r.get("tool") != tool_filter:
                    continue
                records.append(r)
            if len(records) >= n * len(host_dirs):
                break

    # Merge by ts (descending), take the most recent n.
    records.sort(key=lambda r: r.get("ts", ""), reverse=True)
    for r in records[:n]:
        if pretty:
            click.echo(json.dumps(r, indent=2))
            click.echo()
        else:
            click.echo(json.dumps(r))


# ---------------------------------------------------------------------------
# `serve`
# ---------------------------------------------------------------------------


@main.command(help="Start the MCP server over stdio.")
def serve() -> None:
    from tai_mcp_ssh.server import main as serve_main

    serve_main()


if __name__ == "__main__":
    main()
