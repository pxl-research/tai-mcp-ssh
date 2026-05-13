"""Tests for ``tai_mcp_ssh.config``."""

from __future__ import annotations

from pathlib import Path

import pytest

from tai_mcp_ssh.config import (
    AuditSettings,
    Host,
    delete_host,
    load_config,
    save_host,
)
from tai_mcp_ssh.errors import ConfigError


def test_load_missing_file_returns_empty(tmp_path: Path) -> None:
    cfg = load_config(tmp_path / "missing.toml")
    assert cfg.hosts == {}
    assert cfg.audit == AuditSettings()


def test_load_empty_file_returns_empty(tmp_path: Path) -> None:
    p = tmp_path / "hosts.toml"
    p.write_text("", encoding="utf-8")
    cfg = load_config(p)
    assert cfg.hosts == {}
    assert cfg.audit == AuditSettings()


def test_load_key_auth_host(tmp_path: Path) -> None:
    p = tmp_path / "hosts.toml"
    p.write_text(
        """
[hosts.pi]
host = "192.168.1.42"
user = "pi"
""",
        encoding="utf-8",
    )
    cfg = load_config(p)
    h = cfg.hosts["pi"]
    assert h.alias == "pi"
    assert h.host == "192.168.1.42"
    assert h.user == "pi"
    assert h.port == 22
    assert h.auth == "key"
    assert h.password_ref is None


def test_load_password_auth_host(tmp_path: Path) -> None:
    p = tmp_path / "hosts.toml"
    p.write_text(
        """
[hosts.vps]
host = "1.2.3.4"
user = "admin"
auth = "password"
password_ref = "keychain://tai-mcp-ssh/vps"
""",
        encoding="utf-8",
    )
    h = load_config(p).hosts["vps"]
    assert h.auth == "password"
    assert h.password_ref == "keychain://tai-mcp-ssh/vps"


def test_load_rejects_plaintext_password(tmp_path: Path) -> None:
    p = tmp_path / "hosts.toml"
    p.write_text(
        """
[hosts.evil]
host = "1.2.3.4"
password = "hunter2"
""",
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="forbidden"):
        load_config(p)


@pytest.mark.parametrize(
    "forbidden_key", ["passwd", "secret", "token", "private_key", "passphrase"]
)
def test_load_rejects_other_forbidden_keys(tmp_path: Path, forbidden_key: str) -> None:
    p = tmp_path / "hosts.toml"
    p.write_text(
        f"""
[hosts.x]
host = "h"
{forbidden_key} = "leak"
""",
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="forbidden"):
        load_config(p)


def test_load_rejects_forbidden_key_inside_array_of_tables(tmp_path: Path) -> None:
    # `[[hosts.pi.creds]]` parses to a list of tables nested under hosts.pi;
    # the walker must chase list values too, not only dicts.
    p = tmp_path / "hosts.toml"
    p.write_text(
        """
[hosts.pi]
host = "h"

[[hosts.pi.creds]]
password = "leak"
""",
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="forbidden"):
        load_config(p)


def test_load_rejects_password_auth_without_ref(tmp_path: Path) -> None:
    p = tmp_path / "hosts.toml"
    p.write_text(
        """
[hosts.bad]
auth = "password"
""",
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="password_ref"):
        load_config(p)


def test_load_rejects_non_keychain_ref(tmp_path: Path) -> None:
    p = tmp_path / "hosts.toml"
    p.write_text(
        """
[hosts.bad]
auth = "password"
password_ref = "file:///etc/passwd"
""",
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="keychain://"):
        load_config(p)


def test_load_invalid_auth_value(tmp_path: Path) -> None:
    p = tmp_path / "hosts.toml"
    p.write_text(
        """
[hosts.bad]
auth = "rfid"
""",
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="auth"):
        load_config(p)


def test_load_audit_settings(tmp_path: Path) -> None:
    p = tmp_path / "hosts.toml"
    p.write_text(
        """
[audit]
retention_days = 30

[hosts.pi]
host = "h"
""",
        encoding="utf-8",
    )
    cfg = load_config(p)
    assert cfg.audit.retention_days == 30


def test_save_host_roundtrip(tmp_path: Path) -> None:
    p = tmp_path / "hosts.toml"
    h = Host(
        alias="pi",
        host="192.168.1.42",
        user="pi",
        port=2222,
        auth="key",
        identity_file="~/.ssh/pi_ed25519",
    )
    save_host(h, p)
    cfg = load_config(p)
    assert cfg.hosts["pi"] == h


def test_save_password_host_writes_ref_only(tmp_path: Path) -> None:
    p = tmp_path / "hosts.toml"
    h = Host(
        alias="vps",
        host="1.2.3.4",
        user="admin",
        auth="password",
        password_ref="keychain://tai-mcp-ssh/vps",
    )
    save_host(h, p)
    text = p.read_text(encoding="utf-8")
    assert "password_ref" in text
    assert "hunter2" not in text
    assert "password =" not in text
    assert load_config(p).hosts["vps"].password_ref == "keychain://tai-mcp-ssh/vps"


def test_delete_host(tmp_path: Path) -> None:
    p = tmp_path / "hosts.toml"
    save_host(Host(alias="pi", host="h"), p)
    assert "pi" in load_config(p).hosts
    delete_host("pi", p)
    assert "pi" not in load_config(p).hosts


def test_delete_missing_host_is_noop(tmp_path: Path) -> None:
    delete_host("nonexistent", tmp_path / "missing.toml")  # no exception


def test_save_to_new_directory(tmp_path: Path) -> None:
    p = tmp_path / "nested" / "subdir" / "hosts.toml"
    save_host(Host(alias="pi", host="h"), p)
    assert p.exists()
