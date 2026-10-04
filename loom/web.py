"""Lightweight local web UI for `.RUD` tasks.

Three concerns after the agent-loop rewrite:

1. **Task CRUD** - list / create / delete tasks (``<project>/.RUD/<slug>/``).
   Each new task auto-creates a git worktree at
   ``<task>/work/<repo>`` on branch ``loom/<slug>`` (best-effort -
   non-git project roots just skip the worktree step).
2. **Project notes** - one ``<project>/.RUD/NOTES.md`` per project,
   served by ``GET/PUT /api/notes``.
3. **Claude pane** - launch a tmux + ``claude`` CLI in the task's
   worktree, automatically capture the Claude Code session UUID from
   ``~/.claude/projects/<encoded>/``, and let the UI resume any
   previously-captured session even after tmux is killed.

The only per-task editable template is ``PLAN.md``.
"""

from __future__ import annotations

import base64
import binascii
import hmac
import mimetypes
import os
import re
import shlex
import shutil
import subprocess
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from loom import agent_hooks
from loom import researcher_profile as researcher_profiles
from loom.web_jobs import (
    ARLoopManager,
    _ar_read_text,
    _rebuttal_resume_agent_watchers,
    _rebuttal_resume_delivery_watchers,
    _sweep_stale_review_runs,
)
from loom import routes_agent
from loom import routes_ar
from loom import routes_rebuttal
from loom import routes_review
from loom import routes_tmux
from loom.web_util import (
    _REVIEW_DEFAULT_RULES,
    _SLUG_RE,
    _json_bytes,
    _legacy_claude_session_name,
    _read_json,
    _safe_claude_session_name,
    _safe_static_path,
    _session_name_aliases,
    _text_bytes,
    _path_within,
    _session_name_from_tmux_target,
    _SESSION_ID_RE,
)
from loom.web_activity import (
    AgentActivityWatcher,
    TaskMonitorManager,
    _AGENT_WORKING_RE,
)
from loom.web_conversation import (
    _conversation_terminal_answer_keys,
    _conversation_terminal_question,
    _conversation_transcript_path,
    _parse_conversation_transcript,
    _session_last_active,
)
from loom import ar_task as ar
from loom import rebuttal_task as rebuttal
from loom.openclaw import OpenClawClient, OpenClawConfig, openclaw_status
from loom.paths import (
    AR_ROOT_ENV,
    bundled_skills_path,
    default_prompt_path,
    web_static_dir,
)
from loom.rud_task import (
    AGENT_CURSOR,
    AGENT_CLAUDE,
    AGENT_CODEX,
    RUD_DIR,
    PLAN,
    SKILLS_PATH_SEP,
    SUPPORTED_AGENTS,
    add_claude_session,
    agent_default_model,
    agent_label,
    agent_model_options,
    browse_task_dir,
    build_agent_command,
    create_task,
    delete_task,
    detect_and_persist_worktree,
    join_skills_paths,
    list_session_files,
    list_task_markdown_files,
    list_task_worktree_statuses,
    list_task_worktrees,
    list_tasks,
    list_worktree_candidates,
    load_skills_text,
    merge_worktree_to_base,
    normalize_agent,
    path_under_task,
    prepare_task_worktree_from,
    prefer_cursor_fast_model,
    push_worktree_branch,
    read_meta,
    read_markdown_asset,
    read_project_notes,
    read_task_markdown_file,
    read_task_text,
    read_template,
    rud_root,
    remove_task_worktree,
    reorder_tasks,
    rename_task_meta,
    session_id_from_path,
    split_skills_paths,
    task_root,
    task_worktree_diffs,
    task_worktree_path,
    update_meta,
    worktree_diff,
    worktree_status,
    write_project_notes,
    write_template,
)
from loom.tmux_util import (
    capture_pane,
    reap_orphaned_attaches,
    send_pane_key,
    send_pane_text,
    tmux_subprocess_env,
    validate_tmux_target,
)
from loom.web_projects import WebProjectRegistry

_STATIC_MIME: dict[str, str] = {
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".html": "text/html; charset=utf-8",
}


def _project_worktree_candidates(
    registry: WebProjectRegistry, root: Path, project_id: str
) -> list[dict[str, Any]]:
    preferred = registry.get_code_root(project_id)
    return list_worktree_candidates(
        root, [preferred] if preferred is not None else []
    )


# --- naming / filtering helpers --------------------------------------------


def _git_clone(repo_url: str, dest: Path, timeout: int = 900) -> tuple[bool, str]:
    """``git clone`` *repo_url* into *dest*. Returns ``(ok, error)``."""
    import shutil

    if not shutil.which("git"):
        return False, "git is not installed on the server"
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return False, str(exc)
    try:
        # ``--`` so a repo URL can never be misread as a git option.
        r = subprocess.run(
            ["git", "clone", "--", repo_url, str(dest)],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return False, f"git clone timed out after {timeout}s"
    except OSError as exc:
        return False, str(exc)
    if r.returncode != 0:
        return False, (r.stderr or r.stdout or "git clone failed").strip()[-2000:]
    return True, ""


def _launch_root_child_dirs(launch_root: Path, *, limit: int = 200) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    try:
        root = launch_root.resolve()
    except OSError:
        return out
    if not root.is_dir():
        return out
    try:
        for child in sorted(root.iterdir(), key=lambda p: p.name.lower()):
            if len(out) >= limit:
                break
            try:
                if not child.is_dir():
                    continue
            except OSError:
                continue
            if child.name.startswith("."):
                continue
            try:
                out.append({"name": child.name, "path": str(child.resolve())})
            except OSError:
                continue
    except OSError:
        return out
    return out


def _available_skill_options(
    default_skills: Path,
    project_root: Path | None = None,  # kept for call-site compatibility; unused
    *,
    limit: int = 500,
) -> list[dict[str, Any]]:
    """Return selectable skill markdown files for the web UI.

    Scope is intentionally limited to the bundled ``loom/skills``
    directory (plus the configured default skills file). We do **not** scan
    the user's project tree, so unrelated README/PLAN/etc. markdown never
    shows up in the Skills picker - only real skills files are selectable.
    """
    del project_root  # skills come only from the skills directory
    seen: set[Path] = set()
    options: list[dict[str, Any]] = []
    skills_root = bundled_skills_path().parent

    def add(path: Path) -> None:
        try:
            p = path.expanduser().resolve()
        except OSError:
            return
        if (
            len(options) >= limit
            or not p.is_file()
            or p.suffix.lower() != ".md"
            or p in seen
        ):
            return
        seen.add(p)
        # Always injected into every prompt; listing it as a choice would only
        # let someone send it twice.
        if p == default_prompt_path().resolve():
            return
        # The generated index of all skills is a map, not a skill.
        if p.name == "SKILLS.md" and p.parent == skills_root:
            return
        rel = ""
        try:
            rel = str(p.relative_to(skills_root))
        except ValueError:
            pass
        # The AR pipeline injects everything under skills/ar/ into its own
        # prompts - they are not choices (read them in the Factory instead).
        if rel.startswith("ar/") or rel.startswith("ar\\"):
            return
        # Inside a packaged skill directory only SKILL.md is the skill; its
        # PROMPT_TEMPLATE/EXAMPLE/SOURCE siblings are the skill's own reading
        # material and would inject as half a skill.
        if p.name != "SKILL.md" and (p.parent / "SKILL.md").is_file():
            return
        # Label by what you'd call the skill, not where it sits on disk:
        # dev/loom-hot-restart/SKILL.md -> loom-hot-restart,
        # remote_control/remote_control.md -> remote_control.
        if p.name == "SKILL.md":
            label = p.parent.name
        else:
            label = p.stem
        options.append({"label": label, "path": str(p)})

    add(default_skills)
    if skills_root.is_dir():
        for p in sorted(skills_root.rglob("*.md"), key=lambda x: str(x).lower()):
            add(p)
    # The default selection reads first; everything else alphabetically.
    try:
        default_resolved = str(default_skills.expanduser().resolve())
    except OSError:
        default_resolved = ""
    options.sort(key=lambda o: (o["path"] != default_resolved, o["label"].lower()))
    return options


def _skill_summary(path: Path, limit: int = 140) -> str:
    """A skill file's one-line pitch: its frontmatter ``description:`` when
    it has one, else the first substantive line after the frontmatter."""
    # The bundled personal skills file is per-user and skip-worktree: its real
    # first line differs from machine to machine and may hold private notes.
    # Pin a fixed label so neither the generated skills table nor the picker
    # ever embeds local personal content (and the freshness test stays stable
    # everywhere, not just on a clean checkout).
    try:
        if path.resolve() == bundled_skills_path().resolve():
            return "Personal skills"
    except OSError:
        pass
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return ""
    body_start = 0
    if lines and lines[0].strip() == "---":
        for i, line in enumerate(lines[1:], start=1):
            if line.strip() == "---":
                body_start = i + 1
                break
            if line.strip().lower().startswith("description:"):
                desc = line.split(":", 1)[1].strip().strip("\"'")
                if desc:
                    return desc[:limit]
    for line in lines[body_start:]:
        text = line.strip().lstrip("#").strip()
        if text and not text.startswith(("---", "<!--")):
            return text[:limit]
    return ""


# --- HTTP response helpers --------------------------------------------------


def _run_worktree_review(
    wt: Path, rules: str, skills: str, model: str = ""
) -> dict[str, Any]:
    """Bugbot-style review: run the logged-in host ``claude -p`` over a
    worktree's diff against plain-English rules (falling back to the task's
    skills). Returns ``{ok, review}`` markdown."""
    diff = worktree_diff(wt)
    files = diff.get("files", [])
    if not files:
        return {"ok": True, "review": "✅ No changes to review in this worktree.", "files": 0}
    parts: list[str] = []
    total = 0
    for f in files:
        patch = f.get("patch") or ""
        if not patch:
            continue
        parts.append(patch)
        total += len(patch)
        if total > 60000:
            parts.append("\n... (diff truncated for review) ...\n")
            break
    diff_text = "\n".join(parts).strip() or "(no textual diff)"
    rules_block = (rules or "").strip() or (skills or "").strip() or "(use general best practices)"
    prompt = (
        "You are a strict senior code reviewer (think Bugbot). Review the DIFF "
        "for this change. Only flag real problems. For each finding, output a "
        "markdown bullet exactly like:\n"
        "  - `path:line` - **[severity]** what's wrong -> concrete fix\n"
        "Always cover: correctness/bugs, security (secrets, injection), leftover "
        "debug/TODO/dead code, missing or weak tests, and any RULES violations. "
        "If everything looks good, reply with exactly: `✅ No issues found.` "
        "Keep it concise.\n\n"
        f"RULES (plain-English, from the user / task skills):\n{rules_block}\n\n"
        f"DEFAULT CHECKLIST:\n{_REVIEW_DEFAULT_RULES}\n\n"
        f"DIFF:\n{diff_text}"
    )
    cmd = ["claude", "-p", prompt, "--dangerously-skip-permissions"]
    if model:
        cmd += ["--model", model]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "review timed out"}
    except OSError as exc:
        return {"ok": False, "error": str(exc)}
    text = (proc.stdout or "").strip()
    if not text:
        return {
            "ok": False,
            "error": "empty response from claude",
            "stderr": (proc.stderr or "")[-500:],
        }
    return {"ok": True, "review": text, "files": len(files)}


# --- Claude prompt builder --------------------------------------------------


def _build_ar_prompt(
    project_root: Path,
    slug: str,
    meta: Any,
    task_dir: Path,
    skills: str,
) -> str:
    """Pane prompt for an AR task, chosen by the role recorded in ar.json.

    A paper task in the middle of the loop gets that round's author prompt, so
    pressing the pane's prompt button resends exactly what the driver would -
    useful after restarting a pane mid-round.
    """
    state = ar.read_ar_state(project_root, slug)
    if not state:
        # A legacy `aris` task, or one whose ar.json has not been written yet.
        state = ar.new_studio_state()
    if ar.is_paper(state):
        paper_dir = ar.paper_root(project_root, slug)
        stage = str(state.get("stage") or ar.STAGE_DRAFT)
        n = ar.current_round(state)
        if stage == ar.STAGE_LOOP and n >= 1:
            review_path = ((ar.round_record(state, n - 1) or {}).get("review") or {}).get("path", "")
            review_text = _ar_read_text(Path(review_path)) if review_path else ""
            return ar.author_round_prompt(
                task_dir, paper_dir, state, n, review_text=review_text
            )
        return ar.author_draft_prompt(task_dir, paper_dir, state)

    base = ar.default_prompt_block() + "\n" + ar.studio_prompt(
        task_dir, state, meta.general_goal
    )
    # Only skills the user deliberately picked. A task that never chose any
    # carries the bundled default, and pasting an unrelated host runbook into a
    # research prompt is noise at best - and leaks whatever happens to be in
    # that file at worst.
    chosen = (meta.skills_path or "").strip()
    if skills.strip() and chosen and chosen != str(bundled_skills_path()):
        base += f"\nDomain skills selected for this task:\n---\n{skills}\n---\n"
    return base


def _default_prompt_text(limit: int = 20000) -> str:
    """The always-on floor under every task prompt; empty if the file is gone."""
    try:
        return default_prompt_path().read_text(encoding="utf-8", errors="replace")[:limit]
    except OSError:
        return ""


def _project_memory_text(project_root: Path, limit: int = 4000) -> tuple[Path, str]:
    """The project's accumulated lessons, newest-at-the-bottom tail of them.

    Capped from the end because the protocol appends: the most recent
    lessons are the ones a new task most needs.
    """
    path = project_root / RUD_DIR / "MEMORY.md"
    try:
        text = path.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return path, ""
    return path, text[-limit:]


