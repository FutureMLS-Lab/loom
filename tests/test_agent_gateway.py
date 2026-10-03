"""Agent gateway: the tool catalog, the MCP server, the concierge, and routes."""

from __future__ import annotations

import io
import json
import stat
import subprocess
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

import pytest

from loom import concierge, mcp_server, routes_agent
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
        # Deletes, merges, pushes, and raw keystrokes stay human-only.
        assert not any(word in tool.name for word in ("delete", "remove", "merge", "push", "keys"))
    read_only = {t.name for t in TOOLS if t.read_only}
    assert read_only == {
        "loom_status", "list_projects", "list_tasks", "get_task", "read_conversation",
        "read_screen", "paper_factory", "get_paper", "read_review", "get_review_report",
    }
    assert {t.name for t in TOOLS if t.destructive} == {"stop_agent"}


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


# --- concierge ------------------------------------------------------------------------


@pytest.fixture()
def agent_home(tmp_path, monkeypatch):
    monkeypatch.setenv(concierge.HOME_ENV, str(tmp_path / "agent"))
    monkeypatch.setenv(concierge.WORKDIR_ENV, str(tmp_path / "work"))
    monkeypatch.setenv("LOOM_WEB_AUTH_TOKEN", "secret-web-token")
    return tmp_path


class FakeCLI:
    def __init__(self, replies):
        self.replies = list(replies)
        self.calls: list[dict] = []

    def __call__(self, command, **kwargs):
        self.calls.append({"command": command, **kwargs})
        reply = self.replies.pop(0)
        if isinstance(reply, subprocess.CompletedProcess):
            return reply
        return subprocess.CompletedProcess(command, 0, stdout=json.dumps(reply), stderr="")


def _ok(text, session="cli-session-1"):
    return {"type": "result", "is_error": False, "result": text, "session_id": session, "total_cost_usd": 0.01, "num_turns": 2}


def test_conversation_keeps_context_across_turns(agent_home):
    cli = FakeCLI([_ok("two things wait on you"), _ok("gpt-5.6 scored lowest")])
    first = concierge.chat("what waits on me?", base_url="http://127.0.0.1:8765", token="tok", runner=cli)
    assert first["reply"] == "two things wait on you"
    command = cli.calls[0]["command"]
    assert "--session-id" in command and "--resume" not in command
    assert cli.calls[0]["input"] == "what waits on me?"
    assert "what waits on me?" not in command  # stdin, not argv
    second = concierge.chat("which reviewer was lowest?", first["session"], base_url="http://127.0.0.1:8765", token="tok", runner=cli)
    assert second["session"] == first["session"]
    resumed = cli.calls[1]["command"]
    assert resumed[resumed.index("--resume") + 1] == "cli-session-1"
    record = concierge.read_session(first["session"])
    assert [t["role"] for t in record["turns"]] == ["user", "assistant", "user", "assistant"]
    assert concierge.list_sessions()[0]["turns"] == 2


def test_the_cli_is_fenced_to_loom_tools(agent_home):
    cli = FakeCLI([_ok("ok")])
    concierge.chat("hi", base_url="http://127.0.0.1:8765", token="s3cr3t-bearer-9f2", runner=cli)
    call = cli.calls[0]
    command = call["command"]
    assert command[command.index("--tools") + 1] == ""
    assert "--strict-mcp-config" in command
    assert command[command.index("--allowedTools") + 1] == "mcp__loom"
    assert command[command.index("--setting-sources") + 1] == "project"
    assert "s3cr3t-bearer-9f2" not in " ".join(command)  # the token lives in the 0600 config file
    assert "LOOM_WEB_AUTH_TOKEN" not in call["env"]
    config = Path(command[command.index("--mcp-config") + 1])
    assert stat.S_IMODE(config.stat().st_mode) == 0o600
    server = json.loads(config.read_text())["mcpServers"]["loom"]
    assert server == {"type": "http", "url": "http://127.0.0.1:8765/mcp", "headers": {"Authorization": "Bearer s3cr3t-bearer-9f2"}}


def test_default_workdir_stays_outside_home(monkeypatch):
    monkeypatch.delenv(concierge.WORKDIR_ENV, raising=False)
    assert Path.home() not in concierge.workdir().resolve().parents


def test_failures_raise_and_leave_no_half_turn(agent_home):
    cli = FakeCLI([subprocess.CompletedProcess([], 1, stdout="", stderr="auth expired")])
    with pytest.raises(concierge.ConciergeError, match="auth expired"):
        concierge.chat("hi", base_url="http://127.0.0.1:8765", token="tok", runner=cli)
    assert concierge.list_sessions() == []
    cli = FakeCLI([{"type": "result", "is_error": True, "result": "rate limited"}])
    with pytest.raises(concierge.ConciergeError, match="rate limited"):
        concierge.chat("hi", base_url="http://127.0.0.1:8765", token="tok", runner=cli)


def test_input_validation(agent_home):
    with pytest.raises(ValueError):
        concierge.chat("   ", base_url="x", token="t", runner=FakeCLI([]))
    with pytest.raises(ValueError, match="session"):
        concierge.chat("hi", "../../etc/passwd", base_url="x", token="t", runner=FakeCLI([]))
    assert concierge.read_session("../x") is None
    assert concierge.delete_session("../x") is False


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
def server(monkeypatch, agent_home):
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


def test_chat_route_and_sessions(server, monkeypatch):
    seen = {}

    def fake_chat(message, session, **kwargs):
        seen.update(message=message, session=session, **kwargs)
        return {"ok": True, "session": "a" * 32, "reply": "hello"}

    monkeypatch.setattr(concierge, "chat", fake_chat)
    status, _, body = _http(f"{server}/api/agent/chat", "POST", {"message": "hi"})
    assert status == 200 and json.loads(body)["reply"] == "hello"
    assert seen["message"] == "hi" and seen["session"] is None and seen["token"] == "tok"
    assert seen["base_url"].startswith("http://127.0.0.1:")
    assert _http(f"{server}/api/agent/chat", "POST", b"[1, 2]")[0] == 400

    def failing(*a, **k):
        raise concierge.ConciergeError("cli missing")

    monkeypatch.setattr(concierge, "chat", failing)
    status, _, body = _http(f"{server}/api/agent/chat", "POST", {"message": "hi"})
    assert status == 502 and "cli missing" in json.loads(body)["error"]
    status, _, body = _http(f"{server}/api/agent/sessions")
    assert status == 200 and json.loads(body)["sessions"] == []
    assert _http(f"{server}/api/agent/sessions/{'b' * 32}")[0] == 404


def test_manifest_speaks_from_the_callers_side(server):
    status, _, body = _http(f"{server}/api/agent/manifest", headers={"Host": "127.0.0.1:18766"})
    data = json.loads(body)
    assert status == 200
    assert data["mcp"]["url"] == "http://127.0.0.1:18766/mcp"
    assert data["chat"]["url"] == "http://127.0.0.1:18766/api/agent/chat"
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
    for path in ("/mcp", "/api/agent/chat", "/api/agent/manifest", "/api/agent/sessions/" + "a" * 32):
        assert routes_agent.agent_token_allows(token, path), path
    for path in ("/api/tasks", "/api/tasks/x/claude/send", "/api/projects", "/", "/agent", "/mcpx"):
        assert not routes_agent.agent_token_allows(token, path), path
    assert not routes_agent.agent_token_allows("wrong", "/mcp")
    assert not routes_agent.agent_token_allows("", "/mcp")
    monkeypatch.setenv(routes_agent.AGENT_TOKEN_ENV, "from-env")
    assert routes_agent.agent_token_allows("from-env", "/mcp")
