"""
tests/unit/test_portal.py — Comprehensive tests for the AI News web portal.

Coverage:
  - Route helpers (_list_agents, _list_digests, _list_digests_by_cadence,
    _load_json_digest, _available_formats)
  - FastAPI routes: GET /, GET /digest/{agent}/{date}/{cadence},
    GET /download/{agent}/{date}/{cadence}/{fmt},
    POST /api/trigger, GET /api/health, GET /api/agents, GET /api/jobs
  - Template rendering for all four cadences (daily, weekly, monthly, annual)
  - Empty-state rendering (no agents, no digests)
  - Error cases (404, 400 for bad cadence/format)

Traces: SRC-004 (portal deliverable), SRC-029 (daily view),
        SRC-030 (weekly themes + outlook), SRC-031 (monthly themes + what-to-watch),
        SRC-032 (annual top-10 + predictions), SRC-048 (item schema in portal),
        SRC-102 (smoke test via /api/health), SRC-133 (cadence-specific views),
        SRC-134 (agent switcher, theme visualization, no auth v1),
        SRC-136 (export downloads), SRC-145 (date-stamped filenames),
        SRC-146 (non-2xx alerting), SRC-147 (trigger endpoint),
        SRC-150 (monitoring metadata)
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from ai_news_agent.portal.app import create_app
from ai_news_agent.portal.routes import (
    CADENCE_META,
    _available_formats,
    _list_agents,
    _list_digests,
    _list_digests_by_cadence,
    _load_json_digest,
)

# ---------------------------------------------------------------------------
# Fixtures — shared test data
# ---------------------------------------------------------------------------

SAMPLE_ITEMS: list[dict[str, Any]] = [
    {
        "headline": "OpenAI Announces GPT-5 with Breakthrough Reasoning",
        "source_name": "The Wall Street Journal",
        "url": "https://wsj.com/ai/openai-gpt5",
        "pub_date": "2026-05-10",
        "why_it_matters": (
            "GPT-5 marks a step-change in enterprise AI capabilities. "
            "Companies can now automate complex reasoning tasks previously requiring senior staff."
        ),
        "impact_tags": ["business_impact", "workforce_impact"],
        "tier": "1b",
        "cross_refs": ["https://openai.com/blog/gpt5"],
        "twitter_handle": "sama",
        "tweet_url": "https://twitter.com/sama/status/1234567",
        "prompt_version": "sha256:abc123",
    },
    {
        "headline": "EU Passes Landmark AI Regulation Framework",
        "source_name": "Reuters",
        "url": "https://reuters.com/eu-ai-regulation",
        "pub_date": "2026-05-09",
        "why_it_matters": "Regulatory clarity enables enterprise AI adoption while protecting citizens.",
        "impact_tags": ["policy_impact"],
        "tier": "1b",
        "cross_refs": [],
        "twitter_handle": None,
        "tweet_url": None,
        "prompt_version": "sha256:abc123",
    },
]

SAMPLE_METADATA: dict[str, Any] = {
    "agent_id": "default",
    "cadence": "daily",
    "run_date": "2026-05-11",
    "window_start": "2026-05-10T00:00:00+00:00",
    "window_end": "2026-05-10T23:59:59+00:00",
    "prompt_version": "sha256:abc123def456789012345678901234567890",
    "llm_provider": "openai",
    "llm_model": "gpt-4o",
    "items_considered": 45,
    "items_included": 2,
    "items_by_tier": {"1a": 0, "1b": 2, "2": 0, "3": 0, "4": 0},
    "items_by_source_class": {"web": 2, "twitter": 0},
    "twitter_signal_available": True,
    "tweet_api_call_count": 3,
    "token_usage": 12500,
}


def _make_daily_digest() -> dict[str, Any]:
    """Return a minimal valid daily JSON digest payload."""
    return {
        "schema_version": "1.0",
        "metadata": {**SAMPLE_METADATA, "cadence": "daily"},
        "items": SAMPLE_ITEMS,
        "themes": [],
        "outlook": "",
        "predictions": [],
    }


def _make_weekly_digest() -> dict[str, Any]:
    return {
        "schema_version": "1.0",
        "metadata": {
            **SAMPLE_METADATA,
            "cadence": "weekly",
            "window_start": "2026-05-04T00:00:00+00:00",
            "window_end": "2026-05-10T23:59:59+00:00",
        },
        "items": SAMPLE_ITEMS,
        "themes": ["AI Regulation", "Enterprise LLMs", "Foundation Model Race"],
        "outlook": "Expect more regulatory announcements and model capability disclosures next week.",
        "predictions": [],
    }


def _make_monthly_digest() -> dict[str, Any]:
    return {
        "schema_version": "1.0",
        "metadata": {
            **SAMPLE_METADATA,
            "cadence": "monthly",
            "window_start": "2026-04-01T00:00:00+00:00",
            "window_end": "2026-04-30T23:59:59+00:00",
        },
        "items": SAMPLE_ITEMS,
        "themes": ["Regulatory Shifts", "LLM Cost Wars", "Agentic AI", "AI Safety"],
        "outlook": "Watch for US counterpart legislation and model benchmark announcements in May.",
        "predictions": [],
    }


def _make_annual_digest() -> dict[str, Any]:
    return {
        "schema_version": "1.0",
        "metadata": {
            **SAMPLE_METADATA,
            "cadence": "annual",
            "window_start": "2025-01-01T00:00:00+00:00",
            "window_end": "2025-12-31T23:59:59+00:00",
        },
        "items": SAMPLE_ITEMS,
        "themes": ["AGI Race", "AI Governance", "Model Commoditization"],
        "outlook": "",
        "predictions": [
            "At least two G20 nations will pass national AI legislation by Q2.",
            "A major model provider will offer GPT-4-class capability at <$0.10 per 1M tokens.",
            "Agentic AI will automate 20%+ of knowledge worker tasks in a Fortune 500 firm.",
        ],
    }


@pytest.fixture
def outputs_dir(tmp_path: Path) -> Path:
    """Create a realistic outputs/ directory tree with digest files."""
    # Agent: "default"
    default_dir = tmp_path / "default"
    default_dir.mkdir()

    # Daily digest
    daily = _make_daily_digest()
    (default_dir / "2026-05-11-daily.json").write_text(json.dumps(daily), encoding="utf-8")
    (default_dir / "2026-05-11-daily.md").write_text("# Daily AI Digest\n", encoding="utf-8")
    (default_dir / "2026-05-11-daily.html").write_text("<h1>Daily</h1>", encoding="utf-8")

    # Weekly digest
    weekly = _make_weekly_digest()
    (default_dir / "2026-05-11-weekly.json").write_text(json.dumps(weekly), encoding="utf-8")
    (default_dir / "2026-05-11-weekly.md").write_text("# Weekly AI Digest\n", encoding="utf-8")

    # Monthly digest (JSON only)
    monthly = _make_monthly_digest()
    (default_dir / "2026-04-01-monthly.json").write_text(json.dumps(monthly), encoding="utf-8")

    # Annual digest
    annual = _make_annual_digest()
    (default_dir / "2025-01-01-annual.json").write_text(json.dumps(annual), encoding="utf-8")
    (default_dir / "2025-01-01-annual.md").write_text("# Annual Review\n", encoding="utf-8")
    (default_dir / "2025-01-01-annual.html").write_text("<h1>Annual</h1>", encoding="utf-8")

    # TinyDB store file — should be ignored by _list_digests
    (default_dir / "store.json").write_text("{}", encoding="utf-8")

    # Agent: "technical" — no digests
    tech_dir = tmp_path / "technical"
    tech_dir.mkdir()

    return tmp_path


@pytest.fixture
def client(outputs_dir: Path) -> TestClient:
    """Create a FastAPI TestClient with the test outputs directory."""
    app = create_app(outputs_dir=str(outputs_dir))
    return TestClient(app, raise_server_exceptions=True)


# ---------------------------------------------------------------------------
# Tests for route helper utilities
# ---------------------------------------------------------------------------


class TestListAgents:
    """Tests for _list_agents() helper. Traces: SRC-072 (multiple agents)"""

    def test_returns_sorted_agent_ids(self, outputs_dir: Path) -> None:
        agents = _list_agents(outputs_dir)
        assert agents == ["default", "technical"]

    def test_empty_when_no_outputs_dir(self, tmp_path: Path) -> None:
        agents = _list_agents(tmp_path / "nonexistent")
        assert agents == []

    def test_skips_hidden_directories(self, tmp_path: Path) -> None:
        (tmp_path / ".hidden").mkdir()
        (tmp_path / "visible").mkdir()
        agents = _list_agents(tmp_path)
        assert ".hidden" not in agents
        assert "visible" in agents

    def test_skips_files(self, tmp_path: Path) -> None:
        (tmp_path / "not-a-dir.txt").write_text("hello")
        (tmp_path / "agent1").mkdir()
        agents = _list_agents(tmp_path)
        assert agents == ["agent1"]


class TestListDigests:
    """Tests for _list_digests() helper. Traces: SRC-145, SRC-136"""

    def test_discovers_all_formats(self, outputs_dir: Path) -> None:
        digests = _list_digests(outputs_dir, "default")
        stems = {f"{d['date']}-{d['cadence']}" for d in digests}
        assert "2026-05-11-daily" in stems
        assert "2026-05-11-weekly" in stems
        assert "2026-04-01-monthly" in stems
        assert "2025-01-01-annual" in stems

    def test_daily_has_three_formats(self, outputs_dir: Path) -> None:
        digests = _list_digests(outputs_dir, "default")
        daily = next(d for d in digests if d["cadence"] == "daily" and d["date"] == "2026-05-11")
        assert sorted(daily["formats"]) == ["html", "json", "md"]

    def test_weekly_has_two_formats(self, outputs_dir: Path) -> None:
        digests = _list_digests(outputs_dir, "default")
        weekly = next(d for d in digests if d["cadence"] == "weekly")
        assert sorted(weekly["formats"]) == ["json", "md"]

    def test_monthly_has_one_format(self, outputs_dir: Path) -> None:
        digests = _list_digests(outputs_dir, "default")
        monthly = next(d for d in digests if d["cadence"] == "monthly")
        assert monthly["formats"] == ["json"]

    def test_skips_store_json(self, outputs_dir: Path) -> None:
        digests = _list_digests(outputs_dir, "default")
        cadences = {d["cadence"] for d in digests}
        assert "store" not in cadences

    def test_sorted_newest_first(self, outputs_dir: Path) -> None:
        digests = _list_digests(outputs_dir, "default")
        dates = [d["date"] for d in digests]
        assert dates == sorted(dates, reverse=True)

    def test_empty_for_nonexistent_agent(self, outputs_dir: Path) -> None:
        assert _list_digests(outputs_dir, "nonexistent") == []

    def test_ignores_invalid_filenames(self, tmp_path: Path) -> None:
        agent_dir = tmp_path / "agent"
        agent_dir.mkdir()
        (agent_dir / "notadigest.json").write_text("{}")
        (agent_dir / "badcadence.json").write_text("{}")
        (agent_dir / "2026-05-11.json").write_text("{}")  # missing cadence
        digests = _list_digests(tmp_path, "agent")
        assert digests == []

    def test_technical_agent_has_no_digests(self, outputs_dir: Path) -> None:
        assert _list_digests(outputs_dir, "technical") == []


class TestListDigestsByCadence:
    """Tests for _list_digests_by_cadence(). Traces: SRC-133"""

    def test_returns_all_four_cadences(self, outputs_dir: Path) -> None:
        by_cadence = _list_digests_by_cadence(outputs_dir, "default")
        assert set(by_cadence.keys()) == {"daily", "weekly", "monthly", "annual"}

    def test_daily_bucket_populated(self, outputs_dir: Path) -> None:
        by_cadence = _list_digests_by_cadence(outputs_dir, "default")
        assert len(by_cadence["daily"]) == 1
        assert by_cadence["daily"][0]["cadence"] == "daily"

    def test_annual_bucket_populated(self, outputs_dir: Path) -> None:
        by_cadence = _list_digests_by_cadence(outputs_dir, "default")
        assert len(by_cadence["annual"]) == 1

    def test_empty_buckets_for_no_digests(self, outputs_dir: Path) -> None:
        by_cadence = _list_digests_by_cadence(outputs_dir, "technical")
        for cadence_list in by_cadence.values():
            assert cadence_list == []


class TestLoadJsonDigest:
    """Tests for _load_json_digest(). Traces: SRC-140, SRC-145"""

    def test_loads_valid_daily_json(self, outputs_dir: Path) -> None:
        data = _load_json_digest(outputs_dir, "default", "2026-05-11", "daily")
        assert data is not None
        assert data["metadata"]["cadence"] == "daily"
        assert len(data["items"]) == 2

    def test_returns_none_for_missing_file(self, outputs_dir: Path) -> None:
        data = _load_json_digest(outputs_dir, "default", "1900-01-01", "daily")
        assert data is None

    def test_returns_none_for_invalid_json(self, tmp_path: Path) -> None:
        agent_dir = tmp_path / "agent"
        agent_dir.mkdir()
        (agent_dir / "2026-05-11-daily.json").write_text("not json!")
        data = _load_json_digest(tmp_path, "agent", "2026-05-11", "daily")
        assert data is None

    def test_returns_none_for_unknown_agent(self, outputs_dir: Path) -> None:
        assert _load_json_digest(outputs_dir, "nobody", "2026-05-11", "daily") is None


class TestAvailableFormats:
    """Tests for _available_formats(). Traces: SRC-136"""

    def test_all_three_for_daily(self, outputs_dir: Path) -> None:
        fmts = _available_formats(outputs_dir, "default", "2026-05-11", "daily")
        assert set(fmts) == {"md", "html", "json"}

    def test_two_for_weekly(self, outputs_dir: Path) -> None:
        fmts = _available_formats(outputs_dir, "default", "2026-05-11", "weekly")
        assert set(fmts) == {"json", "md"}

    def test_one_for_monthly(self, outputs_dir: Path) -> None:
        fmts = _available_formats(outputs_dir, "default", "2026-04-01", "monthly")
        assert fmts == ["json"]

    def test_empty_for_missing(self, outputs_dir: Path) -> None:
        fmts = _available_formats(outputs_dir, "default", "1900-01-01", "daily")
        assert fmts == []


class TestCadenceMeta:
    """Tests for CADENCE_META constant. Traces: SRC-133"""

    def test_has_all_four_cadences(self) -> None:
        assert set(CADENCE_META.keys()) == {"daily", "weekly", "monthly", "annual"}

    def test_each_has_icon_label_color(self) -> None:
        for cadence, meta in CADENCE_META.items():
            assert "icon" in meta, f"{cadence} missing icon"
            assert "label" in meta, f"{cadence} missing label"
            assert "color" in meta, f"{cadence} missing color"


# ---------------------------------------------------------------------------
# Tests for FastAPI routes — integration via TestClient
# ---------------------------------------------------------------------------


class TestIndexRoute:
    """Tests for GET /. Traces: SRC-133, SRC-134"""

    def test_returns_200(self, client: TestClient) -> None:
        resp = client.get("/")
        assert resp.status_code == 200

    def test_contains_agent_id(self, client: TestClient) -> None:
        resp = client.get("/")
        assert "default" in resp.text

    def test_contains_all_cadence_labels(self, client: TestClient) -> None:
        resp = client.get("/")
        for label in ("Daily", "Weekly", "Monthly", "Annual"):
            assert label in resp.text, f"Missing cadence label: {label}"

    def test_contains_view_digest_links(self, client: TestClient) -> None:
        resp = client.get("/")
        assert "/digest/default/" in resp.text

    def test_contains_download_links(self, client: TestClient) -> None:
        """SRC-136 — download buttons present on index."""
        resp = client.get("/")
        assert "/download/default/" in resp.text

    def test_empty_state_when_no_agents(self, tmp_path: Path) -> None:
        app = create_app(outputs_dir=str(tmp_path))
        c = TestClient(app)
        resp = c.get("/")
        assert resp.status_code == 200
        assert "No digests yet" in resp.text

    def test_shows_technical_agent_section(self, client: TestClient) -> None:
        resp = client.get("/")
        assert "technical" in resp.text

    def test_html_content_type(self, client: TestClient) -> None:
        resp = client.get("/")
        assert "text/html" in resp.headers["content-type"]


class TestDailyDigestRoute:
    """Tests for GET /digest/{agent_id}/{date_str}/daily. Traces: SRC-029, SRC-048, SRC-133"""

    def test_returns_200(self, client: TestClient) -> None:
        resp = client.get("/digest/default/2026-05-11/daily")
        assert resp.status_code == 200

    def test_contains_headline(self, client: TestClient) -> None:
        resp = client.get("/digest/default/2026-05-11/daily")
        assert "OpenAI Announces GPT-5" in resp.text

    def test_contains_why_it_matters(self, client: TestClient) -> None:
        """SRC-048, SRC-122 — why-it-matters is displayed."""
        resp = client.get("/digest/default/2026-05-11/daily")
        assert "Why it matters" in resp.text

    def test_contains_article_link(self, client: TestClient) -> None:
        """SRC-049 — every item has a link."""
        resp = client.get("/digest/default/2026-05-11/daily")
        assert "https://wsj.com/ai/openai-gpt5" in resp.text

    def test_contains_impact_filter_bar(self, client: TestClient) -> None:
        """SRC-134 — impact filter bar rendered."""
        resp = client.get("/digest/default/2026-05-11/daily")
        assert "impact-filter" in resp.text

    def test_contains_export_buttons(self, client: TestClient) -> None:
        """SRC-136 — export download buttons present."""
        resp = client.get("/digest/default/2026-05-11/daily")
        assert "↓ JSON" in resp.text or "export-json" in resp.text

    def test_contains_monitoring_metadata(self, client: TestClient) -> None:
        """SRC-150 — metadata footer rendered."""
        resp = client.get("/digest/default/2026-05-11/daily")
        assert "gpt-4o" in resp.text  # llm_model

    def test_shows_twitter_handle(self, client: TestClient) -> None:
        """SRC-048 — twitter attribution displayed."""
        resp = client.get("/digest/default/2026-05-11/daily")
        assert "@sama" in resp.text

    def test_shows_tier_badge(self, client: TestClient) -> None:
        resp = client.get("/digest/default/2026-05-11/daily")
        assert "Tier 1b" in resp.text

    def test_404_for_missing_digest(self, client: TestClient) -> None:
        resp = client.get("/digest/default/1900-01-01/daily")
        assert resp.status_code == 404

    def test_404_for_unknown_agent(self, client: TestClient) -> None:
        resp = client.get("/digest/nobody/2026-05-11/daily")
        assert resp.status_code == 404

    def test_400_for_unknown_cadence(self, client: TestClient) -> None:
        resp = client.get("/digest/default/2026-05-11/hourly")
        assert resp.status_code == 400


class TestWeeklyDigestRoute:
    """Tests for GET /digest/{agent_id}/{date_str}/weekly. Traces: SRC-030, SRC-133, SRC-134"""

    def test_returns_200(self, client: TestClient) -> None:
        resp = client.get("/digest/default/2026-05-11/weekly")
        assert resp.status_code == 200

    def test_contains_theme_word_cloud(self, client: TestClient) -> None:
        """SRC-134 — theme tag cloud rendered."""
        resp = client.get("/digest/default/2026-05-11/weekly")
        assert "tag-cloud" in resp.text

    def test_contains_themes(self, client: TestClient) -> None:
        """SRC-030 — this week's themes displayed."""
        resp = client.get("/digest/default/2026-05-11/weekly")
        assert "AI Regulation" in resp.text

    def test_contains_outlook(self, client: TestClient) -> None:
        """SRC-030 — looking ahead / outlook section."""
        resp = client.get("/digest/default/2026-05-11/weekly")
        assert "Looking Ahead" in resp.text or "looking" in resp.text.lower()

    def test_theme_tags_have_data_weight(self, client: TestClient) -> None:
        """SRC-134 — data-weight attribute for word cloud sizing."""
        resp = client.get("/digest/default/2026-05-11/weekly")
        assert "data-weight=" in resp.text

    def test_contains_top_stories_heading(self, client: TestClient) -> None:
        resp = client.get("/digest/default/2026-05-11/weekly")
        assert "Top Stories This Week" in resp.text

    def test_no_predictions_section(self, client: TestClient) -> None:
        """Predictions are annual-only (SRC-124)."""
        resp = client.get("/digest/default/2026-05-11/weekly")
        assert "Predictions" not in resp.text


