"""Paths to bundled resources (skills, web static, paper templates)."""

from __future__ import annotations

import os
from pathlib import Path

_PKG = Path(__file__).resolve().parent



def bundled_skills_path() -> Path:
    return _PKG / "skills" / "charlie_skills.md"


def default_prompt_path() -> Path:
    """The always-injected floor under every task prompt.

    Distinct from skills: skills are chosen per task, this is not a choice.
    """
    return _PKG / "skills" / "DEFAULT_PROMPT.md"


def web_static_dir() -> Path:
    return _PKG / "web_static"


AR_ROOT_ENV = "LOOM_AR_ROOT"


def ar_root() -> Path:
    """Loom's own folder - home for AR research tasks, created on startup.

    AR tasks are not tied to any code project - a paper carries its own code
    and manuscript repositories - so they get a root of their own instead of
    burying themselves in the ``.RUD`` of whatever repo happened to spawn them.

    ``~/loom`` by default. ``~/ar``, the original name, stays in use on
    installs that already have one (their papers live there) and on machines
    whose ``~/loom`` is a clone of Loom's own source - the conventional
    checkout location, which must never become a task root. ``LOOM_AR_ROOT``
    overrides all of this.
    """
    override = os.environ.get(AR_ROOT_ENV, "").strip()
    if override:
        return Path(override).expanduser()
    home = Path.home()
    preferred, legacy = home / "loom", home / "ar"
    if _is_loom_checkout(preferred):
        return legacy
    # Any project with tasks gets a .RUD, so an unrelated ~/ar project must not
    # pull the root away from a ~/loom that is already in use.
    if (legacy / ".RUD").is_dir() and not (preferred / ".RUD").is_dir():
        return legacy
    return preferred


def _is_loom_checkout(path: Path) -> bool:
    """Whether *path* is a clone of Loom's own source tree."""
    return (path / "pyproject.toml").is_file() and (path / "loom" / "__init__.py").is_file()


def paper_templates_dir() -> Path:
    """Venue LaTeX skeletons used by AR paper tasks.

    These live inside the package (unlike the repo-root ``templates/``) because
    an installed Loom must be able to seed a paper without a source checkout.
    Style files are vendored by ``scripts/fetch_paper_styles.py``.
    """
    return _PKG / "templates" / "paper"
