"""Filesystem paths used by tai-mcp-ssh.

All host-side path logic lives here so platform differences live in
exactly one place. XDG environment variables are honoured on Linux; macOS
gets a Library/Logs override for audit data per Apple convention.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

_APP = "tai-mcp-ssh"


def config_dir() -> Path:
    """Directory for user configuration (`hosts.toml`)."""
    xdg = os.environ.get("XDG_CONFIG_HOME")
    base = Path(xdg) if xdg else Path.home() / ".config"
    return base / _APP


def hosts_toml() -> Path:
    """Path to the host allowlist."""
    return config_dir() / "hosts.toml"


def state_dir() -> Path:
    """Directory for non-audit runtime state (downloads, caches, …)."""
    xdg = os.environ.get("XDG_STATE_HOME")
    base = Path(xdg) if xdg else Path.home() / ".local" / "state"
    return base / _APP


def audit_dir() -> Path:
    """Root for per-host JSONL audit folders.

    On macOS Apple expects log-like data under ``~/Library/Logs``; on
    Linux we follow XDG and use ``state_dir() / "audit"``.
    """
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Logs" / _APP / "audit"
    return state_dir() / "audit"


def downloads_dir(host: str | None = None) -> Path:
    """Default destination root for `get(...)` downloads."""
    root = state_dir() / "downloads"
    return root / host if host else root
