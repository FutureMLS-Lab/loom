"""The factories' background crews.

Carved out of web.py in the route split: every job that runs outside a
request - the AR studio jobs (mine / ideas / ground / venue), the review
panel job, the author<->reviewer loop driver and its manager, and the
rebuttal agents with their watchers and delivery pipeline glue. The
handler calls in; nothing here knows HTTP.
"""

from __future__ import annotations

import hashlib
import re
import threading
import time
from pathlib import Path
from typing import Any, TYPE_CHECKING

import subprocess

from loom import ar_task as ar
from loom import rebuttal_delivery as delivery
from loom import rebuttal_task as rebuttal
from loom import review_task as review
from loom.openclaw import OpenClawClient
from loom.rud_task import (
    AGENT_CLAUDE,
    AGENT_CURSOR,
    CURSOR_DEFAULT_MODEL,
    agent_default_model,
    build_agent_command,
    create_task,
    list_tasks,
    normalize_agent,
    prefer_cursor_fast_model,
    read_meta,
    task_root,
    update_meta,
)
from loom.tmux_util import (
    capture_pane,
    send_pane_key,
    send_pane_text,
    tmux_subprocess_env,
)
from loom.usage_budget import parse_pane_usage, read_budget_marker, usage_budget_violation
from loom.web_activity import _AGENT_WORKING_RE, _MONITOR_CAPTURE_LINES, _iso_now
from loom.web_util import _sanitize_session_name, _session_name_from_tmux_target

if TYPE_CHECKING:  # pragma: no cover - annotation-only import, avoids a cycle
    from loom.web import ClaudeRegistry

# --- AR paper loop ----------------------------------------------------------

_AR_POLL_SECONDS = 5.0
# An author that ends its turn without writing the round note would park the
# loop forever - the note is the only end-of-round signal. Watch the pane
# while a round is open: idle this many consecutive polls (~3 min) means the
# agent really stopped, not that its working indicator flickered.
_AR_STALL_IDLE_POLLS = 36
# Between continue-nudges. Long enough that an author babysitting a slow
# experiment gets re-woken at a sane pace instead of being spammed.
_AR_NUDGE_COOLDOWN = 600.0
# Total continuation nudges per round before we stop burning turns and tell
# the human instead. This is deliberately a hard cap: "worked for a while"
# must not reset an overnight task's spending limit indefinitely.
_AR_MAX_NUDGES = 2


def _file_sha256(path: Path) -> str:
    try:
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()
    except OSError:
        return ""


def _pause_rebuttal_if_over_budget(target: str, pane_text: str) -> tuple[bool, dict[str, Any], str]:
    """Apply the same live-turn fuse to factory panes outside `.RUD`."""
    if not _AGENT_WORKING_RE.search(pane_text or ""):
        return False, {}, ""
    usage = parse_pane_usage(pane_text)
    reason = usage_budget_violation(usage)
    if not reason:
        return False, usage, ""
    ok, error = send_pane_key(target, "C-c")
    if not ok:
        reason += f"; interrupt failed: {error or 'unknown error'}"
    return True, usage, reason


def _ar_run_async(fn, *args: Any) -> None:
    """Run a long AR step off the request thread; it reports via ar.json."""
    threading.Thread(target=fn, args=args, daemon=True).start()


def _ar_headless_model(meta: Any) -> str:
    """Claude model for headless Studio idea generation.

    Idea generation still goes through ``claude -p``. Paper reviews use the
    fixed Cursor PDF reviewer panel defined in ``ar_task.py``.
    """
    if meta is not None and normalize_agent(getattr(meta, "agent", "")) == AGENT_CLAUDE:
        model = str(getattr(meta, "interview_model", "") or "").strip()
        if model:
            return model
    return agent_default_model(AGENT_CLAUDE)


