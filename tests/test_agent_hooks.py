"""The stop hook is only useful if it survives the real event payloads."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from loom import agent_hooks


@pytest.fixture()
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("LOOM_HOME", str(tmp_path / ".loom"))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    return tmp_path


def test_token_is_stable_and_private(home):
    first = agent_hooks.hook_token()
    assert len(first) >= 32
    assert agent_hooks.hook_token() == first, "a new token each call would break the hook"
    assert oct(agent_hooks.hook_token_path().stat().st_mode)[-3:] == "600"


def test_token_path_does_not_depend_on_the_working_directory(home, monkeypatch):
    """A relative path would drop the secret wherever the server happened to run."""
    monkeypatch.delenv("LOOM_HOME", raising=False)
    path = agent_hooks.hook_token_path()
    assert path.is_absolute()
    assert path.parent == home / ".loom"


def test_install_preserves_hooks_the_user_already_had(home):
    config = home / ".cursor" / "hooks.json"
    config.parent.mkdir(parents=True)
    config.write_text(json.dumps({
        "version": 1,
        "hooks": {
            "stop": [{"command": "./hooks/mine.sh"}],
            "beforeShellExecution": [{"command": "./hooks/gate.sh"}],
        },
    }))

    ok, _ = agent_hooks.install_cursor_hook("http://127.0.0.1:9/api/activity/finished")
    assert ok

    data = json.loads(config.read_text())
    commands = [h["command"] for h in data["hooks"]["stop"]]
    assert "./hooks/mine.sh" in commands, "clobbered a hook the user wrote"
    assert any(agent_hooks.HOOK_MARKER in c for c in commands)
    assert data["hooks"]["beforeShellExecution"] == [{"command": "./hooks/gate.sh"}]


def test_install_is_idempotent(home):
    url = "http://127.0.0.1:9/api/activity/finished"
    agent_hooks.install_cursor_hook(url)
    agent_hooks.install_cursor_hook(url)
    data = json.loads((home / ".cursor" / "hooks.json").read_text())
    ours = [h for h in data["hooks"]["stop"] if agent_hooks.HOOK_MARKER in h["command"]]
    assert len(ours) == 1, "restarting Loom must not stack duplicate hooks"


def test_refuses_to_touch_a_broken_config(home):
    config = home / ".cursor" / "hooks.json"
    config.parent.mkdir(parents=True)
    config.write_text("{ not json")
    ok, detail = agent_hooks.install_cursor_hook("http://127.0.0.1:9/x")
    assert not ok and "left it alone" in detail
    assert config.read_text() == "{ not json"


def _run_hook(home, event: dict) -> subprocess.CompletedProcess:
    script = home / ".cursor" / "hooks" / agent_hooks.HOOK_SCRIPT_NAME
    return subprocess.run(
        [sys.executable, str(script)],
        input=json.dumps(event),
        capture_output=True,
        text=True,
        timeout=15,
    )


def test_hook_posts_the_workspace_and_never_fails_the_agent(home):
    """The real payload shape, captured from a live cursor-agent stop event."""
    import http.server
    import threading

    seen: list[dict] = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            length = int(self.headers.get("Content-Length", 0))
            seen.append({
                "body": json.loads(self.rfile.read(length) or "{}"),
                "token": self.headers.get("X-Loom-Hook-Token", ""),
            })
            self.send_response(200)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, *a):  # keep test output clean
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{server.server_port}/api/activity/finished"
    agent_hooks.install_cursor_hook(url)

    proc = _run_hook(home, {
        "hook_event_name": "stop",
        "workspace_roots": ["/home/admin/ar/.RUD/some-paper/work"],
        "session_id": "abc123",
        "model": "claude-fable-5",
    })
    server.shutdown()

    assert proc.returncode == 0
    assert proc.stdout.strip() == "{}", "a stop hook must return JSON on stdout"
    assert len(seen) == 1
    assert seen[0]["body"]["cwd"] == "/home/admin/ar/.RUD/some-paper/work"
    assert seen[0]["token"] == agent_hooks.hook_token()


def test_hook_stays_silent_when_loom_is_down(home):
    """Loom being stopped must never hang or fail somebody's agent turn."""
    agent_hooks.install_cursor_hook("http://127.0.0.1:1/api/activity/finished")
    proc = _run_hook(home, {"workspace_roots": ["/tmp/x"], "hook_event_name": "stop"})
    assert proc.returncode == 0
    assert proc.stdout.strip() == "{}"


def test_hook_survives_junk_input(home):
    agent_hooks.install_cursor_hook("http://127.0.0.1:1/x")
    script = home / ".cursor" / "hooks" / agent_hooks.HOOK_SCRIPT_NAME
    proc = subprocess.run(
        [sys.executable, str(script)], input="not json at all",
        capture_output=True, text=True, timeout=15,
    )
    assert proc.returncode == 0