def _build_claude_prompt(
    project_root: Path,
    slug: str,
    default_skills: Path | None = None,
) -> str:
    meta = read_meta(project_root, slug)
    if not meta:
        return ""
    td = task_root(project_root, slug)
    wt = task_worktree_path(project_root, slug)
    wt_line = f"Worktree (branch {meta.branch or '(unset)'}): {wt}" if wt else "Worktree: (none)"
    # meta.skills_path may name several ;-joined skills files - inject them all.
    skills = load_skills_text(meta.skills_path, default_skills)
    state_doc = PLAN
    plan_path = td / state_doc
    if ar.is_ar_kind(meta.kind):
        return _build_ar_prompt(project_root, slug, meta, td, skills)
    memory_path, memory = _project_memory_text(project_root)
    return f"""You are running Loom's {agent_label(meta.agent)} pane for this task.

You start in this task's work directory (your git worktree is a subdirectory here - cd into it to touch code):
{td / "work"}

General goal:
{meta.general_goal}

{wt_line}

Default prompt (always active, before any selected skills):
---
{_default_prompt_text() or "(missing)"}
---

Project memory ({memory_path} - append lessons here as the default prompt describes):
---
{memory or "(empty - create it when you have a lesson worth keeping)"}
---

Default skills from:
{meta.skills_path or "(bundled default)"}

Default skills:
---
{skills or "(none)"}
---

RUD workflow:
1. Start from the General goal above and run a short deep-interview. Ask
   one high-leverage question at a time about scope, constraints,
   acceptance, tests, risks, non-goals, and available worktrees.
2. When the interview has enough information, write or overwrite
   {plan_path} with exactly this shape:
   - Under the title, ONE paragraph: what success looks like, plus the
     decisions and constraints the interview settled.
   - "## What we have done" - empty at this point.
   - "## Results" - an EMPTY table whose rows already name every number
     this task must produce (columns like metric / target / value).
     Cells get filled with real measured values during execution, never
     invented ones.
   - "## Future to do" - the executable checklist, in order.
   - "## Progress Log" - empty.
   Do not leave interview notes only in chat; the result of the interview
   must be captured DIRECTLY in {plan_path}.
3. After {state_doc} is solid, tell the user it is ready to run. The user can
   click RUD's "Run /goal" button (or type /goal) to execute {state_doc}.
4. While executing: move finished checklist items from "Future to do"
   into "What we have done" (one concise line each), fill "Results"
   cells as real numbers land, keep "Future to do" pointing at what is
   actually next, and append dated one-liners to "Progress Log". Remove
   obsolete noise, but preserve unrelated prior sections.

Behavioural constraints:
- {state_doc} is the ONLY task-state file. Do not create INTERVIEW.md,
  TODO.md, PROGRESS.md, NOTES.md, or any other scattered status files in
  the task directory or the repo.
- Project-scoped scratch lives in the project's NOTES.md at .RUD/NOTES.md
  (handled by the user via the web UI), not inside the worktree.

Begin by reading {plan_path}, then either ask the first interview
question or, if {state_doc} is already detailed enough, acknowledge that it is
ready and wait for the user to run ``/goal``.
"""


def _task_pane_cwd(project_root: Path, slug: str, meta=None) -> Path:
    """Directory the agent pane launches in - and where Claude Code stores the
    session transcript (``~/.claude/projects/<encoded-cwd>/``).

    All tasks launch in the task's ``work/`` dir: a stable base that holds every
    git worktree (the agent cd's into the relevant one to run git / code), so
    the session-transcript location stays consistent no matter how many
    worktrees a task has. Falls back to the primary worktree / task dir only if
    ``work/`` can't be created.
    """
    td = task_root(project_root, slug)
    wd = td / "work"
    try:
        wd.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    if wd.is_dir():
        return wd
    wt = task_worktree_path(project_root, slug)
    return wt if wt is not None else td


# --- Claude tmux registry ---------------------------------------------------


