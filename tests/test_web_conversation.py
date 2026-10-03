import json
import os
import time

from loom.web import (
    _AGENT_WORKING_RE,
    _conversation_terminal_answer_keys,
    _conversation_terminal_question,
    _parse_conversation_transcript,
    _session_last_active,
)


def _write_jsonl(path, rows) -> None:
    path.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
        encoding="utf-8",
    )


def test_resumed_cursor_chat_ranks_by_its_transcript(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    work = tmp_path / "task" / "work"
    work.mkdir(parents=True)
    now = time.time()

    def chat(chat_id: str, meta_age_h: float, transcript_age_h: float) -> dict:
        meta = tmp_path / ".cursor" / "chats" / "workspace" / chat_id / "meta.json"
        meta.parent.mkdir(parents=True)
        meta.write_text(json.dumps({"cwd": str(work)}), encoding="utf-8")
        meta_time = now - meta_age_h * 3600
        os.utime(meta, (meta_time, meta_time))
        encoded = str(work).lstrip("/").replace("/", "-")
        transcript = (
            tmp_path / ".cursor" / "projects" / encoded / "agent-transcripts"
            / chat_id / f"{chat_id}.jsonl"
        )
        transcript.parent.mkdir(parents=True)
        _write_jsonl(transcript, [{"role": "user", "message": {"content": "hi"}}])
        transcript_time = now - transcript_age_h * 3600
        os.utime(transcript, (transcript_time, transcript_time))
        return {"id": chat_id, "path": str(meta), "mtime": meta_time, "size": 0}

    # Started weeks ago and resumed just now: the transcript moves, meta.json
    # does not. The other chat was opened more recently but left alone since.
    resumed = chat("11111111-aaaa-4aaa-8aaa-111111111111", 300, 0.1)
    idle = chat("22222222-bbbb-4bbb-8bbb-222222222222", 200, 200)

    ranked = sorted(
        [idle, resumed],
        key=lambda session: _session_last_active(session, "cursor"),
        reverse=True,
    )

    assert [session["id"] for session in ranked] == [resumed["id"], idle["id"]]
    assert _session_last_active(resumed, "cursor") > now - 3600
    assert _session_last_active(idle, "cursor") < now - 199 * 3600


def test_agent_working_markers_cover_current_and_legacy_cursor_ui() -> None:
    assert _AGENT_WORKING_RE.search("→ Add a follow-up — ctrl+c to stop")
    assert _AGENT_WORKING_RE.search("Esc to interrupt")
    assert not _AGENT_WORKING_RE.search("→ Add a follow-up")


def test_cursor_conversation_extracts_prompt_text_and_tool_calls(tmp_path) -> None:
    transcript = tmp_path / "cursor-session.jsonl"
    _write_jsonl(
        transcript,
        [
            {
                "role": "user",
                "message": {
                    "content": [
                        {
                            "type": "text",
                            "text": (
                                "<timestamp>now</timestamp>"
                                "<user_query>Fix the mobile view</user_query>"
                            ),
                        }
                    ]
                },
            },
            {
                "role": "assistant",
                "message": {
                    "content": [
                        {"type": "text", "text": "I will inspect it."},
                        {
                            "type": "tool_use",
                            "name": "Shell",
                            "input": {
                                "description": "Run checks",
                                "command": "API_TOKEN=secret-value npm test",
                            },
                        },
                    ]
                },
            },
            {"type": "turn_ended", "status": "completed"},
        ],
    )

    messages = _parse_conversation_transcript(transcript, "cursor")

    assert [message["kind"] for message in messages] == ["user", "assistant", "tool"]
    assert messages[0]["text"] == "Fix the mobile view"
    assert messages[1]["text"] == "I will inspect it."
    assert messages[2]["tool"]["name"] == "Shell"
    assert messages[2]["tool"]["status"] == "completed"
    assert "secret-value" not in messages[2]["tool"]["input"]
    assert "‹redacted›" in messages[2]["tool"]["input"]


def test_claude_conversation_matches_tool_result_and_ignores_thinking(tmp_path) -> None:
    transcript = tmp_path / "claude-session.jsonl"
    _write_jsonl(
        transcript,
        [
            {
                "type": "assistant",
                "timestamp": "2026-07-27T01:00:00Z",
                "message": {
                    "content": [
                        {"type": "thinking", "thinking": "private"},
                        {
                            "type": "tool_use",
                            "id": "tool-1",
                            "name": "Read",
                            "input": {"file_path": "/tmp/example.py"},
                        },
                    ]
                },
            },
            {
                "type": "user",
                "message": {
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "tool-1",
                            "content": "file contents",
                        }
                    ]
                },
            },
            {
                "type": "assistant",
                "message": {"content": [{"type": "text", "text": "Done."}]},
            },
        ],
    )

    messages = _parse_conversation_transcript(transcript, "claude")

    assert [message["kind"] for message in messages] == ["tool", "assistant"]
    assert messages[0]["tool"] == {
        "name": "Read",
        "summary": "/tmp/example.py",
        "status": "completed",
        "input": '{\n  "file_path": "/tmp/example.py"\n}',
        "output": "file contents",
    }
    assert messages[1]["text"] == "Done."


