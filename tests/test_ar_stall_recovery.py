"""An author that stops without its note must not park the loop forever."""

from __future__ import annotations

import json
from pathlib import Path

import loom.web_jobs as web
from loom import ar_task as ar
from loom.web_jobs import _ARLoopDriver


class FakeManager:
    def __init__(self, alive: bool) -> None:
        self.alive = alive
        self.events: list[str] = []
        self.rotations = 0
        self.openclaw = self

    def pane_alive(self, target: str) -> bool:
        return self.alive

    def emit(self, event: str, **kwargs) -> None:
        self.events.append(event)

    def rotate_pane(self, project_root, project_id, slug):
        self.rotations += 1
        return {"ok": True, "target": "loom-cursor-x:0.0"}

    def wait_until_ready(self, target: str) -> None:
        return None


def make_paper(tmp_path: Path, slug: str = "paper-x") -> Path:
    task = tmp_path / ".RUD" / slug
    task.mkdir(parents=True)
    (task / "task.json").write_text(json.dumps({
        "slug": slug, "title": "t", "kind": "ar-paper",
        "tmux_interview_target": "loom-cursor-x:0.0",
    }))
    (task / "ar.json").write_text(json.dumps({
        "role": "paper", "stage": "loop", "loop_running": True, "round": 1,
        "rounds": [{"n": 1, "started_at": "x", "prompt_sent_at": "x"}],
    }))
    return task


def test_dead_pane_clears_the_stamps_so_the_round_restarts(tmp_path):
    slug = "paper-x"
    make_paper(tmp_path, slug)
    driver = _ARLoopDriver(FakeManager(alive=False), tmp_path, "pid1", slug)
    state = ar.read_ar_state(tmp_path, slug)

    driver._watch_author(state, 1)

    meta = json.loads((tmp_path / ".RUD" / slug / "task.json").read_text())
    assert meta["tmux_interview_target"] == ""
    saved = ar.read_ar_state(tmp_path, slug)
    assert saved["rounds"][0]["prompt_sent_at"] == ""


def test_idle_agent_gets_a_continue_nudge(tmp_path, monkeypatch):
    slug = "paper-x"
    make_paper(tmp_path, slug)
    sent: list[str] = []
    monkeypatch.setattr(web, "capture_pane", lambda t, n: (True, "→ Add a follow-up"))
    monkeypatch.setattr(
        web, "send_pane_text", lambda t, p, submit: (sent.append(p), (True, ""))[1]
    )
    driver = _ARLoopDriver(FakeManager(alive=True), tmp_path, "pid1", slug)
    driver._author_idle_polls = web._AR_STALL_IDLE_POLLS
    state = ar.read_ar_state(tmp_path, slug)

    driver._watch_author(state, 1)

    assert len(sent) == 1
    assert "write your summary" in sent[0].lower() or "author.md" in sent[0]
    saved = ar.read_ar_state(tmp_path, slug)
    assert saved["rounds"][0]["nudges"] == 1
    # The stamp survives: the agent keeps its context, no full re-prompt.
    assert saved["rounds"][0]["prompt_sent_at"] == "x"


def test_a_working_agent_is_left_alone(tmp_path, monkeypatch):
    slug = "paper-x"
    make_paper(tmp_path, slug)
    sent: list[str] = []
    monkeypatch.setattr(web, "capture_pane", lambda t, n: (True, "esc to interrupt"))
    monkeypatch.setattr(
        web, "send_pane_text", lambda t, p, submit: (sent.append(p), (True, ""))[1]
    )
    driver = _ARLoopDriver(FakeManager(alive=True), tmp_path, "pid1", slug)
    driver._author_idle_polls = web._AR_STALL_IDLE_POLLS
    driver._watch_author(ar.read_ar_state(tmp_path, slug), 1)
    assert not sent
    assert driver._author_idle_polls == 0


