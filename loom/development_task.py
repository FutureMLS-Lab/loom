"""Bounded implement/review workflow for normal software development tasks.

The interactive task agent is the only writer. Two fresh, read-only Cursor
reviewers inspect exact commit ranges with different SDE skills. State lives
beside the task rather than in the repository, so review bookkeeping never
pollutes the user's branch.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from loom.rud_task import CURSOR_DEFAULT_MODEL, read_meta, task_root, task_worktree_path

KIND_DEVELOPMENT = "development"
STATE_FILE = "development.json"
REPORT_FILE = "DEVELOPMENT_REVIEW.md"
DEFAULT_MAX_REVIEW_ROUNDS = 2
MAX_REVIEW_ROUNDS_LIMIT = 5
DEFAULT_REVIEWER_MODEL = os.environ.get(
    "LOOM_DEVELOPMENT_REVIEWER_MODEL", CURSOR_DEFAULT_MODEL
).strip() or CURSOR_DEFAULT_MODEL

_LOCK = threading.RLock()
_JOBS: set[tuple[str, str]] = set()
_SLUG_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]*$")
REVIEW_CATEGORIES = {
    "A": (
        "requirements_behavior",
        "edge_state",
        "failure_safety",
        "security_data_integrity",
        "verification",
    ),
    "B": (
        "component_boundary",
        "integration_contract",
        "lifecycle_operability",
        "performance_cost",
        "maintainability",
    ),
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def skill_root() -> Path:
    return Path(__file__).resolve().parent / "skills" / "dev"


def implementer_skill_path() -> Path:
    return skill_root() / "sde-implementer" / "SKILL.md"


def reviewer_skill_path(role: str) -> Path:
    name = {
        "A": "sde-correctness-review",
        "B": "sde-architecture-review",
    }.get(str(role).upper())
    if not name:
        raise ValueError(f"unknown reviewer role {role!r}")
    return skill_root() / name / "SKILL.md"


def skill_catalog() -> list[dict[str, str]]:
    """Internal role skills injected by Development Tasks, never user-picked."""
    return [
        {
            "name": "sde-implementer",
            "role": "Implementer",
            "description": "Sole-writer engineering contract: test, commit a clean checkpoint, never push or merge.",
            "injection": "Automatically added to every Development Task implementer.",
            "path": str(implementer_skill_path()),
        },
        {
            "name": "sde-correctness-review",
            "role": "Reviewer A",
            "description": "Behavioral correctness and verification: requirements, failure paths, data safety, and regression evidence.",
            "injection": "Injected only into Reviewer A's fresh headless session.",
            "path": str(reviewer_skill_path("A")),
        },
        {
            "name": "sde-architecture-review",
            "role": "Reviewer B",
            "description": "System fit and operability: boundaries, integration contracts, lifecycle, cost, and maintainability.",
            "injection": "Injected only into Reviewer B's fresh headless session.",
            "path": str(reviewer_skill_path("B")),
        },
    ]


def _state_path(project_root: Path, slug: str) -> Path:
    return task_root(project_root, slug) / STATE_FILE


def _report_path(project_root: Path, slug: str) -> Path:
    return task_root(project_root, slug) / REPORT_FILE


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    tmp.replace(path)


def read_state(project_root: Path, slug: str) -> dict[str, Any]:
    try:
        raw = json.loads(_state_path(project_root, slug).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return raw if isinstance(raw, dict) else {}


def write_state(project_root: Path, slug: str, state: dict[str, Any]) -> None:
    state["updated_at"] = _now()
    _atomic_json(_state_path(project_root, slug), state)
    _write_report(project_root, slug, state)


def _git(worktree: Path, *args: str, timeout: int = 30) -> tuple[bool, str]:
    try:
        proc = subprocess.run(
            ["git", *args], cwd=str(worktree), stdin=subprocess.DEVNULL,
            capture_output=True, text=True, timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, str(exc)
    text = (proc.stdout or proc.stderr or "").strip()
    return proc.returncode == 0, text


def _head(worktree: Path) -> str:
    ok, value = _git(worktree, "rev-parse", "HEAD")
    return value.strip() if ok else ""


def _dirty(worktree: Path) -> str:
    ok, value = _git(worktree, "status", "--porcelain=v1", "--untracked-files=all")
    return value if ok else "could not inspect git status"


def normalize_max_rounds(value: Any) -> int:
    try:
        rounds = int(value)
    except (TypeError, ValueError):
        rounds = DEFAULT_MAX_REVIEW_ROUNDS
    return max(1, min(MAX_REVIEW_ROUNDS_LIMIT, rounds))


def initialize_task(
    project_root: Path,
    slug: str,
    *,
    test_command: str = "",
    max_review_rounds: Any = DEFAULT_MAX_REVIEW_ROUNDS,
    reviewer_model: str = DEFAULT_REVIEWER_MODEL,
) -> dict[str, Any]:
    if not _SLUG_RE.fullmatch(slug):
        raise ValueError("invalid task slug")
    meta = read_meta(project_root, slug)
    worktree = task_worktree_path(project_root, slug)
    if not meta or meta.kind != KIND_DEVELOPMENT:
        raise ValueError("not a Development task")
    if worktree is None:
        raise ValueError("Development tasks require an isolated git worktree")
    base = _head(worktree)
    if not base:
        raise ValueError("could not read the worktree HEAD")
    state = {
        "version": 1,
        "stage": "implementing",
        "base_commit": base,
        "last_reviewed_commit": "",
        "current_commit": base,
        "test_command": str(test_command or "").strip(),
        "max_review_rounds": normalize_max_rounds(max_review_rounds),
        "reviewer_model": str(reviewer_model or DEFAULT_REVIEWER_MODEL).strip(),
        "reviewers": {
            "A": {"name": "Behavioral correctness & verification", "skill": str(reviewer_skill_path("A"))},
            "B": {"name": "Architecture, integration & operability", "skill": str(reviewer_skill_path("B"))},
        },
        "rounds": [],
        "approved_at": "",
        "created_at": _now(),
        "updated_at": _now(),
    }
    write_state(project_root, slug, state)
    return state


def _task_and_worktree(project_root: Path, slug: str) -> tuple[dict[str, Any], Path]:
    if not _SLUG_RE.fullmatch(slug):
        raise ValueError("invalid task slug")
    meta = read_meta(project_root, slug)
    if not meta or meta.kind != KIND_DEVELOPMENT:
        raise ValueError("not a Development task")
    state = read_state(project_root, slug)
    if not state:
        raise ValueError("Development state is missing")
    worktree = task_worktree_path(project_root, slug)
    if worktree is None:
        raise ValueError("Development worktree is missing")
    return state, worktree


def public_state(project_root: Path, slug: str) -> dict[str, Any]:
    with _LOCK:
        state, worktree = _task_and_worktree(project_root, slug)
        key = (str(project_root.resolve()), slug)
        # A daemon review thread cannot survive a Loom server restart. Turn the
        # orphaned "reviewing" marker into a retryable partial failure instead of
        # leaving the UI spinning forever; completed reviewer results stay cached.
        if state.get("stage") == "reviewing" and key not in _JOBS:
            rounds = state.get("rounds") or []
            if rounds:
                rounds[-1]["status"] = "error"
                rounds[-1]["error"] = (
                    "Review process was interrupted; retry runs only missing reviewers."
                )
            state["stage"] = "review_error"
            write_state(project_root, slug, state)
        state = json.loads(json.dumps(state))
        state["current_commit"] = _head(worktree)
        state["worktree_clean"] = not bool(_dirty(worktree))
        state["worktree"] = str(worktree)
        state["report_path"] = str(_report_path(project_root, slug))
        try:
            state["report"] = _report_path(project_root, slug).read_text(encoding="utf-8")
        except OSError:
            state["report"] = ""
        return state


def _create_review_snapshot(worktree: Path, candidate: str) -> tuple[Path | None, str]:
    """Create a detached checkout so the live implementer cannot move review files."""
    parent = Path(tempfile.mkdtemp(prefix="loom-development-review-"))
    snapshot = parent / "checkout"
    ok, message = _git(
        worktree,
        "worktree",
        "add",
        "--detach",
        str(snapshot),
        candidate,
        timeout=120,
    )
    if not ok:
        shutil.rmtree(parent, ignore_errors=True)
        return None, message or "could not create detached review snapshot"
    return snapshot, ""


def _remove_review_snapshot(worktree: Path, snapshot: Path | None) -> None:
    if snapshot is None:
        return
    _git(worktree, "worktree", "remove", "--force", str(snapshot), timeout=120)
    shutil.rmtree(snapshot.parent, ignore_errors=True)


def _review_prompt(
    project_root: Path,
    slug: str,
    worktree: Path,
    state: dict[str, Any],
    review_round: dict[str, Any],
    role: str,
) -> str:
    meta = read_meta(project_root, slug)
    skill = reviewer_skill_path(role).read_text(encoding="utf-8")
    prior = []
    for old in state.get("rounds", []):
        if old is review_round:
            continue
        for result in (old.get("reviewers") or {}).values():
            for finding in result.get("findings", []) if isinstance(result, dict) else []:
                prior.append(
                    f"- {finding.get('id', '?')} {finding.get('severity', '?')}: "
                    f"{finding.get('title', '')}"
                )
    prior_text = "\n".join(prior[-20:]) or "(none)"
    test_command = str(state.get("test_command") or "").strip() or "(not configured)"
    categories = " | ".join(REVIEW_CATEGORIES[role])
    category_example = REVIEW_CATEGORIES[role][0]
    return f"""You are Reviewer {role} in a Loom Development Task.

