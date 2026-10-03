"""Loom as a set of typed tools for agents and bots.

One catalog behind Loom's MCP server (``loom mcp`` over stdio, ``POST /mcp``
over Streamable HTTP). Each tool is a thin wrapper over the
documented REST API (docs/API.md), so the running server stays the single
source of truth - and each one *summarizes*: a raw task payload runs to half
a megabyte and a paper's AR state to ~80 KB, so tools return what an agent
can act on, not what the web UI renders.

Tools carry MCP safety annotations. Clients such as Codex app-server (and so
OpenClaw) auto-approve ``readOnlyHint`` tools and gate the rest - the right
default: reading Loom is free, changing it asks first. Destructive endpoints
(deletes, worktree merge/push, raw keystrokes, OpenReview submission) are
deliberately absent; those stay human-only in the UI.
"""

from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from loom.tmux_util import _KEY_RE, _KEYS, _TARGET_RE

DEFAULT_URL = "http://127.0.0.1:8765"
URL_ENV = "LOOM_URL"
TOKEN_ENV = "LOOM_WEB_AUTH_TOKEN"


class ToolError(Exception):
    """A tool failed in a way the calling agent should read and adapt to."""


class LoomClient:
    """Minimal REST client for a running ``loom web`` server."""

    def __init__(
        self,
        base_url: str | None = None,
        token: str | None = None,
        *,
        timeout: float = 60.0,
    ) -> None:
        self.base_url = (base_url or os.environ.get(URL_ENV) or DEFAULT_URL).rstrip("/")
        self.token = os.environ.get(TOKEN_ENV, "") if token is None else token
        self.timeout = timeout
        # Loopback traffic must never detour through an HTTP proxy from env.
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        self._projects: dict[str, Any] | None = None
        self._tasks: dict[str, list[dict[str, Any]]] = {}

    def request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        body: Any = None,
    ) -> Any:
        query = {k: v for k, v in (params or {}).items() if v not in (None, "")}
        url = self.base_url + path + ("?" + urllib.parse.urlencode(query) if query else "")
        data = None if body is None else json.dumps(body).encode("utf-8")
        req = urllib.request.Request(url, data=data, method=method)
        if self.token:
            req.add_header("Authorization", f"Bearer {self.token}")
        if data is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with self._opener.open(req, timeout=self.timeout) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace").strip()
            try:
                detail = str(json.loads(detail).get("error") or detail)
            except (ValueError, AttributeError):
                pass
            if exc.code == 401:
                detail = (
                    f"Loom rejected the credentials - set {TOKEN_ENV} to the "
                    "server's --auth-token"
                )
            raise ToolError(
                f"Loom API {method} {path} -> HTTP {exc.code}: {detail[:400]}"
            ) from None
        except (urllib.error.URLError, OSError) as exc:
            reason = getattr(exc, "reason", exc)
            raise ToolError(
                f"cannot reach Loom at {self.base_url} ({reason}); is `loom web` running?"
            ) from None
        if not raw:
            return {}
        try:
            return json.loads(raw)
        except ValueError:
            return raw.decode("utf-8", "replace")

    def get(self, path: str, **params: Any) -> Any:
        return self.request("GET", path, params=params)

    def post(self, path: str, body: Any = None, **params: Any) -> Any:
        return self.request("POST", path, params=params, body={} if body is None else body)

    # -- discovery -----------------------------------------------------------

    def projects(self) -> dict[str, Any]:
        if self._projects is None:
            data = self.get("/api/projects")
            self._projects = data if isinstance(data, dict) else {}
        return self._projects

    def project_names(self) -> dict[str, str]:
        return {
            str(p.get("id")): str(p.get("name") or p.get("id"))
            for p in self.projects().get("projects") or []
        }

    def resolve_project(self, project: str | None) -> str:
        """Project id from an id, name, or path; the server default when blank."""
        data = self.projects()
        items = data.get("projects") or []
        wanted = str(project or "").strip()
        if not wanted:
            pid = str(data.get("defaultProjectId") or data.get("currentProjectId") or "")
            if not pid and items:
                pid = str(items[0].get("id") or "")
            if not pid:
                raise ToolError("Loom has no registered projects")
            return pid
        for item in items:
            path = str(item.get("path") or "").rstrip("/")
            if wanted in (str(item.get("id")), str(item.get("name"))) or wanted.rstrip("/") == path:
                return str(item.get("id"))
        hits = [i for i in items if wanted.lower() in str(i.get("name", "")).lower()]
        if len(hits) == 1:
            return str(hits[0].get("id"))
        known = ", ".join(sorted(str(i.get("name") or i.get("id")) for i in items))
        raise ToolError(f"unknown project {wanted!r}; known projects: {known}")

    def tasks(self, project_id: str) -> list[dict[str, Any]]:
        if project_id not in self._tasks:
            data = self.get("/api/tasks", project=project_id)
            self._tasks[project_id] = (
                list(data.get("tasks") or []) if isinstance(data, dict) else []
            )
        return self._tasks[project_id]

    def locate_task(self, task: Any, project: Any = None) -> tuple[str, dict[str, Any]]:
        """``(project_id, task_meta)`` for a slug, title, or unique fragment.

        Searches every project when none is given, so an agent can say "the
        selection-error paper" without knowing which project owns it.
        """
        wanted = str(task or "").strip()
        if not wanted:
            raise ToolError("task is required (a slug, or a unique piece of its slug or title)")
        if project:
            scope = [self.resolve_project(str(project))]
        else:
            scope = [str(p.get("id")) for p in self.projects().get("projects") or []]
        lowered = wanted.lower()
        partial: list[tuple[str, dict[str, Any]]] = []
        for pid in scope:
            for meta in self.tasks(pid):
                slug = str(meta.get("slug") or "")
                if slug == wanted:
                    return pid, meta
                if lowered in slug.lower() or lowered in str(meta.get("title") or "").lower():
                    partial.append((pid, meta))
        if len(partial) == 1:
            return partial[0]
        if partial:
            names = self.project_names()
            listing = "; ".join(
                f"{m.get('slug')} (project {names.get(pid, pid)})" for pid, m in partial[:8]
            )
            raise ToolError(
                f"{wanted!r} matches {len(partial)} tasks - pass the exact slug: {listing}"
            )
        raise ToolError(f"no task matches {wanted!r}" + ("" if project else " in any project"))


