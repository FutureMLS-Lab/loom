"""Parsing agent transcripts into structured conversations.

Carved out of web.py in the route split: everything here turns a session
transcript (Claude / Codex / Cursor) into the message list the
/api/tasks/<slug>/conversation endpoint serves - questions detected,
secrets redacted, tool calls summarized. Pure functions over files; no
HTTP awareness.
"""

from __future__ import annotations

import json
import re
import threading
from pathlib import Path
from typing import Any

from datetime import datetime
import uuid

from loom.rud_task import AGENT_CODEX, AGENT_CURSOR
from loom.web_util import _path_within, _SESSION_ID_RE

# --- structured agent conversations ---------------------------------------

_CONVERSATION_CACHE_LOCK = threading.Lock()
_CONVERSATION_CACHE: dict[str, tuple[int, int, list[dict[str, Any]]]] = {}
_CONVERSATION_PREVIEW_LIMIT = 4000


def _conversation_redact(text: str) -> str:
    """Keep credentials from being surfaced by the mobile transcript view."""
    value = re.sub(
        r"(?i)(authorization\s*:\s*bearer\s+)[^\s\"']+",
        r"\1‹redacted›",
        text,
    )
    value = re.sub(
        r"(?i)((?:token|password|secret|api[_-]?key)[A-Za-z0-9_-]*\s*[=:]\s*)[^\s\"']+",
        r"\1‹redacted›",
        value,
    )
    return re.sub(r"\b[0-9a-fA-F]{40,}\b", "‹redacted›", value)


def _conversation_clip(value: Any, limit: int = _CONVERSATION_PREVIEW_LIMIT) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        text = value
    else:
        try:
            text = json.dumps(value, ensure_ascii=False, indent=2)
        except (TypeError, ValueError):
            text = str(value)
    text = _conversation_redact(text.strip())
    if len(text) <= limit:
        return text
    return f"{text[:limit].rstrip()}\n…"


def _conversation_user_text(text: str) -> str:
    """Extract the actual prompt from Cursor's context-wrapped user event."""
    matches = re.findall(r"<user_query>\s*(.*?)\s*</user_query>", text, re.DOTALL)
    if matches:
        return _conversation_clip(matches[-1], 24000)
    value = re.sub(
        r"<(?:system_reminder|open_and_recently_viewed_files|timestamp)>.*?</(?:system_reminder|open_and_recently_viewed_files|timestamp)>",
        "",
        text,
        flags=re.DOTALL,
    )
    return _conversation_clip(value, 24000)


def _conversation_timestamp(value: Any) -> int | None:
    if isinstance(value, (int, float)):
        return int(value * 1000 if value < 10_000_000_000 else value)
    if not isinstance(value, str) or not value:
        return None
    try:
        return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp() * 1000)
    except ValueError:
        return None


def _conversation_tool_summary(name: str, payload: Any) -> str:
    if not isinstance(payload, dict):
        return name
    for key in (
        "description",
        "path",
        "file_path",
        "target_file",
        "query",
        "pattern",
        "url",
        "command",
        "prompt",
    ):
        candidate = payload.get(key)
        if isinstance(candidate, str) and candidate.strip():
            line = _conversation_redact(candidate.strip().splitlines()[0])
            return line[:180] + ("…" if len(line) > 180 else "")
    return name


