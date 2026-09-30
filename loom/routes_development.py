"""HTTP routes for the bounded Development Task workflow."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from loom import development_task as development
from loom.web_util import _json_bytes


def _scope(self, parsed) -> tuple[Path | None, str | None]:
    return self._resolve_scope(parsed)


def handle_get(self, path: str, parsed) -> bool:
    match = re.match(r"^/api/tasks/([^/]+)/development$", path)
    if not match:
        return False
    root, _project_id = _scope(self, parsed)
    if root is None:
        self._bad_project()
        return True
    try:
        payload = {"ok": True, "state": development.public_state(root, match.group(1))}
        st, b, h = _json_bytes(payload)
    except ValueError as exc:
        st, b, h = _json_bytes({"ok": False, "error": str(exc)}, 404)
    self._send(st, b, h)
    return True


def handle_post(
    self,
    path: str,
    parsed,
    body: dict[str, Any],
    *,
    claude_registry,
    default_skills: Path,
) -> bool:
    match = re.match(
        r"^/api/tasks/([^/]+)/development/(review|repair|approve)$", path
    )
    if not match:
        return False
    slug, action = match.groups()
    root, project_id = _scope(self, parsed)
    if root is None or project_id is None:
        self._bad_project()
        return True
    try:
        if action == "review":
            payload = development.start_review(root, slug)
            st, b, h = _json_bytes(payload, 202)
        elif action == "repair":
            prompt = development.repair_prompt(root, slug)
            result = claude_registry.rotate_and_send(
                root,
                project_id,
                slug,
                prompt,
                default_skills=default_skills,
            )
            if not result.get("ok"):
                st, b, h = _json_bytes(result, 400)
            else:
                state = development.mark_repair_started(root, slug)
                st, b, h = _json_bytes({"ok": True, "agent": result, "state": state})
        else:
            state = development.approve(root, slug)
            st, b, h = _json_bytes({"ok": True, "state": state})
    except ValueError as exc:
        st, b, h = _json_bytes({"ok": False, "error": str(exc)}, 409)
    self._send(st, b, h)
    return True