# --- helpers -----------------------------------------------------------------


def _q(slug: str) -> str:
    return urllib.parse.quote(str(slug), safe="")


def _clip(text: Any, limit: int) -> str:
    value = str(text or "")
    if len(value) <= limit:
        return value
    return value[:limit].rstrip() + f"\n… [truncated {len(value) - limit} chars]"


def _int(value: Any, default: int, low: int, high: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return max(low, min(high, number))


def _iso(epoch: Any) -> str:
    try:
        return datetime.fromtimestamp(float(epoch), timezone.utc).isoformat(timespec="minutes")
    except (TypeError, ValueError, OSError):
        return ""


def _activity(client: LoomClient) -> list[dict[str, Any]]:
    data = client.get("/api/activity")
    return list((data.get("tasks") or {}).values()) if isinstance(data, dict) else []


def _scores(entry: dict[str, Any]) -> dict[str, Any]:
    scores = entry.get("scores") or {}
    return {
        "model": entry.get("model"),
        "rating": scores.get("rating"),
        "soundness": scores.get("soundness"),
        "recommendation": scores.get("recommendation") or entry.get("recommendation"),
    }


# --- read-only tools -----------------------------------------------------------


def _loom_status(client: LoomClient, args: dict[str, Any]) -> dict[str, Any]:
    names = client.project_names()
    entries = _activity(client)
    approvals = client.get("/api/factories/approvals")
    return {
        "projects": len(names),
        "default_project": names.get(str(client.projects().get("defaultProjectId")), ""),
        "agents_working": [
            {"task": e.get("slug"), "project": names.get(str(e.get("project")), e.get("project"))}
            for e in entries
            if e.get("working")
        ],
        "agents_finished_unseen": [
            {
                "task": e.get("slug"),
                "project": names.get(str(e.get("project")), e.get("project")),
                "finished_at": _iso(e.get("finished_at")),
            }
            for e in entries
            if not e.get("working") and e.get("finished_at")
        ],
        "waiting_on_you": [
            {k: item.get(k) for k in ("factory", "gate", "id", "title", "detail")}
            for item in (approvals.get("items") or [] if isinstance(approvals, dict) else [])
        ],
    }


def _list_projects(client: LoomClient, args: dict[str, Any]) -> dict[str, Any]:
    data = client.projects()
    default = str(data.get("defaultProjectId") or "")
    return {
        "projects": [
            {
                "id": p.get("id"),
                "name": p.get("name"),
                "path": p.get("path"),
                "default": str(p.get("id")) == default,
            }
            for p in data.get("projects") or []
        ]
    }


def _list_tasks(client: LoomClient, args: dict[str, Any]) -> dict[str, Any]:
    names = client.project_names()
    status = {
        (str(e.get("project")), str(e.get("slug"))): ("working" if e.get("working") else "finished")
        for e in _activity(client)
    }
    scope = [client.resolve_project(args["project"])] if args.get("project") else list(names)
    rows: list[dict[str, Any]] = []
    for pid in scope:
        for meta in client.tasks(pid):
            slug = str(meta.get("slug") or "")
            rows.append(
                {
                    "project": names.get(pid, pid),
                    "task": slug,
                    "title": meta.get("title"),
                    "agent": meta.get("agent"),
                    "kind": meta.get("kind"),
                    "status": status.get((pid, slug), "idle"),
                    "updated_at": meta.get("updated_at"),
                }
            )
    rows.sort(key=lambda r: str(r.get("updated_at") or ""), reverse=True)
    return {"count": len(rows), "tasks": rows[:200]}


def _get_task(client: LoomClient, args: dict[str, Any]) -> dict[str, Any]:
    pid, meta = client.locate_task(args.get("task"), args.get("project"))
    slug = str(meta["slug"])
    detail = client.get(f"/api/tasks/{_q(slug)}", project=pid)
    templates = detail.get("templates") or {}
    claude = detail.get("claude") or {}
    return {
        "project": client.project_names().get(pid, pid),
        "task": slug,
        "title": meta.get("title"),
        "goal": _clip(meta.get("general_goal"), 1500),
        "agent": meta.get("agent"),
        "kind": meta.get("kind"),
        "created_at": meta.get("created_at"),
        "updated_at": meta.get("updated_at"),
        "agent_pane": meta.get("tmux_interview_target") or "",
        "agent_sessions": len(claude.get("tracked") or []),
        "worktrees": [
            {
                "path": w.get("path"),
                "branch": w.get("branch"),
                "clean": w.get("clean"),
                "dirty_files": w.get("dirty_count"),
                "ahead": w.get("ahead"),
                "behind": w.get("behind"),
            }
            for w in detail.get("worktree_statuses") or []
        ],
        "plan_md": _clip(templates.get("PLAN.md"), 8000),
        "other_markdown_files": [
            name for name in detail.get("task_markdown_files") or [] if name != "PLAN.md"
        ][:40],
    }


def _read_conversation(client: LoomClient, args: dict[str, Any]) -> dict[str, Any]:
    pid, meta = client.locate_task(args.get("task"), args.get("project"))
    slug = str(meta["slug"])
    limit = _int(args.get("limit"), 12, 1, 50)
    data = client.get(
        f"/api/tasks/{_q(slug)}/conversation", project=pid, limit=max(20, min(500, limit * 8))
    )
    messages: list[dict[str, Any]] = []
    tools_pending = 0
    for message in data.get("messages") or []:
        kind = message.get("kind")
        if kind == "tool":
            tools_pending += 1
            continue
        if kind not in ("assistant", "user"):
            continue
        entry: dict[str, Any] = {"role": kind, "text": _clip(message.get("text"), 2000)}
        if tools_pending:
            entry["tool_calls_before"] = tools_pending
            tools_pending = 0
        messages.append(entry)
    return {
        "task": slug,
        "agent": data.get("agent"),
        "online": data.get("online"),
        "working": data.get("working"),
        "messages": messages[-limit:],
        "tool_calls_since_last_message": tools_pending,
        "transcript_entries_total": data.get("total"),
    }


def _pane(client: LoomClient, args: dict[str, Any]) -> tuple[str, str]:
    """``(tmux_target, label)`` from either a raw pane target or a task."""
    target = str(args.get("target") or "").strip()
    if target:
        if not _TARGET_RE.match(target):
            raise ToolError("target must look like session:window.pane - list_sessions shows them")
        return target, target
    if not args.get("task"):
        raise ToolError("give either task (a Loom task) or target (any tmux pane, see list_sessions)")
    _pid, meta = client.locate_task(args.get("task"), args.get("project"))
    target = str(meta.get("tmux_interview_target") or "")
    if not target:
        raise ToolError("this task has no agent pane yet - call start_agent first")
    return target, str(meta["slug"])


def _capture(client: LoomClient, target: str, lines: int) -> str:
    data = client.get("/api/tmux/capture", target=target, lines=lines)
    if not isinstance(data, dict) or not data.get("ok"):
        error = data.get("error") if isinstance(data, dict) else data
        raise ToolError(f"pane capture failed (is {target} alive?): {error}")
    return str(data.get("text") or "").rstrip()


def _read_screen(client: LoomClient, args: dict[str, Any]) -> dict[str, Any]:
    target, label = _pane(client, args)
    screen = _capture(client, target, _int(args.get("lines"), 60, 5, 400))
    return {"pane": target, "of": label, "screen": _clip(screen, 16000)}


def _watch_pane(client: LoomClient, args: dict[str, Any]) -> dict[str, Any]:
    """Attach to a pane for a bounded window: the agent-readable live view.

    The raw PTY stream is xterm redraw bytes; an agent wants the rendered
    screen, so this polls it once a second until ``until`` matches or time
    runs out.
    """
    target, label = _pane(client, args)
    seconds = _int(args.get("seconds"), 15, 1, 110)
    lines = _int(args.get("lines"), 60, 5, 400)
    pattern = str(args.get("until") or "")
    try:
        until = re.compile(pattern, re.MULTILINE) if pattern else None
    except re.error as exc:
        raise ToolError(f"until is not a valid regex: {exc}") from None
    first = screen = _capture(client, target, lines)
    started = time.monotonic()
    matched = bool(until and until.search(screen))
    while not matched and time.monotonic() - started < seconds:
        time.sleep(1.0)
        screen = _capture(client, target, lines)
        matched = bool(until and until.search(screen))
    return {
        "pane": target,
        "of": label,
        "watched_seconds": round(time.monotonic() - started, 1),
        "until_matched": matched if until else None,
        "changed": screen != first,
        "screen": _clip(screen, 16000),
    }


def _list_sessions(client: LoomClient, args: dict[str, Any]) -> dict[str, Any]:
    data = client.get("/api/tmux/sessions")
    if not isinstance(data, dict) or not data.get("tmux"):
        raise ToolError("tmux is not available on the Loom host")
    # Which Loom task owns each session, from every task's pane target.
    names = client.project_names()
    owners: dict[str, str] = {}
    for pid in names:
        for meta in client.tasks(pid):
            target = str(meta.get("tmux_interview_target") or "")
            if target:
                owners[target.split(":", 1)[0]] = f"{names[pid]}/{meta.get('slug')}"
    wanted = str(args.get("session") or "").strip()
    sessions = [
        {
            "session": s.get("name"),
            "attached": str(s.get("attached")) not in ("0", "", "False", "false"),
            "task": owners.get(str(s.get("name")), ""),
        }
        for s in data.get("sessions") or []
        if not wanted or s.get("name") == wanted
    ]
    if wanted:
        if not sessions:
            raise ToolError(f"no tmux session named {wanted!r}")
        panes = client.get("/api/tmux/panes", session=wanted).get("panes") or []
        sessions[0]["panes"] = [{"target": p.get("id"), "title": p.get("title")} for p in panes]
    return {
        "count": len(sessions),
        "sessions": sessions,
        "hint": "pass session=<name> for its pane targets; read_screen / watch_pane / send_keys take target=<session:window.pane>",
    }


def _paper_row(child: dict[str, Any]) -> dict[str, Any]:
    return {
        "task": child.get("slug"),
        "title": child.get("title"),
        "stage": child.get("stage_label") or child.get("stage"),
        "round": f"{child.get('round')}/{child.get('max_rounds')}",
        "best_rating": child.get("best_rating"),
        "plateaued": child.get("plateaued"),
        "loop_running": child.get("loop_running"),
        "awaiting_you": child.get("awaiting_you"),
    }


def _paper_factory(client: LoomClient, args: dict[str, Any]) -> dict[str, Any]:
    names = client.project_names()
    scope = [client.resolve_project(args["project"])] if args.get("project") else list(names)
    studios: list[dict[str, Any]] = []
    orphans: list[dict[str, Any]] = []
    for pid in scope:
        try:
            overview = client.get("/api/ar/overview", project=pid)
        except ToolError:
            continue
        for studio in overview.get("studios") or []:
            studios.append(
                {
                    "project": names.get(pid, pid),
                    "studio": studio.get("slug"),
                    "title": studio.get("title"),
                    "venue": studio.get("venue"),
                    "direction": studio.get("direction"),
                    "ideas": studio.get("ideas"),
                    "papers": [_paper_row(c) for c in studio.get("children") or []],
                }
            )
        orphans.extend(
            {"project": names.get(pid, pid), **_paper_row(c)} for c in overview.get("orphans") or []
        )
    return {
        "studios": studios,
        "orphan_papers": orphans,
        "papers_total": sum(len(s["papers"]) for s in studios) + len(orphans),
    }


def _get_paper(client: LoomClient, args: dict[str, Any]) -> dict[str, Any]:
    pid, meta = client.locate_task(args.get("task"), args.get("project"))
    slug = str(meta["slug"])
    data = client.get(f"/api/tasks/{_q(slug)}/ar", project=pid)
    state = data.get("state") or {}
    role = state.get("role")
    if role == "studio":
        return {
            "task": slug,
            "kind": "studio",
            "title": data.get("title"),
            "venue": state.get("venue"),
            "direction": data.get("direction_label"),
            "ideas": [
                {
                    "title": _clip(idea.get("title"), 200),
                    "status": idea.get("status"),
                    "paper": idea.get("paper_slug") or idea.get("spawned_slug") or "",
                }
                for idea in state.get("ideas") or []
                if isinstance(idea, dict)
            ][:30],
        }
    if role != "paper":
        raise ToolError(f"{slug} is not a Paper Factory studio or paper")
    actions = data.get("actions") or {}
    latest = data.get("latest_review") or {}
    idea = state.get("idea") or {}
    return {
        "task": slug,
        "kind": "paper",
        "title": data.get("title"),
        "idea": _clip(idea.get("title") if isinstance(idea, dict) else idea, 300),
        "venue": state.get("venue"),
        "stage": data.get("stage_label") or state.get("stage"),
        "stage_id": state.get("stage"),
        "round": state.get("round"),
        "max_rounds": state.get("max_rounds"),
        "loop": data.get("loop"),
        "best_rating": data.get("best_rating"),
        "plateaued": data.get("plateaued"),
        "stop_reason": state.get("stop_reason") or "",
        "ratings_by_round": [
            {
                "round": r.get("n"),
                "lowest_rating": ((r.get("review") or {}).get("scores") or {}).get("rating"),
                "deciding_model": (r.get("review") or {}).get("deciding_model"),
            }
            for r in state.get("rounds") or []
            if ((r.get("review") or {}).get("scores") or {}).get("rating") is not None
        ],
        "latest_review": {
            "headline": latest.get("headline"),
            "reviewers": [_scores(x) for x in latest.get("reviewers") or []],
        },
        "gates": state.get("gates") or [],
        "actions_available": sorted(k for k, v in actions.items() if (v or {}).get("ok")),
        "actions_blocked": {k: (v or {}).get("why") for k, v in actions.items() if not (v or {}).get("ok")},
        "cost_usd": state.get("cost_usd"),
        "pdf_available": data.get("pdf_available"),
    }


def _read_review(client: LoomClient, args: dict[str, Any]) -> dict[str, Any]:
    pid, meta = client.locate_task(args.get("task"), args.get("project"))
    slug = str(meta["slug"])
    round_n = args.get("round")
    if round_n in (None, ""):
        state = (client.get(f"/api/tasks/{_q(slug)}/ar", project=pid).get("state") or {})
        reviewed = [
            r.get("n") for r in state.get("rounds") or [] if (r.get("review") or {}).get("scores")
        ]
        if not reviewed:
            raise ToolError("this paper has no reviewed round yet")
        round_n = reviewed[-1]
    review = client.get(f"/api/tasks/{_q(slug)}/ar/review/{_int(round_n, 0, 0, 999)}", project=pid)
    text = str(review.get("review") or "")
    wanted = str(args.get("reviewer") or "").strip().lower()
    if wanted:
        # The panel file stacks one "# Reviewer: `model`" section per model.
        sections = [s for s in text.split("# Reviewer:") if wanted in s[:120].lower()]
        if not sections:
            models = [_scores(x)["model"] for x in review.get("reviewers") or []]
            raise ToolError(f"no reviewer matches {wanted!r}; this round had: {', '.join(map(str, models))}")
        text = "# Reviewer:" + sections[0]
    return {
        "task": slug,
        "round": review.get("round"),
        "headline": review.get("headline"),
        "deciding_model": review.get("deciding_model"),
        "reviewers": [_scores(x) for x in review.get("reviewers") or []],
        "review_markdown": _clip(text, 14000),
    }


def _get_review_report(client: LoomClient, args: dict[str, Any]) -> dict[str, Any]:
    review_id = str(args.get("review_id") or "").strip()
    if not review_id:
        listing = client.get("/api/review/projects")
        return {
            "review_projects": [
                {k: p.get(k) for k in ("id", "title", "venue", "status", "rating", "headline", "reviewed_at")}
                for p in (listing.get("projects") or [])
            ]
        }
    data = client.get(f"/api/review/projects/{_q(review_id)}")
    state = data.get("state") or {}
    record = data.get("project") or {}
    runs = data.get("runs") or []
    report = ""
    if runs:
        latest = runs[0] if isinstance(runs[0], dict) else {}
        run_id = str(latest.get("run") or latest.get("id") or "")
        if run_id:
            text = client.get(f"/api/review/projects/{_q(review_id)}/runs/{_q(run_id)}/review.md")
            report = text if isinstance(text, str) else json.dumps(text)
    return {
        "review_id": review_id,
        "title": record.get("title"),
        "venue": record.get("venue") or state.get("venue"),
        "status": state.get("status"),
        "error": state.get("error") or "",
        "rating": state.get("rating") or record.get("rating"),
        "headline": state.get("headline") or record.get("headline"),
        "runs": len(runs),
        "report_markdown": _clip(report, 14000),
    }


# --- tools that change things ------------------------------------------------------


def _send_to_agent(client: LoomClient, args: dict[str, Any]) -> dict[str, Any]:
    text = str(args.get("text") or "").strip()
    if not text:
        raise ToolError("text is required")
    submit = args.get("submit", True) is not False
    if args.get("target"):
        target, label = _pane(client, args)
        client.post("/api/tmux/send-text", {"target": target, "text": text, "submit": submit})
    else:
        pid, meta = client.locate_task(args.get("task"), args.get("project"))
        label = str(meta["slug"])
        client.post(f"/api/tasks/{_q(label)}/claude/send", {"text": text, "submit": submit}, project=pid)
    return {
        "ok": True,
        "to": label,
        "sent": _clip(text, 300),
        "submitted": submit,
        "next": "give it a moment, then read_conversation, read_screen, or watch_pane for the reply",
    }


def _send_keys(client: LoomClient, args: dict[str, Any]) -> dict[str, Any]:
    keys = args.get("keys")
    if isinstance(keys, str):
        keys = [keys]
    if not isinstance(keys, list) or not keys or len(keys) > 20:
        raise ToolError("keys must be a list of 1-20 key names, e.g. [\"Escape\"] or [\"C-c\"]")
    bad = [k for k in keys if not isinstance(k, str) or (k not in _KEYS and not _KEY_RE.match(k))]
    if bad:
        raise ToolError(
            f"unsupported key(s) {bad}; use {', '.join(sorted(_KEYS))}, "
            "C-<letter>, M-<letter>, or F1-F12 (type text with send_to_agent)"
        )
    target, label = _pane(client, args)
    for key in keys:
        client.post("/api/tmux/send-key", {"target": target, "key": key})
    return {"ok": True, "pane": target, "of": label, "sent_keys": keys}


def _create_task(client: LoomClient, args: dict[str, Any]) -> dict[str, Any]:
    pid = client.resolve_project(args.get("project"))
    title = str(args.get("title") or "").strip()
    if not title:
        raise ToolError("title is required")
    body: dict[str, Any] = {"title": title, "general_goal": str(args.get("goal") or "").strip()}
    if args.get("agent"):
        body["agent"] = str(args["agent"]).strip().lower()
    created = client.post("/api/tasks", body, project=pid)
    meta = created.get("meta") or {}
    return {
        "ok": True,
        "project": client.project_names().get(pid, pid),
        "task": meta.get("slug"),
        "agent": meta.get("agent"),
        "next": "start_agent launches its agent pane; send_to_agent briefs it",
    }


def _agent_pane(action: str) -> Callable[[LoomClient, dict[str, Any]], dict[str, Any]]:
    def run(client: LoomClient, args: dict[str, Any]) -> dict[str, Any]:
        pid, meta = client.locate_task(args.get("task"), args.get("project"))
        result = client.post(f"/api/tasks/{_q(meta['slug'])}/claude/{action}", project=pid)
        return {"ok": True, "task": meta["slug"], "action": action, "result": result}

    return run


def _paper_loop(client: LoomClient, args: dict[str, Any]) -> dict[str, Any]:
    action = str(args.get("action") or "").strip().lower()
    if action not in ("start", "stop"):
        raise ToolError("action must be start or stop")
    pid, meta = client.locate_task(args.get("task"), args.get("project"))
    data = client.post(f"/api/tasks/{_q(meta['slug'])}/ar/loop/{action}", project=pid)
    state = data.get("state") or {}
    return {
        "ok": data.get("ok", True),
        "task": meta["slug"],
        "loop": data.get("loop"),
        "stage": data.get("stage_label") or state.get("stage"),
        "round": state.get("round"),
    }


def _paper_gate(client: LoomClient, args: dict[str, Any]) -> dict[str, Any]:
    gate = str(args.get("gate") or "").strip().lower()
    decision = str(args.get("decision") or "").strip().lower()
    if gate not in ("draft", "final") or decision not in ("approve", "reject"):
        raise ToolError("gate must be draft|final and decision approve|reject")
    pid, meta = client.locate_task(args.get("task"), args.get("project"))
    data = client.post(
        f"/api/tasks/{_q(meta['slug'])}/ar/gate",
        {"gate": gate, "decision": decision, "note": str(args.get("note") or "")},
        project=pid,
    )
    state = data.get("state") or {}
    return {
        "ok": data.get("ok", True),
        "task": meta["slug"],
        "recorded": f"{gate} gate: {decision}",
        "stage": data.get("stage_label") or state.get("stage"),
    }


def _review_paper(client: LoomClient, args: dict[str, Any]) -> dict[str, Any]:
    url = str(args.get("url") or "").strip()
    if not url.startswith(("http://", "https://")):
        raise ToolError("url must be an http(s) link to an arXiv / OpenReview page or a PDF")
    created = client.post(
        "/api/review/projects",
        {"url": url, "venue": str(args.get("venue") or ""), "title": str(args.get("title") or "")},
    )
    review_id = str(created.get("id") or (created.get("project") or {}).get("id") or "")
    if not review_id:
        raise ToolError(f"import did not return a review id: {_clip(json.dumps(created), 300)}")
    client.post(f"/api/review/projects/{_q(review_id)}/run")
    return {
        "ok": True,
        "review_id": review_id,
        "status": "running",
        "next": "the panel takes ~10-15 minutes; poll get_review_report with this review_id",
    }


# --- catalog -----------------------------------------------------------------------


@dataclass(frozen=True)
class Tool:
    name: str
    title: str
    description: str
    properties: dict[str, Any]
    handler: Callable[[LoomClient, dict[str, Any]], Any]
    required: tuple[str, ...] = ()
    read_only: bool = True
    destructive: bool = False
    idempotent: bool = True
    open_world: bool = False
    extra: dict[str, Any] = field(default_factory=dict)

    def schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": self.properties,
            "required": list(self.required),
            "additionalProperties": False,
        }

    def annotations(self) -> dict[str, Any]:
        return {
            "title": self.title,
            "readOnlyHint": self.read_only,
            "destructiveHint": self.destructive,
            "idempotentHint": self.idempotent,
            "openWorldHint": self.open_world,
        }


