"""The default prompt and project memory reach every task prompt."""

from __future__ import annotations

import json
from pathlib import Path

import loom.web as web
from loom.paths import default_prompt_path


def make_task(tmp_path: Path, slug: str = "t1") -> None:
    task = tmp_path / ".RUD" / slug
    (task / "work").mkdir(parents=True)
    (task / "task.json").write_text(json.dumps({
        "slug": slug, "title": "T", "general_goal": "G", "agent": "claude",
    }))


def test_default_prompt_is_always_injected(tmp_path):
    make_task(tmp_path)
    prompt = web._build_claude_prompt(tmp_path, "t1")
    assert "Default prompt (always active" in prompt
    assert "Simplicity first" in prompt          # content, not just the header
    assert "Project memory" in prompt            # protocol present even when empty


def test_project_memory_is_injected_and_tail_capped(tmp_path):
    make_task(tmp_path)
    memory = tmp_path / ".RUD" / "MEMORY.md"
    filler = "\n".join(f"- [old] lesson {i}" for i in range(400))
    memory.write_text(filler + "\n- [recent] never trust the cache\n")
    prompt = web._build_claude_prompt(tmp_path, "t1")
    # The newest lesson survives the tail cap; the oldest may not.
    assert "never trust the cache" in prompt
    assert "lesson 0\n" not in prompt


def test_default_prompt_is_not_a_picker_option(tmp_path):
    options = web._available_skill_options(default_prompt_path())
    paths = {o["path"] for o in options}
    assert str(default_prompt_path().resolve()) not in paths


def test_oversize_session_resume_is_blocked_before_launch(tmp_path, monkeypatch):
    from loom import rud_task

    fake_home = tmp_path / "home"
    monkeypatch.setattr(rud_task.Path, "home", classmethod(lambda cls: fake_home))
    monkeypatch.setenv("LOOM_SESSION_CONTEXT_BUDGET_MB", "0.25")
    meta = rud_task.create_task(
        tmp_path,
        "budgeted",
        "goal",
        agent="cursor",
        auto_worktree=False,
    )
    cwd = web._task_pane_cwd(tmp_path, meta.slug, meta)
    sid = "11111111-2222-3333-4444-555555555555"
    chat = fake_home / ".cursor" / "chats" / "workspace" / sid
    chat.mkdir(parents=True)
    (chat / "meta.json").write_text(
        json.dumps({"cwd": str(cwd.resolve())}), encoding="utf-8"
    )
    transcript = (
        fake_home
        / ".cursor"
        / "projects"
        / "workspace"
        / "agent-transcripts"
        / sid
        / f"{sid}.jsonl"
    )
    transcript.parent.mkdir(parents=True)
    transcript.write_bytes(b"x" * (300 * 1024))
    rud_task.add_claude_session(tmp_path, meta.slug, sid)

    result = web.ClaudeRegistry().start(
        tmp_path,
        "project",
        meta.slug,
        resume_session_id=sid,
    )

    assert result["ok"] is False
    assert result["code"] == "context_budget_exceeded"
    assert result["session"]["over_budget"] is True


def test_fresh_session_handoff_reloads_files_without_replaying_full_prompt(tmp_path):
    make_task(tmp_path)
    handoff = web._build_session_handoff_prompt(
        tmp_path,
        "t1",
        user_text="continue the audit",
    )

    assert "continue the audit" in handoff
    assert str(tmp_path / ".RUD" / "t1" / "PLAN.md") in handoff
    assert str(default_prompt_path()) in handoff
    assert "Simplicity first" not in handoff


def test_live_context_limit_persists_rotation_requirement(tmp_path, monkeypatch):
    from loom import rud_task
    from loom.usage_budget import read_budget_marker

    meta = rud_task.create_task(
        tmp_path,
        "live budget",
        "goal",
        agent="cursor",
        auto_worktree=False,
    )
    target = "loom-cursor-project-live-budget:0.0"
    rud_task.update_meta(tmp_path, meta.slug, tmux_interview_target=target)
    monkeypatch.setenv("LOOM_CONTEXT_PERCENT_BUDGET", "50")
    monkeypatch.setattr(
        web,
        "capture_pane",
        lambda _target, _lines: (
            True,
            "GPT-5.6 Sol 272K Max Fast · 61.0% · Run Everything\n",
        ),
    )

    status = web.ClaudeRegistry().context_status(tmp_path, meta.slug)

    assert status["over_budget"] is True
    assert status["rotation_required"] is True
    marker = read_budget_marker(rud_task.task_root(tmp_path, meta.slug))
    assert marker["reason"] == "context reached 61.0% (limit 50.0%)"

    blocked = web.ClaudeRegistry().start(
        tmp_path,
        "project",
        meta.slug,
        resume_session_id="11111111-2222-3333-4444-555555555555",
    )
    assert blocked["code"] == "context_budget_exceeded"
    assert "61.0%" in blocked["reason"]
