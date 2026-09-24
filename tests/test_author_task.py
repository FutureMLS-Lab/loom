from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from loom import author_task as author
from loom.rud_task import split_skills_paths


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-c", "user.name=Loom Test", "-c", "user.email=loom@example.test", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )


def _repo(path: Path, files: dict[str, str]) -> Path:
    path.mkdir(parents=True)
    _git(path, "init", "-q")
    for relative, content in files.items():
        target = path / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    _git(path, "add", ".")
    _git(path, "commit", "-qm", "seed")
    return path


def test_normalize_config_requires_safe_existing_main_tex(tmp_path: Path) -> None:
    manuscript = _repo(tmp_path / "paper", {"tex/main.tex": "paper"})

    config = author.normalize_config(
        manuscript_repo=str(manuscript),
        venue=" WACV ",
        main_tex="tex/main.tex",
    )

    assert config["venue"] == "wacv"
    assert config["main_tex"] == "tex/main.tex"
    assert config["source_manuscript_repo"] == str(manuscript.resolve())
    with pytest.raises(ValueError, match="relative"):
        author.normalize_config(
            manuscript_repo=str(manuscript), venue="wacv", main_tex="../main.tex"
        )
    with pytest.raises(ValueError, match="does not exist"):
        author.normalize_config(
            manuscript_repo=str(manuscript), venue="wacv", main_tex="missing.tex"
        )


def test_create_existing_paper_task_builds_two_worktrees_and_skills(tmp_path: Path) -> None:
    project = tmp_path / "workspace"
    project.mkdir()
    manuscript = _repo(project / "manuscript", {"main.tex": "paper"})
    experiments = _repo(project / "experiments", {"run.py": "print('ok')"})
    config = author.normalize_config(
        manuscript_repo=str(manuscript),
        experiment_repo=str(experiments),
        venue="wacv",
        main_tex="main.tex",
    )

    meta, error = author.create_existing_paper_task(
        project,
        title="Finish Paper",
        user_goal="Complete the missing ablations and tighten the claims.",
        config=config,
        selected_skills="",
        interview_model="gpt-test",
        agent="cursor",
    )

    assert error == ""
    assert meta is not None
    assert meta.kind == author.KIND_AUTHOR
    assert len(meta.worktrees) == 2
    assert Path(meta.worktrees[0], "main.tex").is_file()
    assert Path(meta.worktrees[1], "run.py").is_file()
    assert "work/manuscript/main.tex" in meta.general_goal
    assert "work/experiments/" in meta.general_goal
    selected = {path.as_posix() for path in split_skills_paths(meta.skills_path)}
    assert any(path.endswith("/ar/AR-AUTHOR.md") for path in selected)
    assert any(path.endswith("/paper-results-reporting/SKILL.md") for path in selected)
    assert any(path.endswith("/paper-ai-tone/SKILL.md") for path in selected)
    assert any(path.endswith("/wacv-submission-readiness/SKILL.md") for path in selected)
    state = author.read_state(project, meta.slug)
    assert state["venue"] == "wacv"
    assert state["manuscript_worktree"] == meta.worktrees[0]
    assert state["experiment_worktree"] == meta.worktrees[1]


def test_same_repository_is_reused_for_manuscript_and_experiments(tmp_path: Path) -> None:
    project = tmp_path / "workspace"
    project.mkdir()
    monorepo = _repo(project / "paper", {"main.tex": "paper", "run.py": "pass"})
    config = author.normalize_config(
        manuscript_repo=str(monorepo),
        experiment_repo=str(monorepo),
        venue="iclr",
        main_tex="main.tex",
    )

    meta, error = author.create_existing_paper_task(
        project,
        title="One Repo Paper",
        user_goal="Finish it.",
        config=config,
        selected_skills="",
        interview_model="gpt-test",
        agent="cursor",
    )

    assert error == ""
    assert meta is not None and len(meta.worktrees) == 1
    state = author.read_state(project, meta.slug)
    assert state["experiment_worktree"] == state["manuscript_worktree"]


def test_author_create_controls_are_present() -> None:
    static = Path(__file__).resolve().parents[1] / "loom" / "web_static"
    html = (static / "index.html").read_text(encoding="utf-8")
    js = (static / "app.js").read_text(encoding="utf-8")
    assert '<option value="author">' in html
    assert 'id="author-manuscript-repo"' in html
    assert 'id="author-experiment-repo"' in html
    assert "body.kind = 'author'" in js