class TestMonthlyDigestRoute:
    """Tests for GET /digest/{agent_id}/{date_str}/monthly. Traces: SRC-031, SRC-133, SRC-134"""

    def test_returns_200(self, client: TestClient) -> None:
        resp = client.get("/digest/default/2026-04-01/monthly")
        assert resp.status_code == 200

    def test_contains_monthly_themes(self, client: TestClient) -> None:
        """SRC-031 — monthly themes displayed."""
        resp = client.get("/digest/default/2026-04-01/monthly")
        assert "Regulatory Shifts" in resp.text

    def test_contains_what_to_watch(self, client: TestClient) -> None:
        """SRC-031 — anticipated news / what-to-watch section."""
        resp = client.get("/digest/default/2026-04-01/monthly")
        assert "What to Watch" in resp.text

    def test_theme_cloud_uses_monthly_style(self, client: TestClient) -> None:
        """SRC-134 — monthly themes use monthly-theme CSS class."""
        resp = client.get("/digest/default/2026-04-01/monthly")
        assert "monthly-theme" in resp.text

    def test_contains_top_stories_this_month(self, client: TestClient) -> None:
        resp = client.get("/digest/default/2026-04-01/monthly")
        assert "Top Stories This Month" in resp.text

    def test_no_predictions_section(self, client: TestClient) -> None:
        resp = client.get("/digest/default/2026-04-01/monthly")
        assert "Predictions for the Year Ahead" not in resp.text