def _conversation_question_tool(name: str, payload: Any) -> dict[str, Any] | None:
    if name not in {"AskQuestion", "AskUserQuestion"} or not isinstance(payload, dict):
        return None
    raw_questions = payload.get("questions")
    if not isinstance(raw_questions, list):
        return None
    questions: list[dict[str, Any]] = []
    for index, raw_question in enumerate(raw_questions):
        if not isinstance(raw_question, dict):
            continue
        prompt = str(
            raw_question.get("prompt") or raw_question.get("question") or ""
        ).strip()
        raw_options = raw_question.get("options")
        if not prompt or not isinstance(raw_options, list):
            continue
        options: list[dict[str, str]] = []
        for option_index, raw_option in enumerate(raw_options):
            if not isinstance(raw_option, dict):
                continue
            label = str(raw_option.get("label") or "").strip()
            if not label:
                continue
            option_id = str(raw_option.get("id") or option_index + 1)
            options.append(
                {
                    "id": option_id,
                    "label": _conversation_clip(label, 500),
                    "description": _conversation_clip(
                        raw_option.get("description"), 1000
                    ),
                    # Cursor/Claude's terminal prompt accepts the visible answer.
                    "value": _conversation_clip(label, 500),
                }
            )
        if len(options) < 2:
            continue
        questions.append(
            {
                "id": str(raw_question.get("id") or index + 1),
                "header": _conversation_clip(raw_question.get("header"), 120),
                "prompt": _conversation_clip(prompt, 2000),
                "allow_multiple": bool(
                    raw_question.get("allow_multiple")
                    or raw_question.get("multiSelect")
                ),
                "options": options,
            }
        )
    if not questions:
        return None
    return {
        "title": _conversation_clip(payload.get("title"), 160) or "Input needed",
        "source": "transcript",
        "status": "pending",
        "questions": questions,
    }


_NUMBERED_CHOICE_CUES = (
    "choose",
    "select one",
    "pick one",
    "which one",
    "which option",
    "which would",
    "would you like",
    "do you want",
    "shall i",
    "reply with",
    "请选择",
    "选择一个",
    "选一个",
    "选哪",
    "哪一个",
    "哪个",
    "哪种",
    "要不要",
    "回复数字",
    "你想",
    "你希望",
    "您希望",
)


def _conversation_asks(line: str) -> bool:
    """A line that puts a question to the reader, not one announcing a list."""
    stripped = line.strip().rstrip("*_ ")
    if stripped.endswith(("?", "？")):
        return True
    lowered = stripped.lower()
    return any(cue in lowered for cue in _NUMBERED_CHOICE_CUES)


def _conversation_numbered_question(text: str) -> dict[str, Any] | None:
    """Recognize a final plain-text 1/2/3 choice without parsing normal lists.

    The list has to close the message, with the question asked beside it: the
    line before it or a line after it. A question mark elsewhere — a quoted
    example, an earlier paragraph — left a closing list of points ("two things
    to know: 1. … 2. …") offered as buttons to press.
    """
    matches = list(
        re.finditer(
            r"(?m)^\s*(\d{1,2})[\.\)、:：]\s+(.+?)\s*$",
            text,
        )
    )
    if not 2 <= len(matches) <= 8:
        return None
    numbers = [int(match.group(1)) for match in matches]
    if numbers != list(range(1, len(matches) + 1)):
        return None
    prompt = text[: matches[0].start()].strip()
    prompt_lines = [line.strip() for line in prompt.splitlines() if line.strip()]
    # Indented lines after the last item still belong to it.
    trailing = [
        line
        for line in text[matches[-1].end() :].splitlines()
        if line.strip() and not line[:1].isspace()
    ]
    if len(trailing) > 1:
        return None
    asked_before = bool(prompt_lines) and _conversation_asks(prompt_lines[-1])
    asked_after = bool(trailing) and _conversation_asks(trailing[0])
    if not (asked_before or asked_after):
        return None
    options = []
    for match in matches:
        label = re.sub(r"^\*\*(.*?)\*\*$", r"\1", match.group(2).strip())
        options.append(
            {
                "id": match.group(1),
                "label": _conversation_clip(label, 500),
                "description": "",
                "value": match.group(1),
            }
        )
    if asked_after and not asked_before:
        prompt_lines = [trailing[0].strip()]
    return {
        "title": "Choose an option",
        "source": "numbered",
        "status": "pending",
        "questions": [
            {
                "id": "choice",
                "header": "",
                "prompt": _conversation_clip(
                    prompt_lines[-1] if prompt_lines else "What should the agent do?",
                    1000,
                ),
                "allow_multiple": False,
                "options": options,
            }
        ],
    }


