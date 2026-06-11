"""Domain exceptions raised by tai-mcp-ssh.

Caught at the server.py boundary and mapped to MCP error responses.
Nothing else should stringify them.
"""

from __future__ import annotations


class TaiMcpSshError(Exception):
    """Base class for all tai-mcp-ssh errors."""


class ConfigError(TaiMcpSshError):
    """`hosts.toml` is malformed, missing required fields, or contains forbidden data."""


class HostNotAllowed(TaiMcpSshError):
    """Requested host alias is not present in the allowlist."""


class TmuxMissing(TaiMcpSshError):
    """Managed host lacks `tmux` on PATH."""


class KeychainUnavailable(TaiMcpSshError):
    """OS keychain is not accessible; password-auth host cannot be reached."""


class TransferDenied(TaiMcpSshError):
    """SFTP put/get failed because the SSH user lacks permission."""


class HostUnreachable(TaiMcpSshError):
    """SSH transport to the host is observed to be dead.

    The cached connection is marked dead at the point this is raised; the
    pool evicts it on the next ``get()`` call so a retry transparently
    reconnects.
    """
