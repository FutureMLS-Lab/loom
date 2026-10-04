"""Every task in its own worktree: git init for new projects, the repo picker
on create, the create preview and the sidebar change counts - over real HTTP,
against real git."""

from __future__ import annotations

import json
import os
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

from loom import rud_task
from loom.openclaw import OpenClawClient
from loom.rud_task import (
    TaskChangeCounter,
    create_task,
    describe_source_repo,
    detect_and_persist_worktree,
    prepare_task_worktree_from,
    task_root,
)
from loom.web import AgentActivityWatcher, ClaudeRegistry, make_handler
from loom.web_projects import WebProjectRegistry

WARNING_TAIL = "so this task has no isolated copy; its agent would edit the folder itself."


@pytest.fixture(autouse=True)
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A git with no user or system config and no identity in the environment,
    so what Loom's own commits end up signed as is decided by the test.

    The working directory moves into the test's tmp dir too: these tests run
    git commands that write, and a path that comes back empty must land them
    there, never in whatever checkout pytest was started from.
    """
    monkeypatch.chdir(tmp_path)
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    for var in (
        "GIT_AUTHOR_NAME",
        "GIT_AUTHOR_EMAIL",
        "GIT_COMMITTER_NAME",
        "GIT_COMMITTER_EMAIL",
        "EMAIL",
        "GIT_CONFIG_GLOBAL",
        "GIT_DIR",
        "GIT_WORK_TREE",
        "GIT_INDEX_FILE",
        "LOOM_BRANCH_PREFIX",
    ):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(rud_task.getpass, "getuser", lambda: "tester")
    monkeypatch.setattr(rud_task.socket, "gethostname", lambda: "devbox")
    return home


def _git(cwd: Path, *args: str) -> str:
    """The test's own git, with an identity of its own on every call."""
    assert cwd.is_absolute(), f"refusing to run git in relative path {cwd!r}"
    return subprocess.run(
        [
            "git", "-c", "user.name=Test", "-c", "user.email=test@example.com",
            "-c", "commit.gpgsign=false", *args,
        ],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


def _repo(path: Path, files: dict[str, str] | None = None) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init", "-q")
    for name, text in (files or {"README.md": "hello\n"}).items():
        (path / name).write_text(text, encoding="utf-8")
    _git(path, "add", "-A")
    _git(path, "commit", "-qm", "init")
    return path


class Loom:
    def __init__(self, base: str, registry: WebProjectRegistry, launch: Path) -> None:
        self.base = base
        self.registry = registry
        self.launch = launch

    def call(
        self, method: str, path: str, body: dict[str, Any] | None = None, **params: str
    ) -> tuple[int, dict[str, Any]]:
        url = self.base + path + ("?" + urllib.parse.urlencode(params) if params else "")
        data = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(
            url, data=data, method=method, headers={"Content-Type": "application/json"}
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read())

    def project(self, path: Path) -> str:
        pid, err = self.registry.add_by_path(str(path))
        assert pid, err
        return pid


@pytest.fixture()
def loom(tmp_path: Path):
    launch = tmp_path / "launch"
    launch.mkdir()
    registry = WebProjectRegistry(tmp_path / "web-projects.json")
    handler = make_handler(
        registry,
        launch,
        tmp_path / "no-skills.md",
        ClaudeRegistry(),
        OpenClawClient(),
        # Passed in so it is never started: no pane polling in tests.
        activity_watcher=AgentActivityWatcher(registry),
    )
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(
        target=httpd.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True
    ).start()
    yield Loom(f"http://127.0.0.1:{httpd.server_address[1]}", registry, launch)
    httpd.shutdown()
    httpd.server_close()


# --- new empty projects become git repos ----------------------------------------


def test_new_empty_project_is_a_repo_its_tasks_branch_from(loom: Loom) -> None:
    repo = loom.launch / "fresh"
    status, data = loom.call("POST", "/api/projects", {"path": str(repo), "mode": "empty"})
    assert status == 201, data
    assert data["git_initialized"] is True
    assert "git_error" not in data
    assert _git(repo, "log", "--format=%s") == "Initial commit"

    status, created = loom.call(
        "POST", "/api/tasks", {"title": "First task", "general_goal": "g"}, project=data["id"]
    )
    assert status == 201, created
    assert created["worktree_created"] is True
    assert "worktree_warning" not in created
    wt = task_root(repo, "first-task") / "work" / "fresh"
    meta = created["meta"]
    # The response already names the worktree, not just the next GET.
    assert meta["worktree_path"] == str(wt)
    assert meta["worktrees"] == [str(wt)]
    assert meta["branch"] == "loom/first-task"
    assert meta["worktree_bases"][str(wt)]["commit"] == _git(repo, "rev-parse", "HEAD")
    assert _git(wt, "branch", "--show-current") == "loom/first-task"
    # .RUD/ - worktree and all - stays out of the new repo's status.
    assert _git(repo, "status", "--porcelain") == ""


def test_git_init_signs_as_the_os_user_without_writing_config(loom: Loom, home: Path) -> None:
    repo = loom.launch / "anon"
    status, data = loom.call("POST", "/api/projects", {"path": str(repo), "mode": "empty"})
    assert status == 201 and data["git_initialized"] is True, data
    assert _git(repo, "log", "--format=%an <%ae>|%cn <%ce>") == (
        "tester <tester@devbox>|tester <tester@devbox>"
    )
    # The fallback rode on that one command: no config gained an identity.
    assert not (home / ".gitconfig").exists()
    assert not (home / ".config" / "git").exists()
    assert "[user]" not in (repo / ".git" / "config").read_text()


def test_git_init_keeps_the_configured_half_of_an_identity(loom: Loom, home: Path) -> None:
    config = "[user]\n\tname = Real Person\n"
    (home / ".gitconfig").write_text(config)
    repo = loom.launch / "named"
    status, data = loom.call("POST", "/api/projects", {"path": str(repo), "mode": "empty"})
    assert status == 201 and data["git_initialized"] is True, data
    assert _git(repo, "log", "--format=%an <%ae>") == "Real Person <tester@devbox>"
    assert (home / ".gitconfig").read_text() == config


def test_git_init_leaves_opt_outs_and_existing_repos_alone(loom: Loom) -> None:
    plain = loom.launch / "plain"
    status, data = loom.call(
        "POST", "/api/projects", {"path": str(plain), "mode": "empty", "git_init": False}
    )
    assert status == 201, data
    assert data["git_initialized"] is False and "git_error" not in data
    assert not (plain / ".git").exists()

    outer = _repo(loom.launch / "outer")
    status, data = loom.call(
        "POST", "/api/projects", {"path": str(outer / "inner"), "mode": "empty"}
    )
    assert status == 201, data
    assert data["git_initialized"] is False and "git_error" not in data
    assert not (outer / "inner" / ".git").exists()

    # Registering an existing folder is unchanged: no git keys at all.
    existing = loom.launch / "existing"
    existing.mkdir()
    status, data = loom.call("POST", "/api/projects", {"path": str(existing)})
    assert status == 201, data
    assert "git_initialized" not in data
    assert not (existing / ".git").exists()


def test_git_failure_still_registers_the_project(loom: Loom, home: Path) -> None:
    hooks = home / "hooks"
    hooks.mkdir()
    hook = hooks / "pre-commit"
    hook.write_text("#!/bin/sh\necho 'commits are blocked here' >&2\nexit 1\n")
    hook.chmod(0o755)
    (home / ".gitconfig").write_text(f"[core]\n\thooksPath = {hooks}\n")
    repo = loom.launch / "hooked"
    status, data = loom.call("POST", "/api/projects", {"path": str(repo), "mode": "empty"})
    assert status == 201, data
    assert data["git_initialized"] is False
    assert "commits are blocked here" in data["git_error"]
    assert str(repo.resolve()) in [p["path"] for p in data["projects"]]


# --- choosing the repo a task branches from -----------------------------------


def test_create_task_from_a_chosen_candidate_repo(loom: Loom) -> None:
    container = loom.launch / "multi"
    alpha = _repo(container / "alpha")
    beta = _repo(container / "beta", {"b.txt": "b\n"})
    pid = loom.project(container)

    status, err = loom.call(
        "POST",
        "/api/tasks",
        {"title": "Bad pick", "general_goal": "g", "source_repo": str(loom.launch)},
        project=pid,
    )
    assert status == 400
    assert err["allowed"] == sorted([str(alpha.resolve()), str(beta.resolve())])
    assert not (container / ".RUD").exists()  # refused before anything was made

    status, created = loom.call(
        "POST",
        "/api/tasks",
        {"title": "Use beta", "general_goal": "g", "source_repo": str(beta)},
        project=pid,
    )
    assert status == 201, created
    assert created["worktree_created"] is True
    wt = task_root(container, "use-beta") / "work" / "beta"
    assert created["meta"]["worktrees"] == [str(wt)]
    assert (wt / "b.txt").is_file()
    assert created["meta"]["worktree_bases"][str(wt)]["commit"] == _git(beta, "rev-parse", "HEAD")

    # Adding a worktree later goes through the same whitelist.
    status, err = loom.call(
        "POST", "/api/tasks/use-beta/worktree", {"source_repo": "/etc"}, project=pid
    )
    assert status == 400 and "allowed" in err


def test_task_worktrees_leave_the_users_checkout_clean(loom: Loom) -> None:
    repo = _repo(loom.launch / "own")
    pid = loom.project(repo)
    for title in ("One", "Two"):
        status, created = loom.call(
            "POST", "/api/tasks", {"title": title, "general_goal": "g"}, project=pid
        )
        assert status == 201 and created["worktree_created"] is True, created
    # .RUD/ - both worktrees included - no longer shows up as `?? .RUD/`.
    assert _git(repo, "status", "--porcelain") == ""
    exclude = (repo / ".git" / "info" / "exclude").read_text().splitlines()
    assert exclude.count("/.RUD/") == 1  # anchored, and added once
    assert not (repo / ".gitignore").exists()

    # A project nested inside a repo anchors its own .RUD/, not the top's.
    outer = _repo(loom.launch / "outer")
    (outer / "sub").mkdir()
    status, created = loom.call(
        "POST", "/api/tasks", {"title": "Nested", "general_goal": "g"},
        project=loom.project(outer / "sub"),
    )
    assert status == 201 and created["worktree_created"] is True, created
    assert "/sub/.RUD/" in (outer / ".git" / "info" / "exclude").read_text().splitlines()
    assert _git(outer, "status", "--porcelain") == ""

    # A child repo of a container never holds the container's .RUD/, so it
    # is left alone.
    container = loom.launch / "multi"
    child = _repo(container / "child")
    status, created = loom.call(
        "POST", "/api/tasks",
        {"title": "Kid", "general_goal": "g", "source_repo": str(child)},
        project=loom.project(container),
    )
    assert status == 201 and created["worktree_created"] is True, created
    assert ".RUD" not in (child / ".git" / "info" / "exclude").read_text()


def test_create_task_says_when_no_worktree_could_be_made(loom: Loom) -> None:
    plain = loom.launch / "notes"
    plain.mkdir()
    pid = loom.project(plain)
    status, created = loom.call(
        "POST", "/api/tasks", {"title": "Loose", "general_goal": "g"}, project=pid
    )
    assert status == 201, created
    assert created["worktree_created"] is False
    assert created["worktree_warning"] == f"{plain.resolve()} is not a git repository, {WARNING_TAIL}"
    assert created["meta"]["worktrees"] == []

    unborn = loom.launch / "unborn"
    unborn.mkdir()
    _git(unborn, "init", "-q")
    status, created = loom.call(
        "POST", "/api/tasks", {"title": "Early", "general_goal": "g"}, project=loom.project(unborn)
    )
    assert status == 201, created
    assert created["worktree_created"] is False
    assert created["worktree_warning"] == f"{unborn.resolve()} has no commits yet, {WARNING_TAIL}"

    # A studio never gets a worktree: the choice is ignored and there is
    # nothing to warn about.
    status, created = loom.call(
        "POST", "/api/tasks", {"title": "Studio", "kind": "ar", "source_repo": "/nowhere"}, project=pid
    )
    assert status == 201, created
    assert created["worktree_created"] is False
    assert "worktree_warning" not in created


# --- the create preview -----------------------------------------------------------


def test_task_preview_shows_what_create_would_do_and_does_none_of_it(loom: Loom) -> None:
    repo = _repo(loom.launch / "proj", {"a.py": "x = 1\n"})
    (repo / "a.py").write_text("x = 2\n")
    (repo / "scratch.txt").write_text("notes\n")
    pid = loom.project(repo)

    status, p = loom.call("GET", "/api/task-preview", project=pid, title="Fix the Bug!")
    assert status == 200, p
    td = task_root(repo, "fix-the-bug")
    assert p["ok"] is True
    assert p["slug"] == "fix-the-bug"
    assert p["branch"] == "loom/fix-the-bug"
    assert p["task_dir"] == str(td)
    assert p["worktree_dest"] == str(td / "work" / "proj")
    assert p["code_root"] == str(repo.resolve())
    assert p["source"] == {
        "path": str(repo.resolve()),
        "name": "proj",
        "is_git": True,
        "branch": _git(repo, "branch", "--show-current"),
        "head": _git(repo, "rev-parse", "HEAD"),
        "head_short": _git(repo, "rev-parse", "--short", "HEAD"),
        "dirty": 1,
        "untracked": 1,
    }
    [cand] = p["candidates"]
    assert cand["path"] == str(repo.resolve()) and cand["name"] == "proj"
    assert cand["branch"] == p["source"]["branch"]
    assert cand["head_short"] == p["source"]["head_short"]
    assert "worktree_warning" not in p
    # Nothing was created: no task dir, no branch.
    assert not (repo / ".RUD").exists()
    assert _git(repo, "branch", "--list", "loom/*") == ""

    # Once creation takes the slug, the preview moves on, as creation would.
    status, _ = loom.call(
        "POST", "/api/tasks", {"title": "Fix the Bug!", "general_goal": "g"}, project=pid
    )
    assert status == 201
    _, again = loom.call("GET", "/api/task-preview", project=pid, title="Fix the Bug!")
    assert again["slug"] == "fix-the-bug-2"
    # The task's own .RUD/ folder is now untracked in the repo, but it is
    # Loom's, not the user's uncommitted work.
    assert again["source"]["untracked"] == 1
    # A blank title has no slug yet, but the repo side is still described.
    _, blank = loom.call("GET", "/api/task-preview", project=pid)
    assert blank["slug"] == blank["task_dir"] == blank["worktree_dest"] == ""
    assert blank["source"]["is_git"] is True


def test_task_preview_source_repo_and_non_git_roots(loom: Loom) -> None:
    container = loom.launch / "multi"
    beta = _repo(container / "beta")
    _repo(container / "gamma")
    pid = loom.project(container)

    _, p = loom.call("GET", "/api/task-preview", project=pid, title="T")
    # The code root is a plain container: no worktree unless a repo is picked.
    assert p["source"]["is_git"] is False
    assert p["source"]["path"] == str(container.resolve())
    assert p["source"]["dirty"] is None and p["source"]["untracked"] is None
    assert p["worktree_dest"] == ""
    assert p["worktree_warning"] == f"{container.resolve()} is not a git repository, {WARNING_TAIL}"
    assert [c["name"] for c in p["candidates"]] == ["beta", "gamma"]

    _, p = loom.call("GET", "/api/task-preview", project=pid, title="T", source_repo=str(beta))
    assert p["source"]["path"] == str(beta.resolve()) and p["source"]["is_git"] is True
    assert p["worktree_dest"] == str(task_root(container, "t") / "work" / "beta")
    assert "worktree_warning" not in p

    status, err = loom.call("GET", "/api/task-preview", project=pid, title="T", source_repo="/etc")
    assert status == 400
    assert err["allowed"] == sorted(c["path"] for c in p["candidates"])


def test_source_description_survives_a_git_status_timeout(tmp_path: Path) -> None:
    repo = _repo(tmp_path / "huge")
    info = describe_source_repo(repo, status_timeout=1e-9)
    assert info["is_git"] is True
    assert info["head"] == _git(repo, "rev-parse", "HEAD")
    assert info["dirty"] is None and info["untracked"] is None


# --- sidebar change counts ----------------------------------------------------


def test_task_changes_counts_committed_uncommitted_and_untracked_work(loom: Loom) -> None:
    repo = _repo(loom.launch / "app", {"a.txt": "1\n2\n3\n", "b.txt": "b\n"})
    pid = loom.project(repo)
    _, created = loom.call(
        "POST", "/api/tasks", {"title": "Feature", "general_goal": "g"}, project=pid
    )
    wt = task_root(repo, "feature") / "work" / "app"
    assert created["meta"]["worktree_path"] == str(wt) and wt.is_dir()
    (wt / "a.txt").write_text("1\nTWO\n3\n4\n")  # committed: +2 -1
    _git(wt, "commit", "-qam", "edit a")
    (wt / "b.txt").write_text("b\nmore\n")  # uncommitted: +1
    (wt / "new.py").write_text("x\ny\nz\n")  # untracked: +3
    create_task(repo, "Bare", "g", auto_worktree=False)

    status, data = loom.call("GET", "/api/task-changes", project=pid)
    assert status == 200 and data["ok"] is True
    assert data["tasks"]["feature"] == {
        "branch": "loom/feature",
        "worktrees": 1,
        "files": 3,
        "insertions": 6,
        "deletions": 1,
        "uncommitted": 2,
        "unknown_base": 0,
        "pending": False,
    }
    assert data["tasks"]["bare"] == {"worktrees": 0}

    # Without a recorded fork point there is nothing honest to diff against;
    # uncommitted work is still counted.
    meta_path = task_root(repo, "feature") / "task.json"
    raw = json.loads(meta_path.read_text())
    raw["worktree_bases"] = {}
    meta_path.write_text(json.dumps(raw))
    row = loom.call("GET", "/api/task-changes", project=pid)[1]["tasks"]["feature"]
    assert row["files"] is None and row["insertions"] is None and row["deletions"] is None
    assert row["unknown_base"] == 1
    assert row["uncommitted"] == 2

    assert loom.call("GET", "/api/task-changes", project="nope")[0] == 400


def _task_with_worktree(tmp_path: Path) -> tuple[Path, str, Path]:
    repo = _repo(tmp_path / "r", {"a.txt": "a\n"})
    meta = create_task(repo, "T", "g", auto_worktree=False)
    wt, _branch, msg = prepare_task_worktree_from(repo, meta.slug, repo)
    assert wt is not None, msg
    detect_and_persist_worktree(repo, meta.slug)
    return repo, meta.slug, wt


def test_change_counts_are_cached_until_head_or_index_moves(tmp_path: Path) -> None:
    repo, slug, wt = _task_with_worktree(tmp_path)
    counter = TaskChangeCounter(ttl=3600)
    first = counter.project_changes(repo)[slug]
    assert first["files"] == 0 and first["uncommitted"] == 0

    # An unstaged edit moves neither HEAD nor the index, so the cached
    # numbers stand until the TTL runs out...
    (wt / "a.txt").write_text("a\nb\n")
    assert counter.project_changes(repo)[slug] == first
    assert TaskChangeCounter(ttl=0).project_changes(repo)[slug]["insertions"] == 1

    # ...while staging rewrites the index and committing moves HEAD, and
    # either shows on the very next poll.
    _git(wt, "add", "a.txt")
    staged = counter.project_changes(repo)[slug]
    assert staged["insertions"] == 1 and staged["uncommitted"] == 1
    _git(wt, "commit", "-qm", "b")
    committed = counter.project_changes(repo)[slug]
    assert committed["files"] == 1 and committed["uncommitted"] == 0


def test_a_worktree_that_lost_its_git_is_not_counted_as_the_project_repo(
    tmp_path: Path,
) -> None:
    repo, slug, wt = _task_with_worktree(tmp_path)
    (repo / "a.txt").write_text("the user's own edit\n")
    (wt / ".git").unlink()  # git now walks up to the project's repo
    row = TaskChangeCounter().project_changes(repo)[slug]
    assert row["worktrees"] == 1
    assert row["uncommitted"] is None and row["files"] is None


def test_slow_worktrees_report_pending_and_finish_in_the_background(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, slug, _wt = _task_with_worktree(tmp_path)
    gate = threading.Event()
    calls: list[int] = []
    real = rud_task.worktree_change_counts

    def slow(*args: Any, **kwargs: Any) -> dict[str, Any]:
        calls.append(1)
        gate.wait(10)
        return real(*args, **kwargs)

    monkeypatch.setattr(rud_task, "worktree_change_counts", slow)
    counter = TaskChangeCounter(wait_seconds=0.05)
    first = counter.project_changes(repo)[slug]
    assert first["pending"] is True and first["files"] is None
    # A second poll while the first count is running joins it.
    assert counter.project_changes(repo)[slug]["pending"] is True
    assert len(calls) == 1

    gate.set()
    deadline = time.monotonic() + 10
    row = first
    while row["pending"] and time.monotonic() < deadline:
        time.sleep(0.02)
        row = counter.project_changes(repo)[slug]
    assert row["pending"] is False
    assert row["files"] == 0 and row["uncommitted"] == 0
    assert len(calls) == 1


def test_untracked_line_counts_are_bounded_and_never_block(tmp_path: Path) -> None:
    (tmp_path / "text.py").write_text("a\nb\nc")  # no trailing newline: still 3
    (tmp_path / "blob.bin").write_bytes(b"\x00\x01binary\n")
    (tmp_path / "big.txt").write_bytes(b"x\n" * (rud_task._UNTRACKED_LINES_MAX_FILE // 2 + 1))
    (tmp_path / "link").symlink_to(tmp_path / "text.py")
    os.mkfifo(tmp_path / "pipe")  # opening this for a blocking read would hang
    names = [b"text.py", b"blob.bin", b"big.txt", b"link", b"pipe", b"gone.txt"]
    assert rud_task._untracked_insertions(tmp_path, names) == 3 + 1


def test_status_parsing_skips_rename_sources() -> None:
    out = b"R  new.py\0old.py\0 M edited.py\0?? notes/a.txt\0?? b.txt\0"
    assert rud_task._parse_status_z(out) == (2, [b"notes/a.txt", b"b.txt"])
