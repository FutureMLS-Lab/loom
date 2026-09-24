"""Existing-paper Author tasks built on Loom's ordinary task workflow.

Unlike an Auto Research paper, an Author task does not own a reviewer loop.
It gives one long-lived agent isolated worktrees for an existing manuscript
and its experiment code, plus the paper-writing skills needed to finish them.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from loom.paths import bundled_skills_path
from loom.rud_task import (
    TaskMeta,
    create_task,
    delete_task,
    git_toplevel,
    join_skills_paths,
    prepare_task_worktree_from,
    read_meta,
    split_skills_paths,
    task_root,
    update_meta,
)

KIND_AUTHOR = "author"
AUTHOR_STATE = "author.json"

_BASE_SKILLS = (
    "ar/AR-AUTHOR.md",
    "ar/paper-results-reporting/SKILL.md",
    "ar/paper-ai-tone/SKILL.md",
)
_VENUE_SKILLS = {
    "wacv": "ar/wacv-submission-readiness/SKILL.md",
    "wsdm": "ar/wsdm-submission-readiness/SKILL.md",
}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _repository_root(raw: str, label: str, *, required: bool) -> Path | None:
    value = str(raw or "").strip()
    if not value:
        if required:
            raise ValueError(f"{label} repository path is required")
        return None
    try:
        path = Path(value).expanduser().resolve()
    except OSError as exc:
        raise ValueError(f"invalid {label} repository path: {exc}") from exc
    if not path.is_dir():
        raise ValueError(f"{label} repository does not exist: {path}")
    root = git_toplevel(path)
    if root is None:
        raise ValueError(f"{label} repository is not a git repository: {path}")
    root = root.resolve()
    if root != path:
        raise ValueError(
            f"{label} path must be the git repository root; use {root}"
        )
    return root


def normalize_config(
    *,
    manuscript_repo: str,
    experiment_repo: str = "",
    venue: str,
    main_tex: str = "main.tex",
) -> dict[str, str]:
    """Validate an existing-paper request and return canonical host paths."""
    manuscript = _repository_root(manuscript_repo, "manuscript", required=True)
    assert manuscript is not None
    experiment = _repository_root(experiment_repo, "experiment", required=False)

    venue_id = str(venue or "").strip().lower()
    if not venue_id:
        raise ValueError("target venue is required")

    raw_tex = str(main_tex or "main.tex").strip() or "main.tex"
    tex_rel = Path(raw_tex)
    if tex_rel.is_absolute() or ".." in tex_rel.parts:
        raise ValueError("main TeX file must be relative to the manuscript repository")
    if tex_rel.suffix.lower() != ".tex":
        raise ValueError("main TeX file must end in .tex")
    tex_path = (manuscript / tex_rel).resolve()
    try:
        tex_path.relative_to(manuscript)
    except ValueError as exc:
        raise ValueError("main TeX file escapes the manuscript repository") from exc
    if not tex_path.is_file():
        raise ValueError(f"main TeX file does not exist: {tex_path}")

    if (
        experiment is not None
        and experiment != manuscript
        and experiment.name == manuscript.name
    ):
        raise ValueError(
            "manuscript and experiment repositories must have different directory names"
        )

    return {
        "venue": venue_id,
        "main_tex": tex_rel.as_posix(),
        "source_manuscript_repo": str(manuscript),
        "source_experiment_repo": str(experiment) if experiment else "",
    }


def author_skill_paths(venue: str, selected: str = "") -> list[Path]:
    """Built-in author skills followed by any extra user-selected skills."""
    skills_root = bundled_skills_path().parent
    relative = [*_BASE_SKILLS]
    venue_skill = _VENUE_SKILLS.get(str(venue or "").strip().lower())
    if venue_skill:
        relative.append(venue_skill)

    paths: list[Path] = []
    seen: set[Path] = set()
    for candidate in [
        *(skills_root / rel for rel in relative),
        *split_skills_paths(selected),
    ]:
        try:
            path = candidate.expanduser().resolve()
        except OSError:
            continue
        if path.is_file() and path not in seen:
            seen.add(path)
            paths.append(path)
    return paths


def author_goal(user_goal: str, config: dict[str, str]) -> str:
    manuscript_name = Path(config["source_manuscript_repo"]).name
    experiment_value = config.get("source_experiment_repo", "")
    experiment_name = Path(experiment_value).name if experiment_value else ""
    workspace = [
        f"- Manuscript: work/{manuscript_name}/{config['main_tex']}",
        (
            f"- Experiments: work/{experiment_name}/"
            if experiment_name and experiment_name != manuscript_name
            else f"- Experiments: use work/{manuscript_name}/ (same repository)"
            if experiment_name
            else "- Experiments: no separate repository supplied"
        ),
    ]
    return "\n".join(
        [
            (
                f"Finish an existing {config['venue'].upper()} paper using its "
                "real experiment code and evidence."
            ),
            "",
            "Author workspace:",
            *workspace,
            "",
            "User objective:",
            str(user_goal or "").strip(),
            "",
            (
                "Preserve existing work, never invent experimental values, and keep "
                "manuscript claims synchronized with reproducible results."
            ),
        ]
    ).strip()


def read_state(project_root: Path, slug: str) -> dict[str, Any]:
    path = task_root(project_root, slug) / AUTHOR_STATE
    if not path.is_file():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _write_state(project_root: Path, slug: str, state: dict[str, Any]) -> None:
    path = task_root(project_root, slug) / AUTHOR_STATE
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
    tmp.replace(path)


def create_existing_paper_task(
    project_root: Path,
    *,
    title: str,
    user_goal: str,
    config: dict[str, str],
    selected_skills: str,
    interview_model: str,
    agent: str,
) -> tuple[TaskMeta | None, str]:
    """Create an Author task and both worktrees, rolling back on failure."""
    skills = author_skill_paths(config["venue"], selected_skills)
    if not skills:
        return None, "bundled Author skills are missing"
    meta: TaskMeta | None = None
    try:
        meta = create_task(
            project_root,
            title,
            author_goal(user_goal, config),
            skills_path=join_skills_paths(skills),
            interview_model=interview_model,
            agent=agent,
            kind=KIND_AUTHOR,
            auto_worktree=False,
        )
        roles: list[tuple[str, Path]] = [
            ("manuscript", Path(config["source_manuscript_repo"])),
        ]
        experiment_value = config.get("source_experiment_repo", "")
        if experiment_value:
            experiment = Path(experiment_value)
            if experiment.resolve() != roles[0][1].resolve():
                roles.append(("experiment", experiment))

        worktrees: dict[str, str] = {}
        worktree_order: list[str] = []
        branch_order: list[str] = []
        for role, source in roles:
            worktree, branch, message = prepare_task_worktree_from(
                project_root, meta.slug, source
            )
            if worktree is None:
                raise RuntimeError(f"could not create {role} worktree: {message}")
            worktrees[role] = str(worktree)
            worktree_order.append(str(worktree))
            branch_order.append(branch)
        if experiment_value and "experiment" not in worktrees:
            worktrees["experiment"] = worktrees["manuscript"]

        updated = update_meta(
            project_root,
            meta.slug,
            worktrees=worktree_order,
            branches=branch_order,
        )
        if updated is None:
            raise RuntimeError("could not persist Author worktrees")
        meta = updated

        state: dict[str, Any] = {
            **config,
            "manuscript_worktree": worktrees["manuscript"],
            "experiment_worktree": worktrees.get("experiment", ""),
            "created_at": _now_iso(),
        }
        _write_state(project_root, meta.slug, state)
        return read_meta(project_root, meta.slug) or meta, ""
    except (OSError, RuntimeError, ValueError) as exc:
        if meta is not None:
            delete_task(project_root, meta.slug)
        return None, str(exc)