Reviewer skill (mandatory):
---
{skill}
---

Task goal: {meta.general_goal if meta else ''}
Workspace: {worktree}
Base commit: {review_round['base_commit']}
Candidate commit: {review_round['candidate_commit']}
Configured test command: {test_command}

Review only `git diff {review_round['base_commit']}..{review_round['candidate_commit']}`
and the minimum surrounding code/tests required to validate it. The candidate
commit is the immutable review unit. You are read-only: do not edit files,
run write-capable formatters or migrations, commit, push, or merge. Focused
verification may create disposable ignored caches, but it must not change any
tracked file. Do not repeat a prior finding unless it remains present.

Prior findings for context:
{prior_text}

Allowed finding categories for Reviewer {role}: {categories}

Return one JSON object and nothing else:
{{
  "verdict": "approve" | "request_changes",
  "summary": "short evidence-based summary",
  "findings": [
    {{
      "category": "{category_example}",
      "severity": "P0" | "P1" | "P2" | "P3",
      "title": "short defect title",
      "file": "repository-relative path",
      "line": 1,
      "evidence": "specific code or observed behavior",
      "impact": "what breaks and for whom",
      "recommendation": "smallest safe correction",
      "test": "specific regression test or verification"
    }}
  ]
}}

Use request_changes for any P0/P1 finding. Approve when there are no
merge-blocking findings; P2/P3 suggestions may accompany approval.
"""


def _extract_json(text: str) -> dict[str, Any]:
    clean = text.strip()
    if clean.startswith("```"):
        clean = re.sub(r"^```(?:json)?\s*", "", clean, flags=re.I)
        clean = re.sub(r"\s*```$", "", clean)
    try:
        payload = json.loads(clean)
    except json.JSONDecodeError:
        start, end = clean.find("{"), clean.rfind("}")
        if start < 0 or end <= start:
            raise ValueError("reviewer did not return a JSON object")
        try:
            payload = json.loads(clean[start : end + 1])
        except json.JSONDecodeError as exc:
            raise ValueError(f"reviewer returned invalid JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError("reviewer JSON is not an object")
    return payload


def _normalize_review(payload: dict[str, Any], role: str) -> dict[str, Any]:
    if role not in REVIEW_CATEGORIES:
        raise ValueError(f"unknown reviewer role {role!r}")
    verdict = str(payload.get("verdict") or "").strip().lower()
    if verdict not in {"approve", "request_changes"}:
        raise ValueError("reviewer verdict must be approve or request_changes")
    raw_findings = payload.get("findings")
    if not isinstance(raw_findings, list):
        raise ValueError("reviewer findings must be a list")
    findings: list[dict[str, Any]] = []
    for index, raw in enumerate(raw_findings, 1):
        if not isinstance(raw, dict):
            raise ValueError("each reviewer finding must be an object")
        severity = str(raw.get("severity") or "").upper()
        if severity not in {"P0", "P1", "P2", "P3"}:
            raise ValueError(f"invalid finding severity {severity!r}")
        category = str(raw.get("category") or "").strip().lower()
        if category not in REVIEW_CATEGORIES[role]:
            allowed = ", ".join(REVIEW_CATEGORIES[role])
            raise ValueError(
                f"Reviewer {role} finding category must be one of: {allowed}"
            )
        title = str(raw.get("title") or "").strip()
        if not title:
            raise ValueError("each finding needs a title")
        try:
            line = max(0, int(raw.get("line") or 0))
        except (TypeError, ValueError):
            line = 0
        findings.append({
            "id": f"{role}-{index:03d}",
            "category": category,
            "severity": severity,
            "title": title,
            "file": str(raw.get("file") or "").strip(),
            "line": line,
            "evidence": str(raw.get("evidence") or "").strip(),
            "impact": str(raw.get("impact") or "").strip(),
            "recommendation": str(raw.get("recommendation") or "").strip(),
            "test": str(raw.get("test") or "").strip(),
        })
    if any(f["severity"] in {"P0", "P1"} for f in findings):
        verdict = "request_changes"
    return {
        "ok": True,
        "role": role,
        "verdict": verdict,
        "summary": str(payload.get("summary") or "").strip(),
        "findings": findings,
        "completed_at": _now(),
    }


def _run_reviewer(prompt: str, model: str, workspace: Path, role: str) -> dict[str, Any]:
    binary = shutil.which("agent") or shutil.which("cursor-agent")
    if not binary:
        return {"ok": False, "role": role, "error": "Cursor CLI `agent` is not on PATH"}
    command = [
        binary, "--print", "--workspace", str(workspace), "--mode", "ask",
        "--trust", "--model", model, "--output-format", "json", prompt,
    ]
    started = time.monotonic()
    try:
        proc = subprocess.run(
            command, cwd=str(workspace), stdin=subprocess.DEVNULL,
            capture_output=True, text=True, timeout=900,
        )
    except subprocess.TimeoutExpired:
        return {"ok": False, "role": role, "error": "review timed out after 900s"}
    except OSError as exc:
        return {"ok": False, "role": role, "error": str(exc)}
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "review failed").strip()[-2000:]
        return {"ok": False, "role": role, "error": detail}
    try:
        envelope = json.loads(proc.stdout or "")
        if not isinstance(envelope, dict):
            raise ValueError("Cursor response is not an object")
        if envelope.get("is_error") is True or envelope.get("subtype") == "error":
            raise ValueError(str(envelope.get("result") or envelope.get("error") or "review failed"))
        review = _normalize_review(_extract_json(str(envelope.get("result") or "")), role)
    except (json.JSONDecodeError, ValueError) as exc:
        return {"ok": False, "role": role, "error": str(exc)}
    review["model"] = model
    review["duration_seconds"] = round(time.monotonic() - started, 1)
    try:
        review["cost_usd"] = float(envelope.get("total_cost_usd") or envelope.get("cost_usd") or 0.0)
    except (TypeError, ValueError):
        review["cost_usd"] = 0.0
    return review


def _find_round(state: dict[str, Any], number: int) -> dict[str, Any] | None:
    for item in state.get("rounds", []):
        if isinstance(item, dict) and int(item.get("number") or 0) == number:
            return item
    return None


def start_review(
    project_root: Path,
    slug: str,
    *,
    runner: Callable[[str, str, Path, str], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Validate a clean committed checkpoint and start both reviewers."""
    project_root = project_root.resolve()
    key = (str(project_root), slug)
    with _LOCK:
        state, worktree = _task_and_worktree(project_root, slug)
        if key in _JOBS or state.get("stage") == "reviewing":
            return {"ok": True, "status": "reviewing", "state": public_state(project_root, slug)}
        dirty = _dirty(worktree)
        if dirty:
            raise ValueError("commit the checkpoint and leave the worktree clean before review")
        candidate = _head(worktree)
        if not candidate:
            raise ValueError("could not read the candidate commit")
        rounds = state.setdefault("rounds", [])
        current = rounds[-1] if rounds else None
        retry_partial = bool(
            current
            and current.get("candidate_commit") == candidate
            and current.get("status") == "error"
            and any(
                not ((current.get("reviewers") or {}).get(role) or {}).get("ok")
                for role in ("A", "B")
            )
        )
        if retry_partial:
            review_round = current
        else:
            if state.get("last_reviewed_commit") == candidate or (
                current and current.get("candidate_commit") == candidate
            ):
                raise ValueError("this commit has already been reviewed; create a new commit first")
            if len(rounds) >= int(state.get("max_review_rounds") or DEFAULT_MAX_REVIEW_ROUNDS):
                raise ValueError("maximum review rounds reached; human decision required")
            base = str(state.get("last_reviewed_commit") or state.get("base_commit") or "")
            if not base or base == candidate:
                raise ValueError("the checkpoint has no committed changes from its review base")
            review_round = {
                "number": len(rounds) + 1,
                "base_commit": base,
                "candidate_commit": candidate,
                "status": "running",
                "started_at": _now(),
                "completed_at": "",
                "reviewers": {},
            }
            rounds.append(review_round)
        review_round["status"] = "running"
        review_round["error"] = ""
        state["stage"] = "reviewing"
        state["current_commit"] = candidate
        state["approved_at"] = ""
        write_state(project_root, slug, state)
        _JOBS.add(key)
        run = runner or _run_reviewer
        threading.Thread(
            target=_review_job,
            args=(project_root, slug, int(review_round["number"]), run),
            daemon=True,
        ).start()
    return {"ok": True, "status": "reviewing", "state": public_state(project_root, slug)}


