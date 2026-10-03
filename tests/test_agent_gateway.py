"""Agent gateway: the tool catalog, the MCP server, its routes, and the agent token."""

from __future__ import annotations

import io
import json
import os
import shutil
import stat
import subprocess
import threading
import time
import uuid
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

import pytest

from loom import agent_tools, mcp_server, routes_agent
from loom.agent_tools import TOOLS, LoomClient, ToolError, call_tool

# --- a canned Loom ------------------------------------------------------------------

PAPER = "kv-paper-selection-error"


def _tasks(params, body):
    return {
        "tasks": {
            "p1": [
                {
                    "slug": PAPER,
                    "title": "Selection Error, Not Value Error",
                    "agent": "cursor",
                    "tmux_interview_target": "loom-cursor-p1-kv:0.0",
                    "updated_at": "2026-09-10",
                },
                {"slug": "kv-paper-studio", "title": "KV studio", "agent": "cursor", "updated_at": "2026-09-01"},
            ],
            "p2": [{"slug": "xorl-dev", "title": "xorl dev", "agent": "claude", "updated_at": "2026-09-30"}],
        }[params["project"]]
    }


ROUTES = {
    ("GET", "/api/projects"): {
        "projects": [
            {"id": "p1", "name": "ar", "path": "/home/x/ar"},
            {"id": "p2", "name": "xorl", "path": "/home/x/xorl"},
        ],
        "defaultProjectId": "p2",
    },
    ("GET", "/api/tasks"): _tasks,
    ("GET", "/api/activity"): {
        "tasks": {
            "p2/xorl-dev": {"project": "p2", "slug": "xorl-dev", "working": True},
            f"p1/{PAPER}": {"project": "p1", "slug": PAPER, "working": False, "finished_at": 1789011279},
        }
    },
    ("GET", "/api/factories/approvals"): {
        "items": [{"factory": "paper", "gate": "final", "id": PAPER, "title": "Selection Error", "detail": "8/15"}]
    },
    ("GET", f"/api/tasks/{PAPER}/ar"): {
        "title": "Selection Error, Not Value Error",
        "stage_label": "Waiting for your final review",
        "loop": {"running": False},
        "best_rating": 6.0,
        "plateaued": True,
        "pdf_available": True,
        "latest_review": {
            "headline": "3 reviewers · lowest 6",
            "reviewers": [
                {"model": "gpt-5.6", "scores": {"rating": 6, "soundness": 3, "recommendation": "weak accept"}},
                {"model": "kimi-k3-max", "scores": {"rating": 7, "soundness": 3}},
            ],
        },
        "actions": {"gate": {"ok": True}, "start": {"ok": False, "why": "waiting for you"}},
        "logs": {"loop": "x" * 100_000},
        "state": {
            "role": "paper",
            "stage": "await_final_review",
            "round": 8,
            "max_rounds": 15,
            "venue": "iclr",
            "idea": {"title": "Selection Error, Not Value Error"},
            "stop_reason": "plateau",
            "gates": [{"kind": "draft", "decision": "approve"}],
            "rounds": [
                {"n": 1, "review": {"scores": {"rating": 5}, "deciding_model": "gpt-5.6"}},
                {"n": 2, "review": {}},
                {"n": 8, "review": {"scores": {"rating": 6}, "deciding_model": "gpt-5.6"}},
            ],
        },
    },
    ("GET", f"/api/tasks/{PAPER}/conversation"): {
        "agent": "cursor",
        "online": True,
        "working": False,
        "total": 99,
        "messages": [
            {"kind": "user", "text": "start round 8"},
            {"kind": "tool", "tool": {"name": "Read"}},
            {"kind": "tool", "tool": {"name": "Edit"}},
            {"kind": "assistant", "text": "Round 8 is complete."},
            {"kind": "tool", "tool": {"name": "Shell"}},
        ],
    },
    ("GET", f"/api/tasks/{PAPER}/ar/review/8"): {
        "round": 8,
        "headline": "3 reviewers · lowest 6",
        "deciding_model": "gpt-5.6",
        "reviewers": [{"model": "gpt-5.6", "scores": {"rating": 6}}, {"model": "kimi-k3-max", "scores": {"rating": 7}}],
        "review": "# Panel\n\n# Reviewer: `gpt-5.6`\n\ngpt says scale up\n\n---\n\n# Reviewer: `kimi-k3-max`\n\nkimi says decompose",
    },
    ("POST", "/api/tasks/xorl-dev/claude/send"): {"ok": True},
    ("GET", "/api/tmux/sessions"): {
        "tmux": True,
        "sessions": [{"name": "loom-cursor-p1-kv", "attached": "1"}, {"name": "scratch", "attached": "0"}],
    },
    ("GET", "/api/tmux/panes"): lambda params, body: {"panes": [{"id": f"{params['session']}:0.0", "title": "bash"}]},
    ("GET", "/api/tmux/capture"): {"ok": True, "text": "agent idle\n"},
    ("POST", "/api/tmux/send-key"): {"ok": True},
    ("POST", "/api/tmux/send-text"): {"ok": True},
}


