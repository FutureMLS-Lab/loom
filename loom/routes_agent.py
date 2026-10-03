"""Agent gateway routes: MCP, the concierge chat, and discovery.

- ``POST /mcp``                    Streamable HTTP MCP (``GET``/``DELETE`` -> 405)
- ``POST /api/agent/chat``         one concierge turn ``{message, session?}``
- ``GET  /api/agent/sessions``     concierge conversations, newest first
- ``GET|DELETE /api/agent/sessions/<id>``
- ``GET  /api/agent/manifest``     how an agent or bot connects to this server

Everything sits behind the server's normal auth. Browser-originated POSTs
must also be same-origin, so a page on another site cannot drive the MCP
tools or the concierge with a browser's cached Basic credentials.
"""

from __future__ import annotations

import hmac
import json
import os
import re
import secrets
import stat
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from loom import __version__
from loom import concierge
from loom.agent_hooks import loom_home
from loom.agent_tools import TOOLS, LoomClient
from loom.mcp_server import MCPDispatcher, handle_http_post
from loom.web_util import _json_bytes

_SESSION_PATH = re.compile(r"^/api/agent/sessions/([0-9a-f]{32})$")
_MAX_CHAT_BODY = 256 * 1024
AGENT_TOKEN_ENV = "LOOM_AGENT_TOKEN"
_agent_token_cache = ""


def agent_token_path() -> Path:
    return loom_home() / "agent" / "agent-token"


def agent_token() -> str:
    """A credential for agents and bots alone, created once and kept 0600.

    Separate from the web auth token, like the hook token: a bot needs Loom's
    curated tools and the concierge, not the whole REST API, so it gets a
    secret that opens ``/mcp`` and ``/api/agent/*`` and nothing else. Even if
    a bot's config is readable by its own agent, this token cannot reach
    deletes, worktree pushes, or raw keystrokes. ``LOOM_AGENT_TOKEN``
    overrides the generated one.
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


def agent_scope(path: str) -> bool:
    """The only routes the agent token opens."""
    return path == "/mcp" or path.startswith("/api/agent/")


def agent_token_allows(token: str, path: str) -> bool:
    expected = agent_token()
    return bool(token and expected and agent_scope(path) and hmac.compare_digest(token, expected))


def _loopback_url(self) -> str:
    """This server as seen from this host - where the concierge's CLI connects."""
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


def _read_raw(self, limit: int) -> bytes | None:
    try:
        size = int(self.headers.get("Content-Length", "0") or 0)
    except ValueError:
        return None
    if size < 0 or size > limit:
        return None
    return self.rfile.read(size) if size else b""


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
            "bots": "the agent token - opens /mcp and /api/agent/* only (`loom agent-config --show-token`)",
            "owner": "the server's --auth-token opens everything",
        },
        "mcp": {
            "url": f"{base}/mcp",
            "transport": "streamable-http",
            "stdio": "loom mcp  (env: LOOM_URL, LOOM_WEB_AUTH_TOKEN)",
        },
        "chat": {
            "url": f"{base}/api/agent/chat",
            "method": "POST",
            "body": {"message": "what is running?", "session": "<omit to start; reuse the returned id>"},
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
    if path == "/api/agent/sessions":
        st, b, h = _json_bytes({"ok": True, "sessions": concierge.list_sessions()})
        self._send(st, b, h)
        return True
    match = _SESSION_PATH.match(path)
    if match:
        record = concierge.read_session(match.group(1))
        if record is None:
            st, b, h = _json_bytes({"ok": False, "error": "no such session"}, 404)
        else:
            record = {k: v for k, v in record.items() if k != "cli_session"}
            st, b, h = _json_bytes({"ok": True, "session": record})
        self._send(st, b, h)
        return True
    return False


def handle_raw_post(self, path, parsed) -> bool:
    """POSTs that read their own body (MCP must see batches and parse errors)."""
    if path not in ("/mcp", "/api/agent/chat"):
        return False
    if not _same_origin(self):
        st, b, h = _json_bytes({"ok": False, "error": "cross-origin request refused"}, 403)
        self._send(st, b, h)
        return True
    if path == "/mcp":
        raw = _read_raw(self, 4 * 1024 * 1024)
        if raw is None:
            st, b, h = _json_bytes({"ok": False, "error": "bad or oversized body"}, 413)
            self._send(st, b, h)
            return True
        base, token = _loopback_url(self), self.auth_token
        dispatcher = MCPDispatcher(lambda: LoomClient(base, token))
        status, headers, body = handle_http_post(raw, dispatcher)
        _send_raw(self, status, headers, body)
        return True

    raw = _read_raw(self, _MAX_CHAT_BODY)
    try:
        body = json.loads((raw or b"{}").decode("utf-8") or "{}")
    except (UnicodeDecodeError, ValueError):
        body = None
    if not isinstance(body, dict):
        st, b, h = _json_bytes({"ok": False, "error": "send JSON: {message, session?}"}, 400)
        self._send(st, b, h)
        return True
    try:
        result = concierge.chat(
            str(body.get("message") or ""),
            str(body.get("session") or "") or None,
            base_url=_loopback_url(self),
            token=self.auth_token,
            model=str(body.get("model") or "") or None,
        )
    except ValueError as exc:
        st, b, h = _json_bytes({"ok": False, "error": str(exc)}, 400)
    except concierge.ConciergeError as exc:
        st, b, h = _json_bytes({"ok": False, "error": str(exc)}, 502)
    else:
        st, b, h = _json_bytes(result)
    self._send(st, b, h)
    return True


def handle_delete(self, path, parsed) -> bool:
    if path == "/mcp":
        _method_not_allowed(self)
        return True
    match = _SESSION_PATH.match(path)
    if match:
        deleted = concierge.delete_session(match.group(1))
        st, b, h = _json_bytes({"ok": deleted}, 200 if deleted else 404)
        self._send(st, b, h)
        return True
    return False
