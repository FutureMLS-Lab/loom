"""Model Context Protocol server for Loom - the agent-native door.

Speaks MCP (JSON-RPC 2.0) over two transports, both stdlib-only:

- **Streamable HTTP** - ``POST /mcp`` on the running ``loom web`` server,
  behind the same Bearer token as the REST API. Remote agents and bots
  (OpenClaw on the control host, through the SSH tunnel) connect here.
- **stdio** - ``loom mcp`` for local agents that launch their tools as a
  subprocess (Claude Desktop, Cursor, Codex). It talks to the running
  server's REST API, so both transports serve exactly the same tools.

The server is stateless: no ``Mcp-Session-Id``, no server-initiated stream
(``GET /mcp`` answers 405 as the spec allows). Every ``tools/call`` builds a
fresh client, so a restart of ``loom web`` never strands a connected agent.
The tool catalog itself lives in :mod:`loom.agent_tools`.
"""

from __future__ import annotations

import json
import sys
from collections.abc import Callable
from typing import IO, Any

from loom import __version__
from loom.agent_tools import TOOLS, TOOLS_BY_NAME, LoomClient, call_tool

SERVER_NAME = "loom"
SUPPORTED_VERSIONS = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")
LATEST_VERSION = SUPPORTED_VERSIONS[0]
MAX_BODY_BYTES = 4 * 1024 * 1024

PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603

INSTRUCTIONS = """\
Loom is a console that runs fleets of coding agents (Claude Code, Codex,
Cursor) in tmux panes, one task per git worktree, plus a Research Factory
whose Paper / Review / Rebuttal lines draft, review, and rebut ML papers.

Start with loom_status, then drill in: list_tasks / get_task for tasks,
read_conversation / read_screen for what an agent is doing, paper_factory /
get_paper / read_review for papers. A task is named by its slug, or any
unique fragment of its slug or title.

Read before acting. Tools that change state (send_to_agent, start_agent,
stop_agent, paper_loop, create_task, review_paper) act on the owner's behalf
- use them when asked. paper_gate records the owner's own decision at a
human gate: never decide one on your own initiative."""


class _RPCError(Exception):
    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _error(msg_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": code, "message": message}}


def tool_listing() -> list[dict[str, Any]]:
    return [
        {
            "name": tool.name,
            "title": tool.title,
            "description": tool.description,
            "inputSchema": tool.schema(),
            "annotations": tool.annotations(),
        }
        for tool in TOOLS
    ]


class MCPDispatcher:
    """Transport-independent JSON-RPC handling for the Loom MCP server."""

    def __init__(self, client_factory: Callable[[], LoomClient]) -> None:
        self._client_factory = client_factory

    def handle_payload(self, payload: Any) -> Any:
        """A parsed body: one message or a (2025-03-26-era) batch.

        Returns the response object, a list of them, or ``None`` when the
        input held only notifications / client responses.
        """
        if isinstance(payload, list):
            if not payload:
                return _error(None, INVALID_REQUEST, "empty batch")
            responses = [r for r in (self.handle(m) for m in payload) if r is not None]
            return responses or None
        return self.handle(payload)

    def handle(self, message: Any) -> dict[str, Any] | None:
        if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
            return _error(None, INVALID_REQUEST, "expected a JSON-RPC 2.0 object")
        method = message.get("method")
        if not isinstance(method, str):
            # A response to a server request - this server never sends any.
            return None
        if "id" not in message:
            return None  # notifications/initialized, notifications/cancelled, ...
        msg_id = message.get("id")
        params = message.get("params")
        if params is not None and not isinstance(params, dict):
            return _error(msg_id, INVALID_PARAMS, "params must be an object")
        try:
            result = self._dispatch(method, params or {})
        except _RPCError as exc:
            return _error(msg_id, exc.code, exc.message)
        except Exception as exc:  # noqa: BLE001 - never kill the transport
            return _error(msg_id, INTERNAL_ERROR, f"{type(exc).__name__}: {exc}")
        return {"jsonrpc": "2.0", "id": msg_id, "result": result}

    def _dispatch(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        if method == "initialize":
            requested = params.get("protocolVersion")
            return {
                "protocolVersion": requested if requested in SUPPORTED_VERSIONS else LATEST_VERSION,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": SERVER_NAME, "title": "Loom", "version": __version__},
                "instructions": INSTRUCTIONS,
            }
        if method == "ping":
            return {}
        if method == "tools/list":
            return {"tools": tool_listing()}
        if method == "tools/call":
            name = params.get("name")
            arguments = params.get("arguments")
            if not isinstance(name, str) or name not in TOOLS_BY_NAME:
                raise _RPCError(INVALID_PARAMS, f"unknown tool: {name}")
            if arguments is not None and not isinstance(arguments, dict):
                raise _RPCError(INVALID_PARAMS, "arguments must be an object")
            # Argument faults come back as tool errors (isError) rather than
            # protocol errors, so the calling model can read them and retry.
            text, is_error = call_tool(self._client_factory(), name, arguments or {})
            return {"content": [{"type": "text", "text": text}], "isError": is_error}
        if method == "resources/list":
            return {"resources": []}
        if method == "resources/templates/list":
            return {"resourceTemplates": []}
        if method == "prompts/list":
            return {"prompts": []}
        raise _RPCError(METHOD_NOT_FOUND, f"method not found: {method}")


def handle_http_post(
    raw_body: bytes, dispatcher: MCPDispatcher
) -> tuple[int, dict[str, str], bytes]:
    """Streamable HTTP ``POST /mcp``: ``(status, headers, body)``.

    Requests get a single ``application/json`` response (the spec lets a
    server answer with JSON instead of an SSE stream); notification-only
    bodies get ``202 Accepted`` with no body.
    """
    if len(raw_body) > MAX_BODY_BYTES:
        body = json.dumps(_error(None, INVALID_REQUEST, "request too large")).encode()
        return 413, {"Content-Type": "application/json"}, body
    try:
        payload = json.loads(raw_body.decode("utf-8") or "null")
    except (UnicodeDecodeError, ValueError):
        body = json.dumps(_error(None, PARSE_ERROR, "invalid JSON")).encode()
        return 400, {"Content-Type": "application/json"}, body
    result = dispatcher.handle_payload(payload)
    if result is None:
        return 202, {}, b""
    body = json.dumps(result, ensure_ascii=False).encode("utf-8")
    return 200, {"Content-Type": "application/json"}, body


def serve_stdio(
    base_url: str | None = None,
    token: str | None = None,
    *,
    stdin: IO[str] | None = None,
    stdout: IO[str] | None = None,
) -> None:
    """Newline-delimited JSON-RPC on stdin/stdout until EOF.

    Only protocol messages go to stdout; diagnostics go to stderr.
    """
    reader = stdin or sys.stdin
    writer = stdout or sys.stdout
    probe = LoomClient(base_url, token)
    print(f"loom mcp: serving {len(TOOLS)} tools from {probe.base_url}", file=sys.stderr, flush=True)
    dispatcher = MCPDispatcher(lambda: LoomClient(base_url, token))
    for line in reader:
        line = line.strip()
        if not line:
            continue
        try:
            payload = json.loads(line)
        except ValueError:
            result: Any = _error(None, PARSE_ERROR, "invalid JSON")
        else:
            result = dispatcher.handle_payload(payload)
        if result is not None:
            writer.write(json.dumps(result, ensure_ascii=False) + "\n")
            writer.flush()
