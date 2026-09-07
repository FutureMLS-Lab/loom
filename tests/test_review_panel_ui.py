"""Per-model reviewer persistence and API payload compatibility."""

from __future__ import annotations

from pathlib import Path

from loom import ar_task as ar
from loom import web_jobs as web


def _task(tmp_path: Path, slug: str = "paper") -> tuple[Path, str]:
    root = tmp_path
    (root / ".RUD" / slug).mkdir(parents=True)
    return root, slug


def _review(model: str, rating: int, recommendation: str) -> dict:
    body = (
        f"## Summary\n{model} summary.\n\n"
        "## Scores\n"
        "Soundness: 3\n"
        "Presentation: 3\n"
        "Contribution: 2\n"
        f"Rating: {rating}\n"
        "Confidence: 4\n"
        f"Recommendation: {recommendation}\n"
    )
    scores = ar.parse_review_scores(body)
    return {
        "model": model,
        "review": body,
        "scores": scores,
        "headline": ar.review_headline(scores),
        "duration_seconds": 1.5,
        "cost": 0.1,
    }


def test_review_payload_reads_three_persisted_model_reports(tmp_path: Path) -> None:
    root, slug = _task(tmp_path)
    reviewers = [
        _review("gpt-5.6-sol-max-fast", 6, "borderline"),
        _review("claude-fable-5-thinking-max", 4, "weak reject"),
        _review("kimi-k3-max", 7, "weak accept"),
    ]
    stored = web._ar_store_panel_reviews(root, slug, 1, reviewers)
    combined = "\n\n---\n\n".join(
        f"# Reviewer: `{item['model']}`\n\n{item['review']}" for item in reviewers
    )
    ar.review_note_path(root, slug, 1).write_text(combined, encoding="utf-8")

    state = ar.new_paper_state(parent_slug="studio", idea={"title": "T"})
    rec = ar.ensure_round(state, 1)
    rec["review"] = {
        "model": ar.CURSOR_REVIEWER_PANEL,
        "scores": reviewers[1]["scores"],
        "headline": reviewers[1]["headline"],
        "deciding_model": reviewers[1]["model"],
        "reviewers": stored,
    }
    ar.write_ar_state(root, slug, state)

    payload = web._ar_review_payload(root, slug, 1)
    assert payload is not None
    assert payload["deciding_model"] == "claude-fable-5-thinking-max"
    assert len(payload["reviewers"]) == 3
    assert all(item["review"].startswith("## Summary") for item in payload["reviewers"])
    assert {item["scores"]["rating"] for item in payload["reviewers"]} == {4, 6, 7}
    assert all(Path(item["path"]).is_file() for item in stored)


def test_review_payload_recovers_existing_combined_panel_file(tmp_path: Path) -> None:
    root, slug = _task(tmp_path)
    first = _review("model-a", 5, "borderline")
    second = _review("model-b", 3, "reject")
    combined = (
        "# Cursor Reviewer Panel\n\n"
        f"# Reviewer: `model-a`\n\n{first['review']}\n\n"
        "---\n\n"
        f"# Reviewer: `model-b`\n\n{second['review']}\n"
    )
    ar.review_note_path(root, slug, 2).parent.mkdir(parents=True, exist_ok=True)
    ar.review_note_path(root, slug, 2).write_text(combined, encoding="utf-8")

    state = ar.new_paper_state(parent_slug="studio", idea={"title": "T"})
    rec = ar.ensure_round(state, 2)
    rec["review"] = {
        "model": ar.CURSOR_REVIEWER_PANEL,
        "scores": second["scores"],
        "headline": second["headline"],
        "deciding_model": "model-b",
        "reviewers": [
            {"model": "model-a", "scores": first["scores"]},
            {"model": "model-b", "scores": second["scores"]},
        ],
    }
    ar.write_ar_state(root, slug, state)

    payload = web._ar_review_payload(root, slug, 2)
    assert payload is not None
    assert [item["model"] for item in payload["reviewers"]] == ["model-a", "model-b"]
    assert "model-a summary" in payload["reviewers"][0]["review"]
    assert "model-b summary" in payload["reviewers"][1]["review"]