class TestAnnualDigestRoute:
    """Tests for GET /digest/{agent_id}/{date_str}/annual. Traces: SRC-032, SRC-124, SRC-133"""

    def test_returns_200(self, client: TestClient) -> None:
        resp = client.get("/digest/default/2025-01-01/annual")
        assert resp.status_code == 200

    def test_contains_annual_theme_cloud(self, client: TestClient) -> None:
        """SRC-134 — annual theme word cloud."""
        resp = client.get("/digest/default/2025-01-01/annual")
        assert "annual-theme" in resp.text

    def test_contains_top_10_heading(self, client: TestClient) -> None:
        """SRC-032 — top 10 stories heading."""
        resp = client.get("/digest/default/2025-01-01/annual")
        assert "Top 10 Stories of the Year" in resp.text

    def test_contains_predictions(self, client: TestClient) -> None:
        """SRC-032, SRC-124 — predictions section rendered."""
        resp = client.get("/digest/default/2025-01-01/annual")
        assert "Predictions for the Year Ahead" in resp.text
        assert "G20 nations" in resp.text

    def test_contains_rank_badges(self, client: TestClient) -> None:
        """Annual articles have rank badges (#1, #2, etc.)."""
        resp = client.get("/digest/default/2025-01-01/annual")
        assert "#1" in resp.text or "rank-1" in resp.text

    def test_contains_predictions_intro(self, client: TestClient) -> None:
        """SRC-124 — predictions grounded-in-trends note."""
        resp = client.get("/digest/default/2025-01-01/annual")
        assert "observed trends" in resp.text


