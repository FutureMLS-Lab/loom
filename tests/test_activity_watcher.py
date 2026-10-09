"""Turning an agent's stop hook into "this task finished"."""

from __future__ import annotations

import json
import threading

from loom.web import AgentActivityWatcher
from loom.usage_budget import claim_auto_rotation, parse_pane_usage, read_budget_marker


class FakeRegistry:
    def __init__(self, projects):
        self._projects = projects

    def list_projects(self):
        return self._projects


def test_pane_tag_identifies_the_task_directly():
    """The tag Loom stamps on the pane needs no filesystem lookup."""
    watcher = AgentActivityWatcher(FakeRegistry([]))
    assert watcher.report_finished("", "abc123/my-task") == ("abc123", "my-task")

    snap = watcher.snapshot()
    entry = snap["tasks"]["abc123/my-task"]
    assert entry["finished_at"] > 0
    assert entry["working"] is False


def test_ack_clears_the_ring():
    watcher = AgentActivityWatcher(FakeRegistry([]))
    watcher.report_finished("", "abc123/my-task")
    watcher.ack("abc123", "my-task")
    assert watcher.snapshot()["tasks"]["abc123/my-task"]["finished_at"] == 0


def test_projects_aggregate_counts_working_and_finished():
    """The project chip needs both counts: steady light while an agent runs,
    a blink once one has finished unseen."""
    watcher = AgentActivityWatcher(FakeRegistry([]))
    watcher.report_finished("", "abc123/done-task")
    with watcher._lock:
        watcher._state[("abc123", "busy-task")] = {
            "working": True,
            "idle_polls": 0,
            "finished_at": 0.0,
        }
    agg = watcher.snapshot()["projects"]["abc123"]
    assert agg == {"working": 1, "finished": 1}


def test_garbage_tags_are_ignored():
    watcher = AgentActivityWatcher(FakeRegistry([]))
    for bad in ("", "no-slash", "/", "abc/"):
        assert watcher.report_finished("", bad) is None


def test_falls_back_to_the_directory_for_panes_loom_did_not_start(tmp_path):
    project = tmp_path / "proj"
    task = project / ".RUD" / "some-task"
    (task / "work").mkdir(parents=True)
    (task / "task.json").write_text('{"slug": "some-task", "title": "t"}')

    watcher = AgentActivityWatcher(
        FakeRegistry([{"id": "pid1", "path": str(project)}])
    )
    # A worktree sits below the task directory; the deeper match still resolves
    # to the task that owns it.
    assert watcher.report_finished(str(task / "work"), "") == ("pid1", "some-task")


def test_a_directory_outside_every_task_reports_nothing(tmp_path):
    project = tmp_path / "proj"
    (project / ".RUD").mkdir(parents=True)
    watcher = AgentActivityWatcher(
        FakeRegistry([{"id": "pid1", "path": str(project)}])
    )
    # The repository root is what a stop event actually carries, and it maps to
    # no single task - guessing one would ring the wrong thing.
    assert watcher.report_finished(str(project), "") is None


def test_cursor_status_usage_is_parsed(monkeypatch):
    monkeypatch.setenv("LOOM_TURN_TOKEN_BUDGET", "1500000")
    monkeypatch.setenv("LOOM_CONTEXT_PERCENT_BUDGET", "50")
    usage = parse_pane_usage(
        "Running subagent  2.78M tokens\n"
        "GPT-5.6 Sol 272K Max Fast · 57.3% · 3 files edited  Run Everything\n"
    )
    assert usage["turn_tokens"] == 2_780_000
    assert usage["context_percent"] == 57.3


def test_activity_watcher_interrupts_runaway_turn_once(tmp_path, monkeypatch):
    project = tmp_path / "proj"
    task = project / ".RUD" / "costly-task"
    task.mkdir(parents=True)
    monkeypatch.setenv("LOOM_TURN_TOKEN_BUDGET", "1000000")
    monkeypatch.setenv("LOOM_CONTEXT_PERCENT_BUDGET", "80")
    keys = []
    monkeypatch.setattr(
        "loom.web_activity.send_pane_key",
        lambda target, key: (keys.append((target, key)), (True, ""))[1],
    )
    watcher = AgentActivityWatcher(FakeRegistry([]))
    pane = (
        "Running subagent  1.20M tokens\n"
        "→ Add a follow-up  ctrl+c to stop\n"
        "GPT-5.6 Sol 272K Max Fast · 42.0% · 1 file edited  Run Everything\n"
    )

    watcher._observe_pane("p1", project, "costly-task", "loom-x:0.0", pane, 10.0)
    watcher._observe_pane("p1", project, "costly-task", "loom-x:0.0", pane, 14.0)

    assert keys == [("loom-x:0.0", "C-c")]
    marker = read_budget_marker(task)
    assert marker["rotate_required"] is True
    assert marker["usage"]["turn_tokens"] == 1_200_000
    stop = watcher.snapshot()["tasks"]["p1/costly-task"]["budget_stop"]
    assert stop["interrupt_ok"] is True


def test_auto_rotation_quota_is_persistent_and_bounded(tmp_path, monkeypatch):
    monkeypatch.setenv("LOOM_AUTO_ROTATE_BUDGET_LIMIT", "2")
    task = tmp_path / "task"

    first = claim_auto_rotation(task, now=100.0)
    second = claim_auto_rotation(task, now=101.0)
    blocked = claim_auto_rotation(task, now=102.0)
    reset = claim_auto_rotation(task, now=100.0 + 24 * 60 * 60)

    assert first["allowed"] is True and first["attempts"] == 1
    assert second["allowed"] is True and second["attempts"] == 2
    assert blocked["allowed"] is False and blocked["attempts"] == 2
    assert reset["allowed"] is True and reset["attempts"] == 1


def test_budget_stop_auto_rotates_an_ordinary_task(tmp_path, monkeypatch):
    project = tmp_path / "proj"
    task = project / ".RUD" / "overnight-task"
    task.mkdir(parents=True)
    (task / "task.json").write_text(
        json.dumps({"slug": "overnight-task", "title": "t", "kind": "agent"})
    )
    monkeypatch.setenv("LOOM_TURN_TOKEN_BUDGET", "1000000")
    monkeypatch.setenv("LOOM_CONTEXT_PERCENT_BUDGET", "80")
    monkeypatch.setenv("LOOM_AUTO_ROTATE_BUDGET_LIMIT", "2")
    monkeypatch.setattr("loom.web_activity.send_pane_key", lambda t, k: (True, ""))
    monkeypatch.setattr("loom.web_activity.time.sleep", lambda _: None)
    rotated = threading.Event()

    def rotate(root, project_id, slug):
        assert root == project
        assert project_id == "p1"
        assert slug == "overnight-task"
        rotated.set()
        return {"ok": True, "rotated": True}

    watcher = AgentActivityWatcher(FakeRegistry([]), budget_rotate=rotate)
    pane = (
        "Running subagent  1.20M tokens\n"
        "→ Add a follow-up  ctrl+c to stop\n"
        "Grok 4.7 High Fast · 42.0% · 1 file edited  Run Everything\n"
    )
    watcher._observe_pane("p1", project, "overnight-task", "loom-x:0.0", pane, 10.0)

    assert rotated.wait(1.0)