def _review_job(
    project_root: Path,
    slug: str,
    round_number: int,
    runner: Callable[[str, str, Path, str], dict[str, Any]],
) -> None:
    key = (str(project_root.resolve()), slug)
    worktree: Path | None = None
    snapshot: Path | None = None
    try:
        with _LOCK:
            state, worktree = _task_and_worktree(project_root, slug)
            review_round = _find_round(state, round_number)
            if review_round is None:
                return
            existing = review_round.setdefault("reviewers", {})
            missing = [role for role in ("A", "B") if not (existing.get(role) or {}).get("ok")]
            candidate = str(review_round.get("candidate_commit") or "")
            model = str(state.get("reviewer_model") or DEFAULT_REVIEWER_MODEL)

        snapshot, snapshot_error = _create_review_snapshot(worktree, candidate)
        if snapshot is None:
            results = {
                role: {
                    "ok": False,
                    "role": role,
                    "error": f"could not create immutable review snapshot: {snapshot_error}",
                }
                for role in missing
            }
        else:
            with _LOCK:
                state, _live_worktree = _task_and_worktree(project_root, slug)
                review_round = _find_round(state, round_number)
                if review_round is None:
                    return
                prompts = {
                    role: _review_prompt(project_root, slug, snapshot, state, review_round, role)
                    for role in missing
                }

            results = {}
            with ThreadPoolExecutor(max_workers=max(1, len(missing))) as pool:
                futures = {
                    pool.submit(runner, prompts[role], model, snapshot, role): role
                    for role in missing
                }
                for future in as_completed(futures):
                    role = futures[future]
                    try:
                        results[role] = future.result()
                    except Exception as exc:  # noqa: BLE001 - persist reviewer failure
                        results[role] = {"ok": False, "role": role, "error": str(exc)}

        # Do not publish a terminal stage until the immutable checkout is gone
        # and this job can genuinely be retried. Otherwise a fast UI click can
        # see "review_error" while the old job still owns the task key.
        _remove_review_snapshot(worktree, snapshot)
        snapshot = None
        with _LOCK:
            state, _worktree = _task_and_worktree(project_root, slug)
            review_round = _find_round(state, round_number)
            if review_round is None:
                return
            reviewers = review_round.setdefault("reviewers", {})
            reviewers.update(results)
            failed = [role for role in ("A", "B") if not (reviewers.get(role) or {}).get("ok")]
            if failed:
                review_round["status"] = "error"
                review_round["error"] = "Reviewer " + ", ".join(failed) + " failed; retry runs only missing reviewers."
                state["stage"] = "review_error"
            else:
                blocking = any(
                    result.get("verdict") == "request_changes"
                    or any(f.get("severity") in {"P0", "P1"} for f in result.get("findings", []))
                    for result in reviewers.values()
                )
                review_round["status"] = "request_changes" if blocking else "approved"
                review_round["blocking"] = blocking
                review_round["completed_at"] = _now()
                state["last_reviewed_commit"] = review_round["candidate_commit"]
                at_limit = len(state.get("rounds", [])) >= int(
                    state.get("max_review_rounds") or DEFAULT_MAX_REVIEW_ROUNDS
                )
                state["stage"] = "human_gate" if (not blocking or at_limit) else "repair_needed"
            _JOBS.discard(key)
            write_state(project_root, slug, state)
    finally:
        if worktree is not None:
            _remove_review_snapshot(worktree, snapshot)
        with _LOCK:
            _JOBS.discard(key)