_TASK = {
    "type": "string",
    "description": "Task slug, or any unique fragment of its slug or title (e.g. 'selection-error').",
}
_PROJECT = {
    "type": "string",
    "description": "Project id, name, or path. Optional - omit to search every project.",
}
_TARGET = {
    "type": "string",
    "description": "Any tmux pane as session:window.pane (from list_sessions); use instead of task.",
}

TOOLS: tuple[Tool, ...] = (
    Tool(
        "loom_status",
        "Loom status",
        "Start here. One-shot overview of the whole Loom host: which agents are working "
        "right now, which finished and are waiting to be looked at, and every human gate "
        "waiting on the owner across the Paper / Review / Rebuttal factories.",
        {},
        _loom_status,
    ),
    Tool(
        "list_projects",
        "List projects",
        "Registered Loom projects (id, name, path) and which one is the default.",
        {},
        _list_projects,
    ),
    Tool(
        "list_tasks",
        "List tasks",
        "Tasks across every project (or one project), newest first, with each task's agent "
        "CLI and live status (working / finished / idle).",
        {"project": _PROJECT},
        _list_tasks,
    ),
    Tool(
        "get_task",
        "Get task",
        "One task in detail: goal, agent, worktrees (branch, clean/dirty), and its PLAN.md - "
        "the task's own record of progress, decisions, and next steps.",
        {"task": _TASK, "project": _PROJECT},
        _get_task,
        required=("task",),
    ),
    Tool(
        "read_conversation",
        "Read agent conversation",
        "The latest messages exchanged with a task's coding agent (Claude Code / Codex / "
        "Cursor), with tool calls collapsed into counts. Use it to see what the agent "
        "last said or is waiting on.",
        {
            "task": _TASK,
            "project": _PROJECT,
            "limit": {"type": "integer", "minimum": 1, "maximum": 50, "description": "Messages to return (default 12)."},
        },
        _read_conversation,
        required=("task",),
    ),
    Tool(
        "list_sessions",
        "List tmux sessions",
        "Every tmux session on the Loom host, whether a client is attached, and which Loom "
        "task owns it. Pass session to get that session's pane targets.",
        {"session": {"type": "string", "description": "One session's name, to list its panes."}},
        _list_sessions,
    ),
    Tool(
        "read_screen",
        "Read pane screen",
        "A snapshot of a tmux pane - a task's agent (task) or any pane (target) - "
        "including prompts the program is blocked on.",
        {
            "task": _TASK,
            "project": _PROJECT,
            "target": _TARGET,
            "lines": {"type": "integer", "minimum": 5, "maximum": 400, "description": "Scrollback lines (default 60)."},
        },
        _read_screen,
    ),
    Tool(
        "watch_pane",
        "Watch a pane live",
        "Attach to a pane and watch it live for up to `seconds`. Returns as soon as the "
        "screen matches `until` (a regex, e.g. 'PASSED|FAILED' or a shell prompt), or when "
        "time runs out - with the final screen and whether it changed. Keep `seconds` "
        "under your client's tool timeout (often 60).",
        {
            "task": _TASK,
            "project": _PROJECT,
            "target": _TARGET,
            "seconds": {"type": "integer", "minimum": 1, "maximum": 110, "description": "Longest wait (default 15)."},
            "until": {"type": "string", "description": "Regex; stop as soon as the screen matches it."},
            "lines": {"type": "integer", "minimum": 5, "maximum": 400, "description": "Lines to watch (default 60)."},
        },
        _watch_pane,
    ),
    Tool(
        "paper_factory",
        "Paper Factory overview",
        "Every Paper Factory studio and its papers: stage, round, best panel rating, "
        "whether the loop is running, and which papers are waiting on the owner.",
        {"project": _PROJECT},
        _paper_factory,
    ),
    Tool(
        "get_paper",
        "Get paper",
        "One Paper Factory paper (or studio): stage, round, rating trajectory per round, the "
        "latest reviewer panel scores, gate history, and which actions are available now. "
        "For a studio, lists its ideas.",
        {"task": _TASK, "project": _PROJECT},
        _get_paper,
        required=("task",),
    ),
    Tool(
        "read_review",
        "Read panel review",
        "The reviewer panel's full report for one round of a paper (default: the latest "
        "reviewed round). Optionally one reviewer only (e.g. 'kimi', 'gpt', 'claude').",
        {
            "task": _TASK,
            "project": _PROJECT,
            "round": {"type": "integer", "minimum": 0, "description": "Round number (default latest)."},
            "reviewer": {"type": "string", "description": "Substring of one reviewer model's name."},
        },
        _read_review,
        required=("task",),
    ),
    Tool(
        "get_review_report",
        "Get Review Factory report",
        "Review Factory results. With review_id: that review's status, scores, and report. "
        "Without: every review project with its status and rating.",
        {"review_id": {"type": "string", "description": "12-hex review project id (optional)."}},
        _get_review_report,
    ),
    Tool(
        "send_to_agent",
        "Send text to an agent or pane",
        "Type a message into a task's live agent pane (task) or any tmux pane (target) and "
        "press Enter - the way to brief, redirect, or answer a running agent or shell.",
        {
            "task": _TASK,
            "project": _PROJECT,
            "target": _TARGET,
            "text": {"type": "string", "description": "The message or command line."},
            "submit": {"type": "boolean", "description": "Press Enter after typing (default true)."},
        },
        _send_to_agent,
        required=("text",),
        read_only=False,
        idempotent=False,
    ),
    Tool(
        "send_keys",
        "Send special keys",
        "Press named keys in a pane, in order: Escape interrupts Claude Code / Codex / "
        "Cursor; C-c stops a shell command; Up/Down/Enter/Tab drive menus and prompts; also "
        "PageUp/PageDown, Home/End, BTab, Backspace, Space, any C-<letter>, M-<letter>, F1-F12. "
        "Keys can approve prompts or kill processes - read_screen first.",
        {
            "task": _TASK,
            "project": _PROJECT,
            "target": _TARGET,
            "keys": {
                "type": "array",
                "items": {"type": "string"},
                "minItems": 1,
                "maxItems": 20,
                "description": "Key names, e.g. [\"Escape\"] or [\"Down\", \"Enter\"].",
            },
        },
        _send_keys,
        required=("keys",),
        read_only=False,
        destructive=True,
        idempotent=False,
    ),
    Tool(
        "create_task",
        "Create task",
        "Create a new Loom task (its own .RUD folder, PLAN.md, and git worktree when the "
        "project is a repo). Does not start the agent - call start_agent after.",
        {
            "title": {"type": "string", "description": "Short task title."},
            "goal": {"type": "string", "description": "What success looks like."},
            "project": _PROJECT,
            "agent": {"type": "string", "enum": ["cursor", "claude", "codex"], "description": "Agent CLI (default cursor)."},
        },
        _create_task,
        required=("title",),
        read_only=False,
        idempotent=False,
    ),
    Tool(
        "start_agent",
        "Start agent",
        "Launch the task's agent CLI in its tmux pane (resumable sessions are kept).",
        {"task": _TASK, "project": _PROJECT},
        _agent_pane("start"),
        required=("task",),
        read_only=False,
    ),
    Tool(
        "stop_agent",
        "Stop agent",
        "Kill the task's agent pane, interrupting whatever it is doing (its session stays "
        "resumable). Only when the owner asks.",
        {"task": _TASK, "project": _PROJECT},
        _agent_pane("stop"),
        required=("task",),
        read_only=False,
        destructive=True,
    ),
    Tool(
        "paper_loop",
        "Start / stop a paper loop",
        "Start or stop a Paper Factory paper's autonomous author/reviewer loop.",
        {
            "task": _TASK,
            "project": _PROJECT,
            "action": {"type": "string", "enum": ["start", "stop"]},
        },
        _paper_loop,
        required=("task", "action"),
        read_only=False,
    ),
    Tool(
        "paper_gate",
        "Decide a paper gate",
        "Record the owner's decision at a Paper Factory human gate (draft review or final "
        "review). This is the owner's judgement call: use it only when the owner has "
        "explicitly decided, never on your own initiative.",
        {
            "task": _TASK,
            "project": _PROJECT,
            "gate": {"type": "string", "enum": ["draft", "final"]},
            "decision": {"type": "string", "enum": ["approve", "reject"]},
            "note": {"type": "string", "description": "Optional note for the author agent."},
        },
        _paper_gate,
        required=("task", "gate", "decision"),
        read_only=False,
        idempotent=False,
    ),
    Tool(
        "review_paper",
        "Review a paper (Review Factory)",
        "Have Loom's three-vendor reviewer panel review any paper from a link (arXiv, "
        "OpenReview, or a PDF URL), using the named venue's real review form. Returns a "
        "review_id; the panel takes ~10-15 minutes.",
        {
            "url": {"type": "string", "description": "arXiv / OpenReview / PDF link."},
            "venue": {"type": "string", "description": "Venue whose form to review against, e.g. ICLR, NeurIPS (optional)."},
            "title": {"type": "string", "description": "Optional display title."},
        },
        _review_paper,
        required=("url",),
        read_only=False,
        idempotent=False,
        open_world=True,
    ),
)

TOOLS_BY_NAME = {tool.name: tool for tool in TOOLS}


def call_tool(client: LoomClient, name: str, arguments: dict[str, Any] | None) -> tuple[str, bool]:
    """Run one tool. Returns ``(text, is_error)``; never raises for tool faults."""
    tool = TOOLS_BY_NAME.get(name)
    if tool is None:
        return f"unknown tool {name!r}; available: {', '.join(TOOLS_BY_NAME)}", True
    args = dict(arguments or {})
    unknown = sorted(set(args) - set(tool.properties))
    if unknown:
        return f"{name} does not take {', '.join(unknown)}; it takes {', '.join(tool.properties) or 'no arguments'}", True
    missing = [key for key in tool.required if args.get(key) in (None, "")]
    if missing:
        return f"{name} requires {', '.join(missing)}", True
    try:
        result = tool.handler(client, args)
    except ToolError as exc:
        return str(exc), True
    if isinstance(result, str):
        return result, False
    return json.dumps(result, ensure_ascii=False, indent=2, default=str), False