def _conversation_terminal_question(text: str) -> dict[str, Any] | None:
    """Parse the active Cursor/Claude checkbox prompt from a tmux snapshot."""
    raw_lines = text.splitlines()
    marker_index = next(
        (
            index
            for index, line in enumerate(raw_lines)
            if re.search(r"\bQuestion\s+\d+\s+of\s+\d+\b", line, re.IGNORECASE)
        ),
        None,
    )
    if marker_index is None:
        return None

    marker_match = re.search(
        r"\bQuestion\s+(\d+)\s+of\s+(\d+)\b",
        raw_lines[marker_index],
        re.IGNORECASE,
    )
    if marker_match is None:
        return None

    def clean(line: str) -> str:
        value = re.sub(r"^[\s│┃┆┊╎╏┌└├┬┴┼─━╭╰]+", "", line)
        return re.sub(r"[\s│┃┆┊╎╏─━╮╯]+$", "", value).strip()

    prompt = ""
    options: list[dict[str, Any]] = []
    footer = ""
    for raw_line in raw_lines[marker_index + 1 :]:
        line = clean(raw_line)
        if not line:
            continue
        if re.search(r"(?:Space\s+select|Enter\s+(?:next|submit)|Esc\s+to\s+skip)", line, re.I):
            footer = line
            break
        question_match = re.match(r"\d+[\.\)、:：]\s*(.+)", line)
        if question_match and not prompt:
            prompt = question_match.group(1).strip()
            continue
        option_match = re.match(
            r"(?:[›❯>]\s*)?[\[(]([ xX✓✔●○]?)[\])]\s*(.*)",
            line,
        )
        if option_match:
            label = option_match.group(2).strip()
            focused = bool(re.match(r"[›❯>]\s*", line))
            options.append(
                {
                    "id": str(len(options)),
                    "label": label,
                    "description": "",
                    "value": str(len(options)),
                    "terminal_index": len(options),
                    "selected": option_match.group(1).strip().lower()
                    in {"x", "✓", "✔", "●"},
                    "focused": focused,
                }
            )
            continue
        if options and not re.search(r"[↑↓←→].*(?:option|question)", line, re.I):
            options[-1]["label"] = f"{options[-1]['label']} {line}".strip()

    if not prompt or len(options) < 2 or not footer:
        return None
    title = f"Question {marker_match.group(1)} of {marker_match.group(2)}"
    fingerprint_payload = json.dumps(
        {
            "title": title,
            "prompt": prompt,
            "options": [option["label"] for option in options],
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    question_id = str(uuid.uuid5(uuid.NAMESPACE_URL, fingerprint_payload))
    return {
        "id": question_id,
        "title": title,
        "source": "terminal",
        "status": "pending",
        "questions": [
            {
                "id": "current",
                "header": "",
                "prompt": _conversation_clip(prompt, 2000),
                "allow_multiple": bool(re.search(r"Space\s+select", footer, re.I)),
                "options": options,
            }
        ],
    }


def _conversation_terminal_answer_keys(
    question: dict[str, Any],
    selected_ids: list[str],
    *,
    submit: bool = True,
) -> list[str]:
    prompts = question.get("questions") or []
    if len(prompts) != 1 or not isinstance(prompts[0], dict):
        return []
    prompt = prompts[0]
    options = prompt.get("options") or []
    if not options:
        return []
    selected = set(selected_ids)
    valid_ids = {str(option.get("id")) for option in options}
    if not selected or not selected.issubset(valid_ids):
        return []
    focused_index = next(
        (
            index
            for index, option in enumerate(options)
            if bool(option.get("focused"))
        ),
        0,
    )
    keys: list[str] = []
    cursor_index = focused_index

    def move_to(index: int) -> None:
        nonlocal cursor_index
        difference = index - cursor_index
        keys.extend(["Down"] * max(0, difference))
        keys.extend(["Up"] * max(0, -difference))
        cursor_index = index

    if bool(prompt.get("allow_multiple")):
        for index, option in enumerate(options):
            currently_selected = bool(option.get("selected"))
            should_select = str(option.get("id")) in selected
            if currently_selected != should_select:
                move_to(index)
                keys.append("Space")
    else:
        selected_id = next(iter(selected))
        selected_index = next(
            index
            for index, option in enumerate(options)
            if str(option.get("id")) == selected_id
        )
        move_to(selected_index)
    if submit:
        keys.append("Enter")
    return keys


def _conversation_block_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts: list[str] = []
        for item in value:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                text = item.get("text") or item.get("content")
                if isinstance(text, str):
                    parts.append(text)
        return "\n".join(parts)
    if isinstance(value, dict):
        text = value.get("text") or value.get("content")
        if isinstance(text, str):
            return text
    return _conversation_clip(value)


def _cursor_transcript_path(session_id: str, metadata_path: Path) -> Path | None:
    if not _SESSION_ID_RE.match(session_id):
        return None
    cursor_root = Path.home() / ".cursor" / "projects"
    try:
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        metadata = {}
    cwd = str(metadata.get("cwd") or "").strip()
    if cwd:
        encoded = cwd.lstrip("/").replace("/", "-")
        candidate = (
            cursor_root
            / encoded
            / "agent-transcripts"
            / session_id
            / f"{session_id}.jsonl"
        )
        if candidate.is_file() and _path_within(candidate, cursor_root):
            return candidate
    try:
        for candidate in cursor_root.glob(
            f"*/agent-transcripts/{session_id}/{session_id}.jsonl"
        ):
            if candidate.is_file() and _path_within(candidate, cursor_root):
                return candidate
    except OSError:
        pass
    return None


def _conversation_transcript_path(
    session: dict[str, Any], agent: str
) -> Path | None:
    session_id = str(session.get("id") or "").strip()
    if not _SESSION_ID_RE.match(session_id):
        return None
    raw_path = str(session.get("path") or "").strip()
    path = Path(raw_path).expanduser() if raw_path else None
    if agent == AGENT_CURSOR and path is not None:
        return _cursor_transcript_path(session_id, path)
    if path is not None and path.is_file() and path.suffix.lower() == ".jsonl":
        return path
    if agent == AGENT_CODEX:
        codex_root = Path.home() / ".codex" / "sessions"
        try:
            for candidate in codex_root.rglob(f"*{session_id}*.jsonl"):
                if candidate.is_file() and _path_within(candidate, codex_root):
                    return candidate
        except OSError:
            pass
    return None


def _session_last_active(session: dict[str, Any], agent: str) -> float:
    """When *session* last moved: its session file, or its transcript if later.

    Cursor keeps a chat's conversation in a transcript beside its
    ``meta.json``, and a resumed chat writes the transcript without touching
    ``meta.json``. Ranked by ``meta.json`` alone, the chat running in the pane
    sorted below one last used weeks earlier, and the conversation feed showed
    that one instead.
    """
    last = float(session.get("mtime") or 0.0)
    transcript = _conversation_transcript_path(session, agent)
    if transcript is not None:
        try:
            last = max(last, transcript.stat().st_mtime)
        except OSError:
            pass
    return last


def _parse_conversation_transcript(path: Path, agent: str) -> list[dict[str, Any]]:
    """Normalize Claude/Cursor JSONL into a small message protocol for clients."""
    try:
        stat = path.stat()
    except OSError:
        return []
    key = str(path)
    signature = (stat.st_mtime_ns, stat.st_size)
    with _CONVERSATION_CACHE_LOCK:
        cached = _CONVERSATION_CACHE.get(key)
        if cached and cached[:2] == signature:
            return cached[2]

    messages: list[dict[str, Any]] = []
    tools_by_external_id: dict[str, dict[str, Any]] = {}
    questions_by_external_id: dict[str, dict[str, Any]] = {}
    question_messages: list[dict[str, Any]] = []
    cursor_running_tools: list[dict[str, Any]] = []
    session_id = path.stem

    def add_text(kind: str, text: str, line_number: int, index: int, created_at: int | None) -> None:
        normalized = (
            _conversation_user_text(text)
            if kind == "user"
            else _conversation_clip(text, 24000)
        )
        if not normalized:
            return
        if kind == "user":
            for question_message in question_messages:
                question = question_message.get("question") or {}
                if question.get("status") == "pending":
                    question["status"] = "answered"
                    question["answer"] = normalized
        messages.append(
            {
                "id": f"{session_id}:{line_number}:{index}",
                "kind": kind,
                "text": normalized,
                "created_at": created_at,
            }
        )

    try:
        with path.open(encoding="utf-8", errors="replace") as handle:
            for line_number, line in enumerate(handle, 1):
                try:
                    row = json.loads(line)
                except (json.JSONDecodeError, TypeError):
                    continue
                if not isinstance(row, dict):
                    continue

                if agent == AGENT_CURSOR and cursor_running_tools:
                    for tool_message in cursor_running_tools:
                        tool_message["tool"]["status"] = "completed"
                    cursor_running_tools = []

                role = str(row.get("role") or row.get("type") or "")
                message = row.get("message")
                content = message.get("content") if isinstance(message, dict) else None
                created_at = _conversation_timestamp(row.get("timestamp"))
                if isinstance(content, str):
                    content = [{"type": "text", "text": content}]
                if not isinstance(content, list):
                    if agent == AGENT_CURSOR and row.get("type") == "turn_ended":
                        for tool_message in cursor_running_tools:
                            tool_message["tool"]["status"] = "completed"
                        cursor_running_tools = []
                    continue

                for index, block in enumerate(content):
                    if not isinstance(block, dict):
                        continue
                    block_type = str(block.get("type") or "")
                    if role == "user" and block_type == "text":
                        add_text("user", str(block.get("text") or ""), line_number, index, created_at)
                        continue
                    if role == "assistant" and block_type == "text":
                        add_text(
                            "assistant",
                            str(block.get("text") or ""),
                            line_number,
                            index,
                            created_at,
                        )
                        continue
                    if role == "assistant" and block_type == "tool_use":
                        name = str(block.get("name") or "Tool")
                        payload = block.get("input")
                        external_id = str(block.get("id") or f"{line_number}:{index}")
                        question = _conversation_question_tool(name, payload)
                        if question is not None:
                            question_message = {
                                "id": f"{session_id}:{line_number}:{index}",
                                "kind": "question",
                                "created_at": created_at,
                                "question": question,
                            }
                            messages.append(question_message)
                            question_messages.append(question_message)
                            questions_by_external_id[external_id] = question_message
                            continue
                        tool_message = {
                            "id": f"{session_id}:{line_number}:{index}",
                            "kind": "tool",
                            "created_at": created_at,
                            "tool": {
                                "name": name,
                                "summary": _conversation_tool_summary(name, payload),
                                "status": "running",
                                "input": _conversation_clip(payload),
                                "output": "",
                            },
                        }
                        messages.append(tool_message)
                        tools_by_external_id[external_id] = tool_message
                        if agent == AGENT_CURSOR:
                            cursor_running_tools.append(tool_message)
                        continue
                    if role == "user" and block_type == "tool_result":
                        external_id = str(block.get("tool_use_id") or "")
                        question_message = questions_by_external_id.get(external_id)
                        if question_message is not None:
                            question = question_message["question"]
                            question["status"] = (
                                "error" if bool(block.get("is_error")) else "answered"
                            )
                            question["answer"] = _conversation_clip(
                                _conversation_block_text(block.get("content"))
                            )
                            continue
                        tool_message = tools_by_external_id.get(external_id)
                        if tool_message is not None:
                            tool_message["tool"]["status"] = (
                                "error" if bool(block.get("is_error")) else "completed"
                            )
                            tool_message["tool"]["output"] = _conversation_clip(
                                _conversation_block_text(block.get("content"))
                            )
    except OSError:
        return []

    for message_index, question_message in enumerate(messages):
        if question_message.get("kind") != "question":
            continue
        question = question_message.get("question") or {}
        if question.get("status") != "pending":
            continue
        if any(
            later.get("kind") in {"user", "assistant"}
            for later in messages[message_index + 1 :]
        ):
            question["status"] = "answered"

    if messages and messages[-1].get("kind") == "assistant":
        question = _conversation_numbered_question(str(messages[-1].get("text") or ""))
        if question is not None:
            messages.append(
                {
                    "id": f"{messages[-1]['id']}:choices",
                    "kind": "question",
                    "created_at": messages[-1].get("created_at"),
                    "question": question,
                }
            )

    with _CONVERSATION_CACHE_LOCK:
        _CONVERSATION_CACHE[key] = (*signature, messages)
        if len(_CONVERSATION_CACHE) > 64:
            oldest = next(iter(_CONVERSATION_CACHE))
            if oldest != key:
                _CONVERSATION_CACHE.pop(oldest, None)
    return messages


