"""Provider-neutral guardrails for expensive interactive agent turns.

Cursor's terminal UI exposes two numbers that are materially more useful than
the size of its on-disk SQLite store: cumulative tokens for the active goal and
the percentage of the model context currently occupied.  Loom samples those
status lines while it already polls panes for activity, and interrupts a turn
before a single click can grow into a multi-million-token request chain.
"""

from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


DEFAULT_TURN_TOKEN_BUDGET = 1_500_000
DEFAULT_CONTEXT_PERCENT_BUDGET = 50.0
DEFAULT_AUTO_ROTATE_LIMIT = 2
AUTO_ROTATE_WINDOW_SECONDS = 24 * 60 * 60
_TOKEN_RE = re.compile(r"\b([0-9]+(?:\.[0-9]+)?)\s*([KMG]?)\s+tokens\b", re.I)
_PERCENT_RE = re.compile(r"(?:^|[·|])\s*([0-9]+(?:\.[0-9]+)?)%\s*(?:[·|]|$)")


def _bounded_float_env(name: str, default: float, *, maximum: float) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        return default
    if value == 0:
        return 0.0
    return max(1.0, min(maximum, value))


def turn_token_budget() -> int:
    """Maximum tokens shown for one active agent turn; ``0`` disables it."""
    return int(
        _bounded_float_env(
            "LOOM_TURN_TOKEN_BUDGET",
            float(DEFAULT_TURN_TOKEN_BUDGET),
            maximum=100_000_000.0,
        )
    )


def context_percent_budget() -> float:
    """Maximum live context occupancy; ``0`` disables the percentage guard."""
    return _bounded_float_env(
        "LOOM_CONTEXT_PERCENT_BUDGET",
        DEFAULT_CONTEXT_PERCENT_BUDGET,
        maximum=100.0,
    )


def auto_rotate_limit() -> int:
    """Maximum unattended budget rotations per task in a rolling day."""
    return int(
        _bounded_float_env(
            "LOOM_AUTO_ROTATE_BUDGET_LIMIT",
            float(DEFAULT_AUTO_ROTATE_LIMIT),
            maximum=20.0,
        )
    )


def _compact_number(value: str, suffix: str) -> int:
    multiplier = {"": 1, "K": 1_000, "M": 1_000_000, "G": 1_000_000_000}
    return int(float(value) * multiplier.get(suffix.upper(), 1))


def parse_pane_usage(text: str) -> dict[str, Any]:
    """Extract the newest live token/context indicators from terminal text.

    The regexes intentionally require Cursor's status-line punctuation around
    percentages.  This avoids mistaking a benchmark result printed by the
    agent for context occupancy.
    """
    tokens = 0
    context_percent = 0.0
    for line in reversed((text or "").splitlines()):
        if not tokens:
            match = _TOKEN_RE.search(line)
            if match:
                tokens = _compact_number(match.group(1), match.group(2))
        if not context_percent:
            match = _PERCENT_RE.search(line)
            if match and (
                "files edited" in line.lower()
                or "run everything" in line.lower()
                or " max" in line.lower()
            ):
                context_percent = float(match.group(1))
        if tokens and context_percent:
            break
    return {
        "turn_tokens": tokens,
        "context_percent": context_percent,
        "token_budget": turn_token_budget(),
        "context_percent_budget": context_percent_budget(),
    }


def usage_budget_violation(usage: dict[str, Any]) -> str:
    """Return the first configured hard-limit violation, if any."""
    tokens = int(usage.get("turn_tokens") or 0)
    token_limit = int(usage.get("token_budget") or 0)
    if token_limit and tokens >= token_limit:
        return f"turn used {tokens:,} tokens (limit {token_limit:,})"
    percent = float(usage.get("context_percent") or 0.0)
    percent_limit = float(usage.get("context_percent_budget") or 0.0)
    if percent_limit and percent >= percent_limit:
        return f"context reached {percent:.1f}% (limit {percent_limit:.1f}%)"
    return ""


def marker_path(task_dir: Path) -> Path:
    return task_dir / "usage-budget.json"


def read_budget_marker(task_dir: Path) -> dict[str, Any]:
    try:
        value = json.loads(marker_path(task_dir).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def write_budget_marker(
    task_dir: Path,
    *,
    target: str,
    reason: str,
    usage: dict[str, Any],
) -> dict[str, Any]:
    """Persist that the next message must rotate instead of resuming context."""
    value = {
        "rotate_required": True,
        "stopped_at": datetime.now(timezone.utc).isoformat(),
        "target": target,
        "reason": reason,
        "usage": usage,
    }
    path = marker_path(task_dir)
    tmp = path.with_suffix(".tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")
        tmp.replace(path)
    except OSError:
        pass
    return value


def clear_budget_marker(task_dir: Path) -> None:
    try:
        marker_path(task_dir).unlink()
    except FileNotFoundError:
        pass
    except OSError:
        pass


def auto_rotation_path(task_dir: Path) -> Path:
    return task_dir / "usage-auto-rotation.json"


def claim_auto_rotation(task_dir: Path, *, now: float | None = None) -> dict[str, Any]:
    """Atomically claim one bounded unattended continuation slot.

    The small persistent ledger survives Loom restarts, so restarting the web
    process cannot accidentally reset an overnight task's spend ceiling.
    """
    import time

    current = float(time.time() if now is None else now)
    limit = auto_rotate_limit()
    path = auto_rotation_path(task_dir)
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        state = {}
    started = float(state.get("window_started_epoch") or 0.0)
    attempts = int(state.get("attempts") or 0)
    if not started or current - started >= AUTO_ROTATE_WINDOW_SECONDS:
        started = current
        attempts = 0
    allowed = bool(limit and attempts < limit)
    if allowed:
        attempts += 1
    value = {
        "allowed": allowed,
        "attempts": attempts,
        "limit": limit,
        "window_started_epoch": started,
        "last_claim_epoch": current if allowed else state.get("last_claim_epoch", 0),
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")
        tmp.replace(path)
    except OSError:
        # Fail closed: without a durable spend ledger, do not auto-continue.
        value["allowed"] = False
        value["error"] = "could not persist auto-rotation quota"
    return value
