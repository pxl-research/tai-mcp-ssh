"""Tests for ``tai_mcp_ssh.paths``."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from tai_mcp_ssh import paths


def test_config_dir_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    assert paths.config_dir() == Path.home() / ".config" / "tai-mcp-ssh"


def test_config_dir_honours_xdg(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    assert paths.config_dir() == tmp_path / "tai-mcp-ssh"


def test_hosts_toml_inside_config_dir() -> None:
    assert paths.hosts_toml().parent == paths.config_dir()
    assert paths.hosts_toml().name == "hosts.toml"


def test_state_dir_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("XDG_STATE_HOME", raising=False)
    assert paths.state_dir() == Path.home() / ".local" / "state" / "tai-mcp-ssh"


def test_state_dir_honours_xdg(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    assert paths.state_dir() == tmp_path / "tai-mcp-ssh"


def test_audit_dir_on_darwin(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")
    assert paths.audit_dir() == Path.home() / "Library" / "Logs" / "tai-mcp-ssh" / "audit"


def test_audit_dir_on_linux(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.delenv("XDG_STATE_HOME", raising=False)
    assert paths.audit_dir() == Path.home() / ".local" / "state" / "tai-mcp-ssh" / "audit"


def test_downloads_dir_with_host() -> None:
    assert paths.downloads_dir("pi-living").name == "pi-living"
    assert paths.downloads_dir("pi-living").parent == paths.state_dir() / "downloads"


def test_downloads_dir_without_host() -> None:
    assert paths.downloads_dir().name == "downloads"
    assert paths.downloads_dir().parent == paths.state_dir()