class FakeClient(LoomClient):
    def __init__(self, routes=None):
        super().__init__("http://loom.test", "tok")
        self.routes = dict(ROUTES if routes is None else routes)
        self.calls: list[tuple] = []

    def request(self, method, path, *, params=None, body=None):
        self.calls.append((method, path, dict(params or {}), body))
        handler = self.routes.get((method, path))
        if handler is None:
            raise ToolError(f"Loom API {method} {path} -> HTTP 404: not found")
        return handler(params or {}, body) if callable(handler) else handler


def _run(name, args=None, client=None):
    text, is_error = call_tool(client or FakeClient(), name, args or {})
    return (text if is_error else json.loads(text)), is_error


# --- the catalog ------------------------------------------------------------------------


def test_catalog_is_well_formed_and_never_destructive_by_accident():
    names = [tool.name for tool in TOOLS]
    assert len(names) == len(set(names))
    for tool in TOOLS:
        schema = tool.schema()
        assert schema["type"] == "object" and schema["additionalProperties"] is False
        assert set(tool.required) <= set(tool.properties)
        assert tool.description and tool.title
        assert set(tool.annotations()) >= {"readOnlyHint", "destructiveHint", "idempotentHint", "openWorldHint"}
        # Deletes, merges, and pushes stay human-only.
        assert not any(word in tool.name for word in ("delete", "remove", "merge", "push"))
    read_only = {t.name for t in TOOLS if t.read_only}
    assert read_only == {
        "loom_status", "list_projects", "list_tasks", "get_task", "read_conversation",
        "list_sessions", "read_screen", "watch_pane", "paper_factory", "get_paper",
        "read_review", "get_review_report",
    }
    assert {t.name for t in TOOLS if t.destructive} == {"stop_agent", "send_keys"}


def test_locate_task_takes_fragments_and_refuses_to_guess():
    client = FakeClient()
    assert client.locate_task("selection-error") == ("p1", _tasks({"project": "p1"}, None)["tasks"][0])
    assert client.locate_task("xorl-dev")[0] == "p2"
    with pytest.raises(ToolError, match="matches 2 tasks"):
        client.locate_task("kv-paper")
    with pytest.raises(ToolError, match="no task matches"):
        client.locate_task("nope")


def test_status_aggregates_activity_and_the_approvals_inbox():
    status, err = _run("loom_status")
    assert not err
    assert status["default_project"] == "xorl"
    assert status["agents_working"] == [{"task": "xorl-dev", "project": "xorl"}]
    assert status["agents_finished_unseen"][0]["task"] == PAPER
    assert status["waiting_on_you"][0]["gate"] == "final"


def test_get_paper_summarizes_instead_of_dumping():
    paper, err = _run("get_paper", {"task": "selection-error"})
    assert not err
    text = json.dumps(paper)
    assert "x" * 1000 not in text  # the 100 KB log never reaches the agent
    assert paper["stage"] == "Waiting for your final review"
    assert paper["ratings_by_round"] == [
        {"round": 1, "lowest_rating": 5, "deciding_model": "gpt-5.6"},
        {"round": 8, "lowest_rating": 6, "deciding_model": "gpt-5.6"},
    ]
    assert paper["actions_available"] == ["gate"]
    assert paper["actions_blocked"] == {"start": "waiting for you"}
    assert paper["latest_review"]["reviewers"][0] == {
        "model": "gpt-5.6", "rating": 6, "soundness": 3, "recommendation": "weak accept",
    }