class ClaudeRegistry:
    """Manage tmux + agent CLI panes per (project, task).

    A pane's lifecycle:
    1. ``start`` opens a tmux session and runs the selected agent in the task
       worktree; exiting the agent returns the pane to a login shell.
    2. A background watcher records the new CLI session ID so the UI can
       offer Resume.
    3. ``stop`` kills the tmux session but leaves the session UUIDs in
       metadata so they remain resumable from the CLI.
    4. ``resume`` re-launches the agent with ``--resume <uuid>`` in a tmux
       pane.  Useful when the original tmux was killed but the session
       transcript on disk is still good.
    """

    @staticmethod
    def _launch_agent_in_pane(target: str, cwd: Path, argv: list[str]) -> tuple[bool, str]:
        """Run the agent in the pane, returning to a login shell when it exits."""
        env = tmux_subprocess_env()
        executable = shutil.which(argv[0], path=env.get("PATH"))
        if not executable:
            return False, f"{argv[0]} not on PATH"
        direct_argv = [executable, *argv[1:]]
        login_shell = (env.get("SHELL") or "/bin/bash").strip()
        if not Path(login_shell).is_absolute():
            login_shell = shutil.which(login_shell, path=env.get("PATH")) or "/bin/bash"
        pane_command = (
            f"{shlex.join(direct_argv)}; agent_status=$?; "
            "stty sane 2>/dev/null || true; "
            "printf '\\nAgent exited (%s). Returned to shell.\\n' \"$agent_status\"; "
            f"exec {shlex.quote(login_shell)} -l"
        )
        try:
            keep = subprocess.run(
                ["tmux", "set-option", "-w", "-t", target, "remain-on-exit", "on"],
                capture_output=True,
                text=True,
                env=env,
                timeout=5,
            )
            if keep.returncode != 0:
                return False, (keep.stderr or keep.stdout or "could not configure tmux pane").strip()
            launched = subprocess.run(
                [
                    "tmux",
                    "respawn-pane",
                    "-k",
                    "-t",
                    target,
                    "-c",
                    str(cwd),
                    "-e",
                    f"PATH={env.get('PATH', '')}",
                    pane_command,
                ],
                capture_output=True,
                text=True,
                env=env,
                timeout=8,
            )
        except FileNotFoundError:
            return False, "tmux not on PATH"
        except (OSError, subprocess.TimeoutExpired) as exc:
            return False, str(exc)
        if launched.returncode != 0:
            return False, (launched.stderr or launched.stdout or "could not launch agent").strip()
        return True, ""

    def start(
        self,
        project_root: Path,
        project_id: str,
        slug: str,
        *,
        resume_session_id: str = "",
        default_skills: Path | None = None,
        env: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        meta = read_meta(project_root, slug)
        if not meta:
            return {"ok": False, "error": "Task not found"}
        td = task_root(project_root, slug)
        if not td.is_dir():
            return {"ok": False, "error": "Task directory missing"}

        # Run the agent inside the worktree when we have one - that's where
        # the user will eventually want /goal (or codex's equivalent) to act.
        cwd = _task_pane_cwd(project_root, slug, meta)

        agent = normalize_agent(meta.agent)
        selected_model = meta.interview_model or agent_default_model(agent)
        if agent == AGENT_CURSOR:
            fast_model = prefer_cursor_fast_model(selected_model)
            if fast_model != selected_model:
                update_meta(project_root, slug, interview_model=fast_model)
            selected_model = fast_model

        def watch_cursor_ready() -> None:
            if agent == AGENT_CURSOR:
                threading.Thread(
                    target=self._wait_for_claude_ready,
                    args=(target, 45.0),
                    daemon=True,
                ).start()

        session_name = self._live_or_default_session(
            project_id, slug, agent, meta.tmux_interview_target or ""
        )
        target = f"{session_name}:0.0"
        existing_ids = {
            sid
            for p in list_session_files(cwd, agent)
            if (sid := session_id_from_path(p, agent))
        }
        if self._tmux_session_exists(session_name):
            pane_command = self._pane_current_command(target)
            pane_dead = self._pane_is_dead(target)
            if resume_session_id:
                if not pane_dead and not self._pane_is_idle_shell(pane_command):
                    return {
                        "ok": False,
                        "error": (
                            "The tmux pane is still running a command. Stop it before "
                            "resuming another session."
                        ),
                        "target": target,
                        "session": session_name,
                        "pane_command": pane_command,
                    }
                agent_cmd = build_agent_command(
                    agent,
                    model=selected_model,
                    resume_session_id=resume_session_id,
                )
                ok, error = self._launch_agent_in_pane(target, cwd, agent_cmd)
                if not ok:
                    return {"ok": False, "error": error, "target": target}
                watch_cursor_ready()
                update_meta(project_root, slug, tmux_interview_target=target)
                add_claude_session(project_root, slug, resume_session_id)
                threading.Thread(
                    target=self._watch_for_session_id,
                    args=(project_root, slug, cwd, agent, existing_ids),
                    daemon=True,
                ).start()
                return {
                    "ok": True,
                    "target": target,
                    "session": session_name,
                    "cwd": str(cwd),
                    "agent": agent,
                    "resumed_session_id": resume_session_id,
                    "already_running": False,
                    "reused_tmux": True,
                    "prompt_pending": False,
                    "pane_command": pane_command,
                }
            if pane_dead or self._pane_is_idle_shell(pane_command):
                # Reuse old shell-backed sessions and new retained dead panes
                # by replacing the pane process directly with the agent.
                agent_cmd = build_agent_command(
                    agent,
                    model=selected_model,
                )
                ok, error = self._launch_agent_in_pane(target, cwd, agent_cmd)
                if not ok:
                    return {"ok": False, "error": error, "target": target}
                watch_cursor_ready()
                threading.Thread(
                    target=self._watch_for_session_id,
                    args=(project_root, slug, cwd, agent, existing_ids),
                    daemon=True,
                ).start()
                update_meta(project_root, slug, tmux_interview_target=target)
                return {
                    "ok": True,
                    "target": target,
                    "session": session_name,
                    "cwd": str(cwd),
                    "agent": agent,
                    "already_running": False,
                    "reused_tmux": True,
                    "prompt_pending": True,
                    "pane_command": pane_command,
                }
            watch_cursor_ready()
            update_meta(project_root, slug, tmux_interview_target=target)
            return {
                "ok": True,
                "target": target,
                "session": session_name,
                "cwd": str(cwd),
                "agent": agent,
                "already_running": True,
            }

        # Name the task in the pane's environment. The agent's stop hook
        # inherits it and can say exactly which task just finished - its own
        # event only reports the repository root, which is the same for every
        # task in a project.
        pane_env = {"LOOM_TASK_ID": f"{project_id}/{slug}", **(env or {})}
        env_args: list[str] = []
        for key, value in pane_env.items():
            env_args += ["-e", f"{key}={value}"]
        try:
            subprocess.run(
                [
                    "tmux",
                    "new-session",
                    "-d",
                    "-s",
                    session_name,
                    "-x",
                    "240",
                    "-y",
                    "64",
                    "-c",
                    str(cwd),
                    *env_args,
                ],
                capture_output=True,
                text=True,
                env=tmux_subprocess_env(),
                check=True,
                timeout=8,
            )
        except FileNotFoundError:
            return {"ok": False, "error": "tmux not on PATH"}
        except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
            return {"ok": False, "error": str(e)}

        agent_cmd = build_agent_command(
            agent,
            model=selected_model,
            resume_session_id=resume_session_id,
        )
        ok, error = self._launch_agent_in_pane(target, cwd, agent_cmd)
        if not ok:
            self._kill_tmux_session(session_name)
            return {"ok": False, "error": error}
        watch_cursor_ready()

        update_meta(project_root, slug, tmux_interview_target=target)
        if resume_session_id:
            add_claude_session(project_root, slug, resume_session_id)
        threading.Thread(
            target=self._watch_for_session_id,
            args=(project_root, slug, cwd, agent, existing_ids),
            daemon=True,
        ).start()
        return {
            "ok": True,
            "target": target,
            "session": session_name,
            "cwd": str(cwd),
            "agent": agent,
            "resumed_session_id": resume_session_id or None,
            "already_running": False,
            "prompt_pending": not bool(resume_session_id),
        }

    def paste_prompt(
        self,
        project_root: Path,
        project_id: str,
        slug: str,
        *,
        default_skills: Path | None = None,
    ) -> dict[str, Any]:
        """Paste the task's deep-interview prompt into the running agent pane."""
        meta = read_meta(project_root, slug)
        if not meta:
            return {"ok": False, "error": "Task not found"}
        agent = normalize_agent(meta.agent)
        session_name = self._live_or_default_session(
            project_id, slug, agent, meta.tmux_interview_target or ""
        )
        if not self._tmux_session_exists(session_name):
            return {"ok": False, "error": "Start the agent pane first"}
        target = (meta.tmux_interview_target or "").strip() or f"{session_name}:0.0"
        # If the recorded target points at a dead session (while a live pane
        # exists under another name), paste into the live one instead.
        if not self._tmux_session_exists(_session_name_from_tmux_target(target)):
            target = f"{session_name}:0.0"
        if self._pane_is_dead(target):
            return {"ok": False, "error": "Agent has exited; start the pane again", "target": target}
        if agent == AGENT_CURSOR:
            self._wait_for_claude_ready(target, timeout=15.0)
        update_meta(project_root, slug, tmux_interview_target=target)
        return self._paste_prompt_to_target(project_root, slug, target, default_skills=default_skills)

    def stop(self, project_root: Path, project_id: str, slug: str) -> dict[str, Any]:
        meta = read_meta(project_root, slug)
        agent = normalize_agent(meta.agent) if meta else AGENT_CURSOR
        meta_target = (meta.tmux_interview_target or "") if meta else ""
        session_name = self._live_or_default_session(project_id, slug, agent, meta_target)
        stopped, msg = self._kill_tmux_session(session_name)
        # Clean up every other variant for this task: the other agent's session
        # (if the user flipped agents), the old "interview" pane name, and the
        # legacy "claudeloop-" brand from before the rename.
        for alias in _session_name_aliases(project_id, slug):
            if alias != session_name:
                self._kill_tmux_session(alias)
        update_meta(project_root, slug, tmux_interview_target="")
        return {
            "ok": True,
            "tmux_stopped": stopped,
            "tmux_message": msg,
            "tmux_session": session_name,
        }

    # --- helpers ---

    def _tmux_session_exists(self, session_name: str) -> bool:
        try:
            r = subprocess.run(
                ["tmux", "has-session", "-t", session_name],
                capture_output=True,
                text=True,
                env=tmux_subprocess_env(),
                timeout=5,
            )
        except (OSError, subprocess.TimeoutExpired):
            return False
        return r.returncode == 0

    def _live_or_default_session(
        self, project_id: str, slug: str, agent: str = AGENT_CURSOR, meta_target: str = ""
    ) -> str:
        """Session name to operate on: an already-running pane for this task
        (current ``loom-`` or legacy ``claudeloop-`` brand), else the live
        session recorded in task meta (its name embeds the project id from
        creation time, and the registry can re-issue ids - e.g. after the
        claudeloop->loom rename - so it may differ from today's derived name),
        otherwise the new ``loom-`` name for a fresh start."""
        primary = _safe_claude_session_name(project_id, slug, agent)
        if self._tmux_session_exists(primary):
            return primary
        legacy = _legacy_claude_session_name(project_id, slug, agent)
        if legacy != primary and self._tmux_session_exists(legacy):
            return legacy
        recorded = _session_name_from_tmux_target(meta_target)
        if recorded and recorded not in (primary, legacy) and self._tmux_session_exists(recorded):
            return recorded
        return primary

    def _pane_current_command(self, target: str) -> str:
        try:
            r = subprocess.run(
                ["tmux", "display-message", "-p", "-t", target, "#{pane_current_command}"],
                capture_output=True,
                text=True,
                env=tmux_subprocess_env(),
                timeout=5,
            )
        except (OSError, subprocess.TimeoutExpired):
            return ""
        if r.returncode != 0:
            return ""
        return (r.stdout or "").strip()

    def _pane_is_dead(self, target: str) -> bool:
        try:
            r = subprocess.run(
                ["tmux", "display-message", "-p", "-t", target, "#{pane_dead}"],
                capture_output=True,
                text=True,
                env=tmux_subprocess_env(),
                timeout=5,
            )
        except (OSError, subprocess.TimeoutExpired):
            return False
        return r.returncode == 0 and (r.stdout or "").strip() == "1"

    @staticmethod
    def _pane_is_idle_shell(command: str) -> bool:
        cmd = Path((command or "").strip()).name.lower()
        return cmd in {"", "bash", "dash", "fish", "sh", "tmux", "zsh"}

    def _pane_has_agent_process(self, target: str, agent: str) -> bool:
        try:
            result = subprocess.run(
                ["tmux", "display-message", "-p", "-t", target, "#{pane_pid}"],
                capture_output=True,
                text=True,
                env=tmux_subprocess_env(),
                timeout=5,
            )
            pane_pid = int((result.stdout or "").strip()) if result.returncode == 0 else 0
        except (OSError, ValueError, subprocess.TimeoutExpired):
            return False
        expected = {
            AGENT_CURSOR: {"agent", "cursor-agent"},
            AGENT_CLAUDE: {"claude"},
            AGENT_CODEX: {"codex"},
        }.get(normalize_agent(agent), {normalize_agent(agent)})
        pending = [pane_pid] if pane_pid > 0 else []
        seen: set[int] = set()
        while pending and len(seen) < 256:
            pid = pending.pop()
            if pid in seen:
                continue
            seen.add(pid)
            try:
                raw = (Path("/proc") / str(pid) / "cmdline").read_bytes()
                argv = [
                    part.decode("utf-8", errors="replace")
                    for part in raw.split(b"\0")
                    if part
                ]
                executable = Path(argv[0]).name.lower() if argv else ""
                if executable in expected:
                    return True
                children_path = (
                    Path("/proc") / str(pid) / "task" / str(pid) / "children"
                )
                if children_path.is_file():
                    pending.extend(
                        int(child)
                        for child in children_path.read_text().split()
                        if child.isdigit()
                    )
            except OSError:
                continue
        return False

    def session_status(
        self, project_id: str, slug: str, agent: str = AGENT_CURSOR, meta_target: str = ""
    ) -> dict[str, Any]:
        session_name = self._live_or_default_session(project_id, slug, agent, meta_target)
        target = f"{session_name}:0.0"
        tmux_alive = self._tmux_session_exists(session_name)
        pane_command = self._pane_current_command(target) if tmux_alive else ""
        pane_dead = self._pane_is_dead(target) if tmux_alive else False
        agent_process = (
            self._pane_has_agent_process(target, agent)
            if tmux_alive and not pane_dead and self._pane_is_idle_shell(pane_command)
            else False
        )
        return {
            "session": session_name,
            "target": target,
            "tmux_alive": tmux_alive,
            "pane_command": pane_command,
            "pane_dead": pane_dead,
            "agent_running": (
                tmux_alive
                and not pane_dead
                and (
                    not self._pane_is_idle_shell(pane_command)
                    or agent_process
                )
            ),
            "agent": normalize_agent(agent),
        }

    def _kill_tmux_session(self, session_name: str) -> tuple[bool, str]:
        try:
            r = subprocess.run(
                ["tmux", "kill-session", "-t", session_name],
                capture_output=True,
                text=True,
                env=tmux_subprocess_env(),
                timeout=8,
            )
        except FileNotFoundError:
            return False, "tmux not on PATH"
        except subprocess.TimeoutExpired:
            return False, "tmux kill timed out"
        if r.returncode == 0:
            return True, "tmux session killed"
        return False, (r.stderr or r.stdout or "tmux session not found").strip()

    def wait_until_ready(self, target: str, timeout: float = 15.0) -> None:
        """Block until the agent CLI in *target* is ready to receive a prompt."""
        self._wait_for_claude_ready(target, timeout=timeout)

    def target_alive(self, target: str) -> bool:
        """True when *target* still names a live tmux session."""
        if not target.strip():
            return False
        return self._tmux_session_exists(_session_name_from_tmux_target(target))

    def _wait_for_claude_ready(self, target: str, timeout: float = 45.0) -> None:
        deadline = time.time() + timeout
        markers = ("\u276f", "\u256d", "tip:", "tips:", "/help", "cursor agent")
        trust_answered = False
        while time.time() < deadline:
            ok, text = capture_pane(target, 80)
            lower = text.lower() if ok else ""
            # Cursor Agent asks once per new workspace. Loom creates isolated
            # task workdirs, so accept this prompt automatically; otherwise
            # the subsequent deep-interview paste lands on the trust screen.
            # Answer it ONCE: the dialog's text lingers in the captured
            # scrollback after it is dismissed, and re-keying Enter on every
            # sighting typed a string of blank sends into the fresh composer.
            if not trust_answered and "trust this workspace" in lower:
                # Enter activates the preselected "Trust" row without leaking
                # the shortcut letter `a` into the first chat prompt.
                send_pane_key(target, "Enter")
                trust_answered = True
                time.sleep(2)
                continue
            if ok and any(m in lower for m in markers):
                time.sleep(2)
                return
            time.sleep(2)

    def _watch_for_session_id(
        self,
        project_root: Path,
        slug: str,
        cwd: Path,
        agent: str,
        existing_ids: set[str],
    ) -> None:
        """Poll the agent's session dir for a freshly-written session file."""
        deadline = time.time() + 90.0
        while time.time() < deadline:
            for p in list_session_files(cwd, agent):
                sid = session_id_from_path(p, agent)
                if sid and sid not in existing_ids:
                    add_claude_session(project_root, slug, sid)
                    return
            time.sleep(2)

    def _paste_prompt_to_target(
        self,
        project_root: Path,
        slug: str,
        target: str,
        *,
        default_skills: Path | None = None,
    ) -> dict[str, Any]:
        prompt = _build_claude_prompt(project_root, slug, default_skills=default_skills)
        if not prompt:
            return {"ok": False, "error": "empty prompt", "target": target}
        ok, err = send_pane_text(target, prompt, submit=True)
        if ok:
            return {
                "ok": True,
                "target": target,
                "prompt_chars": len(prompt),
                "has_skills": "Default skills:\n---\n(none)" not in prompt,
            }
        return {"ok": False, "error": err or "paste failed", "target": target}


# (run monitors + activity watcher live in loom/web_activity.py)

# (factory background jobs + loop drivers live in loom/web_jobs.py)

# --- HTTP handler factory ---------------------------------------------------


_TERMINAL_STREAM_LEASE_SECONDS = 75.0


class _TerminalStreamRegistry:
    """Route browser terminal input back through its own tmux attach PTY."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._masters: dict[str, int] = {}
        self._procs: dict[str, object] = {}
        self._last_seen: dict[str, float] = {}

    def register(self, master: int, proc: object = None) -> str:
        stream_id = uuid.uuid4().hex
        with self._lock:
            self._masters[stream_id] = master
            self._last_seen[stream_id] = time.monotonic()
            if proc is not None:
                self._procs[stream_id] = proc
        return stream_id

    def unregister(self, stream_id: str, master: int) -> None:
        with self._lock:
            if self._masters.get(stream_id) == master:
                self._masters.pop(stream_id, None)
                self._procs.pop(stream_id, None)
                self._last_seen.pop(stream_id, None)

    def touch(self, stream_id: str) -> tuple[bool, str]:
        """Renew a browser-owned stream lease."""
        if not re.fullmatch(r"[0-9a-f]{32}", stream_id or ""):
            return False, "invalid terminal stream"
        with self._lock:
            if stream_id not in self._masters:
                return False, "terminal stream is not active"
            self._last_seen[stream_id] = time.monotonic()
        return True, ""

    def lease_active(
        self,
        stream_id: str,
        lease_seconds: float = _TERMINAL_STREAM_LEASE_SECONDS,
    ) -> bool:
        """Return whether the real browser has renewed this stream recently."""
        with self._lock:
            last_seen = self._last_seen.get(stream_id)
        return (
            last_seen is not None
            and time.monotonic() - last_seen <= lease_seconds
        )

    def close(self, stream_id: str) -> tuple[bool, str]:
        """End a stream because its client said so.

        A client that goes away is supposed to close its connection, and the
        streaming loop notices and tears the attach down. Through a proxy that
        holds the upstream leg open, that close never arrives, and the attach
        lives on as a tmux client - pinning the window size and accumulating
        one per terminal ever opened. Letting the client ask directly costs
        nothing and does not depend on the socket being honest.
        """
        if not re.fullmatch(r"[0-9a-f]{32}", stream_id or ""):
            return False, "invalid terminal stream"
        with self._lock:
            proc = self._procs.pop(stream_id, None)
            self._masters.pop(stream_id, None)
            self._last_seen.pop(stream_id, None)
        if proc is None:
            return True, ""  # already gone; nothing to do
        try:
            proc.terminate()
        except Exception:
            pass
        return True, ""

    def write(self, stream_id: str, text: str) -> tuple[bool, str]:
        if not re.fullmatch(r"[0-9a-f]{32}", stream_id):
            return False, "invalid terminal stream"
        data = text.encode("utf-8", errors="surrogatepass")
        if len(data) > 64 * 1024:
            return False, "terminal input too large"
        with self._lock:
            master = self._masters.get(stream_id)
            if master is None:
                return False, "terminal stream is not active"
            self._last_seen[stream_id] = time.monotonic()
            try:
                view = memoryview(data)
                while view:
                    written = os.write(master, view)
                    if written <= 0:
                        return False, "terminal stream closed"
                    view = view[written:]
            except OSError as exc:
                self._masters.pop(stream_id, None)
                return False, str(exc)
        return True, ""


def make_handler(
    project_registry: WebProjectRegistry,
    launch_root: Path,
    default_skills: Path,
    claude_registry: ClaudeRegistry,
    openclaw_client: OpenClawClient,
    auth_token: str = "",
    *,
    multi_project_workspace: bool = False,
    monitor_manager: "TaskMonitorManager | None" = None,
    ar_manager: "ARLoopManager | None" = None,
    activity_watcher: "AgentActivityWatcher | None" = None,
) -> type[BaseHTTPRequestHandler]:
    static_root = web_static_dir().resolve()
    required_token = auth_token.strip()
    pr = project_registry
    launch_root_resolved = launch_root.resolve()
    multi_ws = multi_project_workspace
    monitor_manager = monitor_manager or TaskMonitorManager(openclaw_client)
    ar_manager = ar_manager or ARLoopManager(
        openclaw_client, claude_registry, default_skills
    )
    if activity_watcher is None:
        activity_watcher = AgentActivityWatcher(pr)
        activity_watcher.start()
    terminal_streams = _TerminalStreamRegistry()

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt: str, *args: Any) -> None:
            print(f"[web] {self.address_string()} - {fmt % args}", flush=True)

        def _send(self, status: int, body: bytes, headers: list[tuple[str, str]]) -> None:
            self.send_response(status)
            for k, v in headers:
                self.send_header(k, v)
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(body)

        @staticmethod
        def _kill_pty(proc, master) -> None:
            """Detach the tmux attach client + close its PTY (terminal stream)."""
            try:
                proc.terminate()
            except Exception:
                pass
            try:
                proc.wait(timeout=2)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
            try:
                os.close(master)
            except Exception:
                pass

        def _resolve_scope(self, parsed) -> tuple[Path | None, str | None]:
            qs = parse_qs(parsed.query or "")
            qpid = (qs.get("project") or [""])[0].strip()
            hp = (self.headers.get("X-Loom-Project") or "").strip()
            pid = qpid or hp or pr.default_project_id
            if not pid:
                return None, None
            pth = pr.get_path(pid)
            if pth is None:
                return None, None
            return pth, pid

        def _bad_project(self) -> None:
            st, b, h = _json_bytes(
                {"error": "unknown or invalid project; pass ?project=<id> or header X-Loom-Project"},
                400,
            )
            self._send(st, b, h)

        # --- AR helpers ---

        def _is_authorized(self) -> bool:
            if not required_token:
                return True
            raw = self.headers.get("Authorization", "").strip()
            if raw.lower().startswith("bearer "):
                presented = raw[7:].strip()
            elif raw.lower().startswith("basic "):
                try:
                    decoded = base64.b64decode(raw[6:].strip()).decode("utf-8")
                except (binascii.Error, ValueError, UnicodeDecodeError):
                    return False
                presented = decoded.partition(":")[2]
            else:
                return False
            if hmac.compare_digest(presented, required_token):
                return True
            # Bots hold a narrower credential: it opens the MCP endpoint
            # (and its manifest) and nothing else.
            return routes_agent.agent_token_allows(presented, urlparse(self.path).path)

        def _require_auth(self) -> bool:
            if self._is_authorized():
                return True
            body = b"authentication required\n"
            self.send_response(401)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("WWW-Authenticate", 'Basic realm="Loom"')
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(body)
            return False

        def _agent_finished(self, body: dict[str, Any]) -> None:
            """Handle an agent's own report that its turn ended.

            Authorised by the hook token rather than the web session, and
            deliberately loopback-only: the caller is a local process on this
            host, so nothing about it should be reachable from the network.
            """
            client = (self.client_address or ("",))[0]
            token = self.headers.get("X-Loom-Hook-Token", "")
            expected = agent_hooks.hook_token()
            if client not in ("127.0.0.1", "::1") or not expected or not hmac.compare_digest(token, expected):
                st, b, h = _json_bytes({"ok": False}, status=403)
                self._send(st, b, h)
                return
            hit = activity_watcher.report_finished(
                str(body.get("cwd", "")), str(body.get("task", ""))
            )
            st, b, h = _json_bytes({"ok": bool(hit), "task": hit[1] if hit else ""})
            self._send(st, b, h)

        def _claude_session_summary(self, project_id: str, slug: str, meta) -> dict[str, Any]:
            agent = normalize_agent(meta.agent)
            root = pr.get_path(project_id)
            # A session can live under the current pane cwd (work/) OR under any
            # worktree where a pane was historically launched. Scan them all so
            # Resume finds every session this task ever spawned.
            candidates: list[Path] = []
            for c in (
                _task_pane_cwd(root, slug, meta),
                *list_task_worktrees(root, slug),
                task_root(root, slug),
            ):
                if c and c not in candidates:
                    candidates.append(c)
            files_by_id: dict[str, dict[str, Any]] = {}
            for cwd in candidates:
                for p in list_session_files(cwd, agent):
                    sid = session_id_from_path(p, agent)
                    if not sid:
                        continue
                    try:
                        stat = p.stat()
                    except OSError:
                        continue
                    prev = files_by_id.get(sid)
                    if prev is None or stat.st_mtime >= prev.get("mtime", 0.0):
                        files_by_id[sid] = {
                            "id": sid,
                            "path": str(p),
                            "mtime": stat.st_mtime,
                            "size": stat.st_size,
                        }
            # The conversation feed reads the first of these with a transcript,
            # so the order has to be when each session last moved.
            for info in files_by_id.values():
                info["mtime"] = _session_last_active(info, agent)
            # Preserve task-meta order (history of who-was-spawned-when)
            # but enrich with on-disk info.
            ordered = []
            seen: set[str] = set()
            for sid in meta.claude_session_ids:
                if sid in files_by_id:
                    ordered.append(files_by_id[sid])
                else:
                    ordered.append({"id": sid, "path": "", "mtime": 0.0, "size": 0})
                seen.add(sid)
            for sid, info in files_by_id.items():
                if sid not in seen:
                    ordered.append(info)
            ordered.sort(key=lambda x: x.get("mtime", 0.0), reverse=True)
            live = claude_registry.session_status(
                project_id, slug, agent, meta.tmux_interview_target or ""
            )
            return {
                "agent": agent,
                "agent_label": agent_label(agent),
                "tracked": [sid for sid in meta.claude_session_ids],
                "sessions": ordered,
                "tmux_alive": live["tmux_alive"],
                "pane_command": live["pane_command"],
                "agent_running": live["agent_running"],
                "tmux_session": live["session"],
                "tmux_target": meta.tmux_interview_target or "",
                "claude_cwd": str(cwd),
            }

        def do_GET(self) -> None:  # noqa: N802
            if not self._require_auth():
                return
            parsed = urlparse(self.path)
            path = parsed.path

            # Dedicated entry documents share the same authenticated API and
            # static assets while presenting one focused workflow.
            if path in (
                "/",
                "/index.html",
                "/factory",
                "/factory.html",
                "/paper-factory",
                "/paper-factory.html",
                "/review-factory",
                "/review-factory.html",
                "/rebuttal-factory",
                "/rebuttal-factory.html",
                "/terminal",
                "/terminal.html",
            ):
                if path.startswith("/terminal"):
                    # The Agent Terminal as its own page: the factory pages
                    # iframe it to reuse the exact attach/input protocol.
                    name = "terminal.html"
                elif path.startswith("/rebuttal-factory"):
                    name = "rebuttal_factory.html"
                elif path.startswith("/review-factory"):
                    name = "review_factory.html"
                elif path.startswith("/paper-factory"):
                    name = "factory.html"
                elif path.startswith("/factory"):
                    # The Research Factory front door: pick a line to walk.
                    name = "research_factory.html"
                else:
                    name = "index.html"
                idx = static_root / name
                if not idx.is_file():
                    st, b, h = _text_bytes(f"missing {name}", 500)
                    self._send(st, b, h)
                    return
                st, b, h = _text_bytes(
                    idx.read_text(encoding="utf-8"),
                    content_type="text/html; charset=utf-8",
                )
                # Never let the browser reuse a stale index.html - it
                # references the versioned app.css/app.js, so the entry
                # document must always be fresh.
                h.append(("Cache-Control", "no-store, must-revalidate"))
                self._send(st, b, h)
                return

            if path.startswith("/static/"):
                sp = _safe_static_path(static_root, path)
                if sp is None:
                    st, b, h = _json_bytes({"error": "not found"}, 404)
                    self._send(st, b, h)
                    return
                mime = (
                    _STATIC_MIME.get(sp.suffix)
                    or mimetypes.guess_type(str(sp))[0]
                    or "application/octet-stream"
                )
                st, b, h = _text_bytes(sp.read_bytes(), content_type=mime)
                # Assets are cache-busted via ?v=... in index.html; still tell
                # the browser to revalidate so edits show up without a hard refresh.
                h.append(("Cache-Control", "no-cache"))
                self._send(st, b, h)
                return

            if path == "/api/project":
                root, pid = self._resolve_scope(parsed)
                if root is None or pid is None:
                    self._bad_project()
                    return
                sk = default_skills.resolve()
                st, b, h = _json_bytes(
                    {
                        "projectRoot": str(root),
                        "projectId": pid,
                        "codeRootPattern": pr.get_code_root_pattern(pid),
                        "codeRootPath": str(pr.get_code_root(pid) or root),
                        "skillsPath": str(sk),
                        "skillsBundledRelative": "loom/skills/charlie_skills.md",
                        "skillsOptions": _available_skill_options(sk, root),
                        "modelDefaults": {
                            agent: agent_default_model(agent)
                            for agent in sorted(SUPPORTED_AGENTS)
                        },
                        "modelOptions": {
                            agent: list(agent_model_options(agent))
                            for agent in sorted(SUPPORTED_AGENTS)
                        },
                    }
                )
                self._send(st, b, h)
                return

            if path == "/api/projects":
                if multi_ws:
                    pr.prune_redundant_parent_projects(launch_root_resolved)
                cur_id = (parse_qs(parsed.query or "").get("project") or [""])[0].strip()
                hdr = (self.headers.get("X-Loom-Project") or "").strip()
                resolved = cur_id or hdr or pr.default_project_id
                cur_path = pr.get_path(resolved) if resolved else None
                current = resolved if (resolved and cur_path) else ""
                st, b, h = _json_bytes(
                    {
                        "projects": pr.list_projects(),
                        "defaultProjectId": pr.default_project_id,
                        "currentProjectId": current,
                        "launchRoot": str(launch_root_resolved),
                        "launchRootChildren": _launch_root_child_dirs(launch_root_resolved),
                        "multiProjectWorkspace": multi_ws,
                    }
                )
                self._send(st, b, h)
                return

            if path == "/api/notes":
                root, _pid = self._resolve_scope(parsed)
                if root is None:
                    self._bad_project()
                    return
                st, b, h = _json_bytes({"content": read_project_notes(root)})
                self._send(st, b, h)
                return

            if routes_agent.handle_get(self, path, parsed):
                return
            if routes_tmux.handle_get(self, path, parsed):
                return
            if routes_review.handle_get(self, path, parsed):
                return
            if routes_rebuttal.handle_get(self, path, parsed):
                return
            if path == "/api/tasks":
                root, _pid = self._resolve_scope(parsed)
                if root is None:
                    self._bad_project()
                    return
                st, b, h = _json_bytes({"tasks": [m.to_dict() for m in list_tasks(root)]})
                self._send(st, b, h)
                return

            if routes_ar.handle_get(self, path, parsed):
                return
            m_files = re.match(r"^/api/tasks/([^/]+)/files$", path)
            if m_files:
                # The task tree as an editor sees it: PLAN.md and task.json at
                # the top, the worktree below. One directory per request, so a
                # repository with a deep tree costs nothing until it is opened.
                root, _pid = self._resolve_scope(parsed)
                if root is None:
                    self._bad_project()
                    return
                slug = m_files.group(1)
                if not _SLUG_RE.match(slug):
                    st, b, h = _json_bytes({"error": "invalid slug"}, 400)
                    self._send(st, b, h)
                    return
                qs = parse_qs(parsed.query or "")
                rel = (qs.get("path") or [""])[0].strip().lstrip("/")
                base = task_root(root, slug)
                target = path_under_task(base, rel) if rel else base
                if target is None or not target.exists():
                    st, b, h = _json_bytes({"ok": False, "error": "not found"}, 404)
                elif target.is_dir():
                    st, b, h = _json_bytes(
                        {
                            "ok": True,
                            "path": rel,
                            "dir": True,
                            "entries": browse_task_dir(target),
                        }
                    )
                else:
                    st, b, h = _json_bytes(
                        {"ok": True, "path": rel, "dir": False, **read_task_text(target)}
                    )
                self._send(st, b, h)
                return

            if path == "/api/activity":
                # Host-wide on purpose: the point is to surface an agent that
                # finished in a project you are not currently looking at.
                st, b, h = _json_bytes(activity_watcher.snapshot())
                self._send(st, b, h)
                return

            if path == "/api/factories/approvals":
                # One inbox for every human gate on the floor, so "what is
                # waiting on me" is a single glance instead of three pages.
                items: list[dict[str, Any]] = []
                ar_pid = ""
                for project in pr.list_projects():
                    if project.get("path") == str(ar.ar_root()):
                        ar_pid = str(project.get("id") or "")
                        break
                if ar_pid:
                    try:
                        overview = routes_ar._ar_overview(
                            self, Path(str(ar.ar_root())), ar_pid
                        )
                        papers = list(overview.get("orphans") or [])
                        for studio in overview.get("studios") or []:
                            papers.extend(studio.get("children") or [])
                        for paper in papers:
                            stage = str(paper.get("stage") or "")
                            if stage == ar.STAGE_AWAIT_DRAFT_REVIEW:
                                items.append({
                                    "factory": "paper", "gate": "draft",
                                    "id": paper.get("slug"), "project": ar_pid,
                                    "title": paper.get("title"),
                                    "detail": "draft ready for your review",
                                })
                            elif stage == ar.STAGE_AWAIT_FINAL_REVIEW:
                                items.append({
                                    "factory": "paper", "gate": "final",
                                    "id": paper.get("slug"), "project": ar_pid,
                                    "title": paper.get("title"),
                                    "detail": (
                                        f"{paper.get('round')}/{paper.get('max_rounds')} rounds"
                                        f" · best {paper.get('best_rating')}/10"
                                    ),
                                })
                    except Exception:  # noqa: BLE001 - inbox is best-effort
                        pass
                try:
                    for record in rebuttal.list_projects():
                        pid = str(record.get("id") or "")
                        state = rebuttal.read_state(pid)
                        stage = str(state.get("stage") or "")
                        title = str(record.get("title") or pid)
                        if stage == rebuttal.STAGE_RESPONSES:
                            items.append({
                                "factory": "rebuttal", "gate": "validate",
                                "id": pid, "title": title,
                                "detail": "responses drafted - validate, then approve",
                            })
                        elif stage == rebuttal.STAGE_VALIDATED:
                            items.append({
                                "factory": "rebuttal", "gate": "content",
                                "id": pid, "title": title,
                                "detail": "validation passed - content approval (Gate 1)",
                            })
                        elif stage == rebuttal.STAGE_AWAIT_DELIVERY_APPROVAL:
                            items.append({
                                "factory": "rebuttal", "gate": "delivery",
                                "id": pid, "title": title,
                                "detail": "delivery preflight passed (Gate 2)",
                            })
                except Exception:  # noqa: BLE001
                    pass
                st, b, h = _json_bytes({"ok": True, "items": items})
                self._send(st, b, h)
                return

            if path == "/api/asset":
                # Figures referenced by a rendered markdown document. `task`
                # scopes the lookup to one task directory; without it the base
                # is the project's .RUD/ root, which is where NOTES.md lives.
                root, _pid = self._resolve_scope(parsed)
                if root is None:
                    self._bad_project()
                    return
                qs = parse_qs(parsed.query or "")
                rel = (qs.get("path") or [""])[0]
                slug = (qs.get("task") or [""])[0].strip()
                if slug and not _SLUG_RE.match(slug):
                    st, b, h = _json_bytes({"error": "invalid slug"}, 400)
                    self._send(st, b, h)
                    return
                base = task_root(root, slug) if slug else rud_root(root)
                found = read_markdown_asset(base, rel)
                if found is None:
                    st, b, h = _json_bytes({"error": "asset not found"}, 404)
                    self._send(st, b, h)
                    return
                data, ctype = found
                self._send(
                    200,
                    data,
                    [
                        ("Content-Type", ctype),
                        ("Content-Length", str(len(data))),
                        # An SVG rendered in <img> can't run scripts, but one
                        # opened directly at this URL would inherit the app's
                        # origin, so neuter it either way.
                        ("Content-Security-Policy", "default-src 'none'; style-src 'unsafe-inline'"),
                        ("X-Content-Type-Options", "nosniff"),
                        # Figures get regenerated in place; revalidate so the
                        # preview never shows a stale one.
                        ("Cache-Control", "no-cache"),
                    ],
                )
                return

            m_wt_cand = re.match(r"^/api/tasks/([^/]+)/worktree-candidates$", path)
            if m_wt_cand:
                root, project_id = self._resolve_scope(parsed)
                if root is None or project_id is None:
                    self._bad_project()
                    return
                slug = m_wt_cand.group(1)
                if not _SLUG_RE.match(slug):
                    st, b, h = _json_bytes({"error": "invalid slug"}, 400)
                    self._send(st, b, h)
                    return
                meta = read_meta(root, slug)
                if not meta:
                    st, b, h = _json_bytes({"error": "not found"}, 404)
                    self._send(st, b, h)
                    return
                candidates = _project_worktree_candidates(pr, root, project_id)
                # Annotate each candidate with the destination path + a
                # flag the UI uses to disable rows that are already wired
                # in.  "Already created" means the dest dir is a registered
                # git worktree (so picking again would be a no-op).
                dest_parent = task_root(root, slug) / "work"
                existing_paths = {str(p) for p in list_task_worktrees(root, slug)}
                for c in candidates:
                    dest = dest_parent / Path(c["path"]).name
                    c["destination"] = str(dest)
                    c["already_created"] = str(dest.resolve()) in existing_paths
                st, b, h = _json_bytes(
                    {
                        "projectRoot": str(root),
                        "candidates": candidates,
                        "worktrees": list(meta.worktrees),
                    }
                )
                self._send(st, b, h)
                return

            m_diff = re.match(r"^/api/tasks/([^/]+)/diff$", path)
            if m_diff:
                root, _pid = self._resolve_scope(parsed)
                if root is None:
                    self._bad_project()
                    return
                slug = m_diff.group(1)
                if not _SLUG_RE.match(slug):
                    st, b, h = _json_bytes({"error": "invalid slug"}, 400)
                    self._send(st, b, h)
                    return
                meta = read_meta(root, slug)
                if not meta:
                    st, b, h = _json_bytes({"error": "not found"}, 404)
                    self._send(st, b, h)
                    return
                meta = detect_and_persist_worktree(root, slug) or meta
                worktrees = task_worktree_diffs(root, slug)
                st, b, h = _json_bytes(
                    {
                        "slug": slug,
                        "worktrees": worktrees,
                    }
                )
                self._send(st, b, h)
                return

            m_mon_get = re.match(r"^/api/tasks/([^/]+)/monitor$", path)
            if m_mon_get:
                root, project_id = self._resolve_scope(parsed)
                if root is None or project_id is None:
                    self._bad_project()
                    return
                slug = m_mon_get.group(1)
                if not _SLUG_RE.match(slug):
                    st, b, h = _json_bytes({"error": "invalid slug"}, 400)
                    self._send(st, b, h)
                    return
                if not read_meta(root, slug):
                    st, b, h = _json_bytes({"error": "not found"}, 404)
                    self._send(st, b, h)
                    return
                st, b, h = _json_bytes(monitor_manager.status(root, project_id, slug))
                self._send(st, b, h)
                return

            m_conversation = re.match(r"^/api/tasks/([^/]+)/conversation$", path)
            if m_conversation:
                root, project_id = self._resolve_scope(parsed)
                if root is None or project_id is None:
                    self._bad_project()
                    return
                slug = m_conversation.group(1)
                if not _SLUG_RE.match(slug):
                    st, b, h = _json_bytes({"error": "invalid slug"}, 400)
                    self._send(st, b, h)
                    return
                meta = read_meta(root, slug)
                if not meta:
                    st, b, h = _json_bytes({"error": "not found"}, 404)
                    self._send(st, b, h)
                    return
                meta = detect_and_persist_worktree(root, slug) or meta
                summary = self._claude_session_summary(project_id, slug, meta)
                qs = parse_qs(parsed.query or "")
                requested_id = (qs.get("session") or [""])[0].strip()
                try:
                    limit = int((qs.get("limit") or ["160"])[0] or 160)
                except ValueError:
                    limit = 160
                limit = max(20, min(500, limit))
                selected_session: dict[str, Any] | None = None
                transcript_path: Path | None = None
                for candidate in summary.get("sessions") or []:
                    if requested_id and candidate.get("id") != requested_id:
                        continue
                    candidate_path = _conversation_transcript_path(
                        candidate, str(summary.get("agent") or "")
                    )
                    if candidate_path is not None:
                        selected_session = candidate
                        transcript_path = candidate_path
                        break

                active = bool(summary.get("agent_running"))
                working = False
                terminal_question: dict[str, Any] | None = None
                target = str(summary.get("tmux_target") or "").strip()
                if active and validate_tmux_target(target):
                    capture_ok, capture_text = capture_pane(target, 100)
                    if capture_ok:
                        working = bool(_AGENT_WORKING_RE.search(capture_text or ""))
                        terminal_question = _conversation_terminal_question(capture_text)

                if selected_session is None or transcript_path is None:
                    terminal_message = (
                        {
                            "id": f"terminal-question:{terminal_question['id']}",
                            "kind": "question",
                            "created_at": None,
                            "question": terminal_question,
                        }
                        if terminal_question is not None
                        else None
                    )
                    st, b, h = _json_bytes(
                        {
                            "ok": True,
                            "available": terminal_message is not None,
                            "agent": summary.get("agent"),
                            "online": active,
                            "working": working,
                            "session_id": requested_id or None,
                            "updated_at": int(time.time() * 1000)
                            if terminal_message is not None
                            else None,
                            "messages": [terminal_message] if terminal_message is not None else [],
                            "total": 1 if terminal_message is not None else 0,
                            "has_more": False,
                        }
                    )
                    self._send(st, b, h)
                    return

                all_messages = _parse_conversation_transcript(
                    transcript_path, str(summary.get("agent") or "")
                )
                visible_messages: list[dict[str, Any]] = []
                terminal_appended = False
                for original in all_messages[-limit:]:
                    message = dict(original)
                    if message.get("kind") == "tool":
                        tool = dict(message.get("tool") or {})
                        if not active and tool.get("status") == "running":
                            tool["status"] = "canceled"
                        message["tool"] = tool
                    elif message.get("kind") == "question":
                        question = dict(message.get("question") or {})
                        # A list read out of the agent's text is only a
                        # question once the agent has stopped to hear back.
                        if working and question.get("source") == "numbered":
                            continue
                        if not active and question.get("status") == "pending":
                            question["status"] = "canceled"
                        message["question"] = question
                    visible_messages.append(message)
                if terminal_question is not None and not any(
                    message.get("kind") == "question"
                    and (message.get("question") or {}).get("status") == "pending"
                    for message in all_messages
                ):
                    visible_messages.append(
                        {
                            "id": f"terminal-question:{terminal_question['id']}",
                            "kind": "question",
                            "created_at": None,
                            "question": terminal_question,
                        }
                    )
                    terminal_appended = True
                try:
                    transcript_stat = transcript_path.stat()
                    updated_at = int(transcript_stat.st_mtime * 1000)
                except OSError:
                    updated_at = None
                st, b, h = _json_bytes(
                    {
                        "ok": True,
                        "available": True,
                        "agent": summary.get("agent"),
                        "online": active,
                        "working": working,
                        "session_id": selected_session.get("id"),
                        "updated_at": int(time.time() * 1000)
                        if terminal_appended
                        else updated_at,
                        "messages": visible_messages,
                        "total": len(all_messages) + (1 if terminal_appended else 0),
                        "has_more": len(all_messages) > min(limit, len(all_messages)),
                    }
                )
                self._send(st, b, h)
                return

            m_sessions = re.match(r"^/api/tasks/([^/]+)/claude-sessions$", path)
            if m_sessions:
                root, project_id = self._resolve_scope(parsed)
                if root is None or project_id is None:
                    self._bad_project()
                    return
                slug = m_sessions.group(1)
                if not _SLUG_RE.match(slug):
                    st, b, h = _json_bytes({"error": "invalid slug"}, 400)
                    self._send(st, b, h)
                    return
                meta = read_meta(root, slug)
                if not meta:
                    st, b, h = _json_bytes({"error": "not found"}, 404)
                    self._send(st, b, h)
                    return
                # Same back-fill so the live Claude info card always sees
                # the disk truth.
                meta = detect_and_persist_worktree(root, slug) or meta
                st, b, h = _json_bytes(self._claude_session_summary(project_id, slug, meta))
                self._send(st, b, h)
                return

            m = re.match(r"^/api/tasks/([^/]+)$", path)
            if m:
                root, project_id = self._resolve_scope(parsed)
                if root is None:
                    self._bad_project()
                    return
                slug = m.group(1)
                if not _SLUG_RE.match(slug):
                    st, b, h = _json_bytes({"error": "invalid slug"}, 400)
                    self._send(st, b, h)
                    return
                meta = read_meta(root, slug)
                if not meta:
                    st, b, h = _json_bytes({"error": "not found"}, 404)
                    self._send(st, b, h)
                    return
                # Back-fill worktree_path / branch on tasks that pre-date the
                # auto-worktree feature, or tasks where the user manually
                # added a worktree under work/ later on.
                meta = detect_and_persist_worktree(root, slug) or meta
                # The expensive bits are git-status-per-worktree and the
                # Claude session enrichment (tmux subprocess + filesystem
                # scan). Fan both out so they overlap with the synchronous
                # markdown reads below - on a typical task this brings the
                # endpoint from ~600-1500ms down to ~150-400ms.
                with ThreadPoolExecutor(max_workers=2) as pool:
                    statuses_fut = pool.submit(list_task_worktree_statuses, root, slug)
                    summary_fut = (
                        pool.submit(self._claude_session_summary, project_id, slug, meta)
                        if project_id
                        else None
                    )
                    # Surface every top-level *.md file in the task directory.
                    # WIKI.md as the shared worker/evaluator knowledge base.
                    md_names = list_task_markdown_files(root, slug)
                    templates: dict[str, str] = {}
                    for md_name in md_names:
                        content = read_task_markdown_file(root, slug, md_name)
                        if content is not None:
                            templates[md_name] = content
                    primary_md = PLAN
                    if primary_md not in templates:
                        templates[primary_md] = read_template(root, slug, primary_md) or ""
                        if primary_md not in md_names:
                            md_names = [primary_md, *md_names]
                    elif primary_md in md_names:
                        md_names = [primary_md, *(name for name in md_names if name != primary_md)]
                    statuses = statuses_fut.result()
                    summary = summary_fut.result() if summary_fut is not None else None
                st, b, h = _json_bytes(
                    {
                        "meta": meta.to_dict(),
                        "task_root": str(task_root(root, slug)),
                        "plan_path": str(task_root(root, slug) / primary_md),
                        "templates": templates,
                        "task_markdown_files": md_names,
                        "claude": summary or {},
                        "worktree_statuses": statuses,
                        # Tasks carry absolute skill paths, so one moved or
                        # renamed checkout leaves them pointing at nothing.
                        # The prompt silently falls back to the default; tell
                        # the UI so it can stop presenting a dead file as the
                        # task's skill.
                        "skills_missing": [
                            str(p)
                            for p in split_skills_paths(meta.skills_path or "")
                            if not p.is_file()
                        ],
                    }
                )
                self._send(st, b, h)
                return

            st, b, h = _json_bytes({"error": "not found"}, 404)
            self._send(st, b, h)

        # ===== POST =====

        def do_POST(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            # An agent's stop hook reports here. It runs outside any browser
            # session, so it carries its own narrow credential instead of the
            # web token - checked before _require_auth, which would reject it.
            if parsed.path == "/api/activity/finished":
                self._agent_finished(_read_json(self))
                return
            if not self._require_auth():
                return
            path = parsed.path
            if routes_ar.handle_raw_post(self, path, parsed):
                return
            if routes_agent.handle_raw_post(self, path, parsed):
                return
            body = _read_json(self)

            if routes_rebuttal.handle_post(self, path, parsed, body):
                return
            if routes_review.handle_post(self, path, parsed, body):
                return
            if path == "/api/activity/ack":
                root, project_id = self._resolve_scope(parsed)
                if project_id is None:
                    self._bad_project()
                    return
                slug = str(body.get("slug", "")).strip()
                if slug:
                    activity_watcher.ack(project_id, slug)
                st, b, h = _json_bytes({"ok": True})
                self._send(st, b, h)
                return

            if routes_ar.handle_post(self, path, parsed, body):
                return
            if path == "/api/tasks/reorder":
                root, _project_id = self._resolve_scope(parsed)
                if root is None:
                    self._bad_project()
                    return
                raw_slugs = body.get("slugs", [])
                if not isinstance(raw_slugs, list):
                    st, b, h = _json_bytes({"error": "slugs must be a list"}, 400)
                    self._send(st, b, h)
                    return
                ok_order, err_order = reorder_tasks(root, [str(x) for x in raw_slugs])
                if not ok_order:
                    st, b, h = _json_bytes({"error": err_order}, 400)
                    self._send(st, b, h)
                    return
                st, b, h = _json_bytes({"ok": True, "tasks": [m.to_dict() for m in list_tasks(root)]})
                self._send(st, b, h)
                return

            if path == "/api/tasks":
                root, project_id = self._resolve_scope(parsed)
                if root is None or project_id is None:
                    self._bad_project()
                    return
                title = str(body.get("title", "")).strip()
                general_goal = str(body.get("general_goal", "")).strip()
                kind = {"ar": ar.KIND_AR, "aris": ar.KIND_AR}.get(
                    str(body.get("kind", "")).strip().lower(), "agent"
                )
                ar_state: dict[str, Any] | None = None
                if kind == ar.KIND_AR:
                    venue_url = str(body.get("ar_venue_url", "")).strip()
                    if venue_url and not venue_url.startswith(("http://", "https://")):
                        st, b, h = _json_bytes(
                            {"error": "venue URL must be an http(s) address"},
                            400,
                        )
                        self._send(st, b, h)
                        return
                    profile_id = str(
                        body.get("ar_background_profile_id") or ""
                    ).strip()
                    if profile_id and not _SLUG_RE.match(profile_id):
                        st, b, h = _json_bytes(
                            {"error": "invalid researcher profile id"}, 400
                        )
                        self._send(st, b, h)
                        return
                    fit_mode = str(
                        body.get("ar_background_fit_mode")
                        or ar.DEFAULT_BACKGROUND_FIT_MODE
                    ).strip().lower()
                    if fit_mode not in ar.BACKGROUND_FIT_MODES:
                        st, b, h = _json_bytes(
                            {"error": "unknown background exploration mode"}, 400
                        )
                        self._send(st, b, h)
                        return
                    profile_snapshot: dict[str, Any] = {}
                    if profile_id:
                        try:
                            profile = researcher_profiles.read_profile(profile_id)
                        except ValueError:
                            st, b, h = _json_bytes(
                                {"error": "invalid researcher profile id"}, 400
                            )
                            self._send(st, b, h)
                            return
                        if not profile:
                            st, b, h = _json_bytes(
                                {"error": "researcher profile not found"}, 404
                            )
                            self._send(st, b, h)
                            return
                        if str(profile.get("status") or "") != "active":
                            st, b, h = _json_bytes(
                                {
                                    "error": (
                                        "activate the researcher profile before "
                                        "creating a Studio with it"
                                    )
                                },
                                409,
                            )
                            self._send(st, b, h)
                            return
                        profile_snapshot = researcher_profiles.profile_snapshot(
                            profile, fit_mode=fit_mode
                        )
                    ar_state = ar.new_studio_state(
                        direction=str(body.get("ar_direction", "")),
                        custom_direction=str(body.get("ar_custom_direction", "")),
                        venue=str(body.get("ar_venue", "")),
                        mode=str(body.get("ar_mode", "")),
                        seed_idea=str(body.get("ar_seed_idea", "")),
                        venue_url=venue_url,
                        venue_kickoff=bool(body.get("ar_venue_kickoff")),
                        max_rounds=body.get("ar_max_rounds", ar.DEFAULT_MAX_ROUNDS),
                        background_profile_id=profile_id,
                        background_profile_snapshot=profile_snapshot,
                        background_fit_mode=fit_mode,
                    )
                    # AR asks for the paper's content, not a goal to interview
                    # about, so derive the stored goal from the AR fields.
                    general_goal = general_goal or ar.default_general_goal(ar_state)
                if not title or not general_goal:
                    st, b, h = _json_bytes({"error": "title and general_goal required"}, 400)
                    self._send(st, b, h)
                    return
                # skills_path may be one path, a ;-joined string, or a list of
                # paths (multiple skills used together).
                raw_sp = body.get("skills_path")
                if isinstance(raw_sp, list):
                    raw_sp = SKILLS_PATH_SEP.join(str(x) for x in raw_sp)
                requested = [
                    p.resolve() for p in split_skills_paths(str(raw_sp or ""))
                    if p.is_file()
                ]
                if not requested:
                    requested = [
                        default_skills.resolve() if default_skills.is_file()
                        else bundled_skills_path().resolve()
                    ]
                skills_path = join_skills_paths(requested)
                raw_agent = str(body.get("agent", AGENT_CURSOR)).strip().lower()
                if raw_agent and raw_agent not in SUPPORTED_AGENTS:
                    st, b, h = _json_bytes(
                        {"error": f"agent must be one of {sorted(SUPPORTED_AGENTS)}"},
                        400,
                    )
                    self._send(st, b, h)
                    return
                meta = create_task(
                    root,
                    title,
                    general_goal,
                    skills_path=skills_path,
                    interview_model=(
                        str(body.get("interview_model", "")).strip()
                        or agent_default_model(raw_agent or AGENT_CURSOR)
                    ),
                    agent=raw_agent or AGENT_CURSOR,
                    kind=kind,
                    auto_worktree=False,
                )
                if ar_state is not None:
                    ar.write_ar_state(root, meta.slug, ar_state)
                code_root = pr.get_code_root(project_id) or root
                if ar_state is not None:
                    # A studio only mines and spawns; it has no code of its own,
                    # so a worktree of the project would sit there unused.
                    wt, _branch, auto_msg = None, "", "AR studio: no worktree needed"
                else:
                    wt, _branch, auto_msg = prepare_task_worktree_from(
                        root, meta.slug, code_root
                    )
                meta = read_meta(root, meta.slug) or meta
                cands = _project_worktree_candidates(pr, root, project_id)
                hint = ""
                if not meta.worktree_path:
                    if not cands:
                        hint = (
                            f" (configured code root {code_root} is not a git repo)"
                        )
                    else:
                        hint = (
                            f" (auto-skip: {auto_msg}; {len(cands)} candidate(s) "
                            f"available - pick one via the Agent tab)"
                        )
                print(
                    f"[web] created task slug={meta.slug} dir={task_root(root, meta.slug)} "
                    f"worktree={meta.worktree_path or '(none)'} "
                    f"branch={meta.branch or '(none)'}{hint}",
                    flush=True,
                )
                openclaw_client.emit(
                    "task-created",
                    instruction=f"Loom task created: {meta.slug}",
                    project_root=root,
                    task_slug=meta.slug,
                    data={
                        "title": meta.title,
                        "taskDir": str(task_root(root, meta.slug)),
                        "projectId": project_id,
                        "worktree": meta.worktree_path or "",
                        "branch": meta.branch or "",
                    },
                )
                st, b, h = _json_bytes({"meta": meta.to_dict()}, 201)
                self._send(st, b, h)
                return

            if routes_tmux.handle_post(self, path, parsed, body):
                return
            if path == "/api/projects/reorder":
                raw_ids = body.get("ids", [])
                if not isinstance(raw_ids, list):
                    st, b, h = _json_bytes({"error": "ids must be a list"}, 400)
                    self._send(st, b, h)
                    return
                ok_order, err_order = pr.reorder([str(x) for x in raw_ids])
                if not ok_order:
                    st, b, h = _json_bytes({"error": err_order}, 400)
                    self._send(st, b, h)
                    return
                st, b, h = _json_bytes(
                    {
                        "ok": True,
                        "projects": pr.list_projects(),
                        "defaultProjectId": pr.default_project_id,
                    }
                )
                self._send(st, b, h)
                return

            if path == "/api/projects":
                raw_path = str(body.get("path", "")).strip()
                mode = str(body.get("mode", "existing")).strip().lower()
                repo_url = str(body.get("repo_url", "")).strip()
                code_root_pattern = str(body.get("code_root_pattern", ".") or ".").strip()
                if not raw_path:
                    st, b, h = _json_bytes({"error": "path required"}, 400)
                    self._send(st, b, h)
                    return
                if mode in ("empty", "clone"):
                    # These create files on disk, so confine them to the launch
                    # directory tree (the modal promises nothing is written
                    # outside it). Registering an *existing* folder is unrestricted.
                    try:
                        dest = Path(raw_path).expanduser().resolve()
                    except OSError:
                        st, b, h = _json_bytes({"error": "invalid path"}, 400)
                        self._send(st, b, h)
                        return
                    if not _path_within(dest, launch_root_resolved):
                        st, b, h = _json_bytes(
                            {"error": f"new folders must be inside {launch_root_resolved}"}, 400
                        )
                        self._send(st, b, h)
                        return
                    if mode == "empty":
                        try:
                            dest.mkdir(parents=True, exist_ok=True)
                        except OSError as exc:
                            st, b, h = _json_bytes({"error": f"could not create folder: {exc}"}, 400)
                            self._send(st, b, h)
                            return
                    else:  # clone
                        if not repo_url:
                            st, b, h = _json_bytes({"error": "repo URL is required to clone"}, 400)
                            self._send(st, b, h)
                            return
                        ok_clone, msg_clone = _git_clone(repo_url, dest)
                        if not ok_clone:
                            st, b, h = _json_bytes({"error": msg_clone or "git clone failed"}, 400)
                            self._send(st, b, h)
                            return
                    raw_path = str(dest)
                try:
                    normalized_code_root = pr._normalize_code_root_pattern(code_root_pattern)
                except ValueError as exc:
                    st, b, h = _json_bytes({"error": str(exc)}, 400)
                    self._send(st, b, h)
                    return
                candidate_code_root = (Path(raw_path).expanduser().resolve() / normalized_code_root)
                if not candidate_code_root.is_dir():
                    st, b, h = _json_bytes(
                        {"error": f"code root directory does not exist: {candidate_code_root}"},
                        400,
                    )
                    self._send(st, b, h)
                    return
                new_id, err = pr.add_by_path(raw_path, normalized_code_root)
                if err or not new_id:
                    st, b, h = _json_bytes({"error": err or "failed"}, 400)
                    self._send(st, b, h)
                    return
                st, b, h = _json_bytes(
                    {"id": new_id, "defaultProjectId": pr.default_project_id, "projects": pr.list_projects()},
                    201,
                )
                self._send(st, b, h)
                return

            m_code_root = re.match(r"^/api/projects/([^/]+)/code-root$", path)
            if m_code_root:
                pid_code = m_code_root.group(1)
                ok_code, err_code = pr.set_code_root_pattern(
                    pid_code, str(body.get("pattern", "."))
                )
                if not ok_code:
                    status = 404 if err_code == "project not found" else 400
                    st, b, h = _json_bytes({"error": err_code}, status)
                    self._send(st, b, h)
                    return
                st, b, h = _json_bytes({
                    "ok": True,
                    "pattern": pr.get_code_root_pattern(pid_code),
                    "path": str(pr.get_code_root(pid_code) or ""),
                    "projects": pr.list_projects(),
                })
                self._send(st, b, h)
                return

            m_move = re.match(r"^/api/projects/([^/]+)/move$", path)
            if m_move:
                pid_move = m_move.group(1)
                direction = str(body.get("direction", "")).strip().lower()
                ok_move, err_move = pr.move(pid_move, direction)
                if not ok_move:
                    status = 404 if err_move == "project not found" else 400
                    st, b, h = _json_bytes({"error": err_move}, status)
                    self._send(st, b, h)
                    return
                st, b, h = _json_bytes(
                    {
                        "ok": True,
                        "projects": pr.list_projects(),
                        "defaultProjectId": pr.default_project_id,
                    }
                )
                self._send(st, b, h)
                return

            m_activate = re.match(r"^/api/projects/([^/]+)/activate$", path)
            if m_activate:
                pid_act = m_activate.group(1)
                if not pr.set_default(pid_act):
                    st, b, h = _json_bytes({"error": "project not found"}, 404)
                    self._send(st, b, h)
                    return
                st, b, h = _json_bytes({"ok": True, "defaultProjectId": pid_act})
                self._send(st, b, h)
                return

            # Claude pane lifecycle - the same two route prefixes were
            # called /interview/{start,stop} before the rename, accept both.
            m_start = re.match(r"^/api/tasks/([^/]+)/(?:claude|interview)/start$", path)
            if m_start:
                root, project_id = self._resolve_scope(parsed)
                if root is None or project_id is None:
                    self._bad_project()
                    return
                slug = m_start.group(1)
                if not _SLUG_RE.match(slug):
                    st, b, h = _json_bytes({"error": "invalid slug"}, 400)
                    self._send(st, b, h)
                    return
                result = claude_registry.start(root, project_id, slug, default_skills=default_skills)
                print(
                    f"[web] start claude slug={slug} ok={bool(result.get('ok'))} "
                    f"session={result.get('session', '')} target={result.get('target', '')}",
                    flush=True,
                )
                openclaw_client.emit(
                    "claude-start",
                    instruction=f"Loom Claude pane started for task {slug}",
                    project_root=root,
                    task_slug=slug,
                    data=result,
                )
                st, b, h = (
                    _json_bytes(result)
                    if result.get("ok")
                    else _json_bytes(result, 400)
                )
                self._send(st, b, h)
                return

            m_paste = re.match(r"^/api/tasks/([^/]+)/(?:claude|interview)/paste-prompt$", path)
            if m_paste:
                root, project_id = self._resolve_scope(parsed)
                if root is None or project_id is None:
                    self._bad_project()
                    return
                slug = m_paste.group(1)
                if not _SLUG_RE.match(slug):
                    st, b, h = _json_bytes({"error": "invalid slug"}, 400)
                    self._send(st, b, h)
                    return
                result = claude_registry.paste_prompt(
                    root,
                    project_id,
                    slug,
                    default_skills=default_skills,
                )
                st, b, h = (
                    _json_bytes(result)
                    if result.get("ok")
                    else _json_bytes(result, 400)
                )
                self._send(st, b, h)
                return

            m_stop = re.match(r"^/api/tasks/([^/]+)/(?:claude|interview)/stop$", path)
            if m_stop:
                root, project_id = self._resolve_scope(parsed)
                if root is None or project_id is None:
                    self._bad_project()
                    return
                slug = m_stop.group(1)
                if not _SLUG_RE.match(slug):
                    st, b, h = _json_bytes({"error": "invalid slug"}, 400)
                    self._send(st, b, h)
                    return
                if not read_meta(root, slug):
                    st, b, h = _json_bytes({"error": "not found"}, 404)
                    self._send(st, b, h)
                    return
                result = claude_registry.stop(root, project_id, slug)
                openclaw_client.emit(
                    "claude-stop",
                    instruction=f"Loom Claude pane stopped for task {slug}",
                    project_root=root,
                    task_slug=slug,
                    data=result,
                )
                st, b, h = _json_bytes(result)
                self._send(st, b, h)
                return

            m_mon_post = re.match(r"^/api/tasks/([^/]+)/monitor$", path)
            if m_mon_post:
                root, project_id = self._resolve_scope(parsed)
                if root is None or project_id is None:
                    self._bad_project()
                    return
                slug = m_mon_post.group(1)
                if not _SLUG_RE.match(slug):
                    st, b, h = _json_bytes({"error": "invalid slug"}, 400)
                    self._send(st, b, h)
                    return
                if not read_meta(root, slug):
                    st, b, h = _json_bytes({"error": "not found"}, 404)
                    self._send(st, b, h)
                    return
                pattern = str(body.get("pattern", "")).strip()
                if pattern:
                    try:
                        re.compile(pattern)
                    except re.error as exc:
                        st, b, h = _json_bytes({"error": f"invalid regex: {exc}"}, 400)
                        self._send(st, b, h)
                        return
                result = monitor_manager.enable(root, project_id, slug, pattern)
                print(
                    f"[web] monitor enabled slug={slug} pattern={result.get('pattern', '')!r}",
                    flush=True,
                )
                st, b, h = _json_bytes(result)
                self._send(st, b, h)
                return

            m_conversation_answer = re.match(
                r"^/api/tasks/([^/]+)/conversation/answer$", path
            )
            if m_conversation_answer:
                root, project_id = self._resolve_scope(parsed)
                if root is None or project_id is None:
                    self._bad_project()
                    return
                slug = m_conversation_answer.group(1)
                if not _SLUG_RE.match(slug):
                    st, b, h = _json_bytes({"error": "invalid slug"}, 400)
                    self._send(st, b, h)
                    return
                meta = read_meta(root, slug)
                if not meta:
                    st, b, h = _json_bytes({"error": "not found"}, 404)
                    self._send(st, b, h)
                    return
                target = (meta.tmux_interview_target or "").strip()
                if not validate_tmux_target(target):
                    st, b, h = _json_bytes(
                        {"ok": False, "error": "no active agent pane for this task"},
                        409,
                    )
                    self._send(st, b, h)
                    return
                capture_ok, capture_text = capture_pane(target, 100)
                question = (
                    _conversation_terminal_question(capture_text)
                    if capture_ok
                    else None
                )
                question_id = str(body.get("question_id") or "").strip()
                selected_ids = body.get("selected_ids")
                custom_text = body.get("custom_text", "")
                if (
                    question is None
                    or not question_id
                    or question.get("id") != question_id
                ):
                    st, b, h = _json_bytes(
                        {
                            "ok": False,
                            "error": "the active terminal question has changed",
                        },
                        409,
                    )
                    self._send(st, b, h)
                    return
                if not isinstance(selected_ids, list) or not all(
                    isinstance(item, str) for item in selected_ids
                ):
                    st, b, h = _json_bytes(
                        {"ok": False, "error": "selected_ids must be a string list"},
                        400,
                    )
                    self._send(st, b, h)
                    return
                if not isinstance(custom_text, str) or len(custom_text) > 12000:
                    st, b, h = _json_bytes(
                        {"ok": False, "error": "custom_text must be at most 12000 characters"},
                        400,
                    )
                    self._send(st, b, h)
                    return
                prompt = (question.get("questions") or [{}])[0]
                options_by_id = {
                    str(option.get("id")): option
                    for option in (prompt.get("options") or [])
                    if isinstance(option, dict)
                }
                other_selected = any(
                    option_id in options_by_id
                    and re.match(
                        r"^other\b",
                        str(options_by_id[option_id].get("label") or "").strip(),
                        re.IGNORECASE,
                    )
                    for option_id in selected_ids
                )
                if other_selected and not custom_text.strip():
                    st, b, h = _json_bytes(
                        {
                            "ok": False,
                            "error": "type a custom answer before submitting Other",
                        },
                        400,
                    )
                    self._send(st, b, h)
                    return
                if other_selected and len(selected_ids) != 1:
                    st, b, h = _json_bytes(
                        {
                            "ok": False,
                            "error": "Other cannot be combined with another option",
                        },
                        400,
                    )
                    self._send(st, b, h)
                    return
                keys = _conversation_terminal_answer_keys(
                    question,
                    selected_ids,
                    submit=not other_selected,
                )
                if not keys and not other_selected:
                    st, b, h = _json_bytes(
                        {"ok": False, "error": "select at least one valid option"},
                        400,
                    )
                    self._send(st, b, h)
                    return
                for key in keys:
                    # Ink-based agent menus can drop Enter while processing the
                    # preceding cursor/checkbox render. Keep navigation snappy,
                    # but give selection and submit events time to settle.
                    if key == "Enter":
                        time.sleep(0.3)
                    ok, error = send_pane_key(target, key)
                    if not ok:
                        st, b, h = _json_bytes(
                            {"ok": False, "error": error or "could not answer question"},
                            400,
                        )
                        self._send(st, b, h)
                        return
                    time.sleep(0.14 if key == "Space" else 0.08)
                if other_selected:
                    # Selecting Other changes the menu into an inline text field.
                    # Typing must happen before Enter; an empty Enter is ignored.
                    time.sleep(0.3)
                    staged_ok, staged_text = capture_pane(target, 100)
                    staged_question = (
                        _conversation_terminal_question(staged_text)
                        if staged_ok
                        else None
                    )
                    staged_options = (
                        (staged_question.get("questions") or [{}])[0].get("options")
                        if staged_question is not None
                        else []
                    ) or []
                    other_ready = any(
                        str(option.get("id")) in selected_ids
                        and bool(option.get("selected"))
                        for option in staged_options
                        if re.match(
                            r"^other\b",
                            str(option.get("label") or "").strip(),
                            re.IGNORECASE,
                        )
                    )
                    if not other_ready:
                        st, b, h = _json_bytes(
                            {
                                "ok": False,
                                "error": "could not activate the Other text field",
                            },
                            409,
                        )
                        self._send(st, b, h)
                        return
                    ok, error = send_pane_text(
                        target,
                        custom_text.strip(),
                        submit=True,
                    )
                    if not ok:
                        st, b, h = _json_bytes(
                            {"ok": False, "error": error or "could not send custom answer"},
                            400,
                        )
                        self._send(st, b, h)
                        return
                    time.sleep(0.5)
                else:
                    time.sleep(0.25)
                after_ok, after_text = capture_pane(target, 100)
                after_question = (
                    _conversation_terminal_question(after_text) if after_ok else None
                )
                still_pending = bool(
                    after_question is not None
                    and after_question.get("id") == question.get("id")
                )
                print(
                    f"[web] conversation answer slug={slug} options={selected_ids!r} "
                    f"custom_chars={len(custom_text.strip())} pending={still_pending}",
                    flush=True,
                )
                st, b, h = _json_bytes(
                    {
                        "ok": True,
                        "target": target,
                        "pending": still_pending,
                    }
                )
                self._send(st, b, h)
                return

            m_force_send = re.match(
                r"^/api/tasks/([^/]+)/claude/force-send$", path
            )
            if m_force_send:
                root, project_id = self._resolve_scope(parsed)
                if root is None or project_id is None:
                    self._bad_project()
                    return
                slug = m_force_send.group(1)
                if not _SLUG_RE.match(slug):
                    st, b, h = _json_bytes({"error": "invalid slug"}, 400)
                    self._send(st, b, h)
                    return
                meta = read_meta(root, slug)
                if not meta:
                    st, b, h = _json_bytes({"error": "not found"}, 404)
                    self._send(st, b, h)
                    return
                if normalize_agent(meta.agent) != AGENT_CURSOR:
                    st, b, h = _json_bytes(
                        {"error": "force send is supported only for Cursor Agent"},
                        400,
                    )
                    self._send(st, b, h)
                    return
                live = claude_registry.session_status(
                    project_id,
                    slug,
                    meta.agent,
                    meta.tmux_interview_target or "",
                )
                target = (meta.tmux_interview_target or live.get("target") or "").strip()
                if not live.get("agent_running") or not validate_tmux_target(target):
                    st, b, h = _json_bytes(
                        {"ok": False, "error": "no active Cursor Agent pane"},
                        409,
                    )
                    self._send(st, b, h)
                    return
                before_ok, before_text = capture_pane(target, 35)
                before_working = bool(
                    before_ok and _AGENT_WORKING_RE.search(before_text or "")
                )
                ok, error = send_pane_key(target, "Enter")
                if not ok:
                    st, b, h = _json_bytes(
                        {"ok": False, "error": error or "force send failed"},
                        400,
                    )
                    self._send(st, b, h)
                    return
                time.sleep(0.35)
                after_ok, after_text = capture_pane(target, 35)
                after_working = bool(
                    after_ok and _AGENT_WORKING_RE.search(after_text or "")
                )
                print(
                    f"[web] force-send slug={slug} before_working={before_working} "
                    f"after_working={after_working}",
                    flush=True,
                )
                st, b, h = _json_bytes(
                    {
                        "ok": True,
                        "target": target,
                        "working": after_working,
                    }
                )
                self._send(st, b, h)
                return

            m_send = re.match(r"^/api/tasks/([^/]+)/claude/send$", path)
            if m_send:
                root, project_id = self._resolve_scope(parsed)
                if root is None or project_id is None:
                    self._bad_project()
                    return
                slug = m_send.group(1)
                if not _SLUG_RE.match(slug):
                    st, b, h = _json_bytes({"error": "invalid slug"}, 400)
                    self._send(st, b, h)
                    return
                meta = read_meta(root, slug)
                if not meta:
                    st, b, h = _json_bytes({"error": "not found"}, 404)
                    self._send(st, b, h)
                    return
                target = (meta.tmux_interview_target or "").strip()
                if not target:
                    st, b, h = _json_bytes(
                        {"ok": False, "error": "no active Claude pane for this task"}, 409
                    )
                    self._send(st, b, h)
                    return
                text = body.get("text", "")
                if not isinstance(text, str) or not text:
                    st, b, h = _json_bytes({"ok": False, "error": "text required"}, 400)
                    self._send(st, b, h)
                    return
                submit = bool(body.get("submit", True))
                ok, msg = send_pane_text(target, text, submit=submit)
                print(
                    f"[web] inbound claude/send slug={slug} ok={ok} chars={len(text)}",
                    flush=True,
                )
                st, b, h = (
                    _json_bytes({"ok": True, "target": target})
                    if ok
                    else _json_bytes({"ok": False, "error": msg}, 400)
                )
                self._send(st, b, h)
                return

            m_wt_create = re.match(r"^/api/tasks/([^/]+)/worktree$", path)
            if m_wt_create:
                root, project_id = self._resolve_scope(parsed)
                if root is None or project_id is None:
                    self._bad_project()
                    return
                slug = m_wt_create.group(1)
                if not _SLUG_RE.match(slug):
                    st, b, h = _json_bytes({"error": "invalid slug"}, 400)
                    self._send(st, b, h)
                    return
                if not read_meta(root, slug):
                    st, b, h = _json_bytes({"error": "not found"}, 404)
                    self._send(st, b, h)
                    return
                raw_src = str(body.get("source_repo", "")).strip()
                if not raw_src:
                    st, b, h = _json_bytes({"error": "source_repo required"}, 400)
                    self._send(st, b, h)
                    return
                # Whitelist against the project's candidate list so a
                # poisoned request can't make us run `git worktree add`
                # against an arbitrary path on disk.
                allowed = {
                    str(Path(c["path"]).resolve())
                    for c in _project_worktree_candidates(pr, root, project_id)
                }
                try:
                    src_resolved = str(Path(raw_src).expanduser().resolve())
                except OSError as exc:
                    st, b, h = _json_bytes({"error": f"invalid path: {exc}"}, 400)
                    self._send(st, b, h)
                    return
                if src_resolved not in allowed:
                    st, b, h = _json_bytes(
                        {
                            "error": "source_repo is not in the project's candidate list",
                            "allowed": sorted(allowed),
                        },
                        400,
                    )
                    self._send(st, b, h)
                    return
                wt, branch, msg = prepare_task_worktree_from(
                    root, slug, Path(src_resolved)
                )
                print(
                    f"[web] manual worktree slug={slug} src={src_resolved} "
                    f"ok={wt is not None} msg={msg}",
                    flush=True,
                )
                if wt is None:
                    st, b, h = _json_bytes(
                        {"ok": False, "error": msg, "branch": branch}, 400
                    )
                    self._send(st, b, h)
                    return
                # Append (or refresh) the worktree list from disk.  Don't
                # call update_meta directly so order / branches stay in
                # sync across the existing entries.
                updated = detect_and_persist_worktree(root, slug) or read_meta(root, slug)
                openclaw_client.emit(
                    "worktree-created",
                    instruction=f"Loom worktree created for task {slug}",
                    project_root=root,
                    task_slug=slug,
                    data={"source_repo": src_resolved, "worktree": str(wt), "branch": branch},
                )
                st, b, h = _json_bytes(
                    {
                        "ok": True,
                        "worktree_path": str(wt),
                        "branch": branch,
                        "message": msg,
                        "meta": updated.to_dict() if updated else None,
                    }
                )
                self._send(st, b, h)
                return

            m_wt_push = re.match(r"^/api/tasks/([^/]+)/worktree/push$", path)
            if m_wt_push:
                root, project_id = self._resolve_scope(parsed)
                if root is None or project_id is None:
                    self._bad_project()
                    return
                slug = m_wt_push.group(1)
                if not _SLUG_RE.match(slug):
                    st, b, h = _json_bytes({"error": "invalid slug"}, 400)
                    self._send(st, b, h)
                    return
                meta = read_meta(root, slug)
                if not meta:
                    st, b, h = _json_bytes({"error": "not found"}, 404)
                    self._send(st, b, h)
                    return
                raw_path = str(body.get("path", "")).strip()
                if not raw_path:
                    st, b, h = _json_bytes({"error": "path required"}, 400)
                    self._send(st, b, h)
                    return
                try:
                    wt = Path(raw_path).expanduser().resolve()
                except OSError as exc:
                    st, b, h = _json_bytes({"error": f"invalid path: {exc}"}, 400)
                    self._send(st, b, h)
                    return
                if str(wt) not in meta.worktrees:
                    st, b, h = _json_bytes(
                        {"error": "worktree is not registered with this task"},
                        400,
                    )
                    self._send(st, b, h)
                    return
                result = push_worktree_branch(wt)
                # Refresh status snapshot so the UI can update ahead/behind.
                result["status"] = worktree_status(wt)
                print(
                    f"[web] push worktree slug={slug} path={wt} "
                    f"ok={result.get('ok')} branch={result.get('branch')}",
                    flush=True,
                )
                st, b, h = _json_bytes(result, 200 if result.get("ok") else 400)
                self._send(st, b, h)
                return

            m_wt_merge = re.match(r"^/api/tasks/([^/]+)/worktree/merge$", path)
            if m_wt_merge:
                root, project_id = self._resolve_scope(parsed)
                if root is None or project_id is None:
                    self._bad_project()
                    return
                slug = m_wt_merge.group(1)
                if not _SLUG_RE.match(slug):
                    st, b, h = _json_bytes({"error": "invalid slug"}, 400)
                    self._send(st, b, h)
                    return
                meta = read_meta(root, slug)
                if not meta:
                    st, b, h = _json_bytes({"error": "not found"}, 404)
                    self._send(st, b, h)
                    return
                raw_path = str(body.get("path", "")).strip()
                if not raw_path:
                    st, b, h = _json_bytes({"error": "path required"}, 400)
                    self._send(st, b, h)
                    return
                try:
                    wt = Path(raw_path).expanduser().resolve()
                except OSError as exc:
                    st, b, h = _json_bytes({"error": f"invalid path: {exc}"}, 400)
                    self._send(st, b, h)
                    return
                if str(wt) not in meta.worktrees:
                    st, b, h = _json_bytes(
                        {"error": "worktree is not registered with this task"}, 400
                    )
                    self._send(st, b, h)
                    return
                result = merge_worktree_to_base(wt)
                print(
                    f"[web] merge worktree slug={slug} path={wt} "
                    f"ok={result.get('ok')} {result.get('branch')}->{result.get('base')}",
                    flush=True,
                )
                st, b, h = _json_bytes(result, 200 if result.get("ok") else 400)
                self._send(st, b, h)
                return

            m_review = re.match(r"^/api/tasks/([^/]+)/review$", path)
            if m_review:
                root, project_id = self._resolve_scope(parsed)
                if root is None or project_id is None:
                    self._bad_project()
                    return
                slug = m_review.group(1)
                if not _SLUG_RE.match(slug):
                    st, b, h = _json_bytes({"error": "invalid slug"}, 400)
                    self._send(st, b, h)
                    return
                meta = read_meta(root, slug)
                if not meta:
                    st, b, h = _json_bytes({"error": "not found"}, 404)
                    self._send(st, b, h)
                    return
                raw_path = str(body.get("path", "")).strip()
                if not raw_path:
                    st, b, h = _json_bytes({"error": "path required"}, 400)
                    self._send(st, b, h)
                    return
                try:
                    wt = Path(raw_path).expanduser().resolve()
                except OSError as exc:
                    st, b, h = _json_bytes({"error": f"invalid path: {exc}"}, 400)
                    self._send(st, b, h)
                    return
                if str(wt) not in meta.worktrees:
                    st, b, h = _json_bytes(
                        {"error": "worktree is not registered with this task"}, 400
                    )
                    self._send(st, b, h)
                    return
                skills_text = load_skills_text(
                    meta.skills_path, default_skills, limit_total=8000
                )
                result = _run_worktree_review(
                    wt,
                    str(body.get("rules", "")),
                    skills_text,
                    model=meta.interview_model or "",
                )
                print(
                    f"[web] review worktree slug={slug} path={wt} ok={result.get('ok')}",
                    flush=True,
                )
                st, b, h = _json_bytes(result, 200 if result.get("ok") else 502)
                self._send(st, b, h)
                return

            m_wt_push_all = re.match(r"^/api/tasks/([^/]+)/worktrees/push-all$", path)
            if m_wt_push_all:
                root, project_id = self._resolve_scope(parsed)
                if root is None or project_id is None:
                    self._bad_project()
                    return
                slug = m_wt_push_all.group(1)
                if not _SLUG_RE.match(slug):
                    st, b, h = _json_bytes({"error": "invalid slug"}, 400)
                    self._send(st, b, h)
                    return
                meta = read_meta(root, slug)
                if not meta:
                    st, b, h = _json_bytes({"error": "not found"}, 404)
                    self._send(st, b, h)
                    return
                results: list[dict[str, Any]] = []
                for p_str in meta.worktrees:
                    wt = Path(p_str)
                    row = push_worktree_branch(wt)
                    row["path"] = p_str
                    row["status"] = worktree_status(wt)
                    results.append(row)
                ok_all = bool(results) and all(r.get("ok") for r in results)
                print(
                    f"[web] push-all slug={slug} ok={sum(1 for r in results if r.get('ok'))}/{len(results)}",
                    flush=True,
                )
                openclaw_client.emit(
                    "worktrees-pushed",
                    instruction=f"Loom pushed worktree branches for task {slug}",
                    project_root=root,
                    task_slug=slug,
                    data={"results": results},
                )
                st, b, h = _json_bytes(
                    {"ok": ok_all, "count": len(results), "results": results}
                )
                self._send(st, b, h)
                return

            m_resume = re.match(r"^/api/tasks/([^/]+)/claude/resume$", path)
            if m_resume:
                root, project_id = self._resolve_scope(parsed)
                if root is None or project_id is None:
                    self._bad_project()
                    return
                slug = m_resume.group(1)
                if not _SLUG_RE.match(slug):
                    st, b, h = _json_bytes({"error": "invalid slug"}, 400)
                    self._send(st, b, h)
                    return
                sid = str(body.get("session_id", "")).strip()
                if not _SESSION_ID_RE.match(sid):
                    st, b, h = _json_bytes({"error": "invalid session_id"}, 400)
                    self._send(st, b, h)
                    return
                result = claude_registry.start(root, project_id, slug, resume_session_id=sid)
                print(
                    f"[web] resume claude slug={slug} session={sid} ok={bool(result.get('ok'))} "
                    f"target={result.get('target', '')}",
                    flush=True,
                )
                openclaw_client.emit(
                    "claude-resume",
                    instruction=f"Loom Claude pane resumed for task {slug}",
                    project_root=root,
                    task_slug=slug,
                    data={**result, "session_id": sid},
                )
                st, b, h = (
                    _json_bytes(result)
                    if result.get("ok")
                    else _json_bytes(result, 400)
                )
                self._send(st, b, h)
                return

            st, b, h = _json_bytes({"error": "not found"}, 404)
            self._send(st, b, h)

        # ===== PUT =====

        def do_PUT(self) -> None:  # noqa: N802
            if not self._require_auth():
                return
            parsed = urlparse(self.path)
            path = parsed.path
            body = _read_json(self)

            if routes_ar.handle_put(self, path, parsed, body):
                return
            if path == "/api/notes":
                root, _pid = self._resolve_scope(parsed)
                if root is None:
                    self._bad_project()
                    return
                content = body.get("content", "")
                if not isinstance(content, str):
                    st, b, h = _json_bytes({"error": "content must be string"}, 400)
                    self._send(st, b, h)
                    return
                if not write_project_notes(root, content):
                    st, b, h = _json_bytes({"error": "failed to write NOTES.md"}, 500)
                    self._send(st, b, h)
                    return
                st, b, h = _json_bytes({"ok": True})
                self._send(st, b, h)
                return

            m_meta = re.match(r"^/api/tasks/([^/]+)/meta$", path)
            if m_meta:
                root, _pid = self._resolve_scope(parsed)
                if root is None:
                    self._bad_project()
                    return
                slug = m_meta.group(1)
                if not _SLUG_RE.match(slug):
                    st, b, h = _json_bytes({"error": "invalid slug"}, 400)
                    self._send(st, b, h)
                    return
                title = body.get("title")
                goal = body.get("general_goal")
                agent_in = body.get("agent")
                skills_in = body.get("skills_path")
                model_in = body.get("interview_model")
                if (
                    title is None
                    and goal is None
                    and agent_in is None
                    and skills_in is None
                    and model_in is None
                ):
                    st, b, h = _json_bytes(
                        {
                            "error": "supply title and/or general_goal and/or "
                            "agent and/or skills_path and/or interview_model"
                        },
                        400,
                    )
                    self._send(st, b, h)
                    return
                if agent_in is not None:
                    raw = str(agent_in).strip().lower()
                    if raw not in SUPPORTED_AGENTS:
                        st, b, h = _json_bytes(
                            {"error": f"agent must be one of {sorted(SUPPORTED_AGENTS)}"},
                            400,
                        )
                        self._send(st, b, h)
                        return
                    update_meta(
                        root,
                        slug,
                        agent=raw,
                        # A model from the other CLI is generally invalid
                        # (claude-* for Codex or gpt-* for Claude). Switching
                        # agent resets to that CLI's configured default unless
                        # the request explicitly supplies a model below.
                        interview_model=(
                            agent_default_model(raw) if model_in is None else None
                        ),
                    )
                if skills_in is not None:
                    if isinstance(skills_in, list):
                        skills_in = SKILLS_PATH_SEP.join(str(x) for x in skills_in)
                    try:
                        cands = [
                            p.resolve() for p in split_skills_paths(str(skills_in))
                        ]
                    except OSError as exc:
                        st, b, h = _json_bytes({"error": f"invalid skills_path: {exc}"}, 400)
                        self._send(st, b, h)
                        return
                    bad = [
                        str(c) for c in cands
                        if not c.is_file() or c.suffix.lower() != ".md"
                    ]
                    if not cands or bad:
                        st, b, h = _json_bytes(
                            {"error": "every skills_path entry must be an existing markdown file",
                             "invalid": bad},
                            400,
                        )
                        self._send(st, b, h)
                        return
                    update_meta(root, slug, skills_path=join_skills_paths(cands))
                if model_in is not None:
                    model = str(model_in).strip()
                    if not model:
                        current = read_meta(root, slug)
                        model = agent_default_model(
                            current.agent if current is not None else AGENT_CURSOR
                        )
                    update_meta(root, slug, interview_model=model)
                updated = rename_task_meta(
                    root,
                    slug,
                    title=str(title) if title is not None else None,
                    general_goal=str(goal) if goal is not None else None,
                ) or read_meta(root, slug)
                if updated is None:
                    st, b, h = _json_bytes({"error": "not found"}, 404)
                    self._send(st, b, h)
                    return
                st, b, h = _json_bytes({"ok": True, "meta": updated.to_dict()})
                self._send(st, b, h)
                return

            m = re.match(r"^/api/tasks/([^/]+)/template$", path)
            if not m:
                st, b, h = _json_bytes({"error": "not found"}, 404)
                self._send(st, b, h)
                return
            root, _pid = self._resolve_scope(parsed)
            if root is None:
                self._bad_project()
                return
            slug = m.group(1)
            if not _SLUG_RE.match(slug):
                st, b, h = _json_bytes({"error": "invalid slug"}, 400)
                self._send(st, b, h)
                return
            name = str(body.get("name", ""))
            content = body.get("content", "")
            if not isinstance(content, str):
                st, b, h = _json_bytes({"error": "content must be string"}, 400)
                self._send(st, b, h)
                return
            if not write_template(root, slug, name, content):
                st, b, h = _json_bytes({"error": "invalid template"}, 400)
                self._send(st, b, h)
                return
            st, b, h = _json_bytes({"ok": True})
            self._send(st, b, h)

        # ===== DELETE =====

        def do_DELETE(self) -> None:  # noqa: N802
            if not self._require_auth():
                return
            parsed = urlparse(self.path)
            path = parsed.path

            if routes_agent.handle_delete(self, path, parsed):
                return
            if routes_rebuttal.handle_delete(self, path, parsed):
                return
            if routes_review.handle_delete(self, path, parsed):
                return
            if routes_ar.handle_delete(self, path, parsed):
                return
            m_mon_del = re.match(r"^/api/tasks/([^/]+)/monitor$", path)
            if m_mon_del:
                root, project_id = self._resolve_scope(parsed)
                if root is None or project_id is None:
                    self._bad_project()
                    return
                slug = m_mon_del.group(1)
                if not _SLUG_RE.match(slug):
                    st, b, h = _json_bytes({"error": "invalid slug"}, 400)
                    self._send(st, b, h)
                    return
                if not read_meta(root, slug):
                    st, b, h = _json_bytes({"error": "not found"}, 404)
                    self._send(st, b, h)
                    return
                result = monitor_manager.disable(root, project_id, slug)
                print(f"[web] monitor disabled slug={slug}", flush=True)
                st, b, h = _json_bytes(result)
                self._send(st, b, h)
                return

            m_wt_del = re.match(r"^/api/tasks/([^/]+)/worktree$", path)
            if m_wt_del:
                root, project_id = self._resolve_scope(parsed)
                if root is None or project_id is None:
                    self._bad_project()
                    return
                slug = m_wt_del.group(1)
                if not _SLUG_RE.match(slug):
                    st, b, h = _json_bytes({"error": "invalid slug"}, 400)
                    self._send(st, b, h)
                    return
                meta = read_meta(root, slug)
                if not meta:
                    st, b, h = _json_bytes({"error": "not found"}, 404)
                    self._send(st, b, h)
                    return
                qs = parse_qs(parsed.query or "")
                raw_path = (qs.get("path") or [""])[0].strip()
                if not raw_path:
                    st, b, h = _json_bytes({"error": "path query param required"}, 400)
                    self._send(st, b, h)
                    return
                try:
                    wt_target = Path(raw_path).expanduser().resolve()
                except OSError as exc:
                    st, b, h = _json_bytes({"error": f"invalid path: {exc}"}, 400)
                    self._send(st, b, h)
                    return
                ok_rm, msg_rm = remove_task_worktree(root, slug, wt_target)
                print(
                    f"[web] remove worktree slug={slug} path={wt_target} "
                    f"ok={ok_rm} msg={msg_rm}",
                    flush=True,
                )
                if not ok_rm:
                    st, b, h = _json_bytes({"ok": False, "error": msg_rm}, 400)
                    self._send(st, b, h)
                    return
                openclaw_client.emit(
                    "worktree-removed",
                    instruction=f"Loom worktree removed for task {slug}",
                    project_root=root,
                    task_slug=slug,
                    data={"worktree": str(wt_target)},
                )
                updated = read_meta(root, slug)
                st, b, h = _json_bytes(
                    {
                        "ok": True,
                        "message": msg_rm,
                        "meta": updated.to_dict() if updated else None,
                    }
                )
                self._send(st, b, h)
                return

            m_task_del = re.match(r"^/api/tasks/([^/]+)$", path)
            if m_task_del:
                root, _pid = self._resolve_scope(parsed)
                if root is None:
                    self._bad_project()
                    return
                slug = m_task_del.group(1)
                if read_meta(root, slug) is None:
                    st, b, h = _json_bytes({"error": "task not found"}, 404)
                    self._send(st, b, h)
                    return
                ok_task, err_task = delete_task(root, slug)
                if not ok_task:
                    status = 404 if err_task == "task not found" else 400
                    st, b, h = _json_bytes({"error": err_task}, status)
                    self._send(st, b, h)
                    return
                st, b, h = _json_bytes(
                    {"ok": True, "slug": slug}
                )
                self._send(st, b, h)
                return

            m_del = re.match(r"^/api/projects/([^/]+)$", path)
            if not m_del:
                st, b, h = _json_bytes({"error": "not found"}, 404)
                self._send(st, b, h)
                return
            pid_del = m_del.group(1)
            ok_del, err_msg = pr.remove(pid_del)
            if not ok_del:
                st, b, h = _json_bytes({"error": err_msg}, 400)
                self._send(st, b, h)
                return
            st, b, h = _json_bytes(
                {
                    "ok": True,
                    "projects": pr.list_projects(),
                    "defaultProjectId": pr.default_project_id,
                }
            )
            self._send(st, b, h)

    Handler.pr = pr
    Handler.terminal_streams = terminal_streams
    Handler.ar_manager = ar_manager
    Handler.claude_registry = claude_registry
    Handler.auth_token = required_token
    return Handler


# --- Bootstrap --------------------------------------------------------------


def serve(
    host: str,
    port: int,
    project_root: Path,
    default_skills: Path,
    openclaw_config: OpenClawConfig | None = None,
    auth_token: str = "",
    *,
    multi_project_workspace: bool = False,
) -> None:
    project_root = project_root.resolve()
    os.environ["LOOM_PROJECT_ROOT"] = str(project_root)
    web_project_registry = WebProjectRegistry()
    if multi_project_workspace:
        web_project_registry.prune_redundant_parent_projects(project_root)
    # AR tasks belong to no code project, so they get a root of their own that
    # is always there - registering it means a new AR task has somewhere to go
    # without the user creating a folder first.
    _ar_root, _ar_created = ar.ensure_ar_root()
    web_project_registry.ensure_project(_ar_root, name=_ar_root.name)
    claude_registry = ClaudeRegistry()
    openclaw_client = OpenClawClient(openclaw_config)
    monitor_manager = TaskMonitorManager(openclaw_client)
    # Resume per-task run monitors that were left enabled, so the Notify toggle
    # survives a server restart without re-opening each task.
    _monitor_projects: list[tuple[str, Path]] = []
    try:
        for _p in web_project_registry.list_projects():
            _pid, _pp = _p.get("id"), _p.get("path")
            if _pid and _pp:
                _monitor_projects.append((str(_pid), Path(_pp)))
    except Exception:  # noqa: BLE001
        pass
    _resumed = monitor_manager.resume_enabled(_monitor_projects)
    if _resumed:
        print(f"  Resumed {_resumed} enabled run-monitor(s)", flush=True)
    # A previous server killed hard leaves its web-terminal attaches behind as
    # init's children; they hold every session "attached" (and small) forever.
    _reaped = reap_orphaned_attaches()
    if _reaped:
        print(f"  Reaped {_reaped} orphaned web-terminal attach(es)", flush=True)
    sk = default_skills if default_skills.is_file() else bundled_skills_path().resolve()
    activity_watcher = AgentActivityWatcher(web_project_registry)
    activity_watcher.start()
    # Agents that support a stop hook report their own completion, which beats
    # watching their pane for it. The watcher above stays as the fallback for
    # the ones that don't.
    for _note in agent_hooks.install(port):
        print(f"  Stop hook {_note}", flush=True)
    ar_manager = ARLoopManager(openclaw_client, claude_registry, sk)
    _review_swept = _sweep_stale_review_runs()
    if _review_swept:
        print(f"  Cleared {_review_swept} interrupted review run(s)", flush=True)
    _ar_swept = ar_manager.sweep_stale_jobs(_monitor_projects)
    if _ar_swept:
        print(
            f"  Cleared {_ar_swept} interrupted AR job(s) "
            "(search/mine/ideas/review)",
            flush=True,
        )
    _ar_resumed = ar_manager.resume_running(_monitor_projects)
    if _ar_resumed:
        print(f"  Resumed {_ar_resumed} running AR paper loop(s)", flush=True)
    _rebuttal_swept = rebuttal.sweep_interrupted_jobs()
    if _rebuttal_swept:
        print(
            f"  Cleared {_rebuttal_swept} interrupted Rebuttal Factory job(s)",
            flush=True,
        )
    _rebuttal_agents_resumed = _rebuttal_resume_agent_watchers()
    if _rebuttal_agents_resumed:
        print(
            f"  Resumed {_rebuttal_agents_resumed} live Rebuttal Agent watcher(s)",
            flush=True,
        )
    _rebuttal_delivery_resumed = _rebuttal_resume_delivery_watchers()
    if _rebuttal_delivery_resumed:
        print(
            f"  Resumed {_rebuttal_delivery_resumed} delivery Agent watcher(s)",
            flush=True,
        )
    handler = make_handler(
        web_project_registry,
        project_root,
        sk,
        claude_registry,
        openclaw_client,
        auth_token,
        multi_project_workspace=multi_project_workspace,
        monitor_manager=monitor_manager,
        ar_manager=ar_manager,
        activity_watcher=activity_watcher,
    )
    server = ThreadingHTTPServer((host, port), handler)
    rud_root = project_root / ".RUD"
    print("", flush=True)
    print("Loom", flush=True)
    print(f"  URL:              http://{host}:{port}/", flush=True)
    print(
        f"  Server cwd:       {project_root}  (--project / launch directory; not auto-registered)"
        f"{'  [multi-project workspace: --projects]' if multi_project_workspace else ''}",
        flush=True,
    )
    print(f"  Project registry: {web_project_registry.persist_path}", flush=True)
    print(f"  Task root:        {rud_root}", flush=True)
    print(f"  Project notes:    {rud_root}/NOTES.md", flush=True)
    print(
        f"  AR root:          {_ar_root}"
        f"{'  (created)' if _ar_created else ''}"
        f"  [override with {AR_ROOT_ENV}]",
        flush=True,
    )
    print(f"  Static assets:    {web_static_dir().resolve()}", flush=True)
    print(f"  Default skills:   {sk}", flush=True)
    print("  Tabs:             Claude, PLAN.md (per task) + Notes button (per project)", flush=True)
    print(f"  Auth:             {'enabled' if auth_token.strip() else 'disabled'}", flush=True)
    # Mint the bots' scoped credential before the first request needs it.
    routes_agent.agent_token()
    print(
        f"  Agents:           MCP at http://{host}:{port}/mcp;"
        f" bot token {routes_agent.agent_token_path()}  (`loom agent-config`)",
        flush=True,
    )
    print(f"  OpenClaw:         {openclaw_status(openclaw_client.config)}", flush=True)
    print("", flush=True)
    openclaw_client.emit(
        "web-start",
        instruction=f"Loom web started for project {project_root}",
        project_root=project_root,
        data={"url": f"http://{host}:{port}/", "taskRoot": str(rud_root)},
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
