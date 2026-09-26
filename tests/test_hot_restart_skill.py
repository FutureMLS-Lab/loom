"""Tests for the developer-only Loom hot restart helper."""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest


SCRIPT = (
    Path(__file__).resolve().parents[1]
    / "loom"
    / "skills"
    / "dev"
    / "loom-hot-restart"
    / "scripts"
    / "hot_restart.py"
)
SPEC = importlib.util.spec_from_file_location("loom_hot_restart", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
hot_restart = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = hot_restart
SPEC.loader.exec_module(hot_restart)


def _loom_process(port: int = 8766):
    return hot_restart.Process(
        pid=os.getpid(),
        ppid=os.getppid(),
        argv=(
            "/old/.venv/bin/python",
            "-m",
            "loom",
            "web",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--project",
            "/project",
            "--projects",
        ),
        cwd=Path("/old"),
        env={
            "PATH": os.environ.get("PATH", ""),
            "LOOM_WEB_AUTH_TOKEN": "a" * 48,
            "TOGETHER_API_KEY": "secret-api-key",
        },
    )


def test_find_loom_by_explicit_port(monkeypatch: pytest.MonkeyPatch) -> None:
    expected = _loom_process()
    unrelated = hot_restart.Process(
        pid=999999,
        ppid=1,
        argv=("python", "-m", "http.server", "8766"),
        cwd=Path("/tmp"),
        env={},
    )
    monkeypatch.setattr(hot_restart, "_processes", lambda: [unrelated, expected])
    assert hot_restart.find_loom(8766) == expected


def test_find_and_relaunch_python_console_script_shape(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    expected = hot_restart.Process(
        pid=123456,
        ppid=1,
        argv=(
            "/old/.venv/bin/python",
            "/old/.venv/bin/loom",
            "web",
            "--port",
            "8765",
            "--project",
            "/project",
        ),
        cwd=Path("/old"),
        env={},
    )
    monkeypatch.setattr(hot_restart, "_processes", lambda: [expected])
    assert hot_restart.find_loom(8765) == expected

    source = tmp_path / "loom"
    (source / "loom").mkdir(parents=True)
    python = source / ".venv" / "bin" / "python"
    python.parent.mkdir(parents=True)
    python.write_text("", encoding="utf-8")
    command, cwd = hot_restart._launch_command(expected, source)
    assert command == [
        str(python),
        "-m",
        "loom",
        "web",
        "--port",
        "8765",
        "--project",
        "/project",
    ]
    assert cwd == source


def test_find_turbogate_when_launched_through_dynamic_loader(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    expected = hot_restart.Process(
        pid=123456,
        ppid=1,
        argv=(
            "/lib64/ld-linux-x86-64.so.2",
            "/home/user/.turbogate/bin/turbogate",
            "http",
            "8766",
            "p-test",
            "--public",
        ),
        cwd=Path("/tmp"),
        env={},
    )
    monkeypatch.setattr(hot_restart, "_processes", lambda: [expected])
    assert hot_restart.find_turbogate(8766) == expected


def test_launch_command_preserves_web_flags(tmp_path: Path) -> None:
    source = tmp_path / "loom"
    (source / "loom").mkdir(parents=True)
    python = source / ".venv" / "bin" / "python"
    python.parent.mkdir(parents=True)
    python.write_text("", encoding="utf-8")

    command, cwd = hot_restart._launch_command(_loom_process(), source)
    assert command[0] == str(python)
    assert command[1:4] == ["-m", "loom", "web"]
    assert "--projects" in command
    assert cwd == source


def test_dry_run_never_restarts_or_prints_secrets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "loom"
    (source / "loom").mkdir(parents=True)
    python = source / ".venv" / "bin" / "python"
    python.parent.mkdir(parents=True)
    python.write_text("", encoding="utf-8")

    old = _loom_process()
    tunnel = hot_restart.Process(
        pid=123456,
        ppid=old.pid,
        argv=("turbogate", "http", "8766", "--public"),
        cwd=source,
        env={},
    )
    monkeypatch.setattr(hot_restart, "find_loom", lambda port: old)
    monkeypatch.setattr(hot_restart, "find_turbogate", lambda port: tunnel)
    monkeypatch.setattr(hot_restart, "_active_one_shot_jobs", lambda port, token: [])
    monkeypatch.setattr(
        hot_restart,
        "_public_url",
        lambda port, token, supplied: "https://p-test.gate.together-turbo.com",
    )

    def must_not_stop(*args, **kwargs):
        raise AssertionError("dry-run attempted to stop a process")

    monkeypatch.setattr(hot_restart, "_stop_group", must_not_stop)
    result = hot_restart.restart(
        SimpleNamespace(
            source=source,
            port=8766,
            stop_port=[],
            preserve_tunnel=True,
            public_url="",
            allow_active_jobs=False,
            startup_timeout=5.0,
            dry_run=True,
        )
    )
    rendered = str(result)
    assert result["dry_run"] is True
    assert result["tunnel_pid"] == tunnel.pid
    assert "secret-api-key" not in rendered
    assert "a" * 48 not in rendered