def test_conversation_collapses_tool_noise():
    convo, err = _run("read_conversation", {"task": PAPER})
    assert not err
    assert [m["role"] for m in convo["messages"]] == ["user", "assistant"]
    assert convo["messages"][1]["tool_calls_before"] == 2
    assert convo["tool_calls_since_last_message"] == 1


def test_review_can_be_narrowed_to_one_reviewer():
    review, err = _run("read_review", {"task": PAPER, "reviewer": "kimi"})
    assert not err and review["round"] == 8
    assert "kimi says decompose" in review["review_markdown"]
    assert "gpt says scale up" not in review["review_markdown"]
    text, err = _run("read_review", {"task": PAPER, "reviewer": "grok"})
    assert err and "kimi-k3-max" in text


def test_tool_faults_come_back_as_errors_not_exceptions():
    assert _run("no_such_tool")[1]
    text, err = _run("get_task", {"task": PAPER, "bogus": 1})
    assert err and "does not take bogus" in text
    text, err = _run("send_to_agent", {"task": "xorl-dev"})
    assert err and "requires text" in text
    text, err = _run("paper_gate", {"task": PAPER, "gate": "final", "decision": "maybe"})
    assert err and "approve|reject" in text


def test_send_to_agent_types_into_the_right_pane():
    client = FakeClient()
    result, err = _run("send_to_agent", {"task": "xorl-dev", "text": "rerun the eval"}, client)
    assert not err and result["submitted"] is True
    assert ("POST", "/api/tasks/xorl-dev/claude/send", {"project": "p2"}, {"text": "rerun the eval", "submit": True}) in client.calls


def test_client_reports_unreachable_server_plainly():
    client = LoomClient("http://127.0.0.1:9", "tok", timeout=2)
    with pytest.raises(ToolError, match="cannot reach Loom"):
        client.get("/api/projects")


# --- tmux tools ------------------------------------------------------------------------


def test_list_sessions_names_the_owning_task():
    listing, err = _run("list_sessions")
    assert not err and listing["count"] == 2
    owned = {s["session"]: s["task"] for s in listing["sessions"]}
    assert owned == {"loom-cursor-p1-kv": f"ar/{PAPER}", "scratch": ""}
    one, err = _run("list_sessions", {"session": "scratch"})
    assert one["sessions"][0]["panes"] == [{"target": "scratch:0.0", "title": "bash"}]
    assert _run("list_sessions", {"session": "nope"})[1]


def test_screen_tools_take_a_task_or_any_pane():
    client = FakeClient()
    by_task, err = _run("read_screen", {"task": "selection-error"}, client)
    assert not err and by_task["pane"] == "loom-cursor-p1-kv:0.0" and by_task["of"] == PAPER
    by_target, err = _run("read_screen", {"target": "scratch:0.0"}, client)
    assert not err and by_target["pane"] == "scratch:0.0"
    text, err = _run("read_screen", {"target": "scratch; rm -rf /"})
    assert err and "session:window.pane" in text
    assert _run("read_screen", {})[1]


def test_watch_pane_returns_when_the_screen_matches(monkeypatch):
    screens = iter(["building...", "building... 50%", "tests PASSED", "never read"])

    class Clock:
        now = 0.0

        def monotonic(self):
            return self.now

        def sleep(self, secs):
            self.now += secs

    monkeypatch.setattr(agent_tools, "time", Clock())
    routes = dict(ROUTES)
    routes[("GET", "/api/tmux/capture")] = lambda params, body: {"ok": True, "text": next(screens)}
    watched, err = _run("watch_pane", {"target": "scratch:0.0", "until": "PASSED|FAILED", "seconds": 30}, FakeClient(routes))
    assert not err
    assert watched["until_matched"] is True and watched["changed"] is True
    assert watched["screen"] == "tests PASSED" and watched["watched_seconds"] == 2.0
    routes[("GET", "/api/tmux/capture")] = {"ok": True, "text": "still idle"}
    idle, err = _run("watch_pane", {"target": "scratch:0.0", "until": "PASSED", "seconds": 3}, FakeClient(routes))
    assert idle["until_matched"] is False and idle["changed"] is False and idle["watched_seconds"] == 3.0
    assert _run("watch_pane", {"target": "scratch:0.0", "until": "("})[1]