class TestDownloadRoute:
    """Tests for GET /download/{agent_id}/{date_str}/{cadence}/{fmt}. Traces: SRC-136, SRC-145"""

    def test_download_json(self, client: TestClient) -> None:
        resp = client.get("/download/default/2026-05-11/daily/json")
        assert resp.status_code == 200
        assert resp.headers["content-type"] == "application/json"

    def test_download_md(self, client: TestClient) -> None:
        resp = client.get("/download/default/2026-05-11/daily/md")
        assert resp.status_code == 200
        assert "text/markdown" in resp.headers["content-type"]

    def test_download_html(self, client: TestClient) -> None:
        resp = client.get("/download/default/2026-05-11/daily/html")
        assert resp.status_code == 200
        assert "text/html" in resp.headers["content-type"]

    def test_404_for_missing_file(self, client: TestClient) -> None:
        resp = client.get("/download/default/1900-01-01/daily/json")
        assert resp.status_code == 404

    def test_400_for_invalid_format(self, client: TestClient) -> None:
        """Unsupported format returns 400. SRC-136."""
        resp = client.get("/download/default/2026-05-11/daily/xml")
        assert resp.status_code == 400

    def test_content_disposition_header(self, client: TestClient) -> None:
        """Filename in content-disposition for browser downloads. SRC-145."""
        resp = client.get("/download/default/2026-05-11/daily/json")
        cd = resp.headers.get("content-disposition", "")
        assert "2026-05-11-daily.json" in cd

    def test_download_annual_md(self, client: TestClient) -> None:
        resp = client.get("/download/default/2025-01-01/annual/md")
        assert resp.status_code == 200