def repair_prompt(project_root: Path, slug: str) -> str:
    state, worktree = _task_and_worktree(project_root, slug)
    if state.get("stage") != "repair_needed":
        raise ValueError("there is no blocking review round ready for repair")
    review_round = state.get("rounds", [])[-1]
    blockers: list[str] = []
    for role, result in (review_round.get("reviewers") or {}).items():
        for finding in result.get("findings", []):
            if finding.get("severity") in {"P0", "P1"}:
                loc = finding.get("file") or "(no file)"
                if finding.get("line"):
                    loc += f":{finding['line']}"
                blockers.append(
                    f"- {finding.get('id', role)} {finding.get('severity')} "
                    f"{finding.get('title')} at {loc}: {finding.get('recommendation')}"
                )
    return f"""Development repair round for commit {review_round['candidate_commit']}.

You remain the sole writer. Read the durable report at
{_report_path(project_root, slug)} and address the evidence-backed blocking
findings below in the isolated worktree {worktree}:

{chr(10).join(blockers) or '- Reviewers requested changes; inspect the full report.'}

Run the relevant tests, update PLAN.md with results and any justified rejection,
then commit the repair as a new clean checkpoint. Do not push or merge. Stop once
the new commit is ready for Loom's next reviewer round.
"""


def mark_repair_started(project_root: Path, slug: str) -> dict[str, Any]:
    with _LOCK:
        state, _worktree = _task_and_worktree(project_root, slug)
        if state.get("stage") != "repair_needed":
            raise ValueError("there is no blocking review round ready for repair")
        state["stage"] = "repairing"
        state["repair_started_at"] = _now()
        write_state(project_root, slug, state)
        return public_state(project_root, slug)


