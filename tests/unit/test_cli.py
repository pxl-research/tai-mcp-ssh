"""Tests for ``tai_mcp_ssh.cli`` using ``click.testing.CliRunner``."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import keyring.errors
import pytest
from click.testing import CliRunner

from tai_mcp_ssh.cli import _probe, main
from tai_mcp_ssh.errors import (
    HostNotAllowed,
    KeychainUnavailable,
    TmuxMissing,
)


@pytest.fixture
def tmp_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Isolate config/state/audit/keyring under tmp_path."""
    cfg_dir = tmp_path / "config" / "tai-mcp-ssh"
    state_dir = tmp_path / "state" / "tai-mcp-ssh"
    audit_dir = state_dir / "audit"
    monkeypatch.setattr("tai_mcp_ssh.cli.paths.hosts_toml", lambda: cfg_dir / "hosts.toml")
    monkeypatch.setattr("tai_mcp_ssh.cli.paths.audit_dir", lambda: audit_dir)
    monkeypatch.setattr("tai_mcp_ssh.config.paths.hosts_toml", lambda: cfg_dir / "hosts.toml")
    monkeypatch.setattr("tai_mcp_ssh.audit.paths.audit_dir", lambda: audit_dir)
    return tmp_path


# ---------------------------------------------------------------------------
# Smoke
# ---------------------------------------------------------------------------


def test_help_runs() -> None:
    result = CliRunner().invoke(main, ["--help"])
    assert result.exit_code == 0
    assert "hosts" in result.output
    assert "serve" in result.output


def test_version_runs() -> None:
    result = CliRunner().invoke(main, ["--version"])
    assert result.exit_code == 0


# ---------------------------------------------------------------------------
# `hosts list`
# ---------------------------------------------------------------------------


def test_hosts_list_empty(tmp_home: Path) -> None:
    result = CliRunner().invoke(main, ["hosts", "list"])
    assert result.exit_code == 0
    assert "No hosts configured" in result.output


def test_hosts_list_shows_key_auth(tmp_home: Path) -> None:
    hosts_path = tmp_home / "config" / "tai-mcp-ssh" / "hosts.toml"
    hosts_path.parent.mkdir(parents=True)
    hosts_path.write_text(
        """
[hosts.pi]
host = "192.168.1.42"
user = "pi"
""",
        encoding="utf-8",
    )
    result = CliRunner().invoke(main, ["hosts", "list"])
    assert result.exit_code == 0
    assert "pi" in result.output
    assert "192.168.1.42" in result.output
    assert "key" in result.output


def test_hosts_list_redacts_password_auth(tmp_home: Path) -> None:
    hosts_path = tmp_home / "config" / "tai-mcp-ssh" / "hosts.toml"
    hosts_path.parent.mkdir(parents=True)
    hosts_path.write_text(
        """
[hosts.vps]
host = "1.2.3.4"
auth = "password"
password_ref = "keychain://tai-mcp-ssh/vps"
""",
        encoding="utf-8",
    )
    result = CliRunner().invoke(main, ["hosts", "list"])
    assert result.exit_code == 0
    assert "vps" in result.output
    assert "(keychain)" in result.output
    # password_ref's keychain URL must not leak into list output.
    assert "keychain://" not in result.output


# ---------------------------------------------------------------------------
# `hosts remove`
# ---------------------------------------------------------------------------


def test_hosts_remove_missing(tmp_home: Path) -> None:
    result = CliRunner().invoke(main, ["hosts", "remove", "nope"])
    assert result.exit_code != 0
    assert "no host" in result.output