def test_cursor_question_becomes_clickable_choices(tmp_path) -> None:
    transcript = tmp_path / "cursor-question.jsonl"
    _write_jsonl(
        transcript,
        [
            {
                "role": "assistant",
                "message": {
                    "content": [
                        {
                            "type": "tool_use",
                            "name": "AskQuestion",
                            "input": {
                                "title": "Pick a direction",
                                "questions": [
                                    {
                                        "id": "approach",
                                        "prompt": "Which approach should I use?",
                                        "allow_multiple": False,
                                        "options": [
                                            {"id": "safe", "label": "Safe migration"},
                                            {"id": "fast", "label": "Fast migration"},
                                        ],
                                    }
                                ],
                            },
                        }
                    ]
                },
            }
        ],
    )

    messages = _parse_conversation_transcript(transcript, "cursor")

    assert len(messages) == 1
    assert messages[0]["kind"] == "question"
    assert messages[0]["question"]["status"] == "pending"
    assert messages[0]["question"]["questions"][0]["options"][1] == {
        "id": "fast",
        "label": "Fast migration",
        "description": "",
        "value": "Fast migration",
    }


def test_plain_numbered_final_question_creates_numeric_actions(tmp_path) -> None:
    transcript = tmp_path / "numbered-question.jsonl"
    _write_jsonl(
        transcript,
        [
            {
                "role": "assistant",
                "message": {
                    "content": [
                        {
                            "type": "text",
                            "text": "Which option do you prefer?\n\n1. Keep terminal\n2. Use chat",
                        }
                    ]
                },
            }
        ],
    )

    messages = _parse_conversation_transcript(transcript, "cursor")

    assert [message["kind"] for message in messages] == ["assistant", "question"]
    options = messages[1]["question"]["questions"][0]["options"]
    assert [option["value"] for option in options] == ["1", "2"]


def test_terminal_checkbox_question_is_parsed_and_answered_with_keys() -> None:
    capture = """
│ Question 1 of 1
│
│ 1. 下一步如何处理?
│
│   › [ ] 更新 H100 harness，只补跑 W4A16 MMLU
│         然后登记 step500（推荐）
│     [x] 先只提交完整的 W4A4
│     [ ] Other: (type to answer)
│
│ ↑/↓ option · ←/→ question · Space select · Enter next/submit · Esc to skip
"""

    question = _conversation_terminal_question(capture)

    assert question is not None
    assert question["source"] == "terminal"
    assert question["questions"][0]["prompt"] == "下一步如何处理?"
    assert [option["label"] for option in question["questions"][0]["options"]] == [
        "更新 H100 harness，只补跑 W4A16 MMLU 然后登记 step500（推荐）",
        "先只提交完整的 W4A4",
        "Other: (type to answer)",
    ]
    assert _conversation_terminal_answer_keys(question, ["0", "2"]) == [
        "Space",
        "Down",
        "Space",
        "Down",
        "Space",
        "Enter",
    ]
    question["questions"][0]["options"][2]["focused"] = True
    question["questions"][0]["options"][0]["focused"] = False
    question["questions"][0]["options"][1]["selected"] = False
    question["questions"][0]["options"][2]["selected"] = True
    assert _conversation_terminal_answer_keys(question, ["1"]) == [
        "Up",
        "Space",
        "Down",
        "Space",
        "Enter",
    ]
    assert _conversation_terminal_answer_keys(
        question,
        ["2"],
        submit=False,
    ) == []