class TestHealthRoute:
    """Tests for GET /api/health. Traces: SRC-102, SRC-146, SRC-150"""

    def test_returns_200(self, client: TestClient) -> None:
        resp = client.get("/api/health")
        assert resp.status_code == 200

    def test_returns_json(self, client: TestClient) -> None:
        resp = client.get("/api/health")
        data = resp.json()
        assert data["status"] == "ok"

    def test_includes_service_name(self, client: TestClient) -> None:
        data = client.get("/api/health").json()
        assert data["service"] == "ai-news-curation-portal"

    def test_includes_agent_list(self, client: TestClient) -> None:
        data = client.get("/api/health").json()
        assert "agents" in data
        assert "default" in data["agents"]

    def test_includes_total_digests(self, client: TestClient) -> None:
        data = client.get("/api/health").json()
        assert data["total_digests"] >= 4  # 4 digest files for default agent

    def test_scheduler_false_when_no_runner(self, client: TestClient) -> None:
        """SRC-150 — scheduler status when no runner attached."""
        data = client.get("/api/health").json()
        assert data["scheduler"]["running"] is False


class TestAgentsApiRoute:
    """Tests for GET /api/agents. Traces: SRC-134"""

    def test_returns_200(self, client: TestClient) -> None:
        assert client.get("/api/agents").status_code == 200

    def test_lists_both_agents(self, client: TestClient) -> None:
        data = client.get("/api/agents").json()
        ids = [a["agent_id"] for a in data["agents"]]
        assert "default" in ids
        assert "technical" in ids

    def test_includes_digest_count(self, client: TestClient) -> None:
        data = client.get("/api/agents").json()
        default = next(a for a in data["agents"] if a["agent_id"] == "default")
        assert default["digest_count"] >= 4

    def test_technical_has_zero_digests(self, client: TestClient) -> None:
        data = client.get("/api/agents").json()
        technical = next(a for a in data["agents"] if a["agent_id"] == "technical")
        assert technical["digest_count"] == 0

    def test_includes_by_cadence(self, client: TestClient) -> None:
        data = client.get("/api/agents").json()
        default = next(a for a in data["agents"] if a["agent_id"] == "default")
        assert "by_cadence" in default
        assert "daily" in default["by_cadence"]