def test_send_keys_validates_then_presses_in_order():
    client = FakeClient()
    result, err = _run("send_keys", {"task": PAPER, "keys": ["Escape", "C-c", "Down", "Enter"]}, client)
    assert not err and result["sent_keys"] == ["Escape", "C-c", "Down", "Enter"]
    pressed = [c[3]["key"] for c in client.calls if c[1] == "/api/tmux/send-key"]
    assert pressed == ["Escape", "C-c", "Down", "Enter"]
    text, err = _run("send_keys", {"target": "scratch:0.0", "keys": ["rm -rf /"]})
    assert err and "unsupported key" in text


def test_send_to_agent_reaches_any_pane_by_target():
    client = FakeClient()
    result, err = _run("send_to_agent", {"target": "scratch:0.0", "text": "make test"}, client)
    assert not err and result["to"] == "scratch:0.0"
    assert ("POST", "/api/tmux/send-text", {}, {"target": "scratch:0.0", "text": "make test", "submit": True}) in client.calls


# --- MCP protocol -------------------------------------------------------------------


def _dispatcher():
    return mcp_server.MCPDispatcher(FakeClient)


def test_initialize_negotiates_the_protocol_version():
    reply = _dispatcher().handle(
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}}
    )
    assert reply["result"]["protocolVersion"] == "2025-06-18"
    assert reply["result"]["capabilities"] == {"tools": {"listChanged": False}}
    reply = _dispatcher().handle(
        {"jsonrpc": "2.0", "id": 2, "method": "initialize", "params": {"protocolVersion": "1999-01-01"}}
    )
    assert reply["result"]["protocolVersion"] == mcp_server.LATEST_VERSION


def test_notifications_and_client_responses_get_no_reply():
    dispatcher = _dispatcher()
    assert dispatcher.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}) is None
    assert dispatcher.handle({"jsonrpc": "2.0", "id": 9, "result": {}}) is None


def test_tools_list_and_call():
    dispatcher = _dispatcher()
    listed = dispatcher.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})["result"]["tools"]
    assert [t["name"] for t in listed] == [t.name for t in TOOLS]
    assert all("inputSchema" in t and "annotations" in t for t in listed)
    called = dispatcher.handle(
        {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "loom_status", "arguments": {}}}
    )["result"]
    assert called["isError"] is False
    assert json.loads(called["content"][0]["text"])["projects"] == 2
    unknown = dispatcher.handle(
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "rm_rf"}}
    )
    assert unknown["error"]["code"] == mcp_server.INVALID_PARAMS
    missing = dispatcher.handle({"jsonrpc": "2.0", "id": 4, "method": "nope"})
    assert missing["error"]["code"] == mcp_server.METHOD_NOT_FOUND


def test_http_framing():
    dispatcher = _dispatcher()
    status, _, body = mcp_server.handle_http_post(b"not json", dispatcher)
    assert status == 400 and json.loads(body)["error"]["code"] == mcp_server.PARSE_ERROR
    status, headers, body = mcp_server.handle_http_post(
        b'{"jsonrpc":"2.0","method":"notifications/initialized"}', dispatcher
    )
    assert (status, body) == (202, b"")
    batch = json.dumps(
        [{"jsonrpc": "2.0", "id": 1, "method": "ping"}, {"jsonrpc": "2.0", "method": "notifications/x"}]
    ).encode()
    status, headers, body = mcp_server.handle_http_post(batch, dispatcher)
    assert status == 200 and headers["Content-Type"] == "application/json"
    assert json.loads(body) == [{"jsonrpc": "2.0", "id": 1, "result": {}}]


def test_stdio_transport(monkeypatch):
    monkeypatch.setattr(mcp_server, "LoomClient", lambda *a, **k: FakeClient())
    stdin = io.StringIO(
        '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}\n'
        '{"jsonrpc":"2.0","method":"notifications/initialized"}\n'
        "\n"
        "garbage\n"
        '{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"list_projects"}}\n'
    )
    stdout = io.StringIO()
    mcp_server.serve_stdio("http://loom.test", "tok", stdin=stdin, stdout=stdout)
    replies = [json.loads(line) for line in stdout.getvalue().splitlines()]
    assert [r.get("id") for r in replies] == [1, None, 2]
    assert replies[1]["error"]["code"] == mcp_server.PARSE_ERROR
    assert "ar" in replies[2]["result"]["content"][0]["text"]


# --- routes over real HTTP -----------------------------------------------------------