def _ar_merge_ideas(
    state: dict[str, Any], fresh: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Replace the proposed ideas with a new batch, keeping spawned ones.

    An idea that already has a child task is a commitment the user made, so a
    regenerate must not drop it or renumber it out from under the child.
    """
    kept = [
        i
        for i in (state.get("ideas") or [])
        if isinstance(i, dict) and i.get("status") == ar.IDEA_STATUS_SPAWNED
    ]
    taken = {str(i.get("id")) for i in kept}
    out = list(kept)
    for idea in fresh:
        base = idea["id"]
        candidate = base
        n = 2
        while candidate in taken:
            candidate = f"{base}-{n}"
            n += 1
        idea["id"] = candidate
        taken.add(candidate)
        out.append(idea)
    return out


def _ar_logger(root: Path, slug: str, job: str, *, reset: bool = True):
    """Append-only progress log for one AR job, tailed by the panel."""
    path = ar.job_log_path(root, slug, job)
    if reset:
        ar.reset_job_log(path)
    return lambda line: ar.append_job_log(path, line)


def _ar_reviewer_slug(model: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", str(model or "")).strip("-._")
    return slug or "reviewer"


def _ar_store_panel_reviews(
    root: Path,
    slug: str,
    n: int,
    reviewers: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Persist each independent review and return compact state metadata."""
    directory = ar.round_dir(root, slug, n)
    directory.mkdir(parents=True, exist_ok=True)
    stored: list[dict[str, Any]] = []
    for item in reviewers:
        model = str(item.get("model") or "reviewer")
        text = str(item.get("review") or "").strip()
        path = directory / f"review-{_ar_reviewer_slug(model)}.md"
        if text:
            path.write_text(text + "\n", encoding="utf-8")
        metadata = {
            key: item.get(key)
            for key in ("model", "scores", "headline", "duration_seconds", "cost")
        }
        metadata["path"] = str(path) if text else ""
        stored.append(metadata)
    return stored


_PANEL_REVIEW_RE = re.compile(
    r"(?ms)^# Reviewer: `([^`]+)`\s*\n(.*?)(?=^\s*---\s*$|\Z)"
)


def _ar_review_payload(root: Path, slug: str, n: int) -> dict[str, Any] | None:
    """Review API payload with every model's full report.

    New rounds read per-model files. Existing panel rounds are recovered from
    the combined review.md, and old single-model rounds remain readable.
    """
    combined_path = ar.review_note_path(root, slug, n)
    if not combined_path.is_file():
        return None
    combined = _ar_read_text(combined_path)
    state = ar.read_ar_state(root, slug)
    rec = ar.round_record(state, n) or {}
    review = rec.get("review") if isinstance(rec.get("review"), dict) else {}
    metadata = (
        review.get("reviewers")
        if isinstance(review.get("reviewers"), list)
        else []
    )
    parsed = {
        model: body.strip()
        for model, body in _PANEL_REVIEW_RE.findall(combined)
    }
    directory = ar.round_dir(root, slug, n).resolve()
    reviewers: list[dict[str, Any]] = []
    for item in metadata:
        if not isinstance(item, dict):
            continue
        model = str(item.get("model") or "")
        body = ""
        path_value = str(item.get("path") or "")
        if path_value:
            candidate: Path | None = Path(path_value).expanduser().resolve()
            try:
                candidate.relative_to(directory)
            except ValueError:
                candidate = None
            if candidate is not None and candidate.is_file():
                body = _ar_read_text(candidate)
        if not body:
            body = parsed.get(model, "")
        reviewers.append({**item, "review": body})

    if not reviewers and parsed:
        for model, body in parsed.items():
            scores = ar.parse_review_scores(body)
            reviewers.append(
                {
                    "model": model,
                    "scores": scores,
                    "headline": ar.review_headline(scores),
                    "review": body,
                }
            )
    if not reviewers:
        model = str(review.get("model") or "")
        reviewers = [
            {
                "model": model or "reviewer",
                "scores": review.get("scores") or {},
                "headline": review.get("headline") or "",
                "review": combined,
            }
        ]
    return {
        "ok": True,
        "round": n,
        "review": combined,
        "scores": review.get("scores") or {},
        "headline": review.get("headline") or "",
        "deciding_model": str(review.get("deciding_model") or ""),
        "reviewers": reviewers,
    }


def _ar_mine_job(root: Path, slug: str, limit: int, venue_only: bool) -> None:
    state = ar.read_ar_state(root, slug)
    settings = ar.search_settings(state)
    log = _ar_logger(root, slug, ar.JOB_PAPERS)
    log(
        f"querying arXiv with {len(settings['terms'])} term(s) in "
        f"{', '.join(settings['categories'])} "
        f"(limit {limit}{', venue-tagged only' if venue_only else ''})"
    )
    res = ar.mine_papers(
        str(state.get("direction") or ""),
        str(state.get("custom_direction") or ""),
        search_terms=settings["terms"],
        categories=settings["categories"],
        limit=limit,
        venue_only=venue_only,
    )
    if res.get("ok"):
        log(f"query: {res.get('query', '')}")
        for paper in (res.get("papers") or [])[:10]:
            venue = f" [{paper['venue']}]" if paper.get("venue") else ""
            log(f"  {paper.get('published', '')}{venue} {paper.get('title', '')[:90]}")
        log(f"kept {len(res.get('papers') or [])} paper(s)")
    else:
        log(f"failed: {res.get('error')}")
    if res.get("ok"):
        ar.update_ar_state(
            root,
            slug,
            papers=res.get("papers") or [],
            papers_status="done",
            papers_error="",
            papers_query=res.get("query", ""),
            papers_updated_at=_iso_now(),
        )
        print(f"[ar] {slug}: mined {len(res.get('papers') or [])} paper(s)", flush=True)
    else:
        ar.update_ar_state(
            root, slug, papers_status="error", papers_error=str(res.get("error") or "")
        )
        print(f"[ar] {slug}: mining failed - {res.get('error')}", flush=True)


def _ar_search_suggest_job(root: Path, slug: str, model: str) -> None:
    state = ar.read_ar_state(root, slug)
    log = _ar_logger(root, slug, ar.JOB_SEARCH)
    result = ar.suggest_search_settings(state, model=model, on_line=log)
    latest = ar.read_ar_state(root, slug)
    cost = float(latest.get("cost_usd") or 0.0) + float(result.get("cost") or 0.0)
    if not result.get("ok"):
        error = str(result.get("error") or "search suggestion failed")
        log(f"failed: {error}")
        ar.update_ar_state(
            root,
            slug,
            search_suggest_status="error",
            search_suggest_error=error,
            cost_usd=round(cost, 4),
        )
        return
    terms = list(result.get("terms") or [])
    categories = list(result.get("categories") or [])
    log(f"terms: {', '.join(terms)}")
    log(f"categories: {', '.join(categories)}")
    ar.update_ar_state(
        root,
        slug,
        search_terms=terms,
        search_categories=categories,
        search_terms_source="model",
        search_terms_updated_at=_iso_now(),
        search_suggest_status="done",
        search_suggest_error="",
        search_suggest_rationale=str(result.get("rationale") or ""),
        cost_usd=round(cost, 4),
    )
    print(f"[ar] {slug}: suggested {len(terms)} arXiv search term(s)", flush=True)


def _rebuttal_logger(project_id: str):
    def log(message: str) -> None:
        state = rebuttal.read_state(project_id)
        if not state:
            return
        rebuttal.append_log(state, str(message))
        rebuttal.write_state(project_id, state)

    return log


def _rebuttal_policy_job(studio_id: str, model: str) -> None:
    def log(message: str) -> None:
        state = rebuttal.read_studio(studio_id)
        if not state:
            return
        rebuttal.append_log(state, str(message))
        rebuttal.write_studio(studio_id, state)

    result = rebuttal.discover_studio_policy(
        studio_id,
        model=model,
        on_line=log,
    )
    state = rebuttal.read_studio(studio_id)
    if not state:
        return
    state["active_job"] = ""
    state["cost_usd"] = round(
        float(state.get("cost_usd") or 0.0) + float(result.get("cost") or 0.0),
        4,
    )
    if result.get("ok"):
        state["policy"] = result.get("policy") or {}
        state["policy_evidence"] = result.get("policy_evidence") or {}
        state["strategy"] = result.get("strategy") or {}
        state["unknowns"] = result.get("unknowns") or []
        state["sources"] = result.get("sources") or []
        state["stage"] = rebuttal.STUDIO_STAGE_AWAIT_POLICY_REVIEW
        state["policy_approved_at"] = ""
        state["error"] = ""
        rebuttal.append_log(
            state,
            f"policy draft ready from {len(state['sources'])} source(s)",
        )
    else:
        if isinstance(result.get("sources"), list):
            state["sources"] = result["sources"]
        state["stage"] = rebuttal.STUDIO_STAGE_POLICY_INPUT
        state["error"] = str(result.get("error") or "policy discovery failed")
        rebuttal.append_log(state, f"policy discovery failed: {state['error']}")
    rebuttal.write_studio(studio_id, state)


def _rebuttal_session_name(project_id: str) -> str:
    return _sanitize_session_name(
        f"loom-rebuttal-{project_id}",
        "loom-rebuttal",
    )


def _tmux_session_exists(session_name: str) -> bool:
    try:
        result = subprocess.run(
            ["tmux", "has-session", "-t", session_name],
            capture_output=True,
            text=True,
            env=tmux_subprocess_env(),
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


def _rebuttal_watch_agent(project_id: str) -> None:
    while True:
        state = rebuttal.read_state(project_id)
        if not state or state.get("agent_status") != "running":
            return
        source = Path(str(state.get("source_path") or ""))
        marker = rebuttal.output_root(source) / rebuttal.AGENT_COMPLETE_FILE
        if marker.is_file():
            result = rebuttal.ingest_agent_outputs(project_id)
            state = rebuttal.read_state(project_id)
            if result.get("ok"):
                state["reviewers"] = result.get("reviewers") or []
                state["responses"] = result.get("responses") or {}
                state["validation"] = {}
                state["approved_at"] = ""
                state["content_approval"] = {}
                rebuttal.invalidate_delivery(
                    state,
                    "response-drafting agent produced new content",
                )
                state["stage"] = rebuttal.STAGE_RESPONSES
                state["agent_status"] = "complete"
                state["agent_summary"] = result.get("summary") or ""
                state["error"] = ""
                rebuttal.append_log(
                    state,
                    f"tmux agent completed {len(state['responses'])} response draft(s)",
                )
            else:
                state["agent_status"] = "error"
                state["error"] = str(
                    result.get("error") or "could not ingest agent outputs"
                )
                rebuttal.append_log(
                    state,
                    f"tmux agent output failed validation: {state['error']}",
                )
            rebuttal.write_state(project_id, state)
            return

        target = str(state.get("tmux_target") or "")
        session = _session_name_from_tmux_target(target)
        if not target or not _tmux_session_exists(session):
            state["agent_status"] = "error"
            state["error"] = "rebuttal agent tmux session disappeared"
            rebuttal.append_log(state, state["error"])
            rebuttal.write_state(project_id, state)
            return
        captured, pane_text = capture_pane(target, 80)
        paused, usage, reason = (
            _pause_rebuttal_if_over_budget(target, pane_text)
            if captured
            else (False, {}, "")
        )
        if paused:
            state["agent_status"] = "budget_stopped"
            state["usage_budget"] = {"reason": reason, "usage": usage}
            state["error"] = (
                f"Loom paused the rebuttal agent: {reason}. Partial files were "
                "preserved; start a fresh run after inspection."
            )
            rebuttal.append_log(state, state["error"])
            rebuttal.write_state(project_id, state)
            return
        if captured and "Agent exited (" in pane_text:
            state["agent_status"] = "error"
            state["error"] = (
                "rebuttal agent exited before writing agent-complete.json"
            )
            rebuttal.append_log(state, state["error"])
            rebuttal.write_state(project_id, state)
            return
        time.sleep(2)


def _rebuttal_join_staged(
    studio_id: str, claude_registry: Any
) -> list[dict[str, Any]]:
    """Register every staged quick-import package under a now-active studio.

    A package is a directory holding the fetched submission.pdf; ones already
    registered are recognised by source path. Each fresh one is registered and
    its rebuttal agent started - after the policy approval there is nothing
    left that needs a human hand.
    """
    staged_root = Path.home() / ".loom" / "factories" / "rebuttal" / studio_id
    if not staged_root.is_dir():
        return []
    known = {
        str(Path(str(p.get("source_path") or "")).resolve())
        for p in rebuttal.list_projects()
    }
    joined: list[dict[str, Any]] = []
    for package in sorted(staged_root.iterdir()):
        if not package.is_dir() or not (package / "submission.pdf").is_file():
            continue
        if str(package.resolve()) in known:
            continue
        try:
            payload = rebuttal.register_paper_for_studio(
                studio_id, str(package)
            )
        except (ValueError, OSError) as exc:
            joined.append({"dir": str(package), "error": str(exc)})
            continue
        project = payload.get("project") or {}
        project_id = str(project.get("id") or "")
        entry: dict[str, Any] = {
            "project_id": project_id,
            "title": str(project.get("title") or package.name),
        }
        if project_id and bool((project.get("manifest") or {}).get("ready")):
            started = _rebuttal_start_agent(
                project_id, CURSOR_DEFAULT_MODEL, claude_registry
            )
            entry["agent_started"] = bool(started.get("ok"))
        joined.append(entry)
    return joined


def _rebuttal_start_agent(
    project_id: str,
    model: str,
    registry: "ClaudeRegistry",
) -> dict[str, Any]:
    state = rebuttal.read_state(project_id)
    if not state:
        return {"ok": False, "error": "rebuttal project not found"}
    if not (state.get("manifest") or {}).get("ready"):
        return {
            "ok": False,
            "error": "package needs one paper PDF and at least one review PDF",
        }
    current_target = str(state.get("tmux_target") or "")
    if (
        state.get("agent_status") == "running"
        and current_target
        and _tmux_session_exists(_session_name_from_tmux_target(current_target))
    ):
        return {"ok": True, "running": True, "target": current_target}

    source = Path(str(state.get("source_path") or "")).resolve()
    try:
        instructions = rebuttal.prepare_agent_instructions(project_id)
    except (ValueError, OSError) as exc:
        return {"ok": False, "error": str(exc)}
    session = _rebuttal_session_name(project_id)
    target = f"{session}:0.0"
    env = tmux_subprocess_env()
    if not _tmux_session_exists(session):
        try:
            created = subprocess.run(
                [
                    "tmux",
                    "new-session",
                    "-d",
                    "-s",
                    session,
                    "-x",
                    "240",
                    "-y",
                    "64",
                    "-c",
                    str(source),
                    "-e",
                    f"LOOM_REBUTTAL_ID={project_id}",
                ],
                capture_output=True,
                text=True,
                env=env,
                timeout=8,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return {"ok": False, "error": str(exc)}
        if created.returncode != 0:
            return {
                "ok": False,
                "error": (
                    created.stderr
                    or created.stdout
                    or "could not create rebuttal tmux session"
                ).strip(),
            }
    selected_model = prefer_cursor_fast_model(model or CURSOR_DEFAULT_MODEL)
    command = build_agent_command(
        AGENT_CURSOR,
        model=selected_model,
    )
    ok, error = registry._launch_agent_in_pane(target, source, command)
    if not ok:
        return {"ok": False, "error": error}
    registry.wait_until_ready(target, timeout=45.0)
    prompt = (
        f"Read `{instructions}` completely and execute it now. "
        "Work autonomously through concern extraction and every reviewer response. "
        "Use the specified completion marker only after all required files are ready."
    )
    ok, error = send_pane_text(target, prompt, submit=True)
    if not ok:
        return {"ok": False, "error": error}
    state = rebuttal.read_state(project_id)
    state["tmux_target"] = target
    state["agent_status"] = "running"
    state["agent_model"] = selected_model
    state["agent_started_at"] = _iso_now()
    state["agent_summary"] = ""
    state["active_job"] = ""
    state["error"] = ""
    rebuttal.append_log(state, f"started live rebuttal agent in {target}")
    rebuttal.write_state(project_id, state)
    threading.Thread(
        target=_rebuttal_watch_agent,
        args=(project_id,),
        name=f"loom-rebuttal-watch-{project_id}",
        daemon=True,
    ).start()
    return {"ok": True, "running": True, "target": target}


def _rebuttal_stop_agent(project_id: str) -> dict[str, Any]:
    state = rebuttal.read_state(project_id)
    if not state:
        return {"ok": False, "error": "rebuttal project not found"}
    target = str(state.get("tmux_target") or "")
    session = _session_name_from_tmux_target(target)
    if session:
        try:
            subprocess.run(
                ["tmux", "kill-session", "-t", session],
                capture_output=True,
                text=True,
                env=tmux_subprocess_env(),
                timeout=8,
            )
        except (OSError, subprocess.TimeoutExpired):
            pass
    state["agent_status"] = "stopped"
    state["tmux_target"] = ""
    state["error"] = ""
    rebuttal.append_log(state, "stopped live rebuttal agent")
    rebuttal.write_state(project_id, state)
    return {"ok": True, "running": False}


def _rebuttal_resume_agent_watchers() -> int:
    resumed = 0
    for item in rebuttal.list_projects():
        project_id = str(item.get("id") or "")
        state = rebuttal.read_state(project_id)
        target = str(state.get("tmux_target") or "")
        if (
            state.get("agent_status") == "running"
            and target
            and _tmux_session_exists(_session_name_from_tmux_target(target))
        ):
            threading.Thread(
                target=_rebuttal_watch_agent,
                args=(project_id,),
                name=f"loom-rebuttal-watch-{project_id}",
                daemon=True,
            ).start()
            resumed += 1
    return resumed


def _rebuttal_delivery_session_name(project_id: str) -> str:
    return _sanitize_session_name(
        f"loom-rebuttal-delivery-{project_id}",
        "loom-rebuttal-delivery",
    )


def _rebuttal_verify_figures_job(project_id: str) -> None:
    """Run the three-model figure panel; it writes the verdict state itself."""
    log = _rebuttal_logger(project_id)
    try:
        result = delivery.verify_delivery_figures(project_id, on_line=log)
    except Exception as exc:  # noqa: BLE001 - job boundary
        result = {"ok": False, "error": str(exc)}
    if "report" in result:
        # The panel ran to a verdict; verify_delivery_figures already moved the
        # stage/phase and stored the per-model reports.
        return
    latest = rebuttal.read_state(project_id)
    current = dict(latest.get("delivery") or {})
    if current.get("phase") == "figure_verification_running":
        # The panel could not run at all (missing PDF, model catalog failure):
        # fall back to the pre-run phase so the button comes back.
        current["phase"] = "awaiting_final_approval"
        latest["delivery"] = current
        latest["error"] = str(result.get("error") or "figure verification failed to run")
        rebuttal.append_log(
            latest, f"figure verification failed to run: {latest['error']}"
        )
        rebuttal.write_state(project_id, latest)


def _rebuttal_watch_delivery_agent(project_id: str) -> None:
    while True:
        state = rebuttal.read_state(project_id)
        current = (
            state.get("delivery")
            if isinstance(state.get("delivery"), dict)
            else {}
        )
        if not state or current.get("agent_status") not in (
            "running",
            "validating",
        ):
            return
        marker = Path(str(current.get("marker_path") or ""))
        if marker.is_file():
            current = dict(current)
            current["phase"] = "validating"
            current["agent_status"] = "validating"
            state["delivery"] = current
            state["stage"] = rebuttal.STAGE_DELIVERY_VALIDATING
            state["error"] = ""
            rebuttal.append_log(
                state,
                "delivery agent completed source handoff; running strict preflight",
            )
            rebuttal.write_state(project_id, state)
            result = delivery.ingest_delivery_completion(project_id)
            if not result.get("ok"):
                latest = rebuttal.read_state(project_id)
                current = dict(latest.get("delivery") or {})
                if latest.get("stage") == rebuttal.STAGE_DELIVERY_VALIDATING:
                    current["phase"] = "blocked"
                    current["agent_status"] = "error"
                    latest["delivery"] = current
                    latest["stage"] = rebuttal.STAGE_DELIVERY_BLOCKED
                    latest["error"] = str(
                        result.get("error") or "delivery preflight failed"
                    )
                    rebuttal.append_log(latest, latest["error"])
                    rebuttal.write_state(project_id, latest)
                return
            # Preflight passed. Final approval requires a unanimous figure
            # verdict bound to this exact PDF, so start the panel now instead
            # of waiting for someone to trigger it by script.
            latest = rebuttal.read_state(project_id)
            if latest.get("stage") == rebuttal.STAGE_AWAIT_DELIVERY_APPROVAL:
                current = dict(latest.get("delivery") or {})
                current["phase"] = "figure_verification_running"
                latest["delivery"] = current
                rebuttal.append_log(
                    latest, "starting the three-model figure verification"
                )
                rebuttal.write_state(project_id, latest)
                threading.Thread(
                    target=_rebuttal_verify_figures_job,
                    args=(project_id,),
                    name=f"loom-rebuttal-figures-{project_id}",
                    daemon=True,
                ).start()
            return

        target = str(current.get("tmux_target") or "")
        session = _session_name_from_tmux_target(target)
        if not target or not _tmux_session_exists(session):
            current = dict(current)
            current["agent_status"] = "error"
            current["phase"] = "blocked"
            state["delivery"] = current
            state["stage"] = rebuttal.STAGE_DELIVERY_BLOCKED
            state["error"] = "delivery agent tmux session disappeared"
            rebuttal.append_log(state, state["error"])
            rebuttal.write_state(project_id, state)
            return
        captured, pane_text = capture_pane(target, 80)
        paused, usage, reason = (
            _pause_rebuttal_if_over_budget(target, pane_text)
            if captured
            else (False, {}, "")
        )
        if paused:
            current = dict(current)
            current["agent_status"] = "budget_stopped"
            current["phase"] = "blocked"
            current["usage_budget"] = {"reason": reason, "usage": usage}
            state["delivery"] = current
            state["stage"] = rebuttal.STAGE_DELIVERY_BLOCKED
            state["error"] = (
                f"Loom paused the delivery agent: {reason}. Partial files were "
                "preserved; start a fresh attempt after inspection."
            )
            rebuttal.append_log(state, state["error"])
            rebuttal.write_state(project_id, state)
            return
        if captured and "Agent exited (" in pane_text:
            current = dict(current)
            current["agent_status"] = "error"
            current["phase"] = "blocked"
            state["delivery"] = current
            state["stage"] = rebuttal.STAGE_DELIVERY_BLOCKED
            state["error"] = (
                "delivery agent exited before writing delivery-complete.json"
            )
            rebuttal.append_log(state, state["error"])
            rebuttal.write_state(project_id, state)
            return
        time.sleep(2)


def _rebuttal_start_delivery_agent(
    project_id: str,
    model: str,
    registry: "ClaudeRegistry",
    *,
    rerun: bool = False,
    feedback: str = "",
) -> dict[str, Any]:
    state = rebuttal.read_state(project_id)
    if not state:
        return {"ok": False, "error": "rebuttal project not found"}
    if state.get("agent_status") == "running":
        return {
            "ok": False,
            "error": "finish or stop the response-drafting agent first",
        }
    current = (
        state.get("delivery")
        if isinstance(state.get("delivery"), dict)
        else {}
    )
    current_target = str(current.get("tmux_target") or "")
    if (
        current.get("agent_status") == "running"
        and current_target
        and _tmux_session_exists(
            _session_name_from_tmux_target(current_target)
        )
    ):
        return {"ok": True, "running": True, "target": current_target}
    feedback_parts = [feedback.strip()] if feedback.strip() else []
    if rerun:
        validation = (
            current.get("validation")
            if isinstance(current.get("validation"), dict)
            else {}
        )
        errors_block = "\n".join(
            f"- {error}" for error in (validation.get("errors") or [])
        )
        if errors_block:
            feedback_parts.append(errors_block)
    feedback = "\n\n".join(feedback_parts)
    try:
        prepared = delivery.prepare_delivery_attempt(
            project_id,
            feedback=feedback,
        )
    except (ValueError, OSError) as exc:
        return {"ok": False, "error": str(exc)}

    def launch_failed(message: str) -> dict[str, Any]:
        session_name = _rebuttal_delivery_session_name(project_id)
        if _tmux_session_exists(session_name):
            try:
                subprocess.run(
                    ["tmux", "kill-session", "-t", session_name],
                    capture_output=True,
                    text=True,
                    env=tmux_subprocess_env(),
                    timeout=8,
                )
            except (OSError, subprocess.TimeoutExpired):
                pass
        failed_state = rebuttal.read_state(project_id)
        failed_delivery = dict(failed_state.get("delivery") or {})
        failed_delivery["phase"] = "blocked"
        failed_delivery["agent_status"] = "error"
        failed_state["delivery"] = failed_delivery
        failed_state["stage"] = rebuttal.STAGE_DELIVERY_BLOCKED
        failed_state["error"] = message
        rebuttal.append_log(
            failed_state,
            f"delivery agent launch failed: {message}",
        )
        rebuttal.write_state(project_id, failed_state)
        return {"ok": False, "error": message}

    session = _rebuttal_delivery_session_name(project_id)
    target = f"{session}:0.0"
    env = tmux_subprocess_env()
    if _tmux_session_exists(session):
        try:
            subprocess.run(
                ["tmux", "kill-session", "-t", session],
                capture_output=True,
                text=True,
                env=env,
                timeout=8,
            )
        except (OSError, subprocess.TimeoutExpired):
            pass
    try:
        created = subprocess.run(
            [
                "tmux",
                "new-session",
                "-d",
                "-s",
                session,
                "-x",
                "240",
                "-y",
                "64",
                "-c",
                str(prepared["workspace"]),
                "-e",
                f"LOOM_REBUTTAL_DELIVERY_ID={project_id}",
            ],
            capture_output=True,
            text=True,
            env=env,
            timeout=8,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return launch_failed(str(exc))
    if created.returncode != 0:
        return launch_failed(
            (
                created.stderr
                or created.stdout
                or "could not create delivery tmux session"
            ).strip()
        )
    selected_model = prefer_cursor_fast_model(model or CURSOR_DEFAULT_MODEL)
    command = build_agent_command(AGENT_CURSOR, model=selected_model)
    ok, error = registry._launch_agent_in_pane(
        target,
        Path(prepared["workspace"]),
        command,
    )
    if not ok:
        return launch_failed(error)
    registry.wait_until_ready(target, timeout=45.0)
    prompt = (
        f"Read `{prepared['instructions']}` completely and execute it now. "
        "Produce the synchronized WACV revised-paper and one-page rebuttal "
        "sources in this isolated attempt. Write the run-scoped completion "
        "marker only after every required source and revision-map file is ready."
    )
    ok, error = send_pane_text(target, prompt, submit=True)
    if not ok:
        return launch_failed(error)

    state = rebuttal.read_state(project_id)
    current = dict(state.get("delivery") or {})
    current.update(
        phase="agent_running",
        agent_status="running",
        agent_model=selected_model,
        agent_started_at=_iso_now(),
        tmux_target=target,
    )
    state["delivery"] = current
    state["stage"] = rebuttal.STAGE_DELIVERY_AGENT
    state["error"] = ""
    rebuttal.append_log(
        state,
        f"started delivery agent for attempt {current.get('run_id')} in {target}",
    )
    rebuttal.write_state(project_id, state)
    threading.Thread(
        target=_rebuttal_watch_delivery_agent,
        args=(project_id,),
        name=f"loom-rebuttal-delivery-watch-{project_id}",
        daemon=True,
    ).start()
    return {
        "ok": True,
        "running": True,
        "target": target,
        "run_id": current.get("run_id"),
    }


def _rebuttal_stop_delivery_agent(project_id: str) -> dict[str, Any]:
    state = rebuttal.read_state(project_id)
    if not state:
        return {"ok": False, "error": "rebuttal project not found"}
    current = dict(state.get("delivery") or {})
    target = str(current.get("tmux_target") or "")
    session = _session_name_from_tmux_target(target)
    if session:
        try:
            subprocess.run(
                ["tmux", "kill-session", "-t", session],
                capture_output=True,
                text=True,
                env=tmux_subprocess_env(),
                timeout=8,
            )
        except (OSError, subprocess.TimeoutExpired):
            pass
    current["agent_status"] = "stopped"
    current["phase"] = "blocked"
    current["tmux_target"] = ""
    state["delivery"] = current
    state["stage"] = rebuttal.STAGE_DELIVERY_BLOCKED
    state["error"] = "delivery agent was stopped"
    rebuttal.append_log(state, state["error"])
    rebuttal.write_state(project_id, state)
    return {"ok": True, "running": False}


def _rebuttal_resume_delivery_watchers() -> int:
    resumed = 0
    for item in rebuttal.list_projects():
        project_id = str(item.get("id") or "")
        state = rebuttal.read_state(project_id)
        current = (
            state.get("delivery")
            if isinstance(state.get("delivery"), dict)
            else {}
        )
        target = str(current.get("tmux_target") or "")
        marker = Path(str(current.get("marker_path") or ""))
        alive = bool(
            target
            and _tmux_session_exists(_session_name_from_tmux_target(target))
        )
        if current.get("agent_status") in ("running", "validating") and (
            alive or marker.is_file()
        ):
            threading.Thread(
                target=_rebuttal_watch_delivery_agent,
                args=(project_id,),
                name=f"loom-rebuttal-delivery-watch-{project_id}",
                daemon=True,
            ).start()
            resumed += 1
        elif current.get("agent_status") in ("running", "validating"):
            current = dict(current)
            current["agent_status"] = "error"
            current["phase"] = "blocked"
            state["delivery"] = current
            state["stage"] = rebuttal.STAGE_DELIVERY_BLOCKED
            state["error"] = "delivery agent did not survive the Loom restart"
            rebuttal.append_log(state, state["error"])
            rebuttal.write_state(project_id, state)
    return resumed


def _ar_venue_job(root: Path, slug: str, model: str) -> None:
    """Deep-research the venue's last completed cycle, then persist the report.

    Chaining lives here rather than in the browser: a page reload must not be
    able to lose the "and then propose ideas from it" half of the kickoff.
    """
    state = ar.read_ar_state(root, slug)
    log = _ar_logger(root, slug, ar.JOB_VENUE)
    res = ar.research_venue_cycle(state, model=model, on_line=log)
    chain = bool(state.get("venue_chain_ideas"))
    if res.get("ok"):
        ar.update_ar_state(
            root,
            slug,
            venue_report=res.get("report") or {},
            venue_status="done",
            venue_error="",
            venue_chain_ideas=False,
            venue_updated_at=_iso_now(),
            cost_usd=round(
                float(state.get("cost_usd") or 0.0) + float(res.get("cost") or 0.0), 4
            ),
        )
        print(f"[ar] {slug}: venue-cycle report ready", flush=True)
        if chain:
            log("report ready - generating ideas from it")
            ar.update_ar_state(root, slug, ideas_status="running", ideas_error="")
            _ar_run_async(
                _ar_ideas_job, root, slug, 6, model, ar.IDEA_SOURCE_VENUE
            )
    else:
        log(f"failed: {res.get('error')}")
        ar.update_ar_state(
            root,
            slug,
            venue_status="error",
            venue_error=str(res.get("error") or ""),
            venue_chain_ideas=False,
        )
        print(f"[ar] {slug}: venue research failed - {res.get('error')}", flush=True)


def _ar_ideas_job(
    root: Path,
    slug: str,
    count: int,
    model: str,
    source: str = ar.IDEA_SOURCE_PAPERS,
) -> None:
    state = ar.read_ar_state(root, slug)
    log = _ar_logger(root, slug, ar.JOB_IDEAS)
    res = ar.propose_ideas(
        state,
        ar.ar_skill_text(ar.SKILL_STUDIO),
        count=count,
        model=model,
        source=source,
        on_line=log,
    )
    if not res.get("ok"):
        log(f"failed: {res.get('error')}")
    if res.get("ok"):
        ar.update_ar_state(
            root,
            slug,
            ideas=_ar_merge_ideas(state, res.get("ideas") or []),
            ideas_status="done",
            ideas_error="",
            ideas_updated_at=_iso_now(),
            cost_usd=round(
                float(state.get("cost_usd") or 0.0) + float(res.get("cost") or 0.0), 4
            ),
        )
        print(f"[ar] {slug}: proposed {len(res.get('ideas') or [])} idea(s)", flush=True)
    else:
        ar.update_ar_state(
            root, slug, ideas_status="error", ideas_error=str(res.get("error") or "")
        )
        print(f"[ar] {slug}: idea generation failed - {res.get('error')}", flush=True)


def _ar_link_job(root: Path, slug: str, model: str) -> None:
    state = ar.read_ar_state(root, slug)
    log = _ar_logger(root, slug, ar.JOB_IDEAS, reset=False)
    res = ar.link_ideas(state, model=model, on_line=log)
    if not res.get("ok"):
        log(f"failed: {res.get('error')}")
        ar.update_ar_state(
            root, slug, link_status="error", link_error=str(res.get("error") or "")
        )
        return
    ar.update_ar_state(
        root,
        slug,
        ideas=res.get("ideas") or state.get("ideas") or [],
        link_status="done",
        link_error="",
        ideas_updated_at=_iso_now(),
        cost_usd=round(
            float(state.get("cost_usd") or 0.0) + float(res.get("cost") or 0.0), 4
        ),
    )
    print(f"[ar] {slug}: linked {res.get('linked')} idea(s) to prior work", flush=True)


def _review_run_job(project_id: str) -> None:
    """One standalone Review Factory panel run, off the request thread."""
    try:
        res = review.run_project_review(project_id)
    except Exception as exc:  # noqa: BLE001
        review.update_state(project_id, status="error", error=str(exc)[:300])
        print(f"[review] {project_id} panel error: {exc}", flush=True)
        return
    if not res.get("ok"):
        review.update_state(
            project_id, status="error", error=str(res.get("error") or "")
        )


def _sweep_stale_review_runs() -> int:
    """Review panels run in threads that do not survive a restart."""
    cleared = 0
    for item in review.list_projects():
        if str(item.get("status")) == "running":
            review.update_state(
                str(item.get("id")),
                status="error",
                error="interrupted by a Loom restart - run it again",
            )
            cleared += 1
    return cleared


def _ar_prior_panel_reviews(state: dict[str, Any], n: int) -> dict[str, str]:
    """Each model's OWN report from round n-1, for reviewer continuity."""
    rec = ar.round_record(state, n - 1) or {}
    stored = rec.get("review") if isinstance(rec.get("review"), dict) else {}
    out: dict[str, str] = {}
    for item in stored.get("reviewers") or []:
        model = str((item or {}).get("model") or "")
        path = str((item or {}).get("path") or "")
        if not model or not path:
            continue
        try:
            out[model] = Path(path).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
    return out


def _ar_author_note_text(root: Path, slug: str, n: int) -> str:
    try:
        return ar.author_note_path_for(task_root(root, slug), n).read_text(
            encoding="utf-8", errors="replace"
        )
    except OSError:
        return ""


def _ar_review_job(root: Path, slug: str) -> None:
    """One out-of-band review, triggered from the panel rather than the loop."""
    state = ar.read_ar_state(root, slug)
    paper_dir = ar.paper_root(root, slug)
    n = ar.current_round(state)
    log = _ar_logger(root, slug, ar.JOB_REVIEW)
    build = ar.build_pdf(paper_dir)
    log(
        "PDF built"
        if build.get("ok")
        else f"PDF build failed: {build.get('error')}"
    )
    res = review.panel_review(
        paper_dir,
        venue=str(state.get("venue") or ar.DEFAULT_VENUE),
        round_n=max(1, n),
        build=build,
        models=ar.CURSOR_REVIEWER_MODELS,
        on_line=log,
        prior_reviews=_ar_prior_panel_reviews(state, max(1, n)),
        author_note=_ar_author_note_text(root, slug, max(1, n)),
    )
    if not res.get("ok"):
        log(f"failed: {res.get('error')}")
        readiness = res.get("readiness")
        if isinstance(readiness, dict):
            report_path = ar.round_dir(root, slug, n) / "readiness.md"
            report_path.parent.mkdir(parents=True, exist_ok=True)
            try:
                report_path.write_text(
                    ar.review_readiness_markdown(readiness), encoding="utf-8"
                )
                readiness["report_path"] = str(report_path)
            except OSError:
                pass
            state = ar.read_ar_state(root, slug)
            rec = ar.ensure_round(state, n)
            rec["readiness"] = readiness
            state["review_status"] = "error"
            state["review_error"] = str(res.get("error") or "")
            ar.write_ar_state(root, slug, state)
        else:
            ar.update_ar_state(
                root,
                slug,
                review_status="error",
                review_error=str(res.get("error") or ""),
            )
        return
    path = ar.review_note_path(root, slug, n)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        path.write_text(str(res.get("review") or ""), encoding="utf-8")
    except OSError as exc:
        ar.update_ar_state(root, slug, review_status="error", review_error=str(exc))
        return
    state = ar.read_ar_state(root, slug)
    rec = ar.ensure_round(state, n)
    try:
        stored_reviewers = _ar_store_panel_reviews(
            root, slug, n, list(res.get("reviewers") or [])
        )
    except OSError as exc:
        ar.update_ar_state(
            root, slug, review_status="error", review_error=str(exc)
        )
        return
    rec["review"] = {
        "created_at": _iso_now(),
        "model": ar.CURSOR_REVIEWER_PANEL,
        "models": res.get("models") or list(ar.CURSOR_REVIEWER_MODELS),
        "path": str(path),
        "scores": res.get("scores") or {},
        "headline": res.get("headline") or "",
        "deciding_model": res.get("deciding_model") or "",
        "input_pdf": res.get("input_pdf") or str(paper_dir / "main.pdf"),
        "input_sha256": res.get("input_sha256") or "",
        "reviewers": stored_reviewers,
    }
    state["review_status"] = "done"
    state["review_error"] = ""
    state["cost_usd"] = round(
        float(state.get("cost_usd") or 0.0) + float(res.get("cost") or 0.0), 4
    )
    ar.write_ar_state(root, slug, state)


def _ar_spawn_children(
    root: Path,
    parent_slug: str,
    state: dict[str, Any],
    idea_ids: list[str],
) -> tuple[list[dict[str, Any]], list[str]]:
    """Turn selected idea cards into paper tasks, one task per idea."""
    parent = read_meta(root, parent_slug)
    venue = str(state.get("venue") or ar.DEFAULT_VENUE)
    spawned: list[dict[str, Any]] = []
    errors: list[str] = []

    for idea_id in idea_ids:
        idea = ar.find_idea(state, idea_id)
        if idea is None:
            errors.append(f"unknown idea {idea_id!r}")
            continue
        if idea.get("status") == ar.IDEA_STATUS_SPAWNED and idea.get("child_slug"):
            continue
        try:
            child = create_task(
                root,
                idea["title"],
                ar.idea_summary(idea),
                skills_path=(parent.skills_path if parent else ""),
                interview_model=(
                    parent.interview_model if parent else agent_default_model(AGENT_CURSOR)
                ),
                agent=(parent.agent if parent else AGENT_CURSOR),
                kind=ar.KIND_AR,
                auto_worktree=False,
                slug=ar.child_slug(parent_slug, idea["title"]),
            )
            # A paper gets its own code and manuscript repositories rather than
            # a branch of whatever project spawned it.
            layout = ar.init_paper_workspace(root, child.slug, venue, idea)
            paper_dir = ar.paper_root(root, child.slug)
            if not layout.get("ok"):
                errors.append(f"{child.slug}: {layout.get('skeleton')}")
            paper_state = ar.new_paper_state(
                parent_slug=parent_slug,
                idea=idea,
                venue=venue,
                direction=str(state.get("direction") or ""),
                custom_direction=str(state.get("custom_direction") or ""),
                max_rounds=state.get("max_rounds", ar.DEFAULT_MAX_ROUNDS),
                author_model=(parent.interview_model if parent else ""),
                reviewer_model=ar.CURSOR_REVIEWER_PANEL,
                reviewer_models=ar.CURSOR_REVIEWER_MODELS,
            )
            paper_state["paper_dir"] = str(paper_dir)
            ar.write_ar_state(root, child.slug, paper_state)
            idea["status"] = ar.IDEA_STATUS_SPAWNED
            idea["child_slug"] = child.slug
            spawned.append(
                {"idea_id": idea_id, "slug": child.slug, "title": child.title}
            )
            print(f"[ar] {parent_slug}: spawned paper task {child.slug}", flush=True)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{idea_id}: {exc}")

    ar.write_ar_state(root, parent_slug, state)
    return spawned, errors


class _ARLoopDriver:
    """Drives one AR paper task through draft -> rounds -> final review.

    Phase 1 (the author) runs in the task's tmux pane, because writing a paper
    and running its experiments is interactive work that benefits from the full
    agent. Phase 2 (the reviewer) runs headlessly with a different model, so the
    review is genuinely a second opinion rather than the author grading itself.

    The handoff between the two is a file: the author writes
    ``rounds/round-NN/author.md`` as the last act of its turn, and this driver
    polls for it. Watching a file rather than pane text means a round survives a
    server restart, a killed pane, or an agent that stops talking mid-turn -
    the state on disk is always the truth.
    """

    def __init__(
        self,
        manager: "ARLoopManager",
        project_root: Path,
        project_id: str,
        slug: str,
    ) -> None:
        self.manager = manager
        self.project_root = project_root
        self.project_id = project_id
        self.slug = slug
        self.last_error = ""
        self.last_action = ""
        self._author_idle_polls = 0
        self._author_worked = False
        self._last_nudge_ts = 0.0
        self._stop = threading.Event()
        self.thread = threading.Thread(
            target=self._loop, name=f"loom-ar-{slug}", daemon=True
        )

    # --- lifecycle ---

    def start(self) -> None:
        self.thread.start()

    def stop(self) -> None:
        self._stop.set()

    def is_alive(self) -> bool:
        return self.thread.is_alive() and not self._stop.is_set()

    # --- helpers ---

    def _state(self) -> dict[str, Any]:
        return ar.read_ar_state(self.project_root, self.slug)

    def _save(self, state: dict[str, Any]) -> None:
        ar.write_ar_state(self.project_root, self.slug, state)

    def _note(self, action: str) -> None:
        self.last_action = action
        print(f"[ar] {self.slug}: {action}", flush=True)

    def _paper_dir(self) -> Path:
        return ar.paper_root(self.project_root, self.slug)

    def _paste(self, prompt: str) -> tuple[bool, str]:
        """Send a phase-1 prompt to the task's agent pane, starting it if needed.

        Spawning one paper task per idea would be pointless if each then had to
        be launched by hand, so the loop owns the pane's lifecycle: it starts
        one when there isn't one and lets the next tick do the paste, once the
        agent CLI has finished painting its prompt.
        """
        meta = read_meta(self.project_root, self.slug)
        if meta is None:
            return False, "task not found"
        target = (meta.tmux_interview_target or "").strip()
        # A recorded target outlives the session it names - the pane can be
        # killed, or the task moved. Treat a dead one as no pane at all rather
        # than pasting into a session that no longer exists.
        if target and not self.manager.pane_alive(target):
            update_meta(self.project_root, self.slug, tmux_interview_target="")
            target = ""
        if not target:
            started = self.manager.ensure_pane(
                self.project_root, self.project_id, self.slug
            )
            if not started.get("ok"):
                return False, f"could not start the agent pane: {started.get('error')}"
            self._note("started the agent pane")
            return False, "starting the agent pane…"
        self.manager.wait_until_ready(target)
        return send_pane_text(target, prompt, submit=True)

    def _watch_author(self, state: dict[str, Any], n: int) -> None:
        """Keep an open round moving when the author stops without its note.

        Two failure shapes, two answers: a dead pane gets its prompt stamps
        cleared so the normal tick starts a fresh pane and resends the full
        round prompt (a new agent has none of the old context); a live pane
        whose agent ended its turn gets a short continue-nudge, since its own
        context still holds the round instructions.
        """
        meta = read_meta(self.project_root, self.slug)
        target = ((getattr(meta, "tmux_interview_target", "") or "") if meta else "").strip()
        if target and not self.manager.pane_alive(target):
            update_meta(self.project_root, self.slug, tmux_interview_target="")
            rec = ar.ensure_round(state, n)
            if n == 0:
                state["draft_prompt_sent_at"] = ""
            readiness = rec.get("readiness")
            if isinstance(readiness, dict):
                readiness["repair_prompt_sent_at"] = ""
            rec["prompt_sent_at"] = ""
            self._save(state)
            self._author_idle_polls = 0
            self._note(f"round {n}: the agent pane died - restarting it with a fresh prompt")
            return
        if not target:
            return  # no pane yet; the paste path owns starting one
        if read_budget_marker(task_root(self.project_root, self.slug)).get(
            "rotate_required"
        ):
            # Never let the AR stall-recovery path bypass the context fuse by
            # nudging the same oversized session. AR rounds need their own
            # round-aware rotation rather than the generic PLAN handoff.
            return
        ok, text = capture_pane(target, _MONITOR_CAPTURE_LINES)
        if not ok:
            return
        if _AGENT_WORKING_RE.search(text or ""):
            self._author_idle_polls = 0
            self._author_worked = True
            return
        self._author_idle_polls += 1
        if self._author_idle_polls < _AR_STALL_IDLE_POLLS:
            return
        if time.time() - self._last_nudge_ts < _AR_NUDGE_COOLDOWN:
            return
        rec = ar.ensure_round(state, n)
        nudges = int(rec.get("nudges") or 0)
        if nudges >= _AR_MAX_NUDGES:
            if not rec.get("stall_reported"):
                rec["stall_reported"] = True
                self._save(state)
                self._note(
                    f"round {n}: still no completion note after "
                    f"{nudges} nudges - a human needs to look"
                )
                self._emit(
                    "ar-author-stalled",
                    (
                        f"Loom AR task {self.slug} has an author that keeps "
                        f"stopping without finishing round {n}. It was nudged "
                        f"{nudges} times; open its pane and see what it is stuck on."
                    ),
                    {"event": "ar-author-stalled", "round": n, "nudges": nudges},
                )
            return
        prompt = ar.author_continue_prompt(
            task_root(self.project_root, self.slug), n
        )
        sent, err = send_pane_text(target, prompt, submit=True)
        if not sent:
            self.last_error = err
            return
        self._last_nudge_ts = time.time()
        self._author_worked = False
        rec["nudges"] = nudges + 1
        self._save(state)
        self._note(
            f"round {n}: the agent stopped without its note - "
            f"nudged it to continue ({nudges + 1}/{_AR_MAX_NUDGES})"
        )

    def _emit(self, event: str, instruction: str, data: dict[str, Any]) -> None:
        try:
            self.manager.openclaw.emit(
                event,
                instruction=instruction,
                project_root=self.project_root,
                task_slug=self.slug,
                data=data,
            )
        except Exception as exc:  # noqa: BLE001
            print(f"[ar] {self.slug} emit error: {exc}", flush=True)

    def _build(self) -> dict[str, Any]:
        paper_dir = self._paper_dir()
        build = ar.build_pdf(paper_dir)
        state = self._state()
        state["paper_dir"] = str(paper_dir)
        if build.get("ok"):
            state["pdf_path"] = str(build.get("pdf") or "")
            state["pdf_built_at"] = _iso_now()
            state["pdf_error"] = "" if build.get("clean") else "compiled with LaTeX errors"
        else:
            state["pdf_error"] = str(build.get("error") or "build failed")
        self._save(state)
        return build

    # --- stages ---

    def _tick_draft(self, state: dict[str, Any]) -> None:
        note = ar.author_note_path(self.project_root, self.slug, 0)
        if note.is_file():
            build = self._build()
            state = self._state()
            rec = ar.ensure_round(state, 0)
            rec["author"] = {
                "ended_at": _iso_now(),
                "note": str(note),
                "summary": _ar_read_head(note),
            }
            state["stage"] = ar.STAGE_AWAIT_DRAFT_REVIEW
            state["loop_running"] = False
            self._save(state)
            self._note("draft ready - waiting for the human draft gate")
            self._emit(
                "ar-draft-ready",
                (
                    f"Loom AR task {self.slug} finished its first draft and is "
                    "waiting for your review at the draft gate. Approve it in the "
                    "AR panel to open the author/reviewer loop."
                ),
                {
                    "event": "ar-draft-ready",
                    "pdf": state.get("pdf_path", ""),
                    "build_ok": bool(build.get("ok")),
                    "summary": rec["author"]["summary"],
                },
            )
            self.stop()
            return

        if state.get("draft_prompt_sent_at"):
            self._watch_author(state, 0)
            return
        paper_dir = self._paper_dir()
        if not (paper_dir / "main.tex").is_file():
            layout = ar.init_paper_workspace(
                self.project_root,
                self.slug,
                str(state.get("venue") or ar.DEFAULT_VENUE),
                state.get("idea"),
            )
            if not layout.get("ok"):
                self.last_error = str(layout.get("skeleton") or "could not lay out work/")
                return
        note.parent.mkdir(parents=True, exist_ok=True)
        prompt = ar.author_draft_prompt(
            task_root(self.project_root, self.slug), paper_dir, state
        )
        ok, err = self._paste(prompt)
        if not ok:
            self.last_error = err
            return
        self.last_error = ""
        state["draft_prompt_sent_at"] = _iso_now()
        self._save(state)
        self._note("draft prompt sent to the agent pane")

    def _tick_loop(self, state: dict[str, Any]) -> None:
        n = ar.current_round(state)
        rec = ar.round_record(state, n) if n else None

        # A round is "open" until its review lands; anything else means we are
        # between rounds and should start the next one.
        if rec is None or rec.get("review"):
            self._start_round(state, n + 1)
            return

        readiness = rec.get("readiness")
        if isinstance(readiness, dict) and not readiness.get("ready"):
            # A failed completion note is archived. Wait for the author to
            # write a new one after receiving the deterministic failure list.
            note = ar.author_note_path(self.project_root, self.slug, n)
            if note.is_file():
                self._close_round(state, n, note)
            elif not readiness.get("repair_prompt_sent_at"):
                self._send_readiness_prompt(state, n)
            else:
                self._watch_author(state, n)
            return

        # The author's note is the authoritative end-of-round signal, so check
        # it before the prompt bookkeeping: a round driven by hand, or one whose
        # prompt failed to paste and was sent another way, still closes.
        note = ar.author_note_path(self.project_root, self.slug, n)
        if note.is_file():
            self._close_round(state, n, note)
            return

        if not rec.get("prompt_sent_at"):
            self._send_round_prompt(state, n)
            return
        self._watch_author(state, n)

    def _start_round(self, state: dict[str, Any], n: int) -> None:
        if n > ar.max_rounds(state):
            state["stage"] = ar.STAGE_AWAIT_FINAL_REVIEW
            state["loop_running"] = False
            self._save(state)
            self._note(f"finished {n - 1} round(s) - waiting for the final human gate")
            review = ar.latest_review(state) or {}
            self._emit(
                "ar-loop-complete",
                (
                    f"Loom AR task {self.slug} finished all "
                    f"{ar.max_rounds(state)} author/reviewer rounds and is waiting "
                    "for your final review. Approve to deliver the paper, or send "
                    "it back for more rounds."
                ),
                {
                    "event": "ar-loop-complete",
                    "rounds": ar.max_rounds(state),
                    "last_review": review.get("headline", ""),
                    "pdf": state.get("pdf_path", ""),
                },
            )
            self.stop()
            return
        ar.ensure_round(state, n)
        state["round"] = n
        self._save(state)
        self._send_round_prompt(self._state(), n)

    def _send_round_prompt(self, state: dict[str, Any], n: int) -> None:
        rec = ar.ensure_round(state, n)
        if not rec.get("fresh_session_started_at"):
            rotated = self.manager.rotate_pane(
                self.project_root, self.project_id, self.slug
            )
            if not rotated.get("ok"):
                self.last_error = (
                    "could not start a fresh author session: "
                    + str(rotated.get("error") or "unknown error")
                )
                return
            rec["fresh_session_started_at"] = _iso_now()
            self._save(state)
            self._note(f"round {n}: started a fresh context window")

        previous = ar.round_record(state, n - 1) or {}
        review = previous.get("review") if isinstance(previous.get("review"), dict) else {}
        review_text = ""
        review_path = str((review or {}).get("path") or "")
        if review_path:
            try:
                review_text = Path(review_path).read_text(encoding="utf-8", errors="replace")
            except OSError:
                review_text = ""
        gate = ar.last_gate(state, ar.GATE_DRAFT) or {}
        final_gate = ar.last_gate(state, ar.GATE_FINAL) or {}
        note = str(final_gate.get("note") or "") if final_gate.get("decision") == "reject" else str(gate.get("note") or "")

        ar.round_dir(self.project_root, self.slug, n).mkdir(parents=True, exist_ok=True)
        prompt = ar.author_round_prompt(
            task_root(self.project_root, self.slug),
            self._paper_dir(),
            state,
            n,
            review_text=review_text,
            gate_note=note,
        )
        ok, err = self._paste(prompt)
        if not ok:
            self.last_error = err
            return
        self.last_error = ""
        self._author_idle_polls = 0
        state = self._state()
        rec = ar.ensure_round(state, n)
        rec["prompt_sent_at"] = _iso_now()
        state["round"] = n
        self._save(state)
        self._note(f"round {n} prompt sent to the agent pane")

    def _send_readiness_prompt(self, state: dict[str, Any], n: int) -> None:
        rec = ar.round_record(state, n) or {}
        readiness = (
            rec.get("readiness")
            if isinstance(rec.get("readiness"), dict)
            else {}
        )
        if not readiness.get("repair_session_started_at"):
            rotated = self.manager.rotate_pane(
                self.project_root, self.project_id, self.slug
            )
            if not rotated.get("ok"):
                self.last_error = (
                    "could not start a fresh readiness-repair session: "
                    + str(rotated.get("error") or "unknown error")
                )
                return
            readiness["repair_session_started_at"] = _iso_now()
            rec["readiness"] = readiness
            self._save(state)
            self._note(f"round {n}: started a fresh repair context window")
        report_value = str(readiness.get("report_path") or "")
        prompt = ar.author_readiness_repair_prompt(
            task_root(self.project_root, self.slug),
            self._paper_dir(),
            state,
            n,
            readiness,
            report_path=Path(report_value) if report_value else None,
        )
        ok, err = self._paste(prompt)
        if not ok:
            self.last_error = err
            return
        self.last_error = ""
        self._author_idle_polls = 0
        state = self._state()
        rec = ar.ensure_round(state, n)
        latest = dict(rec.get("readiness") or {})
        latest["repair_prompt_sent_at"] = _iso_now()
        rec["readiness"] = latest
        self._save(state)
        self._note(f"round {n} readiness failures returned to the author")

    def _close_round(self, state: dict[str, Any], n: int, note: Path) -> None:
        self._note(f"round {n} author finished - checking submission readiness")
        build = self._build()

        state = self._state()
        rec = ar.ensure_round(state, n)
        readiness = ar.review_readiness(
            self._paper_dir(),
            venue=str(state.get("venue") or ar.DEFAULT_VENUE),
            build=build,
        )
        attempts = rec.setdefault("readiness_attempts", [])
        attempt_n = len(attempts) + 1
        report_path = (
            ar.round_dir(self.project_root, self.slug, n)
            / (
                "readiness.md"
                if readiness.get("ready")
                else f"readiness-attempt-{attempt_n:02d}.md"
            )
        )
        try:
            report_path.write_text(
                ar.review_readiness_markdown(readiness), encoding="utf-8"
            )
        except OSError as exc:
            self.last_error = f"could not write readiness report: {exc}"
            rec["review_error"] = self.last_error
            self._save(state)
            self.stop()
            return
        readiness["report_path"] = str(report_path)

        if not readiness.get("ready"):
            attempt_note = (
                ar.round_dir(self.project_root, self.slug, n)
                / f"author-attempt-{attempt_n:02d}.md"
            )
            summary = _ar_read_head(note)
            attempts.append(
                {
                    "attempt": attempt_n,
                    "ended_at": _iso_now(),
                    "note": str(attempt_note),
                    "summary": summary,
                    "report": str(report_path),
                    "failed": readiness.get("failed") or [],
                }
            )
            rec["readiness"] = readiness
            rec.pop("author", None)
            rec.pop("review_error", None)
            self._save(state)
            # Persist the blocked state before consuming author.md. If Loom
            # dies between these operations, restart sees the failed gate and
            # safely rechecks the still-present note instead of wedging.
            try:
                note.replace(attempt_note)
            except OSError as exc:
                self.last_error = f"could not archive blocked author note: {exc}"
                state = self._state()
                rec = ar.ensure_round(state, n)
                rec["review_error"] = self.last_error
                self._save(state)
                self.stop()
                return
            self._note(
                f"round {n} review blocked by {len(readiness.get('failed') or [])} "
                "readiness check(s)"
            )
            self._send_readiness_prompt(self._state(), n)
            return

        rec["author"] = {
            "ended_at": _iso_now(),
            "note": str(note),
            "summary": _ar_read_head(note),
        }
        rec["readiness"] = readiness
        rec.pop("review_error", None)
        pdf_path = Path(str(build.get("pdf") or self._paper_dir() / "main.pdf"))
        pdf_sha256 = _file_sha256(pdf_path)
        rec["input_pdf_sha256"] = pdf_sha256
        self._save(state)

        previous = ar.round_record(state, n - 1) or {}
        previous_review = (
            previous.get("review")
            if isinstance(previous.get("review"), dict)
            else {}
        )
        if pdf_sha256 and pdf_sha256 == str(previous_review.get("input_sha256") or ""):
            state["stage"] = ar.STAGE_AWAIT_FINAL_REVIEW
            state["loop_running"] = False
            state["stop_reason"] = (
                f"round {n} produced the same compiled PDF as round {n - 1}; "
                "reviewer calls were skipped"
            )
            self._save(state)
            self._note(state["stop_reason"])
            self.stop()
            return
        self._note(f"round {n} readiness passed - starting reviewer panel")

        def _panel() -> dict[str, Any]:
            return review.panel_review(
                self._paper_dir(),
                venue=str(state.get("venue") or ar.DEFAULT_VENUE),
                round_n=n,
                build=build,
                readiness=readiness,
                models=ar.CURSOR_REVIEWER_MODELS,
                on_line=_ar_logger(self.project_root, self.slug, ar.JOB_REVIEW),
                # Rebuttal dynamics: each reviewer re-reads its OWN prior
                # report and the author's response, then re-scores the pages.
                prior_reviews=_ar_prior_panel_reviews(state, n),
                author_note=_ar_author_note_text(self.project_root, self.slug, n),
            )

        result = _panel()
        if not result.get("ok"):
            # A blind retry re-ran every successful reviewer as well as the
            # failed one, multiplying spend after a transient error. Fail
            # closed and let the user retry deliberately after inspecting the
            # recorded panel error.
            self._note(f"round {n} review failed; automatic retry suppressed")
        state = self._state()
        rec = ar.ensure_round(state, n)
        if not result.get("ok"):
            self.last_error = str(result.get("error") or "review failed")
            rec["review_error"] = self.last_error
            self._save(state)
            self._note(f"round {n} review failed twice: {self.last_error}")
            self.stop()
            return

        review_path = ar.review_note_path(self.project_root, self.slug, n)
        review_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            review_path.write_text(str(result.get("review") or ""), encoding="utf-8")
            stored_reviewers = _ar_store_panel_reviews(
                self.project_root,
                self.slug,
                n,
                list(result.get("reviewers") or []),
            )
        except OSError as exc:
            self.last_error = f"could not write review: {exc}"
            self._save(state)
            return

        rec["review"] = {
            "created_at": _iso_now(),
            "model": ar.CURSOR_REVIEWER_PANEL,
            "models": result.get("models") or list(ar.CURSOR_REVIEWER_MODELS),
            "path": str(review_path),
            "scores": result.get("scores") or {},
            "headline": result.get("headline") or "",
            "deciding_model": result.get("deciding_model") or "",
            "input_pdf": result.get("input_pdf") or str(self._paper_dir() / "main.pdf"),
            "input_sha256": pdf_sha256,
            "reviewers": stored_reviewers,
        }
        rec.pop("review_error", None)
        # A round that reviewed fine outdates any earlier manual-review
        # failure; a stale top-level error would keep an "error" pill on a
        # perfectly healthy paper.
        if str(state.get("review_status") or "") == "error":
            state["review_status"] = "done"
            state["review_error"] = ""
        state["cost_usd"] = round(
            float(state.get("cost_usd") or 0.0) + float(result.get("cost") or 0.0), 4
        )
        ar.update_plateau_tracking(state, n)
        self._save(state)
        self._note(f"round {n} reviewed - {rec['review']['headline']}")

        if ar.should_stop_early(state):
            state["stage"] = ar.STAGE_AWAIT_FINAL_REVIEW
            state["loop_running"] = False
            state["stop_reason"] = (
                f"the lowest panel reviewer rated it {int(ar.best_rating(state))}/10, "
                f"at or above the target of {ar.stop_rating(state)}"
            )
            self._save(state)
            self._note(f"stopping early: {state['stop_reason']}")
            self._emit(
                "ar-loop-complete",
                (
                    f"Loom AR task {self.slug} hit its target rating at round {n} "
                    "and is waiting for your final review."
                ),
                {
                    "event": "ar-loop-complete",
                    "round": n,
                    "headline": rec["review"]["headline"],
                    "stop_reason": state["stop_reason"],
                },
            )
            self.stop()
            return

        if ar.should_pause_for_plateau(state, n):
            started = int(state.get("plateau_started_round") or n)
            state["stage"] = ar.STAGE_AWAIT_FINAL_REVIEW
            state["loop_running"] = False
            state["stop_reason"] = (
                f"the lowest panel rating plateaued at round {started} and did not "
                f"improve after {ar.PLATEAU_HUMAN_GRACE_ROUNDS} structural repair rounds"
            )
            self._save(state)
            self._note(f"pausing for human input: {state['stop_reason']}")
            self._emit(
                "ar-loop-complete",
                (
                    f"Loom AR task {self.slug} stayed on a score plateau through "
                    f"round {n} and is waiting for your decision."
                ),
                {
                    "event": "ar-loop-complete",
                    "round": n,
                    "headline": rec["review"]["headline"],
                    "stop_reason": state["stop_reason"],
                },
            )
            self.stop()
            return
        self._emit(
            "ar-round-reviewed",
            (
                f"Loom AR task {self.slug} finished round {n} of "
                f"{ar.max_rounds(state)}: {rec['review']['headline']}."
            ),
            {
                "event": "ar-round-reviewed",
                "round": n,
                "scores": rec["review"]["scores"],
                "headline": rec["review"]["headline"],
            },
        )

    # --- main loop ---

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                state = self._state()
                if not ar.is_paper(state):
                    self.last_error = "not an AR paper task"
                    break
                stage = str(state.get("stage") or ar.STAGE_DRAFT)
                if stage == ar.STAGE_DRAFT:
                    self._tick_draft(state)
                elif stage == ar.STAGE_LOOP:
                    self._tick_loop(state)
                else:
                    # Waiting on a human, or delivered: nothing to drive.
                    break
            except Exception as exc:  # noqa: BLE001
                self.last_error = str(exc)
                print(f"[ar] {self.slug} loop error: {exc}", flush=True)
            if self._stop.wait(_AR_POLL_SECONDS):
                break
        state = self._state()
        if state.get("loop_running"):
            state["loop_running"] = False
            self._save(state)
        self.manager.forget(self.project_id, self.slug, self)


def _ar_read_text(path: Path, limit: int = 20000) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")[:limit]
    except OSError:
        return ""


def _ar_read_head(path: Path, lines: int = 40) -> str:
    text = _ar_read_text(path)
    return "\n".join(text.splitlines()[:lines]).strip()


class ARLoopManager:
    """Owns per-task AR driver threads keyed by ``(project_id, slug)``."""

    def __init__(
        self,
        openclaw_client: OpenClawClient,
        claude_registry: "ClaudeRegistry | None" = None,
        default_skills: Path | None = None,
    ) -> None:
        self.openclaw = openclaw_client
        self.registry = claude_registry
        self.default_skills = default_skills
        self._drivers: dict[tuple[str, str], _ARLoopDriver] = {}
        self._lock = threading.Lock()

    def ensure_pane(
        self, project_root: Path, project_id: str, slug: str
    ) -> dict[str, Any]:
        if self.registry is None:
            return {"ok": False, "error": "no agent registry - press Start Agent"}
        return self.registry.start(
            project_root,
            project_id,
            slug,
            default_skills=self.default_skills,
            env=ar.agent_env(),
        )

    def rotate_pane(
        self, project_root: Path, project_id: str, slug: str
    ) -> dict[str, Any]:
        """Give an automated author round a clean context window."""
        if self.registry is None:
            return {"ok": False, "error": "no agent registry - press Start Agent"}
        return self.registry.rotate(
            project_root,
            project_id,
            slug,
            default_skills=self.default_skills,
            env=ar.agent_env(),
        )

    def wait_until_ready(self, target: str, timeout: float = 12.0) -> None:
        if self.registry is not None:
            self.registry.wait_until_ready(target, timeout=timeout)

    def pane_alive(self, target: str) -> bool:
        if self.registry is None:
            return bool(target.strip())
        return self.registry.target_alive(target)

    def start(self, project_root: Path, project_id: str, slug: str) -> dict[str, Any]:
        state = ar.read_ar_state(project_root, slug)
        if not ar.is_paper(state):
            return {"ok": False, "error": "not an AR paper task"}
        stage = str(state.get("stage") or ar.STAGE_DRAFT)
        if stage in (ar.STAGE_AWAIT_DRAFT_REVIEW, ar.STAGE_AWAIT_FINAL_REVIEW):
            return {"ok": False, "error": f"waiting for you: {ar.STAGE_LABELS[stage]}"}
        if stage == ar.STAGE_DELIVERED:
            return {"ok": False, "error": "this paper has already been delivered"}
        key = (project_id, slug)
        with self._lock:
            existing = self._drivers.get(key)
            if existing is not None and existing.is_alive():
                return {"ok": True, "running": True, "note": "already running"}
            driver = _ARLoopDriver(self, project_root, project_id, slug)
            self._drivers[key] = driver
        ar.update_ar_state(project_root, slug, loop_running=True)
        driver.start()
        return {"ok": True, "running": True}

    def stop(self, project_root: Path, project_id: str, slug: str) -> dict[str, Any]:
        with self._lock:
            driver = self._drivers.pop((project_id, slug), None)
        if driver is not None:
            driver.stop()
        ar.update_ar_state(project_root, slug, loop_running=False)
        return {"ok": True, "running": False}

    def forget(self, project_id: str, slug: str, driver: "_ARLoopDriver") -> None:
        with self._lock:
            if self._drivers.get((project_id, slug)) is driver:
                self._drivers.pop((project_id, slug), None)

    def status(self, project_id: str, slug: str) -> dict[str, Any]:
        with self._lock:
            driver = self._drivers.get((project_id, slug))
        if driver is None:
            return {"running": False, "last_error": "", "last_action": ""}
        return {
            "running": driver.is_alive(),
            "last_error": driver.last_error,
            "last_action": driver.last_action,
        }

    @staticmethod
    def sweep_stale_jobs(projects: list[tuple[str, Path]]) -> int:
        """Clear AR jobs left ``running`` by a server that went away.

        Search suggestion, mining, idea generation and out-of-band reviews run
        in threads that do not survive a restart, and each endpoint refuses to
        start a second job while its status says running - so without this sweep
        a restart in the middle of one would wedge that task's button forever.
        """
        cleared = 0
        for _project_id, root in projects:
            try:
                metas = list_tasks(root)
            except Exception:  # noqa: BLE001
                continue
            for meta in metas:
                if not ar.is_ar_kind(meta.kind):
                    continue
                try:
                    state = ar.read_ar_state(root, meta.slug)
                except Exception:  # noqa: BLE001
                    continue
                changes: dict[str, Any] = {}
                for job in (
                    "search_suggest",
                    "papers",
                    "ideas",
                    "review",
                    "venue",
                    "link",
                ):
                    if str(state.get(f"{job}_status") or "") == "running":
                        changes[f"{job}_status"] = "error"
                        changes[f"{job}_error"] = (
                            "interrupted by a Loom restart - run it again"
                        )
                if changes:
                    ar.update_ar_state(root, meta.slug, **changes)
                    cleared += 1
        return cleared

    def resume_running(self, projects: list[tuple[str, Path]]) -> int:
        """Restart drivers for papers whose ar.json says the loop was running.

        Round state lives on disk, so a resumed driver picks up exactly where
        the old one left off rather than restarting the round.
        """
        started = 0
        for project_id, root in projects:
            try:
                metas = list_tasks(root)
            except Exception:  # noqa: BLE001
                continue
            for meta in metas:
                if not ar.is_ar_kind(meta.kind):
                    continue
                try:
                    state = ar.read_ar_state(root, meta.slug)
                    if ar.is_paper(state) and state.get("loop_running"):
                        if self.start(root, project_id, meta.slug).get("ok"):
                            started += 1
                except Exception:  # noqa: BLE001
                    continue
        return started
