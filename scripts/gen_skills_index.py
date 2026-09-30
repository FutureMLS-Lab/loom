#!/usr/bin/env python3
"""Regenerate the skills list inside loom/skills/DEFAULT_PROMPT.md.

The default prompt is the one text every agent always receives, so the
map of available skills lives right there - between the SKILLS markers,
rendered from the same catalogs the runtime uses (the AR pipeline's
skill_catalog and the picker's option scan), never hand-kept. A test
compares the section against a fresh render; when a skill is added or
its description changes, run:

    python3 scripts/gen_skills_index.py
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from loom import ar_task as ar  # noqa: E402
from loom import development_task as development  # noqa: E402
from loom.paths import default_prompt_path  # noqa: E402
from loom.rud_task import bundled_skills_path  # noqa: E402
from loom.web import _available_skill_options, _skill_summary  # noqa: E402

BEGIN = "<!-- SKILLS:BEGIN generated - edit skills, then run scripts/gen_skills_index.py -->"
END = "<!-- SKILLS:END -->"


def _rel(path: str | Path) -> str:
    p = Path(path)
    try:
        return str(p.relative_to(REPO))
    except ValueError:
        return str(p)


def _cell(text: str, limit: int = 120) -> str:
    """One table cell: pipes escaped, length capped - the full text lives in
    the skill file itself; this row is only the trigger."""
    flat = " ".join(str(text or "").split()).replace("|", "\\|")
    return flat[: limit - 1] + "…" if len(flat) > limit else flat


def render_section() -> str:
    picker = _available_skill_options(bundled_skills_path())
    dev = development.skill_catalog()
    pipeline = ar.skill_catalog()
    lines: list[str] = [
        "",
        f"Loom ships exactly {len(picker) + len(dev) + len(pipeline)} skills - "
        f"{len(picker)} pick-and-read, {len(dev)} Development Task, "
        f"{len(pipeline)} Paper-Factory. This "
        "generated table is the complete, authoritative set: a skill not "
        "listed here does not exist. Each description says when the skill "
        "applies; when your work matches one, READ its file at the path. To "
        "add a skill, put a markdown file under loom/skills/ (its "
        "frontmatter `description:` becomes its row here) and run "
        "scripts/gen_skills_index.py.",
        "",
        f"### Pick-and-read ({len(picker)}) - selectable at task creation, "
        "readable by anyone",
        "",
        "| Skill | What it does / when to use it | Path |",
        "|---|---|---|",
    ]
    for option in picker:
        summary = _cell(_skill_summary(Path(option["path"])))
        lines.append(f"| {option['label']} | {summary} | `{_rel(option['path'])}` |")
    lines += [
        "",
        f"### Development Task skills ({len(dev)}) - the workflow injects each role itself",
        "",
        "| Skill | Role | What it does / when to use it | How it reaches the agent | Path |",
        "|---|---|---|---|---|",
    ]
    for entry in dev:
        lines.append(
            f"| {entry['name']} | {entry['role']} | {_cell(entry['description'])} "
            f"| {_cell(entry['injection'], 90)} | `{_rel(entry['path'])}` |"
        )
    lines += [
        "",
        f"### Paper Factory (AR) skills ({len(pipeline)}) - the pipeline "
        "injects these itself",
        "",
        "| Skill | Role | What it does / when to use it | How it reaches the agent | Path |",
        "|---|---|---|---|---|",
    ]
    for entry in pipeline:
        lines.append(
            f"| {entry['name']} | {entry['role']} | {_cell(entry['description'])} "
            f"| {_cell(entry['injection'], 90)} | `{_rel(entry['path'])}` |"
        )
    lines.append("")
    return "\n".join(lines)


def render_document() -> str:
    doc = default_prompt_path().read_text(encoding="utf-8")
    pattern = re.compile(
        re.escape(BEGIN) + r".*?" + re.escape(END), flags=re.DOTALL
    )
    if not pattern.search(doc):
        raise SystemExit(
            f"markers not found in {default_prompt_path()} - refusing to guess"
        )
    return pattern.sub(BEGIN + render_section() + END, doc)


if __name__ == "__main__":
    default_prompt_path().write_text(render_document(), encoding="utf-8")
    print(f"updated {default_prompt_path()}")