class _Handler(BaseHTTPRequestHandler):
    auth_token = "tok"

    def log_message(self, *args):  # keep test output quiet
        pass

    def _send(self, status, body, headers):
        self.send_response(status)
        for key, value in headers:
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def _dispatch(self, fn, *extra):
        parsed = urlparse(self.path)
        if not fn(self, parsed.path, parsed, *extra):
            self._send(404, b"{}", [("Content-Length", "2")])

    def do_GET(self):  # noqa: N802
        self._dispatch(routes_agent.handle_get)

    def do_POST(self):  # noqa: N802
        self._dispatch(routes_agent.handle_raw_post)

    def do_DELETE(self):  # noqa: N802
        self._dispatch(routes_agent.handle_delete)


@pytest.fixture()
def server(monkeypatch):
    monkeypatch.setattr(routes_agent, "LoomClient", lambda *a, **k: FakeClient())
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()


def _http(url, method="GET", body=None, headers=None):
    data = None if body is None else (body if isinstance(body, bytes) else json.dumps(body).encode())
    req = urllib.request.Request(url, data=data, method=method, headers={"Content-Type": "application/json", **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, dict(resp.headers), resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers), exc.read()


def test_mcp_route(server):
    init = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}}
    status, headers, body = _http(f"{server}/mcp", "POST", init)
    assert status == 200 and json.loads(body)["result"]["serverInfo"]["name"] == "loom"
    call = {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "list_projects"}}
    status, _, body = _http(f"{server}/mcp", "POST", call)
    assert '"ar"' in json.loads(body)["result"]["content"][0]["text"]
    assert _http(f"{server}/mcp")[0] == 405
    assert _http(f"{server}/mcp", "DELETE")[0] == 405
    status, _, _ = _http(f"{server}/mcp", "POST", init, {"Origin": "https://evil.example"})
    assert status == 403
    host = server.removeprefix("http://")
    assert _http(f"{server}/mcp", "POST", init, {"Origin": f"http://{host}"})[0] == 200


def test_manifest_speaks_from_the_callers_side(server):
    status, _, body = _http(f"{server}/api/agent/manifest", headers={"Host": "127.0.0.1:18766"})
    data = json.loads(body)
    assert status == 200
    assert data["mcp"]["url"] == "http://127.0.0.1:18766/mcp"
    assert "chat" not in data
    assert len(data["tools"]) == len(TOOLS)
    assert "Bearer tok" not in json.dumps(data)  # describes auth, never leaks the token


# --- the scoped agent token -------------------------------------------------------


@pytest.fixture()
def fresh_token(tmp_path, monkeypatch):
    monkeypatch.setenv("LOOM_HOME", str(tmp_path / "loom-home"))
    monkeypatch.delenv(routes_agent.AGENT_TOKEN_ENV, raising=False)
    monkeypatch.setattr(routes_agent, "_agent_token_cache", "")
    return tmp_path


def test_agent_token_is_private_and_stable(fresh_token):
    token = routes_agent.agent_token()
    assert len(token) == 48
    path = routes_agent.agent_token_path()
    assert path.read_text() == token
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    routes_agent._agent_token_cache = ""
    assert routes_agent.agent_token() == token  # re-read, not regenerated


def test_agent_token_opens_only_the_gateway(fresh_token, monkeypatch):
    token = routes_agent.agent_token()
    for path in ("/mcp", "/api/agent/manifest"):
        assert routes_agent.agent_token_allows(token, path), path
    for path in ("/api/tasks", "/api/tasks/x/claude/send", "/api/projects", "/", "/api/agent/chat", "/mcpx"):
        assert not routes_agent.agent_token_allows(token, path), path
    assert not routes_agent.agent_token_allows("wrong", "/mcp")
    assert not routes_agent.agent_token_allows("", "/mcp")
    monkeypatch.setenv(routes_agent.AGENT_TOKEN_ENV, "from-env")
    assert routes_agent.agent_token_allows("from-env", "/mcp")


# --- terminal attach for bots ---------------------------------------------------------


def test_agent_token_opens_attach_but_no_other_tmux_route(fresh_token):
    token = routes_agent.agent_token()
    for path in ("/api/tmux/stream", "/api/tmux/stream-input", "/api/tmux/stream-close", "/api/tmux/stream-heartbeat"):
        assert routes_agent.agent_token_allows(token, path), path
    # Keys, text, captures and listings stay behind MCP's curated tools.
    for path in ("/api/tmux/send-key", "/api/tmux/send-text", "/api/tmux/capture", "/api/tmux/sessions"):
        assert not routes_agent.agent_token_allows(token, path), path


