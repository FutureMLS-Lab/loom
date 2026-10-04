"""Tests for the AR (Automated Research) task pipeline."""

from __future__ import annotations

import io
import json
import re
import shutil
import time
from pathlib import Path

import pytest

import loom.ar_task as ar
from loom.rud_task import create_task, task_root

CANNED_FEED = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom" xmlns:arxiv="http://arxiv.org/schemas/atom">
  <entry>
    <id>http://arxiv.org/abs/2501.01234v1</id>
    <published>2025-01-02T18:00:00Z</published>
    <updated>2025-01-03T09:00:00Z</updated>
    <title>Outlier-Aware  KV Cache
      Quantization</title>
    <summary>We study 2-bit KV caches and find channel outliers dominate error.</summary>
    <author><name>Ada Lovelace</name></author>
    <author><name>Alan Turing</name></author>
    <arxiv:comment>Accepted at ICLR 2025. 18 pages</arxiv:comment>
  </entry>
  <entry>
    <id>http://arxiv.org/abs/2502.09999v2</id>
    <published>2025-02-11T00:00:00Z</published>
    <title>A Paper With No Venue</title>
    <summary>Nothing announced.</summary>
    <author><name>Grace Hopper</name></author>
  </entry>
</feed>"""

SAMPLE_REVIEW = """## Summary
The paper proposes channel rescaling for 2-bit KV caches.

## Weaknesses
- **[critical]** `Table 1` - no matched-memory baseline -> add one.

