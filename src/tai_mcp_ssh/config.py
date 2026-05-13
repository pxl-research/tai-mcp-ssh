"""Host allowlist and audit settings loaded from ``hosts.toml``.

Single source of truth for the LLM-reachable host list. Secrets are
deliberately absent: password-auth entries carry only a
``keychain://tai-mcp-ssh/<alias>`` reference and the actual secret lives
in the OS keychain (resolved by ``ssh.py`` at connect time).
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import tomli_w

from tai_mcp_ssh import paths
from tai_mcp_ssh.errors import ConfigError

# Any of these keys appearing at any depth in hosts.toml is hard-rejected;
# secrets belong in the OS keychain, not on disk.
FORBIDDEN_KEYS: frozenset[str] = frozenset(
    {"password", "passwd", "secret", "token", "private_key", "passphrase"}
)

DEFAULT_PORT = 22
DEFAULT_LOG_RETENTION_DAYS = 7  # remote ~/.tai-ssh/logs/ retention per host
DEFAULT_AUDIT_RETENTION_DAYS = 90

Auth = Literal["key", "password"]


@dataclass(frozen=True, slots=True)
class Host:
    """One allowlist entry. ``host`` / ``user`` may be ``None`` to defer to ``~/.ssh/config``."""

    alias: str
    host: str | None = None
    user: str | None = None
    port: int = DEFAULT_PORT
    auth: Auth = "key"
    identity_file: str | None = None
    password_ref: str | None = None
    log_retention_days: int = DEFAULT_LOG_RETENTION_DAYS


@dataclass(frozen=True, slots=True)
class AuditSettings:
    retention_days: int = DEFAULT_AUDIT_RETENTION_DAYS


@dataclass(frozen=True, slots=True)
class Config:
    hosts: dict[str, Host]
    audit: AuditSettings


def load_config(path: Path | None = None) -> Config:
    """Read ``hosts.toml`` and return a parsed :class:`Config`.

    Missing or empty file returns an empty allowlist so the MCP can boot
    cleanly on a fresh install; the operator then runs
    ``tai-mcp-ssh hosts add`` to populate it.
    """
    p = path or paths.hosts_toml()
    if not p.exists():
        return Config(hosts={}, audit=AuditSettings())

    raw = tomllib.loads(p.read_text(encoding="utf-8"))
    _reject_forbidden_keys(raw, str(p))

    hosts_table = raw.get("hosts", {}) or {}
    if not isinstance(hosts_table, dict):
        raise ConfigError(f"{p}: [hosts] must be a table")

    hosts: dict[str, Host] = {}
    for alias, entry in hosts_table.items():
        if not isinstance(entry, dict):
            raise ConfigError(f"{p}: [hosts.{alias}] must be a table")
        hosts[alias] = _entry_to_host(alias, entry, str(p))

    audit_table = raw.get("audit", {}) or {}
    if not isinstance(audit_table, dict):
        raise ConfigError(f"{p}: [audit] must be a table")
    audit = AuditSettings(
        retention_days=int(audit_table.get("retention_days", DEFAULT_AUDIT_RETENTION_DAYS)),
    )

    return Config(hosts=hosts, audit=audit)


def save_host(host: Host, path: Path | None = None) -> None:
    """Persist ``host`` to ``hosts.toml``, overwriting any prior entry under that alias."""
    p = path or paths.hosts_toml()
    p.parent.mkdir(parents=True, exist_ok=True)

    if p.exists():
        raw: dict[str, Any] = tomllib.loads(p.read_text(encoding="utf-8"))
        _reject_forbidden_keys(raw, str(p))
    else:
        raw = {}

    raw.setdefault("hosts", {})[host.alias] = _host_to_entry(host)
    p.write_text(tomli_w.dumps(raw), encoding="utf-8")


def delete_host(alias: str, path: Path | None = None) -> None:
    """Remove ``[hosts.<alias>]`` from ``hosts.toml``. No-op if missing."""
    p = path or paths.hosts_toml()
    if not p.exists():
        return
    raw: dict[str, Any] = tomllib.loads(p.read_text(encoding="utf-8"))
    hosts = raw.get("hosts", {})
    if alias in hosts:
        del hosts[alias]
        p.write_text(tomli_w.dumps(raw), encoding="utf-8")


# Internals -----------------------------------------------------------------


def _reject_forbidden_keys(node: Any, where: str, path: str = "") -> None:
    if not isinstance(node, dict):
        return
    for k, v in node.items():
        if k in FORBIDDEN_KEYS:
            qualified = f"{path}.{k}" if path else k
            raise ConfigError(
                f"{where}: forbidden key '{qualified}' — secrets must live in the OS "
                f"keychain, referenced by `password_ref = "
                f'"keychain://tai-mcp-ssh/<alias>"`.'
            )
        _reject_forbidden_keys(v, where, f"{path}.{k}" if path else k)


def _entry_to_host(alias: str, entry: dict[str, Any], where: str) -> Host:
    auth = entry.get("auth", "key")
    if auth not in ("key", "password"):
        raise ConfigError(
            f"{where}: [hosts.{alias}].auth must be 'key' or 'password', got {auth!r}"
        )

    password_ref = entry.get("password_ref")
    if auth == "password" and not password_ref:
        raise ConfigError(
            f"{where}: [hosts.{alias}] has auth='password' but no password_ref. "
            f"Set password_ref = 'keychain://tai-mcp-ssh/{alias}' and store the "
            f"secret via `tai-mcp-ssh hosts add`."
        )
    if password_ref is not None and not password_ref.startswith("keychain://"):
        raise ConfigError(f"{where}: [hosts.{alias}].password_ref must use the keychain:// scheme")

    return Host(
        alias=alias,
        host=entry.get("host"),
        user=entry.get("user"),
        port=int(entry.get("port", DEFAULT_PORT)),
        auth=auth,
        identity_file=entry.get("identity_file"),
        password_ref=password_ref,
        log_retention_days=int(entry.get("log_retention_days", DEFAULT_LOG_RETENTION_DAYS)),
    )


def _host_to_entry(host: Host) -> dict[str, Any]:
    entry: dict[str, Any] = {}
    if host.host is not None:
        entry["host"] = host.host
    if host.user is not None:
        entry["user"] = host.user
    if host.port != DEFAULT_PORT:
        entry["port"] = host.port
    entry["auth"] = host.auth
    if host.identity_file is not None:
        entry["identity_file"] = host.identity_file
    if host.password_ref is not None:
        entry["password_ref"] = host.password_ref
    if host.log_retention_days != DEFAULT_LOG_RETENTION_DAYS:
        entry["log_retention_days"] = host.log_retention_days
    return entry