def test_stream_registry_remembers_who_opened_a_stream():
    from loom.web import _TerminalStreamRegistry

    registry = _TerminalStreamRegistry()
    read_end, write_end = os.pipe()
    try:
        stream = registry.register(write_end, owner="agent")
        assert registry.owner(stream) == "agent"
        assert registry.owner(registry.register(write_end)) == "web"
        registry.unregister(stream, write_end)
        assert registry.owner(stream) is None
    finally:
        os.close(read_end)
        os.close(write_end)


def test_bots_cannot_drive_the_owners_streams():
    from loom import routes_tmux

    class Registry:
        def __init__(self, owner):
            self._owner = owner

        def owner(self, stream_id):
            return self._owner

    class Request:
        via_agent_token = True

    request = Request()
    request.terminal_streams = Registry("web")
    assert routes_tmux._foreign_stream(request, "a" * 32)
    request.terminal_streams = Registry("agent")
    assert not routes_tmux._foreign_stream(request, "a" * 32)
    request.via_agent_token = False
    request.terminal_streams = Registry("web")
    assert not routes_tmux._foreign_stream(request, "a" * 32)  # the owner drives anything


def _tmux(*args):
    return subprocess.run(["tmux", *args], capture_output=True, text=True)


def _owner_client(session, cols, rows):
    """A real tmux client attached the way the owner's terminal would be."""
    import fcntl
    import pty
    import struct
    import termios

    master, slave = pty.openpty()
    fcntl.ioctl(master, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
    proc = subprocess.Popen(
        ["tmux", "attach-session", "-t", session],
        stdin=slave, stdout=slave, stderr=slave, start_new_session=True,
    )
    os.close(slave)
    return proc, master


@pytest.mark.skipif(shutil.which("tmux") is None, reason="needs tmux")
def test_isolated_attach_leaves_the_owners_view_alone():
    from loom import tmux_util

    session = f"loom-test-{uuid.uuid4().hex[:6]}"
    _tmux("new-session", "-d", "-s", session, "-x", "200", "-y", "50")
    owner = bot = None
    try:
        _tmux("new-window", "-t", session)
        _tmux("select-window", "-t", f"{session}:1")
        owner = _owner_client(session, 200, 50)
        time.sleep(1.0)
        def size(window):
            return _tmux("display", "-p", "-t", f"{session}:{window}", "#{window_width}x#{window_height}").stdout.strip()

        owner_size = size(1)  # what the owner's 200x50 terminal shows
        bot = tmux_util.open_pane_attach(f"{session}:0.0", 80, 24, isolated=True)
        assert bot[0] is not None
        time.sleep(1.0)
        # The owner still looks at window 1, and every window follows the
        # owner's size - none shrinks to the bot's 80x24. (A plain attach
        # switches the owner to window 0 and shrinks it to 80x23.)
        assert _tmux("display", "-p", "-t", session, "#{window_index}").stdout.strip() == "1"
        assert size(1) == owner_size
        assert size(0) == owner_size
        observers = [
            name for name in _tmux("list-sessions", "-F", "#{session_name}").stdout.split()
            if name.startswith(tmux_util.OBSERVER_PREFIX)
        ]
        assert observers
        bot[0].terminate()
        bot[0].wait(timeout=5)
        time.sleep(0.5)
        left = _tmux("list-sessions", "-F", "#{session_name}").stdout.split()
        assert not set(observers) & set(left)  # gone with its client
        assert session in left  # the shared windows are untouched
    finally:
        for proc in (owner, bot):
            if proc and proc[0] is not None:
                proc[0].terminate()
                os.close(proc[1])
        _tmux("kill-session", "-t", session)


@pytest.mark.skipif(shutil.which("tmux") is None, reason="needs tmux")
def test_stale_observer_sessions_are_reaped():
    from loom import tmux_util

    stale = f"{tmux_util.OBSERVER_PREFIX}{uuid.uuid4().hex[:8]}"
    _tmux("new-session", "-d", "-s", stale)
    try:
        assert tmux_util.reap_stale_observers() >= 1
        assert _tmux("has-session", "-t", stale).returncode != 0
    finally:
        _tmux("kill-session", "-t", stale)
