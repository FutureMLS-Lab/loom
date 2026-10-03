"""The Loom concierge - talk to Loom in plain language.

A server-side agent behind ``POST /api/agent/chat``: ask "what is running?",
"how is the KV-cache paper doing?", "tell the xorl agent to rerun the eval",
and it answers - and acts - through the same MCP tools external agents use
(:mod:`loom.agent_tools`). Any bot that can POST text (Slack, Telegram,
Feishu, a cron job, ``loom ask``) gets a conversational Loom with no agent
logic of its own.

It is a headless coding-agent CLI (Claude Code) run per turn, so it rides
the host's existing login - no separate API key - and is fenced to Loom:

- the Loom MCP server is its only toolset (``--strict-mcp-config``) and the
  built-in shell/file tools are disabled (``--tools ""``);
- it runs in a neutral directory outside the home tree with user settings
  skipped, so it inherits neither the host runbook (``CLAUDE.md``) nor the
  global agent-stop hook;
- each chat session maps to one CLI session (``--session-id`` on the first
  turn, ``--resume`` after), so a conversation keeps its context.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import threading
import uuid
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

HOME_ENV = "LOOM_AGENT_HOME"
WORKDIR_ENV = "LOOM_CONCIERGE_WORKDIR"
MODEL_ENV = "LOOM_CONCIERGE_MODEL"
CLI_ENV = "LOOM_CONCIERGE_CLI"
DEFAULT_MODEL = "sonnet"
DEFAULT_TIMEOUT = 900
MAX_MESSAGE_CHARS = 20_000
SESSION_RE = re.compile(r"^[0-9a-f]{32}$")

SYSTEM_PROMPT = """\
You are the Loom concierge: the conversational front door to a Loom host. Loom
runs fleets of coding agents (Claude Code, Codex, Cursor) in tmux panes, one task
per git worktree, plus a Research Factory whose Paper / Review / Rebuttal lines
draft, review, and rebut ML papers.

You act only through the `loom` tools - you have no shell and no file access.
Ground every statement in what a tool returned in this conversation. If a tool
fails, say so plainly; never guess at state you did not read.

How to work:
- For broad questions start with loom_status, then drill in (list_tasks,
  get_task, read_conversation, read_screen, paper_factory, get_paper, read_review).
- A task is named by its slug; a unique fragment of a slug or title also works.
- Read before you act. Tools that change state (send_to_agent, start_agent,
  stop_agent, paper_loop, create_task, review_paper) are for when the owner asks:
  do what was asked, nothing more, then report what happened.
- paper_gate records the owner's own judgement. Call it only when the owner
  explicitly says approve or reject in this conversation; otherwise lay out the
  evidence (ratings, reviewer asks, plateau) and ask.
- Never put secrets, tokens, or credentials into a message or a tool call.