def test_nudges_stop_at_the_cap_and_tell_the_human(tmp_path, monkeypatch):
    slug = "paper-x"
    task = make_paper(tmp_path, slug)
    state = json.loads((task / "ar.json").read_text())
    state["rounds"][0]["nudges"] = web._AR_MAX_NUDGES
    (task / "ar.json").write_text(json.dumps(state))
    sent: list[str] = []
    monkeypatch.setattr(web, "capture_pane", lambda t, n: (True, "idle"))
    monkeypatch.setattr(
        web, "send_pane_text", lambda t, p, submit: (sent.append(p), (True, ""))[1]
    )
    manager = FakeManager(alive=True)
    driver = _ARLoopDriver(manager, tmp_path, "pid1", slug)
    driver._author_idle_polls = web._AR_STALL_IDLE_POLLS

    driver._watch_author(ar.read_ar_state(tmp_path, slug), 1)

    assert not sent
    assert manager.events == ["ar-author-stalled"]
    assert ar.read_ar_state(tmp_path, slug)["rounds"][0]["stall_reported"] is True


def test_work_does_not_reset_the_hard_nudge_cap(tmp_path, monkeypatch):
    """Visible work must not turn the per-round spending cap into infinity."""
    slug = "paper-x"
    task = make_paper(tmp_path, slug)
    state = json.loads((task / "ar.json").read_text())
    state["rounds"][0]["nudges"] = web._AR_MAX_NUDGES
    state["rounds"][0]["stall_reported"] = True
    (task / "ar.json").write_text(json.dumps(state))
    sent: list[str] = []
    monkeypatch.setattr(web, "capture_pane", lambda t, n: (True, "idle"))
    monkeypatch.setattr(
        web, "send_pane_text", lambda t, p, submit: (sent.append(p), (True, ""))[1]
    )
    driver = _ARLoopDriver(FakeManager(alive=True), tmp_path, "pid1", slug)
    driver._author_idle_polls = web._AR_STALL_IDLE_POLLS
    driver._author_worked = True  # the agent visibly worked since the last nudge

    driver._watch_author(ar.read_ar_state(tmp_path, slug), 1)

    assert not sent
    saved = ar.read_ar_state(tmp_path, slug)["rounds"][0]
    assert saved["nudges"] == web._AR_MAX_NUDGES
    assert saved["stall_reported"] is True


def test_budget_marker_blocks_same_session_nudge(tmp_path, monkeypatch):
    from loom.usage_budget import write_budget_marker

    slug = "paper-x"
    task = make_paper(tmp_path, slug)
    write_budget_marker(task, target="loom-cursor-x:0.0", reason="limit", usage={})
    sent: list[str] = []
    monkeypatch.setattr(web, "capture_pane", lambda t, n: (True, "idle"))
    monkeypatch.setattr(
        web, "send_pane_text", lambda t, p, submit: (sent.append(p), (True, ""))[1]
    )
    driver = _ARLoopDriver(FakeManager(alive=True), tmp_path, "pid1", slug)
    driver._author_idle_polls = web._AR_STALL_IDLE_POLLS

    driver._watch_author(ar.read_ar_state(tmp_path, slug), 1)

    assert not sent


def test_new_round_rotates_context_once_before_prompt(tmp_path, monkeypatch):
    slug = "paper-x"
    task = make_paper(tmp_path, slug)
    state = ar.read_ar_state(tmp_path, slug)
    state["rounds"][0]["prompt_sent_at"] = ""
    ar.write_ar_state(tmp_path, slug, state)
    sent: list[str] = []
    monkeypatch.setattr(
        web, "send_pane_text", lambda t, p, submit: (sent.append(p), (True, ""))[1]
    )
    manager = FakeManager(alive=True)
    driver = _ARLoopDriver(manager, tmp_path, "pid1", slug)

    driver._send_round_prompt(ar.read_ar_state(tmp_path, slug), 1)

    assert manager.rotations == 1
    assert len(sent) == 1
    saved = ar.read_ar_state(tmp_path, slug)["rounds"][0]
    assert saved["fresh_session_started_at"]
    assert saved["prompt_sent_at"]