def approve(project_root: Path, slug: str) -> dict[str, Any]:
    with _LOCK:
        state, worktree = _task_and_worktree(project_root, slug)
        if state.get("stage") != "human_gate":
            raise ValueError("the task is not waiting at the human gate")
        if _dirty(worktree):
            raise ValueError("worktree changed after review; commit and review it first")
        if _head(worktree) != state.get("last_reviewed_commit"):
            raise ValueError("HEAD changed after review; review the new commit first")
        state["stage"] = "ready_to_merge"
        state["approved_at"] = _now()
        write_state(project_root, slug, state)
        return public_state(project_root, slug)


def _write_report(project_root: Path, slug: str, state: dict[str, Any]) -> None:
    lines = [
        "# Development Review",
        "",
        f"- Stage: `{state.get('stage', 'unknown')}`",
        f"- Base: `{state.get('base_commit', '')}`",
        f"- Last reviewed: `{state.get('last_reviewed_commit', '') or '(none)'}`",
        f"- Reviewer model: `{state.get('reviewer_model', '')}`",
        f"- Review rounds: {len(state.get('rounds', []))}/{state.get('max_review_rounds', DEFAULT_MAX_REVIEW_ROUNDS)}",
        "",
    ]
    for review_round in state.get("rounds", []):
        lines.extend([
            f"## Round {review_round.get('number')} — {review_round.get('status', 'unknown')}",
            "",
            f"`{review_round.get('base_commit', '')}` → `{review_round.get('candidate_commit', '')}`",
            "",
        ])
        if review_round.get("error"):
            lines.extend([f"> {review_round['error']}", ""])
        for role in ("A", "B"):
            result = (review_round.get("reviewers") or {}).get(role)
            lines.extend([f"### Reviewer {role}", ""])
            if not result:
                lines.extend(["Pending.", ""])
                continue
            if not result.get("ok"):
                lines.extend([f"Failed: {result.get('error', 'unknown error')}", ""])
                continue
            lines.extend([
                f"**{result.get('verdict', 'unknown')}** — {result.get('summary', '')}",
                "",
            ])
            findings = result.get("findings") or []
            if not findings:
                lines.extend(["No findings.", ""])
            for finding in findings:
                location = finding.get("file") or "(no file)"
                if finding.get("line"):
                    location += f":{finding['line']}"
                lines.extend([
                    f"- **{finding.get('id')} {finding.get('severity')} — {finding.get('title')}** "
                    f"[{finding.get('category') or 'uncategorized'}] (`{location}`)",
                    f"  - Evidence: {finding.get('evidence') or '(none supplied)'}",
                    f"  - Impact: {finding.get('impact') or '(none supplied)'}",
                    f"  - Fix: {finding.get('recommendation') or '(none supplied)'}",
                    f"  - Test: {finding.get('test') or '(none supplied)'}",
                ])
            lines.append("")
    _report_path(project_root, slug).write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