Style: lead with the answer, keep it short and concrete, use short lists for
several items, task slugs in backticks, numbers exactly as the tools gave them.
Reply in the language the owner writes in."""


class ConciergeError(RuntimeError):
    """The concierge could not produce a reply."""


def agent_home() -> Path:
    return Path(os.environ.get(HOME_ENV) or Path.home() / ".loom" / "agent")


def sessions_dir() -> Path:
    return agent_home() / "sessions"


def workdir() -> Path:
    # Deliberately outside $HOME: Claude Code walks up from its cwd collecting
    # CLAUDE.md files, and the host's runbook lives at ~/CLAUDE.md.
    return Path(os.environ.get(WORKDIR_ENV) or "/tmp/loom-concierge")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _write_private(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(text)
    os.replace(tmp, path)


def write_mcp_config(base_url: str, token: str) -> Path:
    """The MCP config pointing the CLI at this server's ``/mcp`` (mode 0600).

    A file rather than an inline argument, so the token never shows in ``ps``.
    """
    server: dict[str, Any] = {"type": "http", "url": base_url.rstrip("/") + "/mcp"}
    if token:
        server["headers"] = {"Authorization": f"Bearer {token}"}
    path = agent_home() / "mcp-config.json"
    _write_private(path, json.dumps({"mcpServers": {"loom": server}}, indent=2))
    return path


def build_command(
    *,
    cli_session: str,
    resume: bool,
    config_path: Path,
    model: str,
    cli: str,
) -> list[str]:
    command = [
        cli,
        "-p",
        "--output-format",
        "json",
        "--mcp-config",
        str(config_path),
        "--strict-mcp-config",
        "--tools",
        "",
        "--allowedTools",
        "mcp__loom",
        "--permission-mode",
        "dontAsk",
        "--setting-sources",
        "project",
        "--append-system-prompt",
        SYSTEM_PROMPT,
        "--model",
        model,
    ]
    command += ["--resume", cli_session] if resume else ["--session-id", cli_session]
    return command


def _session_path(session_id: str) -> Path:
    return sessions_dir() / f"{session_id}.json"


def read_session(session_id: str) -> dict[str, Any] | None:
    if not SESSION_RE.match(str(session_id or "")):
        return None
    path = _session_path(session_id)
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def list_sessions() -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for path in sessions_dir().glob("*.json"):
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        out.append(
            {
                "id": record.get("id"),
                "title": record.get("title") or "",
                "created_at": record.get("created_at"),
                "updated_at": record.get("updated_at"),
                "turns": len(record.get("turns") or []) // 2,
            }
        )
    out.sort(key=lambda r: str(r.get("updated_at") or ""), reverse=True)
    return out


def delete_session(session_id: str) -> bool:
    if not SESSION_RE.match(str(session_id or "")):
        return False
    try:
        _session_path(session_id).unlink()
    except FileNotFoundError:
        return False
    return True


_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()


def _lock_for(session_id: str) -> threading.Lock:
    with _locks_guard:
        return _locks.setdefault(session_id, threading.Lock())


def _child_env() -> dict[str, str]:
    env = dict(os.environ)
    # The CLI reaches Loom through its MCP config; keep the web token out of
    # the model's environment, and do not look like a nested agent session.
    for key in ("LOOM_WEB_AUTH_TOKEN", "CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT"):
        env.pop(key, None)
    return env


def chat(
    message: str,
    session: str | None = None,
    *,
    base_url: str,
    token: str,
    model: str | None = None,
    timeout: int = DEFAULT_TIMEOUT,
    runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
) -> dict[str, Any]:
    """One conversational turn. Returns ``{ok, session, reply, ...}``.

    Turns within one session are serialized; different sessions run in
    parallel. Raises ``ValueError`` for bad input, ``ConciergeError`` when
    the agent fails.
    """
    text = str(message or "").strip()
    if not text:
        raise ValueError("message is required")
    if len(text) > MAX_MESSAGE_CHARS:
        raise ValueError(f"message is longer than {MAX_MESSAGE_CHARS} characters")
    session_id = str(session or "").strip() or uuid.uuid4().hex
    if not SESSION_RE.match(session_id):
        raise ValueError("session must be a 32-character hex id (or omitted to start one)")
    cli = os.environ.get(CLI_ENV) or "claude"
    chosen_model = str(model or os.environ.get(MODEL_ENV) or DEFAULT_MODEL)

    with _lock_for(session_id):
        record = read_session(session_id) or {
            "id": session_id,
            "cli_session": str(uuid.uuid4()),
            "created_at": _now(),
            "title": text[:80],
            "turns": [],
        }
        resume = bool(record.get("turns"))
        command = build_command(
            cli_session=str(record["cli_session"]),
            resume=resume,
            config_path=write_mcp_config(base_url, token),
            model=chosen_model,
            cli=cli,
        )
        cwd = workdir()
        cwd.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            proc = runner(
                command,
                input=text,
                capture_output=True,
                text=True,
                timeout=timeout,
                cwd=str(cwd),
                env=_child_env(),
            )
        except FileNotFoundError as exc:
            raise ConciergeError(f"`{cli}` is not on PATH - install Claude Code or set {CLI_ENV}") from exc
        except subprocess.TimeoutExpired as exc:
            raise ConciergeError(f"the concierge did not answer within {timeout}s") from exc
        payload: dict[str, Any] = {}
        try:
            parsed = json.loads(proc.stdout or "")
            if isinstance(parsed, dict):
                payload = parsed
        except ValueError:
            pass
        if proc.returncode != 0 or not payload or payload.get("is_error"):
            detail = str(payload.get("result") or "") or (proc.stderr or proc.stdout or "")[-500:]
            raise ConciergeError(f"concierge failed: {detail.strip() or f'exit {proc.returncode}'}")
        reply = str(payload.get("result") or "").strip()
        record["cli_session"] = str(payload.get("session_id") or record["cli_session"])
        record["turns"] = list(record.get("turns") or []) + [
            {"role": "user", "text": text, "at": _now()},
            {
                "role": "assistant",
                "text": reply,
                "at": _now(),
                "cost_usd": payload.get("total_cost_usd"),
                "agent_turns": payload.get("num_turns"),
            },
        ]
        record["updated_at"] = _now()
        record["model"] = chosen_model
        _write_private(_session_path(session_id), json.dumps(record, ensure_ascii=False, indent=2))
    return {
        "ok": True,
        "session": session_id,
        "reply": reply,
        "cost_usd": payload.get("total_cost_usd"),
        "agent_turns": payload.get("num_turns"),
        "model": chosen_model,
    }
