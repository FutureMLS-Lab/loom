from __future__ import annotations

import subprocess
import time
from pathlib import Path

import pytest

from loom import development_task as development
from loom.rud_task import create_task, prepare_task_worktree_from


def git(cwd: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, check=True
    )
    return proc.stdout.strip()


def make_development_task(tmp_path: Path, *, max_rounds: int = 2):
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init")
    git(repo, "config", "user.email", "test@example.com")
    git(repo, "config", "user.name", "Test")
    (repo / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
    git(repo, "add", "app.py")
    git(repo, "commit", "-m", "initial")

    project = tmp_path / "project"
    project.mkdir()
    meta = create_task(
        project,
        "Develop feature",
        "Change VALUE safely",
        kind=development.KIND_DEVELOPMENT,
        auto_worktree=False,
    )
    worktree, _branch, message = prepare_task_worktree_from(project, meta.slug, repo)
    assert worktree is not None, message
    state = development.initialize_task(
        project,
        meta.slug,
        test_command="pytest -q",
        max_review_rounds=max_rounds,
    )
    return project, meta.slug, worktree, state


def wait_for_stage(project: Path, slug: str, wanted: str, timeout: float = 5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        state = development.read_state(project, slug)
        if state.get("stage") == wanted:
            return state
        time.sleep(0.02)
    raise AssertionError(f"stage never became {wanted}: {development.read_state(project, slug)}")


def wait_for_path_missing(path: Path, timeout: float = 5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not path.exists():
            return
        time.sleep(0.02)
    raise AssertionError(f"temporary review snapshot was not removed: {path}")


def reviewer_result(role: str, *, blocking: bool = False):
    category = "requirements_behavior" if role == "A" else "component_boundary"
    return {
        "ok": True,
        "role": role,
        "verdict": "request_changes" if blocking else "approve",
        "summary": "found a defect" if blocking else "looks sound",
        "findings": (
            [{
                "id": f"{role}-001",
                "category": category,
                "severity": "P1",
                "title": "Incorrect value",
                "file": "app.py",
                "line": 1,
                "evidence": "VALUE is wrong",
                "impact": "behavior is wrong",
                "recommendation": "set the right value",
                "test": "assert VALUE",
            }]
            if blocking
            else []
        ),
    }


def test_development_rounds_repair_and_human_gate(tmp_path):
    project, slug, worktree, initial = make_development_task(tmp_path)
    assert initial["reviewer_model"] == development.DEFAULT_REVIEWER_MODEL
    assert development.reviewer_skill_path("A") != development.reviewer_skill_path("B")

    (worktree / "app.py").write_text("VALUE = 2\n", encoding="utf-8")
    git(worktree, "add", "app.py")
    git(worktree, "commit", "-m", "change value")

    def first_runner(_prompt, _model, _workspace, role):
        return reviewer_result(role, blocking=role == "B")

    started = development.start_review(project, slug, runner=first_runner)
    assert started["status"] == "reviewing"
    state = wait_for_stage(project, slug, "repair_needed")
    assert state["rounds"][0]["blocking"] is True
    assert "B-001" in development.repair_prompt(project, slug)

    development.mark_repair_started(project, slug)
    (worktree / "app.py").write_text("VALUE = 3\n", encoding="utf-8")
    git(worktree, "add", "app.py")
    git(worktree, "commit", "-m", "repair value")

    development.start_review(
        project,
        slug,
        runner=lambda _prompt, _model, _workspace, role: reviewer_result(role),
    )
    state = wait_for_stage(project, slug, "human_gate")
    assert len(state["rounds"]) == 2
    approved = development.approve(project, slug)
    assert approved["stage"] == "ready_to_merge"
    assert approved["approved_at"]


def test_review_requires_clean_committed_checkpoint(tmp_path):
    project, slug, worktree, _state = make_development_task(tmp_path)
    with pytest.raises(ValueError, match="no committed changes"):
        development.start_review(project, slug)

    (worktree / "app.py").write_text("VALUE = 2\n", encoding="utf-8")
    with pytest.raises(ValueError, match="leave the worktree clean"):
        development.start_review(project, slug)


def test_partial_retry_runs_only_failed_reviewer(tmp_path):
    project, slug, worktree, _state = make_development_task(tmp_path)
    (worktree / "app.py").write_text("VALUE = 2\n", encoding="utf-8")
    git(worktree, "add", "app.py")
    git(worktree, "commit", "-m", "candidate")
    first_calls: list[str] = []

    def first_runner(_prompt, _model, _workspace, role):
        first_calls.append(role)
        if role == "B":
            return {"ok": False, "role": role, "error": "temporary failure"}
        return reviewer_result(role)

    development.start_review(project, slug, runner=first_runner)
    wait_for_stage(project, slug, "review_error")
    assert set(first_calls) == {"A", "B"}

    retry_calls: list[str] = []
    review_workspaces: list[Path] = []

    def retry_runner(_prompt, _model, _workspace, role):
        retry_calls.append(role)
        review_workspaces.append(_workspace)
        assert _workspace != worktree
        assert git(_workspace, "rev-parse", "HEAD") == git(worktree, "rev-parse", "HEAD")
        return reviewer_result(role)

    development.start_review(project, slug, runner=retry_runner)
    state = wait_for_stage(project, slug, "human_gate")
    assert retry_calls == ["B"]
    wait_for_path_missing(review_workspaces[0])
    assert state["rounds"][0]["reviewers"]["A"]["ok"] is True
    assert state["rounds"][0]["reviewers"]["B"]["ok"] is True


def test_normalize_review_promotes_blocking_severity():
    normalized = development._normalize_review(
        {
            "verdict": "approve",
            "summary": "missed the severity",
            "findings": [{
                "category": "edge_state",
                "severity": "P1",
                "title": "Race",
                "file": "worker.py",
                "line": 12,
            }],
        },
        "A",
    )
    assert normalized["verdict"] == "request_changes"
    assert normalized["findings"][0]["id"] == "A-001"
    assert normalized["findings"][0]["category"] == "edge_state"


def test_normalize_review_enforces_reviewer_ownership():
    with pytest.raises(ValueError, match="Reviewer A finding category"):
        development._normalize_review(
            {
                "verdict": "request_changes",
                "summary": "architecture-only concern",
                "findings": [{
                    "category": "component_boundary",
                    "severity": "P1",
                    "title": "Layer violation",
                }],
            },
            "A",
        )

    normalized = development._normalize_review(
        {
            "verdict": "approve",
            "summary": "bounded architecture suggestion",
            "findings": [{
                "category": "maintainability",
                "severity": "P3",
                "title": "Duplicated authority",
            }],
        },
        "B",
    )
    assert normalized["findings"][0]["category"] == "maintainability"
