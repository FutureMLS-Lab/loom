"""Agent gateway routes: Loom's MCP server over HTTP, plus discovery.

- ``POST /mcp``                 Streamable HTTP MCP (``GET``/``DELETE`` -> 405)
- ``GET  /api/agent/manifest``  how an agent or bot connects to this server

Everything sits behind the server's normal auth, and bots can hold a
narrower credential (the agent token) that opens these routes plus the
raw terminal attach (``/api/tmux/stream*``, isolated for bots) - nothing else.
Browser-originated POSTs must also be same-origin, so a page on another
site cannot drive the MCP tools with a browser's cached Basic credentials.
"""

from __future__ import annotations

import hmac
import os
import secrets
import stat
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from loom import __version__
from loom.agent_hooks import loom_home
from loom.agent_tools import TOOLS, LoomClient
from loom.mcp_server import MAX_BODY_BYTES, MCPDispatcher, handle_http_post
from loom.web_util import _json_bytes

AGENT_TOKEN_ENV = "LOOM_AGENT_TOKEN"
AGENT_ROUTES = (
    "/mcp",
    "/api/agent/manifest",
    # Raw terminal attach. A bot's attach is isolated (routes_tmux /
    # tmux_util.open_pane_attach), and it may drive only its own streams.
    "/api/tmux/stream",
    "/api/tmux/stream-input",
    "/api/tmux/stream-close",
    "/api/tmux/stream-heartbeat",
)
_agent_token_cache = ""


def agent_token_path() -> Path:
    return loom_home() / "agent" / "agent-token"


def agent_token() -> str:
    """A credential for agents and bots alone, created once and kept 0600.

    Separate from the web auth token, like the hook token: a bot needs Loom's
    curated MCP tools, not the whole REST API, so it gets a secret that opens
    ``/mcp``, the manifest, and an isolated terminal attach - nothing else. Even if a bot's config is
    readable by its own agent, this token cannot reach deletes, worktree
    pushes, or raw keystrokes. ``LOOM_AGENT_TOKEN`` overrides the generated
    one.
    """
    global _agent_token_cache
    override = os.environ.get(AGENT_TOKEN_ENV, "").strip()
    if override:
        return override
    if _agent_token_cache:
        return _agent_token_cache
    path = agent_token_path()
    try:
        existing = path.read_text(encoding="utf-8").strip()
    except OSError:
        existing = ""
    if not existing:
        existing = secrets.token_hex(24)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(existing, encoding="utf-8")
            path.chmod(stat.S_IRUSR | stat.S_IWUSR)
        except OSError:
            return ""
    _agent_token_cache = existing
    return existing


def agent_token_allows(token: str, path: str) -> bool:
    expected = agent_token()
    return bool(token and expected and path in AGENT_ROUTES and hmac.compare_digest(token, expected))


def _loopback_url(self) -> str:
    """This server as seen from this host - where tool calls loop back to."""
    host, port = self.server.server_address[:2]
    host = str(host)
    if host in ("", "0.0.0.0", "::"):
        host = "127.0.0.1"
    if ":" in host:
        host = f"[{host}]"
    return f"http://{host}:{port}"


def _public_url(self) -> str:
    """This server as the caller reached it (tunnels and proxies included)."""
    host = (self.headers.get("Host") or "").strip()
    return f"http://{host}" if host else _loopback_url(self)


def _same_origin(self) -> bool:
    origin = (self.headers.get("Origin") or "").strip()
    if not origin:
        return True  # non-browser clients send no Origin; auth still applies
    return urlparse(origin).netloc == (self.headers.get("Host") or "").strip()


def _send_raw(self, status: int, headers: dict[str, str], body: bytes) -> None:
    self.send_response(status)
    for key, value in headers.items():
        self.send_header(key, value)
    self.send_header("Content-Length", str(len(body)))
    self.send_header("Cache-Control", "no-store")
    self.end_headers()
    if body:
        self.wfile.write(body)


def _method_not_allowed(self) -> None:
    _send_raw(
        self,
        405,
        {"Allow": "POST", "Content-Type": "application/json"},
        b'{"error": "use POST for MCP; this server offers no SSE stream"}',
    )


def manifest(self) -> dict[str, Any]:
    base = _public_url(self)
    return {
        "name": "loom",
        "version": __version__,
        "auth": {
            "header": "Authorization: Bearer <token>",
            "bots": "the agent token - opens /mcp, this manifest, and isolated terminal attach (`loom agent-config --show-token`)",
            "owner": "the server's --auth-token opens everything",
        },
        "mcp": {
            "url": f"{base}/mcp",
            "transport": "streamable-http",
            "stdio": "loom mcp  (env: LOOM_URL, LOOM_WEB_AUTH_TOKEN)",
        },
        "attach": {
            "open": f"GET {base}/api/tmux/stream?target=<session:window.pane>&cols=120&rows=40",
            "stream": "chunked raw PTY bytes (xterm); response header X-Loom-Terminal-Stream = stream_id",
            "input": f"POST {base}/api/tmux/stream-input {{stream_id, text}}",
            "keepalive": f"POST {base}/api/tmux/stream-heartbeat {{stream_id}} at least every 60 s",
            "close": f"POST {base}/api/tmux/stream-close {{stream_id}}",
            "isolation": "agent-token attaches never switch windows or resize them for other clients",
        },
        "tools": [
            {"name": t.name, "title": t.title, "read_only": t.read_only} for t in TOOLS
        ],
        "setup": "run `loom agent-config` on the Loom host for ready-to-paste client configs",
    }


def handle_get(self, path, parsed) -> bool:
    if path == "/mcp":
        _method_not_allowed(self)
        return True
    if path == "/api/agent/manifest":
        st, b, h = _json_bytes(manifest(self))
        self._send(st, b, h)
        return True
    return False


def handle_raw_post(self, path, parsed) -> bool:
    """``POST /mcp`` reads its own body: MCP must see batches and parse errors."""
    if path != "/mcp":
        return False
    if not _same_origin(self):
        st, b, h = _json_bytes({"ok": False, "error": "cross-origin request refused"}, 403)
        self._send(st, b, h)
        return True
    try:
        size = int(self.headers.get("Content-Length", "0") or 0)
    except ValueError:
        size = -1
    if size < 0 or size > MAX_BODY_BYTES:
        st, b, h = _json_bytes({"ok": False, "error": "bad or oversized body"}, 413)
        self._send(st, b, h)
        return True
    raw = self.rfile.read(size) if size else b""
    # Tool calls loop back over the REST API with the server's own token, so
    # an agent-token caller gets the curated tools and nothing more.
    base, token = _loopback_url(self), self.auth_token
    dispatcher = MCPDispatcher(lambda: LoomClient(base, token))
    status, headers, body = handle_http_post(raw, dispatcher)
    _send_raw(self, status, headers, body)
    return True


def handle_delete(self, path, parsed) -> bool:
    if path == "/mcp":
        _method_not_allowed(self)
        return True
    return False