## Scores
Soundness: 2
**Presentation:** 3
Contribution: 2/4
Rating: 4
Confidence: 4
Recommendation: weak reject
"""


# --- kind normalization -----------------------------------------------------


def test_normalize_kind_maps_legacy_aris() -> None:
    assert ar.normalize_kind("aris") == ar.KIND_AR
    assert ar.normalize_kind("ARIS") == ar.KIND_AR


class TestAvailableActions:
    """A button should only be live when pressing it would work."""

    def test_a_fresh_paper_can_only_be_started(self):
        can = ar.available_actions({"stage": ar.STAGE_DRAFT})
        assert can["start"]["ok"]
        assert can["start"]["action"] == "draft"
        assert not can["stop"]["ok"]
        assert not can["review"]["ok"], "there is nothing written to review"
        assert not can["gate"]["ok"]
        assert not can["pdf"]["ok"]

    def test_a_gate_blocks_the_author_and_opens_the_decision(self):
        can = ar.available_actions(
            {"stage": ar.STAGE_AWAIT_DRAFT_REVIEW}, has_source=True
        )
        assert can["gate"]["ok"]
        assert not can["start"]["ok"]
        assert "waiting for you" in can["start"]["why"]

    def test_stop_follows_the_loop_not_the_stage(self):
        loop = {"stage": ar.STAGE_LOOP}
        assert not ar.available_actions(loop)["stop"]["ok"]
        assert ar.available_actions(loop, loop_running=True)["stop"]["ok"]
        assert not ar.available_actions(loop, loop_running=True)["start"]["ok"]

    def test_start_is_named_for_what_it_does(self):
        loop = ar.available_actions({"stage": ar.STAGE_LOOP})["start"]
        assert loop["action"] == "loop/start" and "loop" in loop["label"]
        draft = ar.available_actions({"stage": ar.STAGE_DRAFT})["start"]
        assert draft["action"] == "draft" and "draft" in draft["label"]

    def test_delivered_stays_readable_but_not_restartable(self):
        can = ar.available_actions(
            {"stage": ar.STAGE_DELIVERED}, has_source=True, pdf_available=True
        )
        assert not can["start"]["ok"]
        assert can["pdf"]["ok"] and can["build"]["ok"]

    def test_every_refusal_explains_itself(self):
        for stage in ar.STAGE_LABELS:
            for name, rule in ar.available_actions({"stage": stage}).items():
                if not rule["ok"]:
                    assert rule["why"], f"{stage}/{name} refuses without saying why"


class TestSkillCatalog:
    def test_lists_the_roles_and_the_figure_skills(self):
        skills = ar.skill_catalog()
        roles = {s["role"] for s in skills}
        assert {"Studio", "Author", "Reviewer", "Rebuttal"} <= roles
        assert "Figures" in roles
        assert all(s["id"] and s["description"] for s in skills)

    def test_lists_the_paper_rebuttal_skill(self):
        rebuttal = next(
            skill for skill in ar.skill_catalog() if skill["id"] == ar.SKILL_REBUTTAL
        )
        assert rebuttal["name"] == "paper-rebuttal"
        assert rebuttal["role"] == "Rebuttal"
        assert "Acceptance-first" in ar.skill_body(ar.SKILL_REBUTTAL)

    def test_lists_the_results_reporting_skill(self):
        reporting = next(
            skill
            for skill in ar.skill_catalog()
            if skill["id"] == ar.SKILL_RESULTS_REPORTING
        )
        assert reporting["name"] == "paper-results-reporting"
        assert reporting["role"] == "Author"
        body = ar.skill_body(ar.SKILL_RESULTS_REPORTING)
        assert "sample standard deviation" in body
        assert "Main-paper interval-placement rule" in body
        assert "appendix experiment details" in body
        assert "main-paper-<submission-ID>.pdf" in body
        assert "never leave both files" in body

    def test_lists_the_wsdm_submission_skill_separately(self):
        wsdm = next(
            skill
            for skill in ar.skill_catalog()
            if skill["id"] == ar.SKILL_WSDM_SUBMISSION
        )
        assert wsdm["name"] == "wsdm-submission-readiness"
        assert wsdm["role"] == "Author"
        body = ar.skill_body(ar.SKILL_WSDM_SUBMISSION)
        assert "nine-page technical-content boundary" in body
        assert "point estimate (mean) only" in body
        assert "Stochastic main-table cells" in body
        assert "no numeric interval endpoints" in body
        assert "appendix experiment details" in body

    def test_lists_the_wacv_submission_skill_separately(self):
        wacv = next(
            skill
            for skill in ar.skill_catalog()
            if skill["id"] == ar.SKILL_WACV_SUBMISSION
        )
        assert wacv["name"] == "wacv-submission-readiness"
        assert wacv["role"] == "Author"
        body = ar.skill_body(ar.SKILL_WACV_SUBMISSION)
        assert "eight-page technical-body boundary" in body
        assert r"\usepackage[review,<track>]{wacv}" in body
        assert "fully packed page eight" in body
        assert "supplement.pdf" in body
        assert "numeric interval endpoints" in body

    def test_body_reads_a_catalogued_skill(self):
        first = ar.skill_catalog()[0]
        assert len(ar.skill_body(first["id"])) > 200

    def test_body_refuses_anything_not_in_the_catalog(self):
        assert ar.skill_body("../../../etc/passwd") == ""
        assert ar.skill_body("/etc/passwd") == ""


class TestBrowse:
    def test_hides_generated_clutter(self, tmp_path):
        (tmp_path / "code").mkdir()
        (tmp_path / "__pycache__").mkdir()
        (tmp_path / "run.py").write_text("print(1)")
        (tmp_path / "model.safetensors").write_bytes(b"\x00" * 10)
        entries = {e["name"]: e for e in ar.browse_dir(tmp_path)}
        assert set(entries) == {"code", "run.py", "model.safetensors"}
        assert entries["run.py"]["readable"]
        assert not entries["model.safetensors"]["readable"], "weights are not text"
        assert entries["code"]["dir"]

    def test_reads_text_but_not_binaries(self, tmp_path):
        src = tmp_path / "a.py"
        src.write_text("x = 1\n")
        assert ar.read_text_file(src) == "x = 1\n"
        blob = tmp_path / "a.bin"
        blob.write_bytes(b"\x00\x01")
        assert ar.read_text_file(blob) == ""
    assert ar.normalize_kind("ar") == ar.KIND_AR
    assert ar.normalize_kind("agent") == "agent"
    assert ar.normalize_kind("") == "agent"
    assert ar.normalize_kind(None) == "agent"


def test_is_ar_kind() -> None:
    assert ar.is_ar_kind("aris")
    assert ar.is_ar_kind("ar")
    assert not ar.is_ar_kind("agent")
    assert not ar.is_ar_kind("agent")


# --- state round-trip -------------------------------------------------------


def _project(tmp_path: Path) -> Path:
    (tmp_path / ".RUD").mkdir(parents=True, exist_ok=True)
    return tmp_path


def test_state_round_trip(tmp_path: Path) -> None:
    root = _project(tmp_path)
    meta = create_task(root, "AR studio", "goal", kind=ar.KIND_AR, auto_worktree=False)
    assert ar.read_ar_state(root, meta.slug) == {}

    state = ar.new_studio_state(direction="quantization", venue="icml", mode="seed", seed_idea="hi")
    assert ar.write_ar_state(root, meta.slug, state)

    back = ar.read_ar_state(root, meta.slug)
    assert back["role"] == ar.ROLE_STUDIO
    assert back["direction"] == "quantization"
    assert back["venue"] == "icml"
    assert back["mode"] == "seed"
    assert back["seed_idea"] == "hi"
    assert back["search_terms"] == ar.direction_entry("quantization")["terms"]
    assert "cs.CV" in back["search_categories"]
    assert back["updated_at"]

    ar.update_ar_state(root, meta.slug, seed_idea="changed")
    assert ar.read_ar_state(root, meta.slug)["seed_idea"] == "changed"


def test_state_factories_reject_unknown_values() -> None:
    state = ar.new_studio_state(direction="nope", venue="nope", mode="nope", max_rounds=999)
    assert state["direction"] in ar.DIRECTION_IDS
    assert state["venue"] == ar.DEFAULT_VENUE
    assert state["mode"] == ar.MODE_AUTO
    assert state["max_rounds"] == ar.MAX_ROUNDS_LIMIT

    assert ar.new_studio_state(max_rounds="not a number")["max_rounds"] == ar.DEFAULT_MAX_ROUNDS
    assert ar.new_studio_state(max_rounds=0)["max_rounds"] == 1


def test_long_custom_brief_waits_for_search_suggestion() -> None:
    state = ar.new_studio_state(
        direction="custom",
        custom_direction=(
            "Image or Video generation. I need a topic for a conference. "
            "Search on the web if needed."
        ),
    )
    assert state["search_terms"] == []
    assert ar.search_settings(state)["terms"] == []
    assert "cs.CV" in ar.search_settings(state)["categories"]


def test_concise_custom_direction_remains_a_legacy_search_fallback() -> None:
    state = {
        "role": ar.ROLE_STUDIO,
        "direction": "custom",
        "custom_direction": "video generation",
    }
    assert ar.search_settings(state)["terms"] == ["video generation"]


def test_read_state_tolerates_corrupt_json(tmp_path: Path) -> None:
    root = _project(tmp_path)
    meta = create_task(root, "AR", "goal", kind=ar.KIND_AR, auto_worktree=False)
    (task_root(root, meta.slug) / ar.AR_STATE).write_text("{not json", encoding="utf-8")
    assert ar.read_ar_state(root, meta.slug) == {}


# --- idea cards -------------------------------------------------------------


def test_normalize_idea_fills_missing_fields() -> None:
    idea = ar.normalize_idea({"title": "T", "experiments": "one run", "score": "0.5"}, 0)
    assert idea["id"] == "idea-1"
    assert idea["experiments"] == ["one run"]
    assert idea["score"] == 0.5
    assert idea["background_fit"] == 0.0
    assert idea["new_concepts"] == []
    assert idea["status"] == ar.IDEA_STATUS_PROPOSED

    blank = ar.normalize_idea("not a dict", 3)
    assert blank["id"] == "idea-4"
    assert blank["title"] == "Idea 4"
    assert blank["score"] == 0.0


def test_normalize_idea_carries_background_fit_fields() -> None:
    idea = ar.normalize_idea(
        {
            "title": "Profile-aware idea",
            "background_fit": "1.4",
            "background_match": "Reuses quantization experience.",
            "why_understandable": "Only the benchmark is new.",
            "new_concepts": "KV-cache evaluation",
            "resource_fit": "One local GPU.",
        }
    )
    assert idea["background_fit"] == 1.0
    assert idea["background_match"] == "Reuses quantization experience."
    assert idea["why_understandable"] == "Only the benchmark is new."
    assert idea["new_concepts"] == ["KV-cache evaluation"]
    assert idea["resource_fit"] == "One local GPU."


def test_normalize_edge() -> None:
    edge = ar.normalize_edge({"paper": "2210.17323", "title": "GPTQ", "relation": "Extends"})
    assert edge == {"paper": "2210.17323", "title": "GPTQ", "relation": "extends"}

    # A bare id is a valid edge; an unknown relation degrades rather than fails.
    assert ar.normalize_edge("2402.02750")["paper"] == "2402.02750"
    assert ar.normalize_edge({"title": "KIVI", "relation": "vibes"})["relation"] == (
        ar.DEFAULT_RELATION
    )
    # Nothing to point at is not an edge.
    assert ar.normalize_edge({}) is None
    assert ar.normalize_edge({"relation": "extends"}) is None
    assert ar.normalize_edge(42) is None


def test_normalize_idea_carries_edges() -> None:
    idea = ar.normalize_idea(
        {
            "title": "T",
            "derived_from": [
                {"paper": "2510.11696", "title": "QeRL", "relation": "contradicts"},
                {"nothing": "here"},
            ],
        }
    )
    assert idea["derived_from"] == [
        {"paper": "2510.11696", "title": "QeRL", "relation": "contradicts"}
    ]
    # An idea generated before the field existed simply has no edges.
    assert ar.normalize_idea({"title": "T"})["derived_from"] == []


def test_idea_summary_includes_experiments() -> None:
    text = ar.idea_summary(
        {"title": "T", "hypothesis": "H", "experiments": ["a", "b"], "risk": "R"}
    )
    assert "Title: T" in text
    assert "Hypothesis: H" in text
    assert "  - a" in text and "  - b" in text
    assert "Main risk: R" in text


def test_find_idea(tmp_path: Path) -> None:
    state = {"ideas": [{"id": "x", "title": "X"}, {"id": "y", "title": "Y"}]}
    assert ar.find_idea(state, "y")["title"] == "Y"
    assert ar.find_idea(state, "zzz") is None


# --- arXiv ------------------------------------------------------------------


def test_parse_arxiv_feed() -> None:
    papers = ar.parse_arxiv_feed(CANNED_FEED)
    assert len(papers) == 2
    first = papers[0]
    # Titles arrive wrapped across lines; whitespace must be collapsed.
    assert first["title"] == "Outlier-Aware KV Cache Quantization"
    assert first["venue"] == "ICLR 2025"
    assert first["arxiv_id"] == "2501.01234v1"
    assert first["authors"] == ["Ada Lovelace", "Alan Turing"]
    assert papers[1]["venue"] == ""


def test_parse_arxiv_feed_tolerates_garbage() -> None:
    assert ar.parse_arxiv_feed("not xml at all") == []


def test_arxiv_query_is_bounded() -> None:
    q = ar._arxiv_query(["a", "b", "c", "d", "e"])
    # arXiv slows to a crawl on long boolean queries, so terms are capped.
    assert q.count('abs:"') == ar._ARXIV_MAX_TERMS
    assert q.count('ti:"') == ar._ARXIV_MAX_TERMS
    assert "cat:cs.CV" in q
    assert "cat:cs.LG" in q
    assert ar._arxiv_query([]) == "(" + " OR ".join(
        f"cat:{category}" for category in ar.DEFAULT_ARXIV_CATEGORIES
    ) + ")"


def test_search_settings_validation_rejects_injection_and_unknown_categories() -> None:
    terms, categories, error = ar.validate_search_settings(
        ['image" OR cat:all', "video generation"],
        ["cs.CV", "not.real"],
    )
    assert terms == []
    assert categories == []
    assert "unknown arXiv categories" in error

    terms, categories, error = ar.validate_search_settings(
        ['image" OR cat:all', "video generation"],
        ["cs.CV"],
    )
    assert error == ""
    assert terms == ["image OR cat:all", "video generation"]
    query = ar._arxiv_query(terms, categories)
    assert query.startswith("(cat:cs.CV) AND")
    assert query.count('"') == 8


def test_model_suggests_editable_search_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        ar,
        "_run_headless",
        lambda *args, **kwargs: {
            "ok": True,
            "text": (
                '```json\n{"terms":["image generation","video generation",'
                '"diffusion model"],"categories":["cs.CV","cs.LG"],'
                '"rationale":"Focus on visual generation."}\n```'
            ),
            "cost": 0.02,
        },
    )
    state = ar.new_studio_state(
        direction="custom",
        custom_direction="Find a WACV topic in image or video generation.",
    )
    result = ar.suggest_search_settings(state, model="claude-test")
    assert result["ok"]
    assert result["terms"] == [
        "image generation",
        "video generation",
        "diffusion model",
    ]
    assert result["categories"] == ["cs.CV", "cs.LG"]
    assert result["cost"] == 0.02


def test_model_search_suggestion_requires_usable_json(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        ar,
        "_run_headless",
        lambda *args, **kwargs: {"ok": True, "text": "not json", "cost": 0.01},
    )
    result = ar.suggest_search_settings(ar.new_studio_state())
    assert result["ok"] is False
    assert "JSON object" in result["error"]


def test_mine_papers_reports_network_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ar, "_arxiv_fetch", lambda url, timeout, attempts=3: ("", "HTTP 429"))
    res = ar.mine_papers("quantization")
    assert res["ok"] is False
    assert "429" in res["error"]
    assert res["papers"] == []


def test_mine_papers_filters_to_venue(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ar, "_arxiv_fetch", lambda url, timeout, attempts=3: (CANNED_FEED, ""))
    both = ar.mine_papers("quantization")
    assert len(both["papers"]) == 2
    only = ar.mine_papers("quantization", venue_only=True)
    assert [p["venue"] for p in only["papers"]] == ["ICLR 2025"]


def test_mine_papers_uses_explicit_search_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[str] = []

    def fake_fetch(url, timeout, attempts=3):
        seen.append(url)
        return CANNED_FEED, ""

    monkeypatch.setattr(ar, "_arxiv_fetch", fake_fetch)
    result = ar.mine_papers(
        "custom",
        "this long brief must not become the query",
        search_terms=["image generation", "video generation"],
        categories=["cs.CV", "eess.IV"],
    )
    assert result["ok"]
    assert result["search_terms"] == ["image generation", "video generation"]
    assert result["search_categories"] == ["cs.CV", "eess.IV"]
    assert "this+long+brief" not in seen[0]
    assert "cat%3Acs.CV" in seen[0]


# --- job progress logs ------------------------------------------------------


def test_stream_event_line_renders_each_event_kind() -> None:
    assert ar.stream_event_line(
        {"type": "system", "subtype": "init", "model": "claude-fable-5"}
    ) == "started · model claude-fable-5"

    tool = ar.stream_event_line(
        {
            "type": "assistant",
            "message": {
                "content": [
                    {"type": "tool_use", "name": "WebSearch", "input": {"query": "low-bit RL"}}
                ]
            },
        }
    )
    assert tool == "→ WebSearch: low-bit RL"

    assert ar.stream_event_line(
        {
            "type": "user",
            "message": {"content": [{"type": "tool_result", "content": "found 3 results"}]},
        }
    ) == "   ← found 3 results"

    done = ar.stream_event_line(
        {"type": "result", "duration_ms": 9900, "num_turns": 2, "total_cost_usd": 0.4412}
    )
    assert done == "· finished in 9.9s, 2 turns, $0.441"

    # Protocol noise produces no line at all.
    assert ar.stream_event_line({"type": "rate_limit_event"}) is None
    assert ar.stream_event_line({"type": "assistant", "message": {"content": []}}) is None


def test_stream_event_line_clips_long_values() -> None:
    line = ar.stream_event_line(
        {
            "type": "assistant",
            "message": {
                "content": [
                    {"type": "tool_use", "name": "Bash", "input": {"command": "x" * 400}}
                ]
            },
        }
    )
    assert len(line) < 160
    assert line.endswith("…")

    # Multi-line text collapses to one log line.
    text = ar.stream_event_line(
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "a\n\nb   c"}]}}
    )
    assert text == "a b c"


def test_job_log_round_trip(tmp_path: Path) -> None:
    root = _project(tmp_path)
    meta = create_task(root, "AR", "goal", kind=ar.KIND_AR, auto_worktree=False)
    path = ar.job_log_path(root, meta.slug, ar.JOB_IDEAS)
    assert path.name == "ar-ideas.log"
    assert ar.read_job_log(path) == []

    ar.reset_job_log(path)
    ar.append_job_log(path, "started")
    ar.append_job_log(path, "→ WebSearch: quantization")
    lines = ar.read_job_log(path)
    assert len(lines) == 2
    assert lines[0].endswith("started")
    # Every line carries a timestamp so a stalled job is obvious.
    assert re.match(r"^\[\d{2}:\d{2}:\d{2}\] ", lines[0])

    ar.reset_job_log(path)
    assert ar.read_job_log(path) == []


def test_job_log_is_bounded(tmp_path: Path) -> None:
    root = _project(tmp_path)
    meta = create_task(root, "AR", "goal", kind=ar.KIND_AR, auto_worktree=False)
    path = ar.job_log_path(root, meta.slug, ar.JOB_REVIEW)
    ar.reset_job_log(path)
    for i in range(ar.AR_LOG_MAX_LINES * 2 + 50):
        ar.append_job_log(path, f"line {i}")
    # Trimming is amortised - the file is rewritten only once it doubles the
    # cap - so it oscillates between the cap and twice it, never growing.
    on_disk = path.read_text(encoding="utf-8").splitlines()
    assert ar.AR_LOG_MAX_LINES <= len(on_disk) <= ar.AR_LOG_MAX_LINES * 2 + 1
    assert on_disk[-1].endswith(f"line {ar.AR_LOG_MAX_LINES * 2 + 49}")
    assert len(ar.read_job_log(path)) == ar.AR_LOG_TAIL


# --- shared paper store -----------------------------------------------------


@pytest.fixture()
def store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("LOOM_AR_ROOT", str(tmp_path / "ar"))
    return ar.paper_store_path()


def test_store_round_trip(store: Path) -> None:
    assert ar.store_get("2510.11696") is None
    ar.store_put("2510.11696", {"verified": True, "real_title": "QeRL", "year": 2025})
    got = ar.store_get("2510.11696")
    assert got["real_title"] == "QeRL"
    assert got["verified"] is True
    assert store.is_file()


def test_store_expiry(store: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ar.store_put("2510.11696", {"verified": True})
    ar.store_put("2999.99999", {"verified": False, "reason": "not found"})
    assert ar.store_get("2510.11696") is not None
    assert ar.store_get("2999.99999") is not None

    # A miss is re-checked far sooner than a hit: indexing lags publication, so
    # today's absence is not tomorrow's.
    later = time.time() + ar.STORE_TTL_MISS + 60
    monkeypatch.setattr(ar.time, "time", lambda: later)
    assert ar.store_get("2510.11696") is not None
    assert ar.store_get("2999.99999") is None

    much_later = time.time() + ar.STORE_TTL_HIT + 60
    monkeypatch.setattr(ar.time, "time", lambda: much_later)
    assert ar.store_get("2510.11696") is None


def test_store_survives_corruption(store: Path) -> None:
    store.parent.mkdir(parents=True, exist_ok=True)
    store.write_text("{not json", encoding="utf-8")
    assert ar.read_paper_store() == {}
    assert ar.store_get("2510.11696") is None
    ar.store_put("2510.11696", {"verified": True})
    assert ar.store_get("2510.11696") is not None


def test_verify_paper_uses_the_store(store: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls = []

    def boom(*_a, **_k):
        calls.append(1)
        raise AssertionError("verify_paper hit the network on a cached id")

    ar.store_put("2510.11696", {"verified": True, "real_title": "QeRL"})
    monkeypatch.setattr(ar.urllib.request, "urlopen", boom)
    got = ar.verify_paper("2510.11696")
    assert got["real_title"] == "QeRL"
    assert "fetched_at" not in got  # bookkeeping stays inside the store
    assert not calls

    # A version suffix is the same paper.
    assert ar.verify_paper("2510.11696v3")["verified"] is True
    # A malformed id never reaches the network either.
    assert ar.verify_paper("nonsense")["verified"] is False


def test_remember_papers_from_mining(store: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        ar, "_arxiv_fetch", lambda url, timeout, attempts=3: (CANNED_FEED, "")
    )
    res = ar.mine_papers("quantization")
    assert res["ok"]
    # Mining is itself proof the paper exists, so a later verify is free.
    cached = ar.store_get("2501.01234")
    assert cached["verified"] is True
    assert cached["source"] == "arxiv"
    assert cached["real_title"] == "Outlier-Aware KV Cache Quantization"

    def boom(*_a, **_k):
        raise AssertionError("hit the network for a paper mining already saw")

    monkeypatch.setattr(ar.urllib.request, "urlopen", boom)
    assert ar.verify_paper("2501.01234")["verified"] is True


def test_openalex_facts_win_over_arxiv(store: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ar.store_put(
        "2510.11696",
        {"verified": True, "real_title": "Full OpenAlex title", "cited_by": 42, "source": "openalex"},
    )
    monkeypatch.setattr(
        ar, "_arxiv_fetch", lambda url, timeout, attempts=3: (CANNED_FEED, "")
    )
    ar.remember_papers([{"arxiv_id": "2510.11696", "title": "short", "published": "2025-10-13"}])
    kept = ar.store_get("2510.11696")
    assert kept["real_title"] == "Full OpenAlex title"
    assert kept["cited_by"] == 42


# --- review parsing ---------------------------------------------------------


def test_parse_review_scores() -> None:
    scores = ar.parse_review_scores(SAMPLE_REVIEW)
    assert scores == {
        "soundness": 2,
        "presentation": 3,
        "contribution": 2,
        "rating": 4,
        "confidence": 4,
        "recommendation": "weak reject",
    }


def test_parse_review_scores_ignores_out_of_range() -> None:
    scores = ar.parse_review_scores("Soundness: 9\nRating: 7\n")
    assert "soundness" not in scores
    assert scores["rating"] == 7


def test_parse_review_scores_empty() -> None:
    assert ar.parse_review_scores("no scores here") == {}
    assert ar.review_headline({}) == "no scores parsed"


def test_review_headline() -> None:
    headline = ar.review_headline({"rating": 6, "soundness": 3, "recommendation": "borderline"})
    assert "rating 6/10" in headline
    assert "soundness 3/4" in headline
    assert "borderline" in headline


def test_paper_state_defaults_to_cursor_reviewer_panel() -> None:
    state = ar.new_paper_state(parent_slug="p", idea={"title": "T"})
    assert state["reviewer_models"] == list(ar.CURSOR_REVIEWER_MODELS)
    assert ar.CURSOR_REVIEWER_MODELS == (
        "gpt-5.6-sol-max-fast",
        "claude-fable-5-thinking-max",
        "kimi-k3-max",
    )


def test_cursor_reviewer_panel_reads_only_isolated_pdf(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paper = tmp_path / "paper"
    paper.mkdir()
    pdf = paper / "main.pdf"
    pdf.write_bytes(b"%PDF-1.4\n% compiled artifact under test\n")
    (paper / "main.tex").write_text("LATEX_SECRET_MUST_NOT_LEAK", encoding="utf-8")

    ratings = {
        "gpt-5.6-sol-max-fast": (4, "weak reject"),
        "claude-fable-5-thinking-max": (6, "borderline"),
        "kimi-k3-max": (8, "weak accept"),
    }
    review_commands: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        if cmd == ["agent", "models"]:
            listing = "Available models\n" + "\n".join(
                f"{model} - test model" for model in ar.CURSOR_REVIEWER_MODELS
            )
            return ar.subprocess.CompletedProcess(cmd, 0, stdout=listing, stderr="")

        review_commands.append(cmd)
        assert cmd[0:2] == ["agent", "--print"]
        assert cmd[cmd.index("--mode") + 1] == "ask"
        assert cmd[cmd.index("--output-format") + 1] == "json"
        assert "--trust" in cmd
        assert "--force" not in cmd
        assert "--yolo" not in cmd

        model = cmd[cmd.index("--model") + 1]
        workspace = Path(cmd[cmd.index("--workspace") + 1])
        assert (workspace / "submission.pdf").read_bytes() == pdf.read_bytes()
        assert not (workspace / "main.tex").exists()
        prompt = cmd[-1]
        assert str(workspace / "submission.pdf") in prompt
        assert "LATEX_SECRET_MUST_NOT_LEAK" not in prompt
        assert "LaTeX sources of the submission follow" not in prompt

        rating, recommendation = ratings[model]
        review = SAMPLE_REVIEW.replace("Rating: 4", f"Rating: {rating}").replace(
            "Recommendation: weak reject",
            f"Recommendation: {recommendation}",
        )
        payload = {
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "result": review,
            "total_cost_usd": rating / 100,
        }
        return ar.subprocess.CompletedProcess(
            cmd, 0, stdout=json.dumps(payload), stderr=""
        )

    monkeypatch.setattr(ar.subprocess, "run", fake_run)
    result = ar.run_reviewer(
        paper,
        "# reviewer methodology",
        venue="iclr",
        round_n=3,
        build={"ok": True, "clean": True, "pdf": str(pdf)},
        readiness={"ready": True, "failed": []},
    )

    assert result["ok"] is True
    assert {cmd[cmd.index("--model") + 1] for cmd in review_commands} == set(
        ar.CURSOR_REVIEWER_MODELS
    )
    assert len(review_commands) == 3
    assert result["models"] == list(ar.CURSOR_REVIEWER_MODELS)
    assert result["scores"]["rating"] == 4
    assert result["scores"]["recommendation"] == "weak reject"
    assert result["deciding_model"] == "gpt-5.6-sol-max-fast"
    assert result["cost"] == pytest.approx(0.18)
    assert result["headline"].startswith("3 reviewers")
    assert result["input_pdf"] == str(pdf)
    for model in ar.CURSOR_REVIEWER_MODELS:
        assert f"# Reviewer: `{model}`" in result["review"]


def test_cursor_reviewer_refuses_missing_compiled_pdf(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def should_not_run(*args, **kwargs):
        raise AssertionError("Cursor CLI must not run without a compiled PDF")

    monkeypatch.setattr(ar.subprocess, "run", should_not_run)
    result = ar.run_reviewer(
        tmp_path,
        "# reviewer methodology",
        build={"ok": False, "error": "latexmk failed"},
    )
    assert result["ok"] is False
    assert "compiled PDF" in result["error"]
    assert "latexmk failed" in result["error"]


def test_cursor_reviewer_refuses_incomplete_paper_before_model_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pdf = tmp_path / "main.pdf"
    pdf.write_bytes(b"%PDF-1.4\n")

    def should_not_run(*args, **kwargs):
        raise AssertionError("Cursor models must not run when readiness fails")

    monkeypatch.setattr(ar.subprocess, "run", should_not_run)
    result = ar.run_reviewer(
        tmp_path,
        "# reviewer methodology",
        build={"ok": True, "clean": True, "pdf": str(pdf)},
        readiness={
            "ready": False,
            "failed": [
                {
                    "ok": False,
                    "label": "No AR placeholders remain",
                    "detail": "3 markers",
                }
            ],
        },
    )
    assert result["ok"] is False
    assert "readiness gate blocked" in result["error"]
    assert "No AR placeholders remain" in result["error"]


def test_loop_driver_returns_failed_readiness_to_same_round(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from loom.web_jobs import _ARLoopDriver

    root = _project(tmp_path)
    meta = create_task(
        root, "readiness", "goal", kind=ar.KIND_AR, auto_worktree=False
    )
    state = ar.new_paper_state(parent_slug="studio", idea={"title": "T"})
    state["stage"] = ar.STAGE_LOOP
    state["round"] = 1
    ar.ensure_round(state, 1)
    ar.write_ar_state(root, meta.slug, state)
    note = ar.author_note_path(root, meta.slug, 1)
    note.parent.mkdir(parents=True, exist_ok=True)
    note.write_text("# Round 1\n\nclaimed complete", encoding="utf-8")

    readiness = {
        "ready": False,
        "checks": [
            {
                "ok": False,
                "label": "No AR placeholders remain",
                "detail": "2 markers",
            }
        ],
        "failed": [
            {
                "ok": False,
                "label": "No AR placeholders remain",
                "detail": "2 markers",
            }
        ],
        "pdf": str(tmp_path / "main.pdf"),
        "checked_at": "2026-08-07T00:00:00+00:00",
    }
    driver = _ARLoopDriver(object(), root, "project", meta.slug)
    monkeypatch.setattr(
        driver,
        "_build",
        lambda: {"ok": True, "clean": True, "pdf": str(tmp_path / "main.pdf")},
    )
    monkeypatch.setattr(ar, "review_readiness", lambda *args, **kwargs: readiness)

    def reviewer_must_not_run(*args, **kwargs):
        raise AssertionError("reviewer ran before the readiness gate passed")

    monkeypatch.setattr(ar, "run_reviewer", reviewer_must_not_run)
    repair_prompts: list[int] = []
    monkeypatch.setattr(
        driver,
        "_send_readiness_prompt",
        lambda current_state, round_n: repair_prompts.append(round_n),
    )

    driver._close_round(state, 1, note)

    after = ar.read_ar_state(root, meta.slug)
    rec = ar.round_record(after, 1)
    assert rec is not None
    assert rec["readiness"]["ready"] is False
    assert rec["review"] is None
    assert "author" not in rec
    assert len(rec["readiness_attempts"]) == 1
    assert Path(rec["readiness_attempts"][0]["note"]).is_file()
    assert Path(rec["readiness_attempts"][0]["report"]).is_file()
    assert not note.exists()
    assert repair_prompts == [1]


# --- gates and rounds -------------------------------------------------------


def _paper_state(**kw) -> dict:
    return ar.new_paper_state(parent_slug="p", idea={"title": "T"}, **kw)


def test_draft_gate_opens_and_reopens() -> None:
    state = _paper_state()
    assert state["stage"] == ar.STAGE_DRAFT

    state["stage"] = ar.STAGE_AWAIT_DRAFT_REVIEW
    ar.record_gate(state, ar.GATE_DRAFT, "reject", "tighten the method")
    assert state["stage"] == ar.STAGE_DRAFT
    assert ar.last_gate(state, ar.GATE_DRAFT)["note"] == "tighten the method"

    state["stage"] = ar.STAGE_AWAIT_DRAFT_REVIEW
    ar.record_gate(state, ar.GATE_DRAFT, "approve")
    assert state["stage"] == ar.STAGE_LOOP
    assert len(state["gates"]) == 2


def test_final_gate_delivers_or_extends() -> None:
    state = _paper_state(max_rounds=10)
    state["stage"] = ar.STAGE_AWAIT_FINAL_REVIEW
    ar.record_gate(state, ar.GATE_FINAL, "reject", "needs a real baseline")
    # A rejection buys another batch of rounds rather than ending the task.
    assert state["stage"] == ar.STAGE_LOOP
    assert state["max_rounds"] == 20

    state["stage"] = ar.STAGE_AWAIT_FINAL_REVIEW
    ar.record_gate(state, ar.GATE_FINAL, "approve")
    assert state["stage"] == ar.STAGE_DELIVERED
    assert state["loop_running"] is False


def test_ensure_round_is_idempotent_and_sorted() -> None:
    state = _paper_state()
    ar.ensure_round(state, 2)
    ar.ensure_round(state, 1)
    again = ar.ensure_round(state, 2)
    assert [r["n"] for r in state["rounds"]] == [1, 2]
    again["author"] = {"summary": "done"}
    assert ar.round_record(state, 2)["author"]["summary"] == "done"


def test_latest_review_returns_newest_round() -> None:
    state = _paper_state(max_rounds=2)
    assert ar.latest_review(state) is None

    ar.ensure_round(state, 1)["review"] = {"headline": "rating 3/10"}
    ar.ensure_round(state, 2)["review"] = {"headline": "rating 5/10"}
    assert ar.latest_review(state)["headline"] == "rating 5/10"


def test_progress_summary_shows_round_counter() -> None:
    state = _paper_state(max_rounds=10)
    state["stage"] = ar.STAGE_LOOP
    state["round"] = 3
    assert ar.progress_summary(state) == "Author / reviewer rounds (3/10)"
    state["stage"] = ar.STAGE_DELIVERED
    assert ar.progress_summary(state) == "Delivered"


# --- adapting the loop ------------------------------------------------------


def _with_reviews(*ratings, **dims) -> dict:
    """A paper state whose rounds carry the given rating sequence."""
    state = _paper_state(max_rounds=10)
    for i, rating in enumerate(ratings, start=1):
        scores = {"rating": rating}
        for field, values in dims.items():
            if i <= len(values):
                scores[field] = values[i - 1]
        ar.ensure_round(state, i)["review"] = {"scores": scores, "headline": ""}
    state["round"] = len(ratings)
    return state


def test_score_history_and_best_rating() -> None:
    state = _with_reviews(3, 4, 4, 4, 5)
    assert ar.score_history(state) == [3.0, 4.0, 4.0, 4.0, 5.0]
    assert ar.best_rating(state) == 5.0
    assert ar.score_history(_paper_state()) == []
    assert ar.best_rating(_paper_state()) == 0.0


def test_plateau_matches_the_real_run() -> None:
    # The sequence the live low-bit-RL paper actually produced.
    assert ar.is_plateaued(_with_reviews(3)) is False
    assert ar.is_plateaued(_with_reviews(3, 4)) is False
    assert ar.is_plateaued(_with_reviews(3, 4, 4)) is False
    # Three identical ratings in a row is a stall.
    assert ar.is_plateaued(_with_reviews(3, 4, 4, 4)) is True
    # A genuine improvement clears it.
    assert ar.is_plateaued(_with_reviews(3, 4, 4, 4, 5)) is False
    # So does steady progress.
    assert ar.is_plateaued(_with_reviews(3, 4, 5, 6)) is False
    # Sliding backwards is still a stall: nothing improved on the best so far.
    assert ar.is_plateaued(_with_reviews(6, 5, 4, 3)) is True


def test_stuck_dimensions() -> None:
    state = _with_reviews(
        3, 4, 4, 4,
        contribution=[2, 2, 2, 2],
        soundness=[2, 3, 3, 3],
        presentation=[3, 3, 3, 4],
    )
    stuck = ar.stuck_dimensions(state)
    assert "contribution" in stuck
    assert "soundness" in stuck        # 3, 3, 3 over the window
    assert "presentation" not in stuck  # moved to 4


def test_plateau_keeps_fixed_panel_then_pauses_for_human() -> None:
    state = _with_reviews(4, 4, 4)
    assert ar.update_plateau_tracking(state, 3) == 3
    assert ar.should_pause_for_plateau(state, 3) is False

    ar.ensure_round(state, 4)["review"] = {"scores": {"rating": 4}, "headline": ""}
    state["round"] = 4
    assert ar.should_pause_for_plateau(state, 4) is False

    ar.ensure_round(state, 5)["review"] = {"scores": {"rating": 4}, "headline": ""}
    state["round"] = 5
    assert ar.should_pause_for_plateau(state, 5) is True
    assert ar.CURSOR_REVIEWER_MODELS == (
        "gpt-5.6-sol-max-fast",
        "claude-fable-5-thinking-max",
        "kimi-k3-max",
    )

    improved = _with_reviews(4, 4, 4, 5)
    improved["plateau_started_round"] = 3
    assert ar.update_plateau_tracking(improved, 4) == 0
    assert improved["plateau_started_round"] == 0


def test_plateau_note_tells_the_author_to_change_tack() -> None:
    assert ar.plateau_note(_with_reviews(3, 4, 5)) == ""
    note = ar.plateau_note(_with_reviews(4, 4, 4, contribution=[2, 2, 2]))
    assert "has not improved in 3 rounds" in note
    assert "contribution" in note
    assert "narrow the claim" in note


def test_early_stop() -> None:
    assert ar.should_stop_early(_paper_state()) is False
    assert ar.should_stop_early(_with_reviews(3, 4, 5)) is False
    assert ar.should_stop_early(_with_reviews(3, 8)) is True

    lenient = _with_reviews(6)
    lenient["stop_rating"] = 6
    assert ar.should_stop_early(lenient) is True
    assert ar.stop_rating(lenient) == 6
    assert ar.stop_rating(_paper_state()) == ar.DEFAULT_STOP_RATING


def test_new_paper_state_carries_budget_fields() -> None:
    state = _paper_state()
    assert state["cost_usd"] == 0.0
    assert state["stop_rating"] == ar.DEFAULT_STOP_RATING
    assert state["stop_reason"] == ""
    assert state["plateau_started_round"] == 0


def test_agent_env_points_every_task_at_one_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LOOM_AR_ROOT", str(tmp_path / "ar"))
    env = ar.agent_env()
    shared = tmp_path / "ar" / ".cache"
    assert env["HF_HOME"] == str(shared / "huggingface")
    assert env["HF_HUB_CACHE"].startswith(str(shared))
    assert env["TORCH_HOME"] == str(shared / "torch")
    assert env["PIP_CACHE_DIR"] == str(shared / "pip")
    # The directories are created, so the first run does not race on them.
    assert (shared / "huggingface").is_dir()
    assert (shared / "torch").is_dir()


# --- paper layout and skeleton ----------------------------------------------


def test_ensure_ar_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    target = tmp_path / "ar"
    monkeypatch.setenv("LOOM_AR_ROOT", str(target))

    root, created = ar.ensure_ar_root()
    assert root == target
    assert created is True
    assert (target / ".RUD").is_dir()

    # Starting again must not report a fresh creation.
    root, created = ar.ensure_ar_root()
    assert root == target
    assert created is False


@pytest.fixture()
def fresh_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("LOOM_AR_ROOT", raising=False)
    return home


def test_ar_root_is_loom_in_a_fresh_home(fresh_home: Path) -> None:
    from loom.paths import ar_root

    assert ar_root() == fresh_home / "loom"
    (fresh_home / "ar").mkdir()  # an unrelated ~/ar folder is not a Loom root
    assert ar_root() == fresh_home / "loom"


def test_ar_root_keeps_an_existing_ar_install(fresh_home: Path) -> None:
    from loom.paths import ar_root

    (fresh_home / "ar" / ".RUD").mkdir(parents=True)
    (fresh_home / "loom").mkdir()
    assert ar_root() == fresh_home / "ar"


def test_ar_root_stays_put_once_established(fresh_home: Path) -> None:
    from loom.paths import ar_root

    (fresh_home / "loom" / ".RUD").mkdir(parents=True)
    (fresh_home / "ar" / ".RUD").mkdir(parents=True)  # tasks in an unrelated ~/ar project
    assert ar_root() == fresh_home / "loom"


def test_ar_root_never_lands_in_loom_source(fresh_home: Path) -> None:
    from loom.paths import ar_root

    checkout = fresh_home / "loom"
    (checkout / "loom").mkdir(parents=True)
    (checkout / "loom" / "__init__.py").write_text("")
    (checkout / "pyproject.toml").write_text('[project]\nname = "loom"\n')
    assert ar_root() == fresh_home / "ar"


def test_ar_root_override_wins(fresh_home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from loom.paths import ar_root

    (fresh_home / "ar" / ".RUD").mkdir(parents=True)
    monkeypatch.setenv("LOOM_AR_ROOT", str(fresh_home / "elsewhere"))
    assert ar_root() == fresh_home / "elsewhere"


def test_work_layout(tmp_path: Path) -> None:
    root = _project(tmp_path)
    meta = create_task(root, "AR paper", "goal", kind=ar.KIND_AR, auto_worktree=False)
    work = task_root(root, meta.slug) / "work"
    assert ar.work_root(root, meta.slug) == work
    assert ar.code_root(root, meta.slug) == work / "code"
    assert ar.paper_root(root, meta.slug) == work / "manuscript"


def test_paper_root_preserves_legacy_work_paper_layout(tmp_path: Path) -> None:
    root = _project(tmp_path)
    meta = create_task(
        root, "Legacy AR paper", "goal", kind=ar.KIND_AR, auto_worktree=False
    )
    legacy = task_root(root, meta.slug) / "work" / "paper"
    legacy.mkdir(parents=True)
    (legacy / "main.tex").write_text("\\documentclass{article}", encoding="utf-8")

    # Legacy tasks are discovered even before their state carries paper_dir.
    assert ar.paper_root(root, meta.slug) == legacy

    state = ar.new_paper_state(parent_slug="studio", idea={"title": "Legacy"})
    state["paper_dir"] = str(legacy)
    ar.write_ar_state(root, meta.slug, state)
    assert ar.paper_root(root, meta.slug) == legacy

    # A persisted path outside this task is ignored rather than trusted.
    state["paper_dir"] = str(tmp_path / "outside")
    ar.write_ar_state(root, meta.slug, state)
    assert ar.paper_root(root, meta.slug) == legacy


def test_child_slug_groups_under_its_studio() -> None:
    assert ar.child_slug("low-bit-rl", "Matched-Entropy Controls") == (
        "low-bit-rl--matched-entropy-controls"
    )
    # Long titles are cut to the slug limit without leaving a trailing dash.
    long = ar.child_slug("low-bit-rl", "A " + "very " * 40 + "long title")
    assert len(long) <= 80
    assert long.startswith("low-bit-rl--")
    assert not long.endswith("-")
    # A title that slugifies to nothing still yields a usable slug.
    assert ar.child_slug("studio", "!!!") == "studio--task"


def test_init_paper_workspace(tmp_path: Path) -> None:
    if not ar.venue_is_available("iclr"):
        pytest.skip("styles not vendored")
    root = _project(tmp_path)
    meta = create_task(root, "AR paper", "goal", kind=ar.KIND_AR, auto_worktree=False)
    layout = ar.init_paper_workspace(root, meta.slug, "iclr", {"title": "A Paper"})
    assert layout["ok"], layout

    code = ar.code_root(root, meta.slug)
    paper = ar.paper_root(root, meta.slug)
    # Both halves are real repositories, so results stay traceable to code.
    assert (code / ".git").is_dir()
    assert (paper / ".git").is_dir()
    assert (code / "README.md").is_file()
    assert (paper / "main.tex").is_file()

    # Re-running is safe: it must not clobber a repo that already has work.
    (code / "run.py").write_text("print('hi')", encoding="utf-8")
    again = ar.init_paper_workspace(root, meta.slug, "iclr", {"title": "A Paper"})
    assert again["code_repo"] == "already a repository"
    assert (code / "run.py").is_file()


def test_round_paths(tmp_path: Path) -> None:
    root = _project(tmp_path)
    meta = create_task(root, "AR paper", "goal", kind=ar.KIND_AR, auto_worktree=False)
    assert ar.author_note_path(root, meta.slug, 0).name == ar.AUTHOR_NOTE
    assert ar.round_dir(root, meta.slug, 7).name == "round-07"
    assert ar.review_note_path(root, meta.slug, 3).parent.name == "round-03"


@pytest.mark.parametrize("venue", [v["id"] for v in ar.VENUES])
def test_seed_paper_skeleton(tmp_path: Path, venue: str) -> None:
    if not ar.venue_is_available(venue):
        pytest.skip(f"{venue} styles not vendored; run scripts/fetch_paper_styles.py")
    dest = tmp_path / venue
    ok, msg = ar.seed_paper_skeleton(dest, venue, {"title": "A 50% Faster & Better Method"})
    assert ok, msg
    assert (dest / "main.tex").is_file()
    assert (dest / "main.bib").is_file()
    assert (dest / "ar_macros.tex").is_file()
    assert (dest / "sections" / "04_experiments.tex").is_file()
    assert (dest / "figures").is_dir()

    main = (dest / "main.tex").read_text(encoding="utf-8")
    assert ar.TOKEN_TITLE not in main
    assert ar.TOKEN_RUNNING_TITLE not in main
    assert ar.TOKEN_KEYWORDS not in main
    # LaTeX specials in the idea title must not break the build.
    assert r"50\% Faster \& Better" in main


def test_seed_paper_skeleton_refuses_to_clobber(tmp_path: Path) -> None:
    dest = tmp_path / "paper"
    dest.mkdir()
    (dest / "main.tex").write_text("mine", encoding="utf-8")
    ok, msg = ar.seed_paper_skeleton(dest, ar.DEFAULT_VENUE, {"title": "T"})
    assert not ok
    assert "already exists" in msg
    assert (dest / "main.tex").read_text(encoding="utf-8") == "mine"

    ok, _ = ar.seed_paper_skeleton(dest, ar.DEFAULT_VENUE, {"title": "T"}, overwrite=True)
    assert ok
    assert (dest / "main.tex").read_text(encoding="utf-8") != "mine"


def test_seed_paper_skeleton_unknown_venue(tmp_path: Path) -> None:
    # An unknown id resolves to the first venue rather than failing, so a stale
    # ar.json can still produce a paper.
    ok, _ = ar.seed_paper_skeleton(tmp_path / "p", "not-a-venue", {"title": "T"})
    assert ok is ar.venue_is_available(ar.VENUES[0]["id"])


# --- PDF build --------------------------------------------------------------


def test_build_pdf_without_source(tmp_path: Path) -> None:
    res = ar.build_pdf(tmp_path)
    assert res["ok"] is False
    assert "no main.tex" in res["error"]


@pytest.mark.skipif(shutil.which("latexmk") is None, reason="latexmk not installed")
def test_build_pdf_compiles_the_skeleton(tmp_path: Path) -> None:
    venue = ar.DEFAULT_VENUE
    if not ar.venue_is_available(venue):
        pytest.skip("styles not vendored; run scripts/fetch_paper_styles.py")
    dest = tmp_path / "paper"
    ok, msg = ar.seed_paper_skeleton(dest, venue, {"title": "Draft Under Test"})
    assert ok, msg
    res = ar.build_pdf(dest)
    assert res["ok"], res.get("error")
    assert res["clean"], ar.latex_errors(res.get("log", ""))
    assert res["bytes"] > 1000
    assert Path(res["pdf"]).is_file()


def test_missing_latex_packages() -> None:
    log = "! LaTeX Error: File `inconsolata.sty' not found.\nfoo\n"
    assert ar.missing_latex_packages(log) == ["inconsolata"]
    assert ar.missing_latex_packages("nothing wrong") == []


def test_latex_errors_extracts_file_line_messages() -> None:
    log = "./main.tex:12: Undefined control sequence.\nnoise\n./x.sty:3: Emergency stop."
    assert ar.latex_errors(log) == [
        "./main.tex:12: Undefined control sequence.",
        "./x.sty:3: Emergency stop.",
    ]


# --- OpenReview submission prep ---------------------------------------------


def test_submission_invitation_ids() -> None:
    assert ar.submission_invitation("iclr", 2027) == "ICLR.cc/2027/Conference/-/Submission"
    assert ar.submission_invitation("icml", 2027) == "ICML.cc/2027/Conference/-/Submission"
    assert (
        ar.submission_invitation("neurips", 2026)
        == "NeurIPS.cc/2026/Conference/-/Submission"
    )
    assert (
        ar.submission_invitation("colm", 2026)
        == "colmweb.org/COLM/2026/Conference/-/Submission"
    )


def test_delatex_drops_placeholders_with_their_prompts() -> None:
    # An \ARTODO's argument is an instruction to the author, so an unwritten
    # section must come out empty rather than looking written.
    assert ar._delatex(r"\ARTODO{write the context sentence}") == ""
    assert ar._delatex(r"\ARfig[0.8]{a figure that does not exist yet}") == ""
    assert (
        ar._delatex(r"We show \textbf{2-bit} caches work~\citep{x} at \ARnum{} cost.")
        == "We show 2-bit caches work at cost."
    )
    assert ar._delatex("% only a comment\n") == ""


def test_extract_paper_fields(tmp_path: Path) -> None:
    if not ar.venue_is_available("iclr"):
        pytest.skip("styles not vendored")
    paper = tmp_path / "paper"
    ar.seed_paper_skeleton(paper, "iclr", {"title": "A Concrete Title"})
    fields = ar.extract_paper_fields(paper)
    assert fields["title"] == "A Concrete Title"
    # The shipped abstract is all placeholders, so it must extract as empty.
    assert fields["abstract"] == ""
    assert fields["abstract_markers"] > 0

    (paper / "sections" / "00_abstract.tex").write_text(
        "We establish a real result.", encoding="utf-8"
    )
    rewritten = ar.extract_paper_fields(paper)
    assert rewritten["abstract"] == "We establish a real result."
    assert rewritten["abstract_markers"] == 0


def test_extract_paper_fields_icml_title_and_keywords(tmp_path: Path) -> None:
    if not ar.venue_is_available("icml"):
        pytest.skip("styles not vendored")
    paper = tmp_path / "paper"
    ar.seed_paper_skeleton(paper, "icml", {"title": "ICML Title", "metric": "ppl, speed"})
    fields = ar.extract_paper_fields(paper)
    assert fields["title"] == "ICML Title"
    assert fields["keywords"] == ["ppl", "speed"]


def test_placeholder_and_results_detection(tmp_path: Path) -> None:
    if not ar.venue_is_available("iclr"):
        pytest.skip("styles not vendored")
    paper = tmp_path / "paper"
    ar.seed_paper_skeleton(paper, "iclr", {"title": "T"})
    assert ar.count_placeholder_markers(paper) > 0
    # Everything in the fresh experiments section is commented out.
    assert ar._has_real_results(paper) is False

    exp = paper / "sections" / "04_experiments.tex"
    exp.write_text(
        "% \\begin{table}[t] commented out\n\\begin{table}[t]\nreal\n\\end{table}\n",
        encoding="utf-8",
    )
    assert ar._has_real_results(paper) is True

    for name in ("01_introduction", "02_related_work", "03_method", "05_conclusion", "06_appendix", "00_abstract"):
        (paper / "sections" / f"{name}.tex").write_text("done", encoding="utf-8")
    assert ar.count_placeholder_markers(paper) == 0


def test_bib_entry_count(tmp_path: Path) -> None:
    if not ar.venue_is_available("iclr"):
        pytest.skip("styles not vendored")
    paper = tmp_path / "paper"
    ar.seed_paper_skeleton(paper, "iclr", {"title": "T"})
    assert ar._bib_entry_count(paper) == ar.SEED_BIB_ENTRIES


def _write_ready_paper(tmp_path: Path) -> Path:
    paper = tmp_path / "paper"
    ok, msg = ar.seed_paper_skeleton(
        paper,
        "iclr",
        {"title": "A Complete Submission", "metric": "accuracy, latency"},
    )
    assert ok, msg
    sections = {
        "00_abstract.tex": (
            "We introduce a concrete method for efficient inference and evaluate "
            "it against matched baselines. Across repeated measurements, the method "
            "improves accuracy while preserving latency, with limitations stated."
        ),
        "01_introduction.tex": (
            "\\section{Introduction}\nThis paper studies an important efficiency "
            "problem, identifies a precise gap, and contributes a reproducible "
            "method plus controlled empirical evidence.\n"
            "\\begin{figure}[t]\\includegraphics{figures/result}"
            "\\caption{Overview of the method and its measured effect.}\\end{figure}"
        ),
        "02_related_work.tex": (
            "\\section{Related Work}\nPrior efficient inference and quantization "
            "methods provide the closest comparisons. Our contribution differs in "
            "its mechanism and matched-cost evaluation \\citep{vaswani2017attention}."
        ),
        "03_method.tex": (
            "\\section{Method}\nWe define the transformation, objective, algorithm, "
            "and assumptions in enough detail to reproduce every operation. The "
            "runtime overhead is measured rather than asserted."
        ),
        "04_experiments.tex": (
            "\\section{Experiments}\nWe evaluate three seeds against tuned baselines "
            "using the same data and compute budget. "
            "\\begin{table}[t]\\caption{Measured results over three seeds.}"
            "\\begin{tabular}{lc}Baseline & 71.2\\\\Ours & 74.8\\end{tabular}"
            "\\end{table}\n"
            "\\begin{figure}[t]\\includegraphics{figures/result}"
            "\\caption{Measured accuracy and latency.}\\end{figure}\n"
            "Ablations isolate the proposed mechanism and the analysis reports "
            "variance, failure cases, memory, and latency."
        ),
        "05_conclusion.tex": (
            "\\section{Conclusion}\nThe measured evidence supports the narrow claim "
            "that the method improves the tested setting. We state the principal "
            "scope limitation and the next experiment needed for broader use."
        ),
        "06_appendix.tex": (
            "\\section{Reproducibility}\nThe appendix records exact commands, "
            "environments, hyperparameters, seeds, negative results, and additional "
            "breakdowns needed to reproduce the reported measurements."
        ),
    }
    for name, text in sections.items():
        (paper / "sections" / name).write_text(text, encoding="utf-8")
    figure = paper / "figures" / "result.pdf"
    figure.write_bytes(b"%PDF-1.4\n% generated figure\n")
    with (paper / "main.bib").open("a", encoding="utf-8") as fh:
        fh.write(
            "\n@inproceedings{extra2026complete,\n"
            "  title={A Complete Baseline}, author={A. Author}, year={2026}\n}\n"
        )
    (paper / "main.pdf").write_bytes(b"%PDF-1.4\n% complete paper\n")
    return paper


def test_review_readiness_accepts_complete_rendered_paper(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paper = _write_ready_paper(tmp_path)
    # Total PDF length is not a readiness condition: references and appendices
    # are venue-specific and may be unbounded.
    monkeypatch.setattr(ar, "pdf_page_count", lambda pdf: 500)
    monkeypatch.setattr(
        ar,
        "_pdf_text",
        lambda pdf: {
            "ok": True,
            "text": "A Complete Submission\nMeasured results and real figures.",
        },
    )
    result = ar.review_readiness(
        paper,
        venue="iclr",
        build={"ok": True, "clean": True, "pdf": str(paper / "main.pdf"), "log": ""},
    )
    assert result["ready"] is True
    assert result["failed"] == []
    assert all(item["ok"] for item in result["checks"])
    assert not any("page count" in item["label"].lower() for item in result["checks"])
    report = ar.review_readiness_markdown(result)
    assert "PASS — reviewer may run" in report


def test_review_readiness_rejects_fresh_draft(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    if not ar.venue_is_available("iclr"):
        pytest.skip("styles not vendored")
    paper = tmp_path / "paper"
    ar.seed_paper_skeleton(paper, "iclr", {"title": "Draft"})
    (paper / "main.pdf").write_bytes(b"%PDF-1.4\n")
    monkeypatch.setattr(ar, "pdf_page_count", lambda pdf: 7)
    monkeypatch.setattr(
        ar,
        "_pdf_text",
        lambda pdf: {"ok": True, "text": "TODO: unfinished result ?? FIGURE PLACEHOLDER"},
    )
    result = ar.review_readiness(
        paper,
        venue="iclr",
        build={"ok": True, "clean": True, "pdf": str(paper / "main.pdf"), "log": ""},
    )
    labels = {item["label"]: item["ok"] for item in result["checks"]}
    assert result["ready"] is False
    assert labels["No AR placeholders remain"] is False
    assert labels["Experiments contain a real table or figure"] is False
    assert labels["Rendered PDF has no visible placeholders or question marks"] is False


def test_review_readiness_rejects_missing_figure_and_question_mark(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paper = _write_ready_paper(tmp_path)
    (paper / "figures" / "result.pdf").unlink()
    monkeypatch.setattr(ar, "pdf_page_count", lambda pdf: 9)
    monkeypatch.setattr(
        ar,
        "_pdf_text",
        lambda pdf: {"ok": True, "text": "Results are still ?? and TBD."},
    )
    result = ar.review_readiness(
        paper,
        venue="iclr",
        build={"ok": True, "clean": True, "pdf": str(paper / "main.pdf"), "log": ""},
    )
    labels = {item["label"]: item["ok"] for item in result["checks"]}
    assert result["ready"] is False
    assert labels["Every referenced figure file exists"] is False
    assert labels["Rendered PDF has no visible placeholders or question marks"] is False


def test_review_readiness_rejects_a_paper_without_an_intro_figure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paper = _write_ready_paper(tmp_path)
    intro = paper / "sections" / "01_introduction.tex"
    intro.write_text(
        "\\section{Introduction}\nA substantive introduction with a clear gap "
        "and contributions, but every figure hides in the experiments.",
        encoding="utf-8",
    )
    monkeypatch.setattr(ar, "pdf_page_count", lambda pdf: 9)
    monkeypatch.setattr(
        ar, "_pdf_text", lambda pdf: {"ok": True, "text": "Complete prose."}
    )
    result = ar.review_readiness(
        paper,
        venue="iclr",
        build={"ok": True, "clean": True, "pdf": str(paper / "main.pdf"), "log": ""},
    )
    labels = {item["label"]: item["ok"] for item in result["checks"]}
    assert result["ready"] is False
    assert labels["Paper opens with an overview figure"] is False


def test_build_submission_flags_an_unfinished_paper(tmp_path: Path) -> None:
    if not ar.venue_is_available("iclr"):
        pytest.skip("styles not vendored")
    root = _project(tmp_path)
    meta = create_task(root, "sub", "g", kind=ar.KIND_AR, auto_worktree=False)
    state = ar.new_paper_state(parent_slug="p", idea={"title": "T"}, venue="iclr")
    ar.write_ar_state(root, meta.slug, state)
    ar.seed_paper_skeleton(ar.paper_root(root, meta.slug), "iclr", {"title": "T"})

    # check_invitation=False keeps the test off the network.
    sub = ar.build_submission(root, meta.slug, state, check_invitation=False)
    assert sub["ready"] is False
    labels = {c["label"]: c["ok"] for c in sub["checks"]}
    assert labels["You approved the paper at the final gate"] is False
    assert labels["No unfilled placeholders left"] is False
    assert labels["Abstract is written"] is False
    assert labels["Experiments section has a real table or figure"] is False
    assert labels["Bibliography goes beyond the template seeds"] is False
    assert sub["fields"]["title"] == "T"
    # The command is prep-only: it must never carry credentials.
    assert "post_note_edit" in sub["command"]
    assert "password" in sub["command"]
    assert "you@example.com" in sub["command"]


def test_fetch_invitation_classifies_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    import urllib.error

    def raise_http(code: int, name: str, message: str):
        def _open(req, timeout=0):
            raise urllib.error.HTTPError(
                "u", code, message, {},  # type: ignore[arg-type]
                io.BytesIO(json.dumps({"name": name, "message": message}).encode()),
            )
        return _open

    monkeypatch.setattr(
        ar.urllib.request, "urlopen", raise_http(400, "InvitationExpiredError", "expired")
    )
    assert ar.fetch_invitation("X")["status"] == "expired"

    monkeypatch.setattr(
        ar.urllib.request, "urlopen", raise_http(404, "NotFoundError", "nope")
    )
    assert ar.fetch_invitation("X")["status"] == "missing"

    monkeypatch.setattr(
        ar.urllib.request, "urlopen", raise_http(403, "ChallengeRequiredError", "challenge")
    )
    blocked = ar.fetch_invitation("X")
    assert blocked["status"] == "blocked"
    assert blocked["ok"] is False


def test_resolve_invitation_prefers_an_open_window(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[str] = []

    def fake(invitation_id, timeout=20):
        seen.append(invitation_id)
        if invitation_id.startswith("ICLR.cc/2030"):
            return {"ok": True, "status": "open", "fields": ["title"], "required": ["title"]}
        return {"ok": False, "status": "missing", "message": "nope"}

    monkeypatch.setattr(ar, "fetch_invitation", fake)
    got = ar.resolve_invitation("iclr", years=[2029, 2030])
    assert got["status"] == "open"
    assert got["year"] == 2030
    assert len(seen) == 2

    monkeypatch.setattr(
        ar, "fetch_invitation", lambda i, timeout=20: {"ok": False, "status": "expired", "message": "gone"}
    )
    assert ar.resolve_invitation("iclr", years=[2025])["status"] == "expired"


# --- skills and prompts -----------------------------------------------------


@pytest.mark.parametrize(
    "name",
    [
        ar.SKILL_STUDIO,
        ar.SKILL_AUTHOR,
        ar.SKILL_REVIEWER,
        ar.SKILL_RESULTS_REPORTING,
        ar.SKILL_WSDM_SUBMISSION,
        ar.SKILL_WACV_SUBMISSION,
    ],
)
def test_ar_skills_are_bundled(name: str) -> None:
    assert len(ar.ar_skill_text(name)) > 500


def test_figure_skills_are_bundled() -> None:
    skills = {s["name"] for s in ar.figure_skills()}
    assert {
        "results-figure-1",
        "results-figure-2",
        "teaser-figure-1",
        "teaser-figure-2",
        "teaser-figure-3",
        "teaser-figure-4",
        "checkbib",
    } <= skills
    for skill in ar.figure_skills():
        assert Path(skill["path"]).is_file()
        assert skill["description"], f"{skill['name']} has no description"
        # Only the summary line travels in a prompt, never the whole skill.
        assert len(skill["description"]) < 400


def test_figure_skills_block_stays_compact() -> None:
    block = ar.figure_skills_block()
    assert ar.DEFAULT_TEASER_SKILL == "teaser-figure-3"
    assert block.startswith("AUTO-RESEARCH DEFAULT TEASER: teaser-figure-3")
    assert "Do not wait for the user to ask" in block
    assert "teaser-figure-3 [DEFAULT TEASER]" in block
    assert "teaser-figure-1" in block
    assert "teaser-figure-3" in block
    assert "teaser-figure-4" in block
    assert "results-figure-2" in block
    assert "checkbib" in block
    # Seven SKILL.md files are much larger than the compact prompt menu.
    assert len(block) < 3500


def test_author_prompts_point_at_the_figure_skills(tmp_path: Path) -> None:
    state = _paper_state()
    draft = ar.author_draft_prompt(tmp_path, tmp_path / "manuscript", state)
    rnd = ar.author_round_prompt(tmp_path, tmp_path / "manuscript", state, 2)
    for prompt in (draft, rnd):
        assert "AUTO-RESEARCH DEFAULT TEASER: teaser-figure-3" in prompt
        assert "teaser-figure-1" in prompt
        assert "teaser-figure-3" in prompt
        assert "teaser-figure-4" in prompt
        assert "results-figure-2" in prompt
        assert "SKILL.md" in prompt


def test_author_prompts_inject_results_reporting(tmp_path: Path) -> None:
    state = _paper_state()
    prompts = (
        ar.author_draft_prompt(tmp_path, tmp_path / "manuscript", state),
        ar.author_round_prompt(tmp_path, tmp_path / "manuscript", state, 2),
        ar.author_readiness_repair_prompt(
            tmp_path,
            tmp_path / "manuscript",
            state,
            2,
            {"ready": False, "failed": []},
        ),
    )
    for prompt in prompts:
        assert "Results reporting and provenance" in prompt
        assert "mean ± sample standard deviation" in prompt
        assert "EXPERIMENT_DETAILS.md" in prompt
        assert "Main-paper interval-placement rule" in prompt
        assert "appendix experiment details" in prompt
        assert "main-paper-<submission-ID>.pdf" in prompt
        assert "WSDM submission requirements" not in prompt
        assert "WACV submission requirements" not in prompt


def test_wsdm_author_prompts_inject_only_the_wsdm_skill(tmp_path: Path) -> None:
    state = _paper_state()
    state["parent_slug"] = "wsdm2027"
    assert ar.is_wsdm_paper(state)
    prompts = (
        ar.author_draft_prompt(tmp_path, tmp_path / "manuscript", state),
        ar.author_round_prompt(tmp_path, tmp_path / "manuscript", state, 2),
        ar.author_readiness_repair_prompt(
            tmp_path,
            tmp_path / "manuscript",
            state,
            2,
            {"ready": False, "failed": []},
        ),
    )
    for prompt in prompts:
        assert "Results reporting and provenance" in prompt
        assert "WSDM submission requirements" in prompt
        assert "WACV submission requirements" not in prompt
        assert r"\documentclass[sigconf,anonymous,review]{acmart}" in prompt
        assert "appendix-backup.tex" in prompt
        assert "point estimate (mean) only" in prompt
        assert "no numeric interval endpoints" in prompt
        assert "appendix experiment details" in prompt
        assert "main-paper-<CMT-ID>.pdf" in prompt

    non_wsdm = _paper_state()
    assert not ar.is_wsdm_paper(non_wsdm)
    assert ar.wsdm_submission_block(non_wsdm) == ""


def test_wacv_author_prompts_inject_only_the_wacv_skill(tmp_path: Path) -> None:
    state = _paper_state()
    state["parent_slug"] = "wacv2027"
    assert ar.is_wacv_paper(state)
    prompts = (
        ar.author_draft_prompt(tmp_path, tmp_path / "manuscript", state),
        ar.author_round_prompt(tmp_path, tmp_path / "manuscript", state, 2),
        ar.author_readiness_repair_prompt(
            tmp_path,
            tmp_path / "manuscript",
            state,
            2,
            {"ready": False, "failed": []},
        ),
    )
    for prompt in prompts:
        assert "Results reporting and provenance" in prompt
        assert "WACV submission requirements" in prompt
        assert "WSDM submission requirements" not in prompt
        assert r"\usepackage[review,<track>]{wacv}" in prompt
        assert "exactly eight pages" in prompt
        assert "supplement.pdf" in prompt
        assert "numeric interval endpoints" in prompt
        assert "main-paper-<OpenReview-ID>.pdf" in prompt

    non_wacv = _paper_state()
    assert not ar.is_wacv_paper(non_wacv)
    assert ar.wacv_submission_block(non_wacv) == ""


def test_ar_skill_text_missing_file() -> None:
    assert ar.ar_skill_text("NOPE.md") == ""


def test_author_prompts_demand_the_teaser_and_name_their_skills(tmp_path: Path) -> None:
    # The draft is skeleton-only EXCEPT Figure 1: the conceptual teaser is
    # drawn at the draft so the human gate sees the visual story, and every
    # completion note must name the skills it applied so the per-paper
    # skills report fills from evidence rather than guesswork.
    state = _paper_state()
    draft = ar.author_draft_prompt(tmp_path, tmp_path / "manuscript", state)
    assert "The one figure you DO draw now is Figure 1" in draft
    assert "every RESULTS figure an \\ARfig{} placeholder" in draft
    assert "Skills used:" in draft

    rnd = ar.author_round_prompt(tmp_path, tmp_path / "manuscript", state, 2)
    assert "Skills used:" in rnd

    repair = ar.author_readiness_repair_prompt(
        tmp_path, tmp_path / "manuscript", state, 2, {"ready": False, "failed": []}
    )
    assert "Skills used:" in repair


def test_author_prompts_carry_the_contract(tmp_path: Path) -> None:
    state = _paper_state(venue="icml")
    task_dir = tmp_path / "task"
    paper_dir = tmp_path / "task" / "work" / "paper"

    draft = ar.author_draft_prompt(task_dir, paper_dir, state)
    assert "FIRST DRAFT" in draft
    assert str(ar.author_note_path_for(task_dir, 0)) in draft
    assert "ICML" in draft
    assert r"\ARnum" in draft

    rnd = ar.author_round_prompt(
        task_dir, paper_dir, state, 3, review_text="Rating: 4", gate_note="add a baseline"
    )
    assert "ROUND 3 of 10" in rnd
    assert "Rating: 4" in rnd
    assert "add a baseline" in rnd
    assert str(ar.author_note_path_for(task_dir, 3)) in rnd
    assert "hard review-readiness gate" in rnd
    assert "ready-to-submit artifact" in rnd

    first = ar.author_round_prompt(task_dir, paper_dir, state, 1)
    assert "no reviewer report yet" in first

    repair = ar.author_readiness_repair_prompt(
        task_dir,
        paper_dir,
        state,
        3,
        {
            "ready": False,
            "failed": [
                {
                    "label": "Every referenced figure file exists",
                    "detail": "missing: figures/main.pdf",
                }
            ],
        },
        report_path=task_dir / "rounds" / "round-03" / "readiness-attempt-01.md",
    )
    assert "reviewer panel was NOT called" in repair
    assert "missing: figures/main.pdf" in repair
    assert "Continue the SAME round" in repair
    assert str(ar.author_note_path_for(task_dir, 3)) in repair


def test_studio_prompt_reflects_mode(tmp_path: Path) -> None:
    auto = ar.studio_prompt(tmp_path, ar.new_studio_state(mode="auto"), "goal")
    assert "Mode: auto direction" in auto
    assert "goal" in auto
    assert "=== Researcher background (" not in auto

    seed = ar.studio_prompt(
        tmp_path, ar.new_studio_state(mode="seed", seed_idea="try rescaling"), ""
    )
    assert "Mode: seed idea" in seed
    assert "try rescaling" in seed


def test_researcher_background_is_snapshotted_and_injected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = ar.new_studio_state(
        direction="quantization",
        background_profile_id="profile-one",
        background_profile_snapshot={
            "id": "profile-one",
            "name": "Researcher One",
            "summary": "Works on low-bit inference.",
            "methods": ["post-training quantization"],
            "resources": ["one GPU"],
        },
        background_fit_mode="strict",
    )
    assert state["background_profile_id"] == "profile-one"
    assert state["background_profile_snapshot"]["fit_mode"] == "strict"

    pane_prompt = ar.studio_prompt(tmp_path, state, "")
    assert "Researcher background" in pane_prompt
    assert "Works on low-bit inference." in pane_prompt
    assert "Strict" in pane_prompt

    captured: list[str] = []

    def fake_run(prompt: str, **_kwargs: object) -> dict[str, object]:
        captured.append(prompt)
        if "bounded arXiv search" in prompt:
            return {
                "ok": True,
                "text": '{"terms":["low-bit inference"],"categories":["cs.LG"]}',
            }
        return {
            "ok": True,
            "text": '[{"title":"T","background_fit":0.9}]',
        }

    monkeypatch.setattr(ar, "_run_headless", fake_run)
    assert ar.suggest_search_settings(state)["ok"]
    assert ar.propose_ideas(state, "skill")["ok"]
    assert len(captured) == 2
    assert all("Works on low-bit inference." in prompt for prompt in captured)


# --- catalog ----------------------------------------------------------------


def test_catalog_shape() -> None:
    cat = ar.catalog()
    assert {d["id"] for d in cat["directions"]} == ar.DIRECTION_IDS
    assert {v["id"] for v in cat["venues"]} == ar.VENUE_IDS
    assert {c["id"] for c in cat["arxiv_categories"]} == ar.ARXIV_CATEGORY_IDS
    assert "cs.CV" in cat["default_arxiv_categories"]
    assert cat["default_venue"] in ar.VENUE_IDS
    assert cat["default_max_rounds"] == ar.DEFAULT_MAX_ROUNDS
    assert [m["id"] for m in cat["modes"]] == [ar.MODE_AUTO, ar.MODE_SEED]


def test_default_general_goal_uses_the_paper_content() -> None:
    seeded = ar.default_general_goal(
        ar.new_studio_state(mode="seed", seed_idea="2-bit KV caches", venue="icml")
    )
    assert seeded == "Write a paper for ICML on: 2-bit KV caches"

    auto = ar.default_general_goal(
        ar.new_studio_state(direction="cot-efficiency", venue="iclr")
    )
    assert "ICLR" in auto
    assert "Chain-of-thought efficiency" in auto

    custom = ar.default_general_goal(
        ar.new_studio_state(direction="custom", custom_direction="weird optimizers")
    )
    assert "weird optimizers" in custom


def test_direction_label_prefers_custom_text() -> None:
    state = ar.new_studio_state(direction="custom", custom_direction="weird optimizers")
    assert ar.direction_label(state) == "weird optimizers"
    assert ar.direction_label(ar.new_studio_state(direction="moe")) == "Mixture of experts"


def test_every_direction_has_search_terms() -> None:
    for d in ar.DIRECTIONS:
        if d["id"] == "custom":
            assert d["terms"] == []
        else:
            assert d["terms"], f"{d['id']} has no arXiv search terms"


def test_sweep_stale_jobs_unwedges_interrupted_work(tmp_path: Path) -> None:
    from loom.web_jobs import ARLoopManager

    root = _project(tmp_path)
    stuck = create_task(root, "stuck studio", "g", kind=ar.KIND_AR, auto_worktree=False)
    fine = create_task(root, "fine studio", "g", kind=ar.KIND_AR, auto_worktree=False)
    plain = create_task(root, "plain task", "g", auto_worktree=False)

    state = ar.new_studio_state()
    state["ideas_status"] = "running"
    state["search_suggest_status"] = "running"
    state["papers_status"] = "done"
    ar.write_ar_state(root, stuck.slug, state)
    ar.write_ar_state(root, fine.slug, ar.new_studio_state())

    cleared = ARLoopManager.sweep_stale_jobs([("p", root)])
    assert cleared == 1

    after = ar.read_ar_state(root, stuck.slug)
    assert after["ideas_status"] == "error"
    assert "restart" in after["ideas_error"]
    assert after["search_suggest_status"] == "error"
    assert "restart" in after["search_suggest_error"]
    # Statuses that were not mid-flight are left exactly as they were.
    assert after["papers_status"] == "done"
    assert "ideas_status" not in ar.read_ar_state(root, fine.slug)
    assert ar.read_ar_state(root, plain.slug) == {}

    # Sweeping again is a no-op once nothing is running.
    assert ARLoopManager.sweep_stale_jobs([("p", root)]) == 0


def test_every_venue_template_is_vendored() -> None:
    missing = [v["id"] for v in ar.VENUES if not ar.venue_is_available(v["id"])]
    assert not missing, f"run scripts/fetch_paper_styles.py for: {missing}"


def test_round_prompt_defends_the_main_body(tmp_path: Path) -> None:
    # The failure mode this guards: reviewers demand rigor, the author
    # responds with defensive side probes and appendix exile, and the paper
    # gets longer and weaker at once.
    rnd = ar.author_round_prompt(tmp_path, tmp_path / "manuscript", _paper_state(), 2)
    assert "The MAIN BODY carries the story" in rnd
    assert "ONE adequately-powered decisive" in rnd
    assert "exiling primary evidence" in rnd or "exile" in rnd


def test_review_prompt_carries_the_reviewers_own_memory(tmp_path: Path) -> None:
    # Rebuttal dynamics: from round two each reviewer re-reads its OWN prior
    # report and the author's response, then re-scores the current pages.
    pdf = tmp_path / "submission.pdf"
    cold = ar._cursor_pdf_review_prompt(
        "RUBRIC", pdf_path=pdf, venue="iclr", round_n=1
    )
    assert "previous review" not in cold
    warm = ar._cursor_pdf_review_prompt(
        "RUBRIC",
        pdf_path=pdf,
        venue="iclr",
        round_n=3,
        prior_review="Rating: 4. The baselines are missing.",
        author_response="Added the baselines in Table 2.",
    )
    assert "your own review of the previous revision (round 2)" in warm
    assert "The baselines are missing." in warm
    assert "Added the baselines in Table 2." in warm
    assert "verify each against the PDF" in warm
    assert "do not anchor on your previous rating" in warm
    # Isolation still holds: no other reviewer's report is ever offered.
    assert "another review's report" in warm


def test_final_gate_reject_grants_a_fresh_plateau_window() -> None:
    # Rejecting at the final gate is a human's "keep going" - without the
    # reset, the old plateau clock re-paused the loop after every single
    # flat round, turning 20 granted rounds into one-round-per-approval.
    state = _paper_state()
    state["plateau_started_round"] = 5
    state["stop_reason"] = "plateaued at round 5"
    ar.record_gate(state, ar.GATE_FINAL, "reject", "keep going")
    assert state["stage"] == ar.STAGE_LOOP
    assert state["plateau_started_round"] == 0
    assert state["stop_reason"] == ""


def test_author_prompts_carry_the_default_prompt_but_reviewers_stay_cold(
    tmp_path: Path,
) -> None:
    """The Loom floor (working style, memory protocol, skills table) now
    reaches the AR author too; the reviewer panel deliberately never sees
    it - isolation is the point of the panel."""
    state = _paper_state()
    draft = ar.author_draft_prompt(tmp_path, tmp_path / "manuscript", state)
    rnd = ar.author_round_prompt(tmp_path, tmp_path / "manuscript", state, 2)
    repair = ar.author_readiness_repair_prompt(
        tmp_path, tmp_path / "manuscript", state, 2, {"ready": False, "failed": []}
    )
    for prompt in (draft, rnd, repair):
        assert "Loom default prompt - always active" in prompt
        assert "Working style" in prompt
    review = ar._cursor_pdf_review_prompt(
        "RUBRIC", pdf_path=tmp_path / "s.pdf", venue="iclr", round_n=2
    )
    assert "Loom default prompt" not in review
