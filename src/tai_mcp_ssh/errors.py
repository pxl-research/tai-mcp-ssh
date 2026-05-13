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


class SessionBusy(TaiMcpSshError):
    """Session is awaiting completion or input; refuses a new command."""


class KeychainUnavailable(TaiMcpSshError):
    """OS keychain is not accessible; password-auth host cannot be reached."""


class SecretInCommand(TaiMcpSshError):
    """Command appears to contain a literal secret; refusing to forward it."""


class TransferDenied(TaiMcpSshError):
    """SFTP put/get failed because the SSH user lacks permission."""


class HostUnreachable(TaiMcpSshError):
    """SSH transport to the host is dead (peer rebooted, network gone, ...).

    Raised after the pool has evicted the broken connection; a retry will
    transparently reconnect.
    """