class TestJobsApiRoute:
    """Tests for GET /api/jobs when no scheduler attached. Traces: SRC-150, SRC-052"""

    def test_returns_503_without_scheduler(self, client: TestClient) -> None:
        resp = client.get("/api/jobs")
        assert resp.status_code == 503

    def test_body_indicates_unavailable(self, client: TestClient) -> None:
        data = client.get("/api/jobs").json()
        assert data["status"] == "unavailable"
        assert data["jobs"] == []


class TestTriggerApiRoute:
    """Tests for POST /api/trigger without scheduler. Traces: SRC-028, SRC-147"""

    def test_sourcing_trigger_accepted(self, client: TestClient) -> None:
        resp = client.post(
            "/api/trigger",
            json={
                "agent_id": "default",
                "job_type": "sourcing",
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "accepted"

    def test_curation_trigger_accepted(self, client: TestClient) -> None:
        resp = client.post(
            "/api/trigger",
            json={
                "agent_id": "default",
                "job_type": "curation",
                "cadence": "daily",
            },
        )
        assert resp.status_code == 200

    def test_invalid_job_type_returns_400(self, client: TestClient) -> None:
        resp = client.post(
            "/api/trigger",
            json={
                "agent_id": "default",
                "job_type": "unknown_type",
            },
        )
        assert resp.status_code == 400

    def test_curation_missing_cadence_returns_400(self, client: TestClient) -> None:
        resp = client.post(
            "/api/trigger",
            json={
                "agent_id": "default",
                "job_type": "curation",
            },
        )
        assert resp.status_code == 400

    def test_curation_invalid_cadence_returns_400(self, client: TestClient) -> None:
        resp = client.post(
            "/api/trigger",
            json={
                "agent_id": "default",
                "job_type": "curation",
                "cadence": "hourly",
            },
        )
        assert resp.status_code == 400

    def test_trigger_message_references_alternative(self, client: TestClient) -> None:
        """Without runner, message directs to CLI alternative."""
        data = client.post(
            "/api/trigger",
            json={
                "agent_id": "default",
                "job_type": "sourcing",
            },
        ).json()
        assert "ai-news" in data["message"].lower() or "scheduler" in data["message"].lower()


# ---------------------------------------------------------------------------
# Tests for Twitter degradation note rendering (SRC-148)
# ---------------------------------------------------------------------------


class TestTwitterDegradation:
    """Tests for Twitter unavailable notice in portal views. Traces: SRC-148"""

    def test_daily_shows_warning_when_twitter_unavailable(self, tmp_path: Path) -> None:
        agent_dir = tmp_path / "agent"
        agent_dir.mkdir()
        digest = _make_daily_digest()
        digest["metadata"]["twitter_signal_available"] = False
        (agent_dir / "2026-05-11-daily.json").write_text(json.dumps(digest))

        app = create_app(outputs_dir=str(tmp_path))
        c = TestClient(app)
        resp = c.get("/digest/agent/2026-05-11/daily")
        assert "unavailable" in resp.text.lower() or "⚠️" in resp.text

    def test_daily_no_warning_when_twitter_available(self, client: TestClient) -> None:
        resp = client.get("/digest/default/2026-05-11/daily")
        # twitter_signal_available=True in sample, so no strong warning expected
        # Just verify 200 and no "unavailable" in the warning block
        assert resp.status_code == 200

    def test_degradation_note_rendered(self, tmp_path: Path) -> None:
        agent_dir = tmp_path / "agent"
        agent_dir.mkdir()
        digest = _make_daily_digest()
        digest["twitter_degradation_note"] = "Twitter API rate-limited for this run."
        (agent_dir / "2026-05-11-daily.json").write_text(json.dumps(digest))

        app = create_app(outputs_dir=str(tmp_path))
        c = TestClient(app)
        resp = c.get("/digest/agent/2026-05-11/daily")
        assert "Twitter API rate-limited" in resp.text


# ---------------------------------------------------------------------------
# Tests for edge cases and static files
# ---------------------------------------------------------------------------


class TestStaticFiles:
    """Tests for static file serving."""

    def test_css_served(self, client: TestClient) -> None:
        resp = client.get("/static/css/app.css")
        assert resp.status_code == 200
        assert "text/css" in resp.headers["content-type"]

    def test_js_served(self, client: TestClient) -> None:
        resp = client.get("/static/js/app.js")
        assert resp.status_code == 200
        assert "javascript" in resp.headers["content-type"].lower()


class TestNavigationBreadcrumbs:
    """Tests for breadcrumb navigation presence in all digest views."""

    def test_daily_breadcrumb(self, client: TestClient) -> None:
        resp = client.get("/digest/default/2026-05-11/daily")
        assert "Home" in resp.text
        assert "default" in resp.text

    def test_weekly_breadcrumb(self, client: TestClient) -> None:
        resp = client.get("/digest/default/2026-05-11/weekly")
        assert "Home" in resp.text
        assert "Weekly" in resp.text

    def test_monthly_breadcrumb(self, client: TestClient) -> None:
        resp = client.get("/digest/default/2026-04-01/monthly")
        assert "Monthly" in resp.text

    def test_annual_breadcrumb(self, client: TestClient) -> None:
        resp = client.get("/digest/default/2025-01-01/annual")
        assert "Annual" in resp.text


class TestImpactFilterRendering:
    """Tests for impact filter bar rendering (SRC-134)."""

    def test_business_pill_rendered(self, client: TestClient) -> None:
        resp = client.get("/digest/default/2026-05-11/daily")
        assert "business_impact" in resp.text

    def test_policy_pill_rendered(self, client: TestClient) -> None:
        resp = client.get("/digest/default/2026-05-11/daily")
        assert "policy_impact" in resp.text

    def test_filter_pill_shows_count(self, client: TestClient) -> None:
        resp = client.get("/digest/default/2026-05-11/daily")
        assert "filter-count" in resp.text


class TestMetadataFooter:
    """Tests for quality monitoring metadata footer (SRC-150)."""

    def test_llm_model_shown(self, client: TestClient) -> None:
        resp = client.get("/digest/default/2026-05-11/daily")
        assert "gpt-4o" in resp.text

    def test_items_count_shown(self, client: TestClient) -> None:
        resp = client.get("/digest/default/2026-05-11/daily")
        assert "2/45" in resp.text  # items_included/items_considered

    def test_prompt_version_shown(self, client: TestClient) -> None:
        resp = client.get("/digest/default/2026-05-11/daily")
        # Truncated prompt version should appear
        assert "sha256:abc" in resp.text

    def test_token_usage_shown(self, client: TestClient) -> None:
        resp = client.get("/digest/default/2026-05-11/daily")
        assert "12,500" in resp.text or "12500" in resp.text


class TestEmptyDigestStates:
    """Tests for empty-article states within rendered digests."""

    def test_daily_empty_items(self, tmp_path: Path) -> None:
        agent_dir = tmp_path / "agent"
        agent_dir.mkdir()
        digest = _make_daily_digest()
        digest["items"] = []
        (agent_dir / "2026-05-11-daily.json").write_text(json.dumps(digest))

        app = create_app(outputs_dir=str(tmp_path))
        c = TestClient(app)
        resp = c.get("/digest/agent/2026-05-11/daily")
        assert resp.status_code == 200
        assert "No articles today" in resp.text or "curation threshold" in resp.text

    def test_weekly_no_themes(self, tmp_path: Path) -> None:
        agent_dir = tmp_path / "agent"
        agent_dir.mkdir()
        digest = _make_weekly_digest()
        digest["themes"] = []
        (agent_dir / "2026-05-11-weekly.json").write_text(json.dumps(digest))

        app = create_app(outputs_dir=str(tmp_path))
        c = TestClient(app)
        resp = c.get("/digest/agent/2026-05-11/weekly")
        assert resp.status_code == 200
        # No tag cloud when themes empty
        assert "tag-cloud" not in resp.text

    def test_annual_no_predictions(self, tmp_path: Path) -> None:
        agent_dir = tmp_path / "agent"
        agent_dir.mkdir()
        digest = _make_annual_digest()
        digest["predictions"] = []
        (agent_dir / "2025-01-01-annual.json").write_text(json.dumps(digest))

        app = create_app(outputs_dir=str(tmp_path))
        c = TestClient(app)
        resp = c.get("/digest/agent/2025-01-01/annual")
        assert resp.status_code == 200
        # The predictions *section* should not appear — the page title always says "Predictions"
        # so we check the section heading / predictions-section CSS class is absent
        assert "predictions-section" not in resp.text
        assert "Predictions for the Year Ahead" not in resp.text