def test_hosts_remove_with_yes(tmp_home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    hosts_path = tmp_home / "config" / "tai-mcp-ssh" / "hosts.toml"
    hosts_path.parent.mkdir(parents=True)
    hosts_path.write_text(
        """
[hosts.pi]
host = "h"
""",
        encoding="utf-8",
    )
    monkeypatch.setattr("tai_mcp_ssh.cli.keyring.delete_password", lambda *_: None)
    result = CliRunner().invoke(main, ["hosts", "remove", "pi", "-y"])
    assert result.exit_code == 0
    assert "Removed" in result.output
    assert "pi" not in hosts_path.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# `hosts add` — argv password is rejected, getpass flow not tested here
# ---------------------------------------------------------------------------


def test_hosts_add_rejects_non_tty(tmp_home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("tai_mcp_ssh.cli.sys.stdin.isatty", lambda: False)
    result = CliRunner().invoke(main, ["hosts", "add", "pi"])
    assert result.exit_code != 0
    assert "interactive" in result.output


def test_hosts_add_prompts_for_identity_file_on_key_auth(
    tmp_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Regression for #8: interactive flow surfaces the identity_file option
    # when auth=key. Cloud-VM users with a specific .pem shouldn't have to
    # discover `--identity-file` to make `hosts add` produce a working entry.
    monkeypatch.setattr("tai_mcp_ssh.cli._stdin_is_tty", lambda: True)
    key_file = tmp_home / "ssh-key.pem"
    key_file.write_text("dummy")
    result = CliRunner().invoke(
        main,
        ["hosts", "add", "oracle"],
        input=f"1.2.3.4\nubuntu\nkey\n{key_file}\nn\n",
    )
    assert result.exit_code == 0, result.output
    assert "Identity file" in result.output  # prompt was shown
    hosts_path = tmp_home / "config" / "tai-mcp-ssh" / "hosts.toml"
    text = hosts_path.read_text(encoding="utf-8")
    # Path is absolute (expanded + resolved) in the saved entry.
    assert f'identity_file = "{key_file.resolve()}"' in text


def test_hosts_add_skips_identity_prompt_when_flag_supplied(
    tmp_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("tai_mcp_ssh.cli._stdin_is_tty", lambda: True)
    key_file = tmp_home / "my.key"
    key_file.write_text("dummy")
    # No identity_file in the input — the flag should bypass the prompt.
    result = CliRunner().invoke(
        main,
        ["hosts", "add", "vps", "--identity-file", str(key_file)],
        input="1.2.3.4\nubuntu\nkey\nn\n",
    )
    assert result.exit_code == 0, result.output
    assert "Identity file" not in result.output
    hosts_path = tmp_home / "config" / "tai-mcp-ssh" / "hosts.toml"
    assert f'identity_file = "{key_file}"' in hosts_path.read_text(encoding="utf-8")


def test_hosts_add_identity_prompt_blank_keeps_none(
    tmp_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Blank answer at the prompt leaves identity_file unset (fall back to
    # ssh_config / agent), matching the historical default for users who
    # don't have a one-off key.
    monkeypatch.setattr("tai_mcp_ssh.cli._stdin_is_tty", lambda: True)
    result = CliRunner().invoke(
        main,
        ["hosts", "add", "pi"],
        input="\n\nkey\n\nn\n",
    )
    assert result.exit_code == 0, result.output
    hosts_path = tmp_home / "config" / "tai-mcp-ssh" / "hosts.toml"
    assert "identity_file" not in hosts_path.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# `audit tail`
# ---------------------------------------------------------------------------


def _seed_audit(tmp_home: Path, lines: list[dict[str, Any]]) -> None:
    today = datetime.now(UTC).date().isoformat()
    audit_root = tmp_home / "state" / "tai-mcp-ssh" / "audit"
    for r in lines:
        host_dir = audit_root / r["host"]
        host_dir.mkdir(parents=True, exist_ok=True)
        f = host_dir / f"{today}.jsonl"
        with f.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(r) + "\n")


def test_audit_tail_empty(tmp_home: Path) -> None:
    result = CliRunner().invoke(main, ["audit", "tail"])
    assert result.exit_code == 0
    assert "No audit log" in result.output


def test_audit_tail_returns_recent_records(tmp_home: Path) -> None:
    _seed_audit(
        tmp_home,
        [
            {"ts": "2026-05-13T10:00:00.000Z", "tool": "session_run", "host": "pi", "exit": 0},
            {"ts": "2026-05-13T10:05:00.000Z", "tool": "session_run", "host": "pi", "exit": 1},
            {"ts": "2026-05-13T10:02:00.000Z", "tool": "session_run", "host": "vps", "exit": 0},
        ],
    )
    result = CliRunner().invoke(main, ["audit", "tail", "-n", "10"])
    assert result.exit_code == 0
    out_lines = [line for line in result.output.splitlines() if line.strip()]
    # Newest first.
    parsed = [json.loads(line) for line in out_lines]
    assert parsed[0]["ts"] == "2026-05-13T10:05:00.000Z"


def test_audit_tail_filters_by_host(tmp_home: Path) -> None:
    _seed_audit(
        tmp_home,
        [
            {"ts": "2026-05-13T10:00:00.000Z", "tool": "session_run", "host": "pi"},
            {"ts": "2026-05-13T10:01:00.000Z", "tool": "session_run", "host": "vps"},
        ],
    )
    result = CliRunner().invoke(main, ["audit", "tail", "--host", "pi"])
    assert result.exit_code == 0
    hosts_in_output = [
        json.loads(line)["host"] for line in result.output.splitlines() if line.strip()
    ]
    assert hosts_in_output == ["pi"]


def test_audit_tail_filters_by_tool(tmp_home: Path) -> None:
    _seed_audit(
        tmp_home,
        [
            {"ts": "2026-05-13T10:00:00.000Z", "tool": "session_run", "host": "pi"},
            {"ts": "2026-05-13T10:01:00.000Z", "tool": "put", "host": "pi"},
        ],
    )
    result = CliRunner().invoke(main, ["audit", "tail", "--tool", "put"])
    assert result.exit_code == 0
    tools_in_output = [
        json.loads(line)["tool"] for line in result.output.splitlines() if line.strip()
    ]
    assert tools_in_output == ["put"]


def test_audit_tail_pretty(tmp_home: Path) -> None:
    _seed_audit(
        tmp_home,
        [{"ts": "2026-05-13T10:00:00.000Z", "tool": "session_run", "host": "pi"}],
    )
    result = CliRunner().invoke(main, ["audit", "tail", "--pretty"])
    assert result.exit_code == 0
    assert '"tool": "session_run"' in result.output


def test_audit_tail_host_filter_missing_dir(tmp_home: Path) -> None:
    # Root exists (another host seeded) but the filtered host has no folder.
    _seed_audit(tmp_home, [{"ts": "2026-05-13T10:00:00.000Z", "tool": "x", "host": "pi"}])
    result = CliRunner().invoke(main, ["audit", "tail", "--host", "ghost"])
    assert result.exit_code == 0
    assert result.output.strip() == ""


def test_audit_tail_skips_malformed_line(tmp_home: Path) -> None:
    audit_root = tmp_home / "state" / "tai-mcp-ssh" / "audit" / "pi"
    audit_root.mkdir(parents=True)
    today = datetime.now(UTC).date().isoformat()
    (audit_root / f"{today}.jsonl").write_text(
        'not json at all\n{"ts": "2026-05-13T10:00:00.000Z", "tool": "put", "host": "pi"}\n',
        encoding="utf-8",
    )
    result = CliRunner().invoke(main, ["audit", "tail"])
    assert result.exit_code == 0
    parsed = [json.loads(line) for line in result.output.splitlines() if line.strip()]
    assert [r["tool"] for r in parsed] == ["put"]


def test_audit_tail_filters_by_session(tmp_home: Path) -> None:
    _seed_audit(
        tmp_home,
        [
            {"ts": "2026-05-13T10:00:00.000Z", "tool": "session_run", "host": "pi", "session": "a"},
            {"ts": "2026-05-13T10:01:00.000Z", "tool": "session_run", "host": "pi", "session": "b"},
        ],
    )
    result = CliRunner().invoke(main, ["audit", "tail", "--session", "b"])
    assert result.exit_code == 0
    sessions = [json.loads(line)["session"] for line in result.output.splitlines() if line.strip()]
    assert sessions == ["b"]


# ---------------------------------------------------------------------------
# `hosts add` — password flow
# ---------------------------------------------------------------------------


def test_hosts_add_password_stores_in_keychain(
    tmp_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("tai_mcp_ssh.cli._stdin_is_tty", lambda: True)
    stored: dict[str, str] = {}
    monkeypatch.setattr(
        "tai_mcp_ssh.cli.keyring.set_password",
        lambda service, alias, pw: stored.update({"service": service, "alias": alias, "pw": pw}),
    )
    tested: list[str] = []
    monkeypatch.setattr("tai_mcp_ssh.cli._run_hosts_test", tested.append)

    result = CliRunner().invoke(
        main,
        ["hosts", "add", "vps", "--auth", "password"],
        input="1.2.3.4\nubuntu\nhunter2\ny\n",
    )
    assert result.exit_code == 0, result.output
    assert stored == {"service": "tai-mcp-ssh", "alias": "vps", "pw": "hunter2"}
    assert "Stored password in keychain" in result.output
    # "Test connection now?" was confirmed → _run_hosts_test invoked.
    assert tested == ["vps"]
    hosts_path = tmp_home / "config" / "tai-mcp-ssh" / "hosts.toml"
    text = hosts_path.read_text(encoding="utf-8")
    assert 'password_ref = "keychain://tai-mcp-ssh/vps"' in text
    # The cleartext password must never be written to hosts.toml.
    assert "hunter2" not in text


def test_hosts_add_password_keychain_failure(
    tmp_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("tai_mcp_ssh.cli._stdin_is_tty", lambda: True)

    def _boom(*_: object) -> None:
        raise keyring.errors.KeyringError("no backend")

    monkeypatch.setattr("tai_mcp_ssh.cli.keyring.set_password", _boom)
    result = CliRunner().invoke(
        main,
        ["hosts", "add", "vps", "--auth", "password"],
        input="1.2.3.4\nubuntu\nhunter2\n",
    )
    assert result.exit_code != 0
    assert "failed to store password" in result.output


# ---------------------------------------------------------------------------
# `hosts list` — port + identity_file detail lines
# ---------------------------------------------------------------------------


def test_hosts_list_shows_port_and_identity_file(tmp_home: Path) -> None:
    hosts_path = tmp_home / "config" / "tai-mcp-ssh" / "hosts.toml"
    hosts_path.parent.mkdir(parents=True)
    hosts_path.write_text(
        """
[hosts.vps]
host = "1.2.3.4"
user = "ubuntu"
port = 2222
identity_file = "/home/me/.ssh/id_special"
""",
        encoding="utf-8",
    )
    result = CliRunner().invoke(main, ["hosts", "list"])
    assert result.exit_code == 0
    assert "port=2222" in result.output
    assert "identity_file=/home/me/.ssh/id_special" in result.output


# ---------------------------------------------------------------------------
# `hosts remove` — abort + keychain error branches
# ---------------------------------------------------------------------------


def test_hosts_remove_abort(tmp_home: Path) -> None:
    hosts_path = tmp_home / "config" / "tai-mcp-ssh" / "hosts.toml"
    hosts_path.parent.mkdir(parents=True)
    hosts_path.write_text('[hosts.pi]\nhost = "h"\n', encoding="utf-8")
    result = CliRunner().invoke(main, ["hosts", "remove", "pi"], input="n\n")
    assert result.exit_code == 0
    assert "Aborted" in result.output
    # Host survives the aborted removal.
    assert "pi" in hosts_path.read_text(encoding="utf-8")


def test_hosts_remove_keychain_absent_is_fine(
    tmp_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    hosts_path = tmp_home / "config" / "tai-mcp-ssh" / "hosts.toml"
    hosts_path.parent.mkdir(parents=True)
    hosts_path.write_text('[hosts.pi]\nhost = "h"\n', encoding="utf-8")

    def _no_entry(*_: object) -> None:
        raise keyring.errors.PasswordDeleteError("no such entry")

    monkeypatch.setattr("tai_mcp_ssh.cli.keyring.delete_password", _no_entry)
    result = CliRunner().invoke(main, ["hosts", "remove", "pi", "-y"])
    assert result.exit_code == 0
    assert "Removed" in result.output


def test_hosts_remove_keychain_error_warns(tmp_home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    hosts_path = tmp_home / "config" / "tai-mcp-ssh" / "hosts.toml"
    hosts_path.parent.mkdir(parents=True)
    hosts_path.write_text('[hosts.pi]\nhost = "h"\n', encoding="utf-8")

    def _boom(*_: object) -> None:
        raise keyring.errors.KeyringError("backend locked")

    monkeypatch.setattr("tai_mcp_ssh.cli.keyring.delete_password", _boom)
    result = CliRunner().invoke(main, ["hosts", "remove", "pi", "-y"])
    assert result.exit_code == 0
    assert "warning: keychain entry removal failed" in result.output
    assert "Removed" in result.output


# ---------------------------------------------------------------------------
# `hosts test` / `_run_hosts_test` / `_probe`
# ---------------------------------------------------------------------------


def test_hosts_test_missing_alias(tmp_home: Path) -> None:
    result = CliRunner().invoke(main, ["hosts", "test", "nope"])
    assert result.exit_code != 0
    assert "no host" in result.output


def _seed_one_host(tmp_home: Path) -> None:
    hosts_path = tmp_home / "config" / "tai-mcp-ssh" / "hosts.toml"
    hosts_path.parent.mkdir(parents=True, exist_ok=True)
    hosts_path.write_text('[hosts.pi]\nhost = "h"\nuser = "pi"\n', encoding="utf-8")


def test_hosts_test_success(tmp_home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _seed_one_host(tmp_home)

    async def _ok(_pool: object, _alias: str) -> None:
        return None

    monkeypatch.setattr("tai_mcp_ssh.cli._probe", _ok)
    result = CliRunner().invoke(main, ["hosts", "test", "pi"])
    assert result.exit_code == 0, result.output
    assert "connect ........ ok" in result.output
    assert "latency" in result.output


@pytest.mark.parametrize(
    ("exc", "fragment"),
    [
        (HostNotAllowed("pi"), "not in allowlist"),
        (TmuxMissing("no tmux on PATH"), "no tmux on PATH"),
        (KeychainUnavailable("locked"), "locked"),
        (RuntimeError("socket exploded"), "connect failed: socket exploded"),
    ],
)
def test_hosts_test_maps_errors(
    tmp_home: Path, monkeypatch: pytest.MonkeyPatch, exc: Exception, fragment: str
) -> None:
    _seed_one_host(tmp_home)

    async def _raise(_pool: object, _alias: str) -> None:
        raise exc

    monkeypatch.setattr("tai_mcp_ssh.cli._probe", _raise)
    result = CliRunner().invoke(main, ["hosts", "test", "pi"])
    assert result.exit_code != 0
    assert fragment in result.output


async def test_probe_runs_checks_and_closes() -> None:
    calls: dict[str, Any] = {"got": None, "ran": None, "closed": False}

    class _Conn:
        async def run(self, command: str, check: bool = False) -> None:
            calls["ran"] = (command, check)

    class _Pool:
        async def get(self, alias: str) -> _Conn:
            calls["got"] = alias
            return _Conn()

        async def close_all(self) -> None:
            calls["closed"] = True

    await _probe(_Pool(), "pi")  # type: ignore[arg-type]
    assert calls["got"] == "pi"
    assert calls["ran"] == ("whoami", True)
    assert calls["closed"] is True


# ---------------------------------------------------------------------------
# `serve`
# ---------------------------------------------------------------------------


def test_serve_invokes_server_main(monkeypatch: pytest.MonkeyPatch) -> None:
    called: list[bool] = []
    monkeypatch.setattr("tai_mcp_ssh.server.main", lambda: called.append(True))
    result = CliRunner().invoke(main, ["serve"])
    assert result.exit_code == 0
    assert called == [True]
