"""
tests/unit/test_rendering.py — Comprehensive Rendering Agent test suite.

Tests the Markdown, HTML, and JSON renderers plus the RenderingAgent
orchestrator. The LLM is never called — all tests operate on pre-built
CurationRunResult objects (SRC-098: mock LLM and Twitter calls).

Coverage map:
  TestUrlValidation          — URL enforcement at renderer (SRC-141, SRC-049)
  TestMarkdownRenderer       — MD format, all cadences, item cards (SRC-004, SRC-138)
  TestHtmlRenderer           — HTML format, all cadences, XSS safety (SRC-004, SRC-137)
  TestJsonRenderer           — JSON format, schema, monitoring fields (SRC-004, SRC-140, SRC-150)
  TestRenderingAgent         — Orchestrator: files written, paths, idempotency (SRC-145)
  TestRenderingAgentStore    — DigestRecord path update after render (SRC-145)
  TestRenderingAgentDryRun   — Dry-run mode (SRC-102)
  TestFilenameConvention     — Date-stamped filenames for distribution (SRC-145, SRC-140)
  TestTwitterDegradation     — Degradation note in all formats (SRC-148)
  TestCrossRefs              — Cross-reference links (SRC-048)
  TestCadenceEdgeCases       — Empty items, missing themes, empty predictions
  TestMonitoringFields       — SRC-150 quality monitoring fields in JSON + Markdown

Traces: SRC-004, SRC-029–SRC-032, SRC-048–SRC-049, SRC-102,
        SRC-129, SRC-135–SRC-141, SRC-145, SRC-148, SRC-150
"""

from __future__ import annotations

import json
import tempfile
from dataclasses import replace
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from ai_news_agent.curation.agent import CurationRunResult
from ai_news_agent.rendering.agent import RenderingAgent, RenderingResult
from ai_news_agent.rendering.html_renderer import HtmlRenderer
from ai_news_agent.rendering.html_renderer import _is_valid_url as html_valid
from ai_news_agent.rendering.json_renderer import SCHEMA_VERSION, JsonRenderer
from ai_news_agent.rendering.markdown_renderer import MarkdownRenderer
from ai_news_agent.rendering.markdown_renderer import _is_valid_url as md_valid
from ai_news_agent.storage.models import CuratedItem, DigestMetadata

# ---------------------------------------------------------------------------
# Helpers — build test fixtures without pytest fixture indirection for clarity
# ---------------------------------------------------------------------------

def _make_item(
    headline: str = "AI Reshapes Enterprise Software",
    source_name: str = "Reuters",
    url: str = "https://reuters.com/ai-enterprise-2026",
    pub_date: date | None = None,
    why_it_matters: str = "Major enterprise software vendors are integrating AI into core products.",
    impact_tags: list[str] | None = None,
    tier: str = "1b",
    cross_refs: list[str] | None = None,
    twitter_handle: str | None = None,
    tweet_url: str | None = None,
    prompt_version: str = "sha256:abc123",
) -> CuratedItem:
    return CuratedItem(
        headline=headline,
        source_name=source_name,
        url=url,
        pub_date=pub_date or date(2026, 5, 10),
        why_it_matters=why_it_matters,
        impact_tags=impact_tags if impact_tags is not None else ["business_impact"],
        tier=tier,
        cross_refs=cross_refs if cross_refs is not None else [],
        twitter_handle=twitter_handle,
        tweet_url=tweet_url,
        prompt_version=prompt_version,
    )


def _make_meta(
    agent_id: str = "test-agent",
    cadence: str = "daily",
    run_date: date | None = None,
    prompt_version: str = "sha256:abc123def456",
) -> DigestMetadata:
    return DigestMetadata(
        agent_id=agent_id,
        cadence=cadence,
        run_date=run_date or date(2026, 5, 10),
        window_start=datetime(2026, 5, 9, 0, 0, tzinfo=UTC),
        window_end=datetime(2026, 5, 9, 23, 59, tzinfo=UTC),
        prompt_version=prompt_version,
        llm_provider="openai",
        llm_model="gpt-4o",
        items_considered=20,
        items_included=5,
        items_by_tier={"1b": 3, "2": 2},
        items_by_source_class={"web": 5},
        twitter_signal_available=True,
        tweet_api_call_count=9,
        token_usage=4200,
    )


def _make_result(
    items: list[CuratedItem] | None = None,
    metadata: DigestMetadata | None = None,
    themes: list[str] | None = None,
    outlook: str = "",
    predictions: list[str] | None = None,
    twitter_degradation_note: str | None = None,
    dry_run: bool = False,
) -> CurationRunResult:
    return CurationRunResult(
        metadata=metadata or _make_meta(),
        items=items if items is not None else [_make_item()],
        themes=themes or [],
        outlook=outlook,
        predictions=predictions or [],
        twitter_degradation_note=twitter_degradation_note,
        dry_run=dry_run,
    )


# ---------------------------------------------------------------------------
# URL validation (SRC-141, SRC-049)
# ---------------------------------------------------------------------------

class TestUrlValidation:
    """
    The renderer-level URL enforcement function must accept only valid http(s)
    URLs and reject everything else: empty, None, bare strings, ftp, etc.

    Traces: SRC-049 (non-negotiable URL requirement),
            SRC-141 (enforced at renderer — second safety layer)
    """

    @pytest.mark.parametrize(("url", "expected"), [
        ("https://reuters.com/article", True),
        ("http://bloomberg.com/news", True),
        ("HTTPS://REUTERS.COM/ARTICLE", True),   # case-insensitive
        ("HTTP://example.com", True),
        ("", False),
        (None, False),
        ("ftp://example.com", False),             # wrong scheme
        ("//example.com/path", False),            # protocol-relative
        ("reuters.com/article", False),           # no scheme
        ("/relative/path", False),                # relative URL
        ("   ", False),                           # whitespace only
        ("javascript:alert(1)", False),           # injection attempt
        ("data:text/html,<h1>xss</h1>", False),  # data URI injection
    ])
    def test_md_is_valid_url(self, url: str | None, expected: bool) -> None:
        """Markdown renderer URL validator accepts only http(s) URLs (SRC-141)."""
        assert md_valid(url) == expected

    @pytest.mark.parametrize(("url", "expected"), [
        ("https://reuters.com/article", True),
        ("http://bloomberg.com/news", True),
        ("", False),
        (None, False),
        ("ftp://example.com", False),
        ("reuters.com/article", False),
    ])
    def test_html_is_valid_url(self, url: str | None, expected: bool) -> None:
        """HTML renderer URL validator accepts only http(s) URLs (SRC-141)."""
        assert html_valid(url) == expected

    def test_markdown_drops_empty_url_item(self) -> None:
        """Items with url='' are dropped from Markdown output (SRC-141)."""
        result = _make_result(items=[
            _make_item(headline="Good Article", url="https://example.com/good"),
            _make_item(headline="No URL Article", url=""),
        ])
        md = MarkdownRenderer().render(result)
        assert "Good Article" in md
        assert "No URL Article" not in md

    def test_markdown_drops_none_url_item(self) -> None:
        """Items with url=None are dropped from Markdown output (SRC-141)."""
        result = _make_result(items=[
            _make_item(headline="Good Article", url="https://example.com/good"),
            _make_item(headline="None URL Article", url=None),  # type: ignore[arg-type]
        ])
        md = MarkdownRenderer().render(result)
        assert "Good Article" in md
        assert "None URL Article" not in md

    def test_markdown_drops_schemeless_url_item(self) -> None:
        """Items with URL that has no http/https scheme are dropped (SRC-141)."""
        result = _make_result(items=[
            _make_item(headline="Good Article", url="https://example.com/good"),
            _make_item(headline="Bad Scheme", url="ftp://files.example.com"),
        ])
        md = MarkdownRenderer().render(result)
        assert "Good Article" in md
        assert "Bad Scheme" not in md

    def test_html_drops_empty_url_item(self) -> None:
        """Items with url='' are dropped from HTML output (SRC-141)."""
        result = _make_result(items=[
            _make_item(headline="Good Article", url="https://example.com/good"),
            _make_item(headline="No URL Article", url=""),
        ])
        html = HtmlRenderer().render(result)
        assert "Good Article" in html
        assert "No URL Article" not in html

    def test_json_drops_empty_url_item(self) -> None:
        """Items with url='' are dropped from JSON output (SRC-141)."""
        result = _make_result(items=[
            _make_item(headline="Good Article", url="https://example.com/good"),
            _make_item(headline="No URL Article", url=""),
        ])
        data = json.loads(JsonRenderer().render(result))
        headlines = [i["headline"] for i in data["items"]]
        assert "Good Article" in headlines
        assert "No URL Article" not in headlines

    def test_all_renderers_drop_consistently(self) -> None:
        """Same items are dropped by all three renderers (SRC-141)."""
        items = [
            _make_item(headline="Valid Item", url="https://example.com/valid"),
            _make_item(headline="Invalid Item", url=""),
        ]
        result = _make_result(items=items)

        md   = MarkdownRenderer().render(result)
        html = HtmlRenderer().render(result)
        data = json.loads(JsonRenderer().render(result))

        for output in (md, html):
            assert "Valid Item" in output
            assert "Invalid Item" not in output
        assert len(data["items"]) == 1
        assert data["items"][0]["headline"] == "Valid Item"

    def test_all_items_invalid_produces_empty_list(self) -> None:
        """All items without URLs → empty items list in all formats (SRC-141)."""
        result = _make_result(items=[
            _make_item(headline="No URL", url=""),
        ])
        data = json.loads(JsonRenderer().render(result))
        assert data["items"] == []

    def test_rendering_agent_counts_dropped_items(self, tmp_path: Path) -> None:
        """RenderingResult.items_dropped_no_url tracks dropped count (SRC-141)."""
        result = _make_result(items=[
            _make_item(headline="Good", url="https://example.com/good"),
            _make_item(headline="Bad", url=""),
        ])
        r = RenderingAgent(output_dir=tmp_path).render(result)
        assert r.items_rendered == 1
        assert r.items_dropped_no_url == 1


# ---------------------------------------------------------------------------
# Markdown renderer — all cadences (SRC-004, SRC-029–SRC-032, SRC-138)
# ---------------------------------------------------------------------------

class TestMarkdownRenderer:
    """
    Traces: SRC-004 (Markdown export), SRC-029–SRC-032 (cadence formatting),
            SRC-048 (item schema), SRC-138 (Slack/Teams paste-ready)
    """

    # ------------------------------------------------------------------
    # Core rendering
    # ------------------------------------------------------------------

    def test_renders_headline_linked_to_url(self) -> None:
        """Headline is a Markdown hyperlink to the primary source URL (SRC-049)."""
        item = _make_item(headline="AI Reshapes Enterprise", url="https://reuters.com/ai")
        md = MarkdownRenderer().render(_make_result(items=[item]))
        assert "[AI Reshapes Enterprise](https://reuters.com/ai)" in md

    def test_renders_why_it_matters(self) -> None:
        """why_it_matters text present in output (SRC-048, SRC-122)."""
        item = _make_item(why_it_matters="This is the key reason this matters today.")
        md = MarkdownRenderer().render(_make_result(items=[item]))
        assert "This is the key reason this matters today." in md

    def test_renders_source_name(self) -> None:
        """Source name is rendered (SRC-048)."""
        item = _make_item(source_name="Bloomberg")
        md = MarkdownRenderer().render(_make_result(items=[item]))
        assert "Bloomberg" in md

    def test_renders_tier(self) -> None:
        """Tier is rendered in the item card (SRC-048)."""
        item = _make_item(tier="2")
        md = MarkdownRenderer().render(_make_result(items=[item]))
        assert "Tier 2" in md

    def test_renders_publication_date(self) -> None:
        """Publication date is rendered in the item card (SRC-048)."""
        item = _make_item(pub_date=date(2026, 3, 15))
        md = MarkdownRenderer().render(_make_result(items=[item]))
        assert "2026-03-15" in md

    def test_renders_business_impact_badge(self) -> None:
        """Business impact tag rendered as badge (SRC-023, SRC-048)."""
        item = _make_item(impact_tags=["business_impact"])
        md = MarkdownRenderer().render(_make_result(items=[item]))
        assert "Business" in md

    def test_renders_workforce_impact_badge(self) -> None:
        """Workforce impact tag rendered (SRC-024, SRC-048)."""
        item = _make_item(impact_tags=["workforce_impact"])
        md = MarkdownRenderer().render(_make_result(items=[item]))
        assert "Workforce" in md

    def test_renders_policy_impact_badge(self) -> None:
        """Policy impact tag rendered (SRC-025, SRC-048)."""
        item = _make_item(impact_tags=["policy_impact"])
        md = MarkdownRenderer().render(_make_result(items=[item]))
        assert "Policy" in md

    def test_renders_multiple_impact_tags(self) -> None:
        """Multiple impact tags joined with separator."""
        item = _make_item(impact_tags=["business_impact", "policy_impact"])
        md = MarkdownRenderer().render(_make_result(items=[item]))
        assert "Business" in md
        assert "Policy" in md

    def test_renders_general_badge_when_no_tags(self) -> None:
        """No impact tags → '📌 General' fallback badge."""
        item = _make_item(impact_tags=[])
        md = MarkdownRenderer().render(_make_result(items=[item]))
        assert "General" in md

    # ------------------------------------------------------------------
    # Header (SRC-116 — concrete ISO dates)
    # ------------------------------------------------------------------

    def test_header_contains_iso_window_dates(
        self, sample_digest_metadata: DigestMetadata
    ) -> None:
        """Window dates are concrete ISO-8601 strings, not relative phrases (SRC-116)."""
        md = MarkdownRenderer().render(_make_result(metadata=sample_digest_metadata))
        assert "2026-05-09" in md
        # Relative phrases must NOT appear
        assert "last week" not in md.lower()
        assert "yesterday" not in md.lower()

    def test_header_contains_agent_id(self, sample_digest_metadata: DigestMetadata) -> None:
        """Agent ID in header (SRC-072)."""
        md = MarkdownRenderer().render(_make_result(metadata=sample_digest_metadata))
        assert "test-agent" in md

    def test_header_contains_run_date(self, sample_digest_metadata: DigestMetadata) -> None:
        """Run date in header."""
        md = MarkdownRenderer().render(_make_result(metadata=sample_digest_metadata))
        assert "2026-05-10" in md

    def test_header_contains_prompt_version(
        self, sample_digest_metadata: DigestMetadata
    ) -> None:
        """Prompt version (SHA-256) in header (SRC-129)."""
        md = MarkdownRenderer().render(_make_result(metadata=sample_digest_metadata))
        assert "sha256:abc123def456" in md

    # ------------------------------------------------------------------
    # Daily cadence (SRC-029)
    # ------------------------------------------------------------------

    def test_daily_section_heading(self) -> None:
        """Daily section shows count heading (SRC-029)."""
        result = _make_result(items=[_make_item(), _make_item(headline="Second Article")])
        md = MarkdownRenderer().render(result)
        assert "Top 2 Stories" in md

    def test_daily_empty_items_shows_message(self) -> None:
        """Empty daily digest shows informative message."""
        result = _make_result(items=[])
        md = MarkdownRenderer().render(result)
        assert "No articles met the curation threshold" in md

    # ------------------------------------------------------------------
    # Weekly cadence (SRC-030)
    # ------------------------------------------------------------------

    def test_weekly_includes_themes_section(self) -> None:
        """Weekly view includes themes section (SRC-030)."""
        meta = _make_meta(cadence="weekly")
        result = _make_result(
            metadata=meta,
            themes=["Enterprise AI", "Regulation Wave"],
        )
        md = MarkdownRenderer().render(result)
        assert "This Week's Themes" in md
        assert "Enterprise AI" in md
        assert "Regulation Wave" in md

    def test_weekly_includes_outlook_section(self) -> None:
        """Weekly view includes looking-ahead outlook (SRC-030)."""
        meta = _make_meta(cadence="weekly")
        result = _make_result(
            metadata=meta,
            outlook="Watch for EU AI Act enforcement actions next week.",
        )
        md = MarkdownRenderer().render(result)
        assert "Looking Ahead" in md
        assert "Watch for EU AI Act" in md

    def test_weekly_stories_section(self) -> None:
        """Weekly view includes stories section (SRC-030)."""
        meta = _make_meta(cadence="weekly")
        result = _make_result(metadata=meta, items=[_make_item()])
        md = MarkdownRenderer().render(result)
        assert "Top Stories This Week" in md

    def test_weekly_no_themes_no_themes_section(self) -> None:
        """Weekly with no themes omits themes section gracefully."""
        meta = _make_meta(cadence="weekly")
        result = _make_result(metadata=meta, themes=[])
        md = MarkdownRenderer().render(result)
        assert "This Week's Themes" not in md

    def test_weekly_no_outlook_no_looking_ahead(self) -> None:
        """Weekly with no outlook omits looking-ahead section."""
        meta = _make_meta(cadence="weekly")
        result = _make_result(metadata=meta, outlook="")
        md = MarkdownRenderer().render(result)
        assert "Looking Ahead" not in md

    # ------------------------------------------------------------------
    # Monthly cadence (SRC-031)
    # ------------------------------------------------------------------

    def test_monthly_includes_themes_section(self) -> None:
        """Monthly view includes themes section (SRC-031)."""
        meta = _make_meta(cadence="monthly")
        result = _make_result(
            metadata=meta,
            themes=["AI Infrastructure Build-out", "Regulatory Consolidation"],
        )
        md = MarkdownRenderer().render(result)
        assert "Monthly Themes" in md
        assert "AI Infrastructure Build-out" in md

    def test_monthly_includes_what_to_watch_section(self) -> None:
        """Monthly view includes What to Watch (anticipated news) section (SRC-031)."""
        meta = _make_meta(cadence="monthly")
        result = _make_result(
            metadata=meta,
            outlook="Anticipate major GPT-5 deployment announcements.",
        )
        md = MarkdownRenderer().render(result)
        assert "What to Watch" in md
        assert "Anticipate major GPT-5" in md

    def test_monthly_stories_section(self) -> None:
        """Monthly view includes top stories section (SRC-031)."""
        meta = _make_meta(cadence="monthly")
        result = _make_result(metadata=meta)
        md = MarkdownRenderer().render(result)
        assert "Top Stories This Month" in md

    def test_monthly_themes_before_stories(self) -> None:
        """Monthly: themes appears before stories (bigger picture first, SRC-031)."""
        meta = _make_meta(cadence="monthly")
        result = _make_result(
            metadata=meta,
            themes=["Theme A"],
            items=[_make_item(headline="Story X")],
        )
        md = MarkdownRenderer().render(result)
        themes_pos = md.find("Monthly Themes")
        stories_pos = md.find("Top Stories This Month")
        assert themes_pos < stories_pos

    def test_monthly_what_to_watch_before_stories(self) -> None:
        """Monthly: outlook appears before top stories (SRC-031)."""
        meta = _make_meta(cadence="monthly")
        result = _make_result(
            metadata=meta,
            outlook="Something to watch.",
            items=[_make_item(headline="Story X")],
        )
        md = MarkdownRenderer().render(result)
        watch_pos = md.find("What to Watch")
        stories_pos = md.find("Top Stories This Month")
        assert watch_pos < stories_pos

    # ------------------------------------------------------------------
    # Annual cadence (SRC-032, SRC-124)
    # ------------------------------------------------------------------

    def test_annual_includes_year_themes(self) -> None:
        """Annual view includes year-in-review themes section (SRC-032)."""
        meta = _make_meta(cadence="annual")
        result = _make_result(
            metadata=meta,
            themes=["AGI Discourse Intensifies", "Enterprise AI Consolidation"],
        )
        md = MarkdownRenderer().render(result)
        assert "Year in Review" in md
        assert "AGI Discourse Intensifies" in md

    def test_annual_includes_top_10_stories(self) -> None:
        """Annual view includes top 10 stories section (SRC-032)."""
        meta = _make_meta(cadence="annual")
        result = _make_result(metadata=meta)
        md = MarkdownRenderer().render(result)
        assert "Top 10 Stories of the Year" in md

    def test_annual_includes_predictions(self) -> None:
        """Annual view includes predictions section (SRC-032, SRC-124)."""
        meta = _make_meta(cadence="annual")
        result = _make_result(
            metadata=meta,
            predictions=[
                "AI agents become mainstream in enterprise by Q2.",
                "GPT-5 resets capability expectations across industries.",
            ],
        )
        md = MarkdownRenderer().render(result)
        assert "10 Predictions for the Year Ahead" in md
        assert "AI agents become mainstream" in md
        assert "GPT-5 resets capability" in md

    def test_annual_predictions_grounded_note(self) -> None:
        """Annual predictions section includes 'grounded in trends' note (SRC-124)."""
        meta = _make_meta(cadence="annual")
        result = _make_result(
            metadata=meta,
            predictions=["Some prediction."],
        )
        md = MarkdownRenderer().render(result)
        assert "grounded in observed trends" in md

    def test_annual_no_predictions_no_section(self) -> None:
        """Annual with no predictions omits predictions section."""
        meta = _make_meta(cadence="annual")
        result = _make_result(metadata=meta, predictions=[])
        md = MarkdownRenderer().render(result)
        assert "10 Predictions for the Year Ahead" not in md

    # ------------------------------------------------------------------
    # Twitter attribution (SRC-048)
    # ------------------------------------------------------------------

    def test_renders_twitter_attribution(self) -> None:
        """Items with twitter_handle+tweet_url show Twitter attribution (SRC-048)."""
        item = _make_item(
            twitter_handle="karpathy",
            tweet_url="https://twitter.com/karpathy/status/123456",
        )
        md = MarkdownRenderer().render(_make_result(items=[item]))
        assert "@karpathy" in md
        assert "https://twitter.com/karpathy/status/123456" in md
        assert "🐦" in md

    def test_no_twitter_attribution_when_not_twitter_sourced(self) -> None:
        """Web-sourced items do not show Twitter attribution."""
        item = _make_item(twitter_handle=None, tweet_url=None)
        md = MarkdownRenderer().render(_make_result(items=[item]))
        assert "🐦" not in md

    def test_twitter_attribution_only_when_both_present(self) -> None:
        """Twitter attribution requires BOTH handle AND tweet_url (SRC-048)."""
        item = _make_item(twitter_handle="karpathy", tweet_url=None)
        md = MarkdownRenderer().render(_make_result(items=[item]))
        assert "🐦" not in md

    # ------------------------------------------------------------------
    # Cross-references (SRC-048)
    # ------------------------------------------------------------------

    def test_renders_cross_refs(self) -> None:
        """Items with cross_refs show related links (SRC-048)."""
        item = _make_item(cross_refs=["https://related.example.com/article-a"])
        md = MarkdownRenderer().render(_make_result(items=[item]))
        assert "Related" in md
        assert "https://related.example.com/article-a" in md

    def test_no_cross_refs_section_when_empty(self) -> None:
        """Items without cross_refs do not show Related section."""
        item = _make_item(cross_refs=[])
        md = MarkdownRenderer().render(_make_result(items=[item]))
        assert "Related:" not in md

    # ------------------------------------------------------------------
    # Footer (SRC-129, SRC-150)
    # ------------------------------------------------------------------

    def test_footer_contains_prompt_version(
        self, sample_digest_metadata: DigestMetadata
    ) -> None:
        """Prompt version SHA-256 in footer (SRC-129)."""
        md = MarkdownRenderer().render(_make_result(metadata=sample_digest_metadata))
        assert "sha256:abc123def456" in md

    def test_footer_contains_llm_model(
        self, sample_digest_metadata: DigestMetadata
    ) -> None:
        """LLM model in footer (SRC-150)."""
        md = MarkdownRenderer().render(_make_result(metadata=sample_digest_metadata))
        assert "gpt-4o" in md

    def test_footer_contains_token_usage(
        self, sample_digest_metadata: DigestMetadata
    ) -> None:
        """Token usage in footer (SRC-150)."""
        md = MarkdownRenderer().render(_make_result(metadata=sample_digest_metadata))
        assert "4,200" in md  # formatted with thousands separator

    def test_footer_twitter_available_flag(
        self, sample_digest_metadata: DigestMetadata
    ) -> None:
        """Twitter signal availability flag in footer (SRC-148, SRC-150)."""
        md_avail = MarkdownRenderer().render(_make_result(metadata=sample_digest_metadata))
        assert "available" in md_avail

        unavail_meta = replace(sample_digest_metadata, twitter_signal_available=False)
        md_unavail = MarkdownRenderer().render(_make_result(metadata=unavail_meta))
        assert "unavailable" in md_unavail

    # ------------------------------------------------------------------
    # Filename convention (SRC-145)
    # ------------------------------------------------------------------

    def test_filename_daily(self, sample_digest_metadata: DigestMetadata) -> None:
        """Daily filename pattern {YYYY-MM-DD}-daily.md (SRC-145)."""
        assert MarkdownRenderer.filename(sample_digest_metadata) == "2026-05-10-daily.md"

    def test_filename_weekly(self) -> None:
        """Weekly filename pattern (SRC-145)."""
        meta = _make_meta(cadence="weekly")
        assert MarkdownRenderer.filename(meta) == "2026-05-10-weekly.md"

    def test_filename_monthly(self) -> None:
        """Monthly filename pattern (SRC-145)."""
        meta = _make_meta(cadence="monthly")
        assert MarkdownRenderer.filename(meta) == "2026-05-10-monthly.md"

    def test_filename_annual(self) -> None:
        """Annual filename pattern (SRC-145)."""
        meta = _make_meta(cadence="annual", run_date=date(2026, 1, 1))
        assert MarkdownRenderer.filename(meta) == "2026-01-01-annual.md"


# ---------------------------------------------------------------------------
# HTML renderer — all cadences (SRC-004, SRC-029–SRC-032, SRC-137)
# ---------------------------------------------------------------------------

class TestHtmlRenderer:
    """
    Traces: SRC-004 (HTML export), SRC-029–SRC-032 (cadence formatting),
            SRC-048 (item schema), SRC-137 (email-paste-ready)
    """

    # ------------------------------------------------------------------
    # Document structure
    # ------------------------------------------------------------------

    def test_produces_valid_html_document(self) -> None:
        """Output is a complete HTML5 document (SRC-137)."""
        html = HtmlRenderer().render(_make_result())
        assert "<!DOCTYPE html>" in html
        assert '<html lang="en">' in html
        assert "</html>" in html
        assert "<head>" in html
        assert "<body>" in html

    def test_has_inline_css_no_external_deps(self) -> None:
        """No external CSS/JS — self-contained for email clients (SRC-137)."""
        html = HtmlRenderer().render(_make_result())
        assert "<style>" in html
        # No CDN link tags
        assert "cdn." not in html.lower()
        assert '<link rel="stylesheet"' not in html

    def test_meta_charset_utf8(self) -> None:
        """HTML declares UTF-8 charset for international content."""
        html = HtmlRenderer().render(_make_result())
        assert 'charset="UTF-8"' in html

    def test_viewport_meta_present(self) -> None:
        """Viewport meta tag present for responsive rendering."""
        html = HtmlRenderer().render(_make_result())
        assert 'name="viewport"' in html

    # ------------------------------------------------------------------
    # Item rendering
    # ------------------------------------------------------------------

    def test_headline_is_linked(self) -> None:
        """Headline links to primary source URL (SRC-049)."""
        item = _make_item(
            headline="AI in Healthcare",
            url="https://reuters.com/ai-health",
        )
        html = HtmlRenderer().render(_make_result(items=[item]))
        assert 'href="https://reuters.com/ai-health"' in html
        assert "AI in Healthcare" in html

    def test_url_has_rel_noopener(self) -> None:
        """External links have rel='noopener' for security."""
        html = HtmlRenderer().render(_make_result())
        assert 'rel="noopener"' in html

    def test_why_it_matters_present(self) -> None:
        """why_it_matters text in HTML output (SRC-048, SRC-122)."""
        item = _make_item(why_it_matters="This is why it matters enormously.")
        html = HtmlRenderer().render(_make_result(items=[item]))
        assert "This is why it matters enormously." in html

    def test_source_name_rendered(self) -> None:
        """Source name rendered (SRC-048)."""
        item = _make_item(source_name="The Economist")
        html = HtmlRenderer().render(_make_result(items=[item]))
        assert "The Economist" in html

    def test_tier_rendered(self) -> None:
        """Tier rendered in item card (SRC-048)."""
        item = _make_item(tier="3")
        html = HtmlRenderer().render(_make_result(items=[item]))
        assert "Tier 3" in html

    # ------------------------------------------------------------------
    # XSS safety
    # ------------------------------------------------------------------

    def test_xss_in_headline_escaped(self) -> None:
        """HTML entities prevent script injection in headlines."""
        item = _make_item(
            headline="<script>alert('xss')</script>",
            url="https://safe.example.com",
        )
        html = HtmlRenderer().render(_make_result(items=[item]))
        assert "<script>" not in html
        assert "&lt;script&gt;" in html

    def test_xss_in_why_it_matters_escaped(self) -> None:
        """HTML entities prevent injection in why_it_matters."""
        item = _make_item(
            why_it_matters='<img src=x onerror="alert(1)">',
            url="https://safe.example.com",
        )
        html = HtmlRenderer().render(_make_result(items=[item]))
        assert '<img src=x onerror' not in html

    def test_xss_in_source_name_escaped(self) -> None:
        """HTML entities prevent injection in source_name."""
        item = _make_item(
            source_name='<b>Bold Injection</b>',
            url="https://safe.example.com",
        )
        html = HtmlRenderer().render(_make_result(items=[item]))
        assert "<b>Bold Injection</b>" not in html

    def test_xss_in_theme_escaped(self) -> None:
        """HTML entities prevent injection in theme labels."""
        meta = _make_meta(cadence="weekly")
        result = _make_result(
            metadata=meta,
            themes=["<script>alert('theme')</script>"],
        )
        html = HtmlRenderer().render(result)
        assert "<script>alert" not in html

    def test_url_with_ampersand_in_href(self) -> None:
        """Ampersands in URLs are properly escaped in href attributes."""
        item = _make_item(url="https://example.com/search?q=ai&lang=en")
        html = HtmlRenderer().render(_make_result(items=[item]))
        # URL should be in the href with & escaped (not double-escaped)
        assert "https://example.com/search?q=ai&amp;lang=en" in html
        # But the unescaped form must NOT appear in attribute context
        # (raw & in href is technically invalid HTML)
        assert 'href="https://example.com/search?q=ai&lang=en"' not in html

    # ------------------------------------------------------------------
    # Twitter attribution (SRC-048)
    # ------------------------------------------------------------------

    def test_twitter_attribution_rendered(self) -> None:
        """Twitter handle and tweet URL rendered with 🐦 icon (SRC-048)."""
        item = _make_item(
            twitter_handle="sama",
            tweet_url="https://twitter.com/sama/status/789",
        )
        html = HtmlRenderer().render(_make_result(items=[item]))
        assert "@sama" in html
        assert 'href="https://twitter.com/sama/status/789"' in html
        assert "🐦" in html

    def test_no_twitter_for_web_items(self) -> None:
        """Web-sourced items have no Twitter section in rendered item cards.
        Note: the CSS class 'twitter-signal' is always in the stylesheet, but no
        div with that class is rendered for web-sourced items."""
        item = _make_item(twitter_handle=None, tweet_url=None)
        html = HtmlRenderer().render(_make_result(items=[item]))
        # The CSS definition is always present; but no rendered <div class="twitter-signal">
        # should appear in item content.  We check that no Twitter handle appears.
        assert "@" not in html or "twitter.com" not in html
        # More precisely: no twitter-signal div in the body content
        assert '<div class="twitter-signal">' not in html

    # ------------------------------------------------------------------
    # Cross-references (SRC-048)
    # ------------------------------------------------------------------

    def test_cross_refs_rendered(self) -> None:
        """Cross-reference links rendered (SRC-048)."""
        item = _make_item(cross_refs=["https://related.example.com/ref"])
        html = HtmlRenderer().render(_make_result(items=[item]))
        assert "https://related.example.com/ref" in html
        assert "Related" in html

    def test_invalid_cross_ref_url_skipped(self) -> None:
        """Cross-refs with non-http(s) URLs are not rendered."""
        item = _make_item(cross_refs=["ftp://files.example.com"])
        html = HtmlRenderer().render(_make_result(items=[item]))
        # No href to the ftp URL
        assert 'ftp://files.example.com' not in html

    # ------------------------------------------------------------------
    # Daily cadence (SRC-029)
    # ------------------------------------------------------------------

    def test_daily_stories_heading(self) -> None:
        """Daily heading includes count (SRC-029)."""
        result = _make_result(items=[_make_item(), _make_item(headline="Item 2")])
        html = HtmlRenderer().render(result)
        assert "Top 2 Stories" in html

    def test_daily_empty_items(self) -> None:
        """Empty daily digest shows informative message."""
        result = _make_result(items=[])
        html = HtmlRenderer().render(result)
        assert "No articles met" in html

    # ------------------------------------------------------------------
    # Weekly cadence (SRC-030)
    # ------------------------------------------------------------------

    def test_weekly_themes_section(self) -> None:
        """Weekly HTML has themes section (SRC-030)."""
        meta = _make_meta(cadence="weekly")
        result = _make_result(
            metadata=meta,
            themes=["Enterprise AI", "Policy Shifts"],
        )
        html = HtmlRenderer().render(result)
        assert "This Week&#x27;s Themes" in html or "This Week's Themes" in html
        assert "Enterprise AI" in html
        assert "Policy Shifts" in html

    def test_weekly_outlook_section(self) -> None:
        """Weekly HTML has looking-ahead outlook (SRC-030)."""
        meta = _make_meta(cadence="weekly")
        result = _make_result(
            metadata=meta,
            outlook="Watch for landmark EU AI decisions.",
        )
        html = HtmlRenderer().render(result)
        assert "Looking Ahead" in html
        assert "Watch for landmark EU AI decisions." in html

    # ------------------------------------------------------------------
    # Monthly cadence (SRC-031)
    # ------------------------------------------------------------------

    def test_monthly_themes_section(self) -> None:
        """Monthly HTML has themes section (SRC-031)."""
        meta = _make_meta(cadence="monthly")
        result = _make_result(
            metadata=meta,
            themes=["AI Infrastructure Wave"],
        )
        html = HtmlRenderer().render(result)
        assert "Monthly Themes" in html
        assert "AI Infrastructure Wave" in html

    def test_monthly_what_to_watch_section(self) -> None:
        """Monthly HTML has What to Watch section (SRC-031)."""
        meta = _make_meta(cadence="monthly")
        result = _make_result(
            metadata=meta,
            outlook="Expect major cloud AI pricing announcements.",
        )
        html = HtmlRenderer().render(result)
        assert "What to Watch" in html
        assert "Expect major cloud AI pricing" in html

    def test_monthly_themes_before_stories(self) -> None:
        """Monthly: themes rendered before top stories (SRC-031 — big picture first)."""
        meta = _make_meta(cadence="monthly")
        result = _make_result(
            metadata=meta,
            themes=["Theme A"],
            items=[_make_item(headline="Story X")],
        )
        html = HtmlRenderer().render(result)
        themes_pos = html.find("Monthly Themes")
        stories_pos = html.find("Top Stories This Month")
        assert themes_pos < stories_pos

    # ------------------------------------------------------------------
    # Annual cadence (SRC-032, SRC-124)
    # ------------------------------------------------------------------

    def test_annual_year_themes_section(self) -> None:
        """Annual HTML has Year in Review themes section (SRC-032)."""
        meta = _make_meta(cadence="annual")
        result = _make_result(
            metadata=meta,
            themes=["Frontier Model Race", "Regulatory Tipping Point"],
        )
        html = HtmlRenderer().render(result)
        assert "Year in Review" in html
        assert "Frontier Model Race" in html

    def test_annual_top_10_stories_heading(self) -> None:
        """Annual HTML has Top 10 Stories heading (SRC-032)."""
        meta = _make_meta(cadence="annual")
        result = _make_result(metadata=meta)
        html = HtmlRenderer().render(result)
        assert "Top 10 Stories of the Year" in html

    def test_annual_predictions_section(self) -> None:
        """Annual HTML has predictions section (SRC-124)."""
        meta = _make_meta(cadence="annual")
        result = _make_result(
            metadata=meta,
            predictions=["AI agents dominate enterprise by EOY."],
        )
        html = HtmlRenderer().render(result)
        assert "10 Predictions for the Year Ahead" in html
        assert "AI agents dominate enterprise" in html

    def test_annual_predictions_grounded_note(self) -> None:
        """Annual predictions section notes grounding in trends (SRC-124)."""
        meta = _make_meta(cadence="annual")
        result = _make_result(metadata=meta, predictions=["Prediction A."])
        html = HtmlRenderer().render(result)
        assert "grounded in observed trends" in html

    # ------------------------------------------------------------------
    # Filename convention (SRC-145)
    # ------------------------------------------------------------------

    def test_filename_daily(self, sample_digest_metadata: DigestMetadata) -> None:
        assert HtmlRenderer.filename(sample_digest_metadata) == "2026-05-10-daily.html"

    def test_filename_annual(self) -> None:
        meta = _make_meta(cadence="annual", run_date=date(2026, 1, 1))
        assert HtmlRenderer.filename(meta) == "2026-01-01-annual.html"


# ---------------------------------------------------------------------------
# JSON renderer (SRC-004, SRC-061, SRC-129, SRC-140, SRC-150)
# ---------------------------------------------------------------------------

class TestJsonRenderer:
    """
    Traces: SRC-004 (JSON export), SRC-048 (item schema), SRC-061 (structured format),
            SRC-124 (predictions), SRC-129 (prompt_version), SRC-140 (archive-ready),
            SRC-141 (URL enforcement), SRC-150 (monitoring fields)
    """

    # ------------------------------------------------------------------
    # Document structure
    # ------------------------------------------------------------------

    def test_valid_json(self) -> None:
        """Output is valid JSON (SRC-061)."""
        json_str = JsonRenderer().render(_make_result())
        data = json.loads(json_str)  # raises on invalid
        assert isinstance(data, dict)

    def test_schema_version_present(self) -> None:
        """schema_version field present and correct."""
        data = json.loads(JsonRenderer().render(_make_result()))
        assert data["schema_version"] == SCHEMA_VERSION

    def test_top_level_keys(self) -> None:
        """All required top-level keys present."""
        data = json.loads(JsonRenderer().render(_make_result()))
        assert "schema_version" in data
        assert "metadata" in data
        assert "items" in data
        assert "themes" in data
        assert "outlook" in data
        assert "predictions" in data

    def test_indented_output(self) -> None:
        """JSON is indented (human-readable archive, SRC-140)."""
        json_str = JsonRenderer().render(_make_result())
        assert "\n" in json_str  # indented = multi-line

    # ------------------------------------------------------------------
    # Item schema (SRC-048)
    # ------------------------------------------------------------------

    def test_item_has_all_required_fields(self) -> None:
        """Each item contains all SRC-048 fields."""
        data = json.loads(JsonRenderer().render(_make_result()))
        item = data["items"][0]
        for field in ("headline", "source_name", "url", "pub_date",
                      "why_it_matters", "impact_tags", "tier",
                      "cross_refs", "twitter_handle", "tweet_url"):
            assert field in item, f"Missing field: {field}"

    def test_item_prompt_version(self) -> None:
        """Each item includes prompt_version for regression tracing (SRC-129)."""
        item_obj = _make_item(prompt_version="sha256:deadbeef")
        data = json.loads(JsonRenderer().render(_make_result(items=[item_obj])))
        assert data["items"][0]["prompt_version"] == "sha256:deadbeef"

    def test_item_pub_date_iso8601(self) -> None:
        """pub_date serialised as ISO-8601 string."""
        item_obj = _make_item(pub_date=date(2026, 3, 15))
        data = json.loads(JsonRenderer().render(_make_result(items=[item_obj])))
        assert data["items"][0]["pub_date"] == "2026-03-15"

    def test_item_twitter_fields_null_for_web(self) -> None:
        """Web-sourced items have null twitter_handle and tweet_url."""
        data = json.loads(JsonRenderer().render(_make_result()))
        assert data["items"][0]["twitter_handle"] is None
        assert data["items"][0]["tweet_url"] is None

    def test_item_twitter_fields_populated_for_twitter(self) -> None:
        """Twitter-sourced items have populated twitter_handle and tweet_url (SRC-048)."""
        item_obj = _make_item(
            twitter_handle="ylecun",
            tweet_url="https://twitter.com/ylecun/status/999",
        )
        data = json.loads(JsonRenderer().render(_make_result(items=[item_obj])))
        assert data["items"][0]["twitter_handle"] == "ylecun"
        assert data["items"][0]["tweet_url"] == "https://twitter.com/ylecun/status/999"

    def test_item_cross_refs_present(self) -> None:
        """Cross-refs serialised in each item (SRC-048)."""
        item_obj = _make_item(cross_refs=["https://related.example.com"])
        data = json.loads(JsonRenderer().render(_make_result(items=[item_obj])))
        assert data["items"][0]["cross_refs"] == ["https://related.example.com"]

    # ------------------------------------------------------------------
    # Metadata / monitoring fields (SRC-129, SRC-148, SRC-150)
    # ------------------------------------------------------------------

    def test_metadata_all_monitoring_fields(
        self, sample_digest_metadata: DigestMetadata
    ) -> None:
        """All SRC-150 quality-monitoring fields present in metadata block."""
        data = json.loads(
            JsonRenderer().render(_make_result(metadata=sample_digest_metadata))
        )
        meta = data["metadata"]
        for field in (
            "agent_id", "cadence", "run_date", "window_start", "window_end",
            "prompt_version",           # SRC-129
            "llm_provider",             # SRC-150
            "llm_model",                # SRC-150
            "items_considered",         # SRC-150
            "items_included",           # SRC-150
            "items_by_tier",            # SRC-150
            "items_by_source_class",    # SRC-150
            "twitter_signal_available", # SRC-148
            "tweet_api_call_count",     # SRC-150
            "token_usage",              # SRC-150
        ):
            assert field in meta, f"Missing monitoring field: {field}"

    def test_metadata_prompt_version(
        self, sample_digest_metadata: DigestMetadata
    ) -> None:
        """Prompt version hash in metadata (SRC-129)."""
        data = json.loads(
            JsonRenderer().render(_make_result(metadata=sample_digest_metadata))
        )
        assert data["metadata"]["prompt_version"] == "sha256:abc123def456"

    def test_metadata_window_dates_iso8601(
        self, sample_digest_metadata: DigestMetadata
    ) -> None:
        """window_start and window_end are ISO-8601 strings (SRC-116)."""
        data = json.loads(
            JsonRenderer().render(_make_result(metadata=sample_digest_metadata))
        )
        # Should parse without error as ISO-8601
        datetime.fromisoformat(data["metadata"]["window_start"])
        datetime.fromisoformat(data["metadata"]["window_end"])

    def test_metadata_items_by_tier(
        self, sample_digest_metadata: DigestMetadata
    ) -> None:
        """items_by_tier dict present in metadata (SRC-150)."""
        data = json.loads(
            JsonRenderer().render(_make_result(metadata=sample_digest_metadata))
        )
        assert isinstance(data["metadata"]["items_by_tier"], dict)

    def test_metadata_twitter_signal_available(
        self, sample_digest_metadata: DigestMetadata
    ) -> None:
        """twitter_signal_available flag in metadata (SRC-148)."""
        data = json.loads(
            JsonRenderer().render(_make_result(metadata=sample_digest_metadata))
        )
        assert data["metadata"]["twitter_signal_available"] is True

    # ------------------------------------------------------------------
    # Cadence-specific fields
    # ------------------------------------------------------------------

    def test_daily_themes_empty(self) -> None:
        """Daily JSON has empty themes list."""
        data = json.loads(JsonRenderer().render(_make_result()))
        assert data["themes"] == []

    def test_weekly_themes_present(self) -> None:
        """Weekly JSON includes themes (SRC-030)."""
        meta = _make_meta(cadence="weekly")
        result = _make_result(metadata=meta, themes=["Theme A", "Theme B"])
        data = json.loads(JsonRenderer().render(result))
        assert data["themes"] == ["Theme A", "Theme B"]

    def test_monthly_outlook_present(self) -> None:
        """Monthly JSON includes outlook (SRC-031)."""
        meta = _make_meta(cadence="monthly")
        result = _make_result(
            metadata=meta,
            outlook="Watch for the next chip generation launch.",
        )
        data = json.loads(JsonRenderer().render(result))
        assert data["outlook"] == "Watch for the next chip generation launch."

    def test_annual_predictions_present(self) -> None:
        """Annual JSON includes predictions list (SRC-032, SRC-124)."""
        meta = _make_meta(cadence="annual")
        result = _make_result(
            metadata=meta,
            predictions=["Prediction 1", "Prediction 2"],
        )
        data = json.loads(JsonRenderer().render(result))
        assert data["predictions"] == ["Prediction 1", "Prediction 2"]

    def test_non_annual_predictions_empty(self) -> None:
        """Non-annual cadences have empty predictions list."""
        data = json.loads(JsonRenderer().render(_make_result()))
        assert data["predictions"] == []

    # ------------------------------------------------------------------
    # Twitter degradation note (SRC-148)
    # ------------------------------------------------------------------

    def test_twitter_degradation_note_in_json(self) -> None:
        """twitter_degradation_note key present when degraded (SRC-148)."""
        result = _make_result(
            twitter_degradation_note="Twitter API unavailable for this run."
        )
        data = json.loads(JsonRenderer().render(result))
        assert "twitter_degradation_note" in data
        assert "Twitter API unavailable" in data["twitter_degradation_note"]

    def test_no_twitter_degradation_note_when_not_degraded(self) -> None:
        """twitter_degradation_note absent when API available (SRC-148)."""
        result = _make_result(twitter_degradation_note=None)
        data = json.loads(JsonRenderer().render(result))
        assert "twitter_degradation_note" not in data

    # ------------------------------------------------------------------
    # Filename convention (SRC-145)
    # ------------------------------------------------------------------

    def test_filename_daily(self, sample_digest_metadata: DigestMetadata) -> None:
        assert JsonRenderer.filename(sample_digest_metadata) == "2026-05-10-daily.json"

    def test_filename_annual(self) -> None:
        meta = _make_meta(cadence="annual", run_date=date(2026, 1, 1))
        assert JsonRenderer.filename(meta) == "2026-01-01-annual.json"

    def test_filename_unicode_safe(self) -> None:
        """Filename only contains ASCII date and cadence — safe for all filesystems."""
        meta = _make_meta(cadence="monthly")
        fn = JsonRenderer.filename(meta)
        fn.encode("ascii")  # raises if non-ASCII


# ---------------------------------------------------------------------------
# RenderingAgent orchestrator (SRC-004, SRC-145)
# ---------------------------------------------------------------------------

class TestRenderingAgent:
    """
    Traces: SRC-004 (three output files per run), SRC-135–SRC-141,
            SRC-145 (date-stamped, idempotent)
    """

    def test_writes_three_files(
        self,
        sample_curated_item: CuratedItem,
        sample_digest_metadata: DigestMetadata,
        tmp_path: Path,
    ) -> None:
        """RenderingAgent writes .md, .html, and .json files (SRC-004)."""
        result = _make_result(
            items=[sample_curated_item], metadata=sample_digest_metadata
        )
        rendering_result = RenderingAgent(output_dir=tmp_path).render(result)

        assert rendering_result.markdown_path.exists()
        assert rendering_result.html_path.exists()
        assert rendering_result.json_path.exists()

    def test_files_have_correct_extensions(
        self,
        sample_curated_item: CuratedItem,
        sample_digest_metadata: DigestMetadata,
        tmp_path: Path,
    ) -> None:
        """Output files have .md, .html, .json extensions (SRC-004)."""
        result = _make_result(
            items=[sample_curated_item], metadata=sample_digest_metadata
        )
        r = RenderingAgent(output_dir=tmp_path).render(result)

        assert r.markdown_path.suffix == ".md"
        assert r.html_path.suffix == ".html"
        assert r.json_path.suffix == ".json"

    def test_files_have_correct_date_stamped_names(
        self,
        sample_curated_item: CuratedItem,
        sample_digest_metadata: DigestMetadata,
        tmp_path: Path,
    ) -> None:
        """Date-stamped filenames: {YYYY-MM-DD}-{cadence}.{ext} (SRC-145)."""
        result = _make_result(
            items=[sample_curated_item], metadata=sample_digest_metadata
        )
        r = RenderingAgent(output_dir=tmp_path).render(result)

        assert r.markdown_path.name == "2026-05-10-daily.md"
        assert r.html_path.name == "2026-05-10-daily.html"
        assert r.json_path.name == "2026-05-10-daily.json"

    def test_creates_output_directory(
        self, sample_curated_item: CuratedItem, sample_digest_metadata: DigestMetadata
    ) -> None:
        """Output directory is created if it does not exist (SRC-145)."""
        with tempfile.TemporaryDirectory() as base:
            nested = Path(base) / "nested" / "subdir"
            assert not nested.exists()
            result = _make_result(
                items=[sample_curated_item], metadata=sample_digest_metadata
            )
            RenderingAgent(output_dir=nested).render(result)
            assert nested.exists()

    def test_rerun_overwrites_cleanly(
        self,
        sample_curated_item: CuratedItem,
        sample_digest_metadata: DigestMetadata,
        tmp_path: Path,
    ) -> None:
        """Second render for same date/cadence overwrites first — idempotent (SRC-145)."""
        result = _make_result(
            items=[sample_curated_item], metadata=sample_digest_metadata
        )
        agent = RenderingAgent(output_dir=tmp_path)
        r1 = agent.render(result)
        r2 = agent.render(result)  # second run — should NOT error

        assert r1.markdown_path == r2.markdown_path
        assert r1.html_path == r2.html_path
        assert r1.json_path == r2.json_path

    def test_url_drop_count_in_result(
        self,
        sample_curated_item: CuratedItem,
        sample_digest_metadata: DigestMetadata,
        tmp_path: Path,
    ) -> None:
        """RenderingResult reports items_dropped_no_url for audit (SRC-141)."""
        no_url_item = _make_item(headline="No URL", url="")
        result = _make_result(
            items=[sample_curated_item, no_url_item],
            metadata=sample_digest_metadata,
        )
        r = RenderingAgent(output_dir=tmp_path).render(result)
        assert r.items_rendered == 1
        assert r.items_dropped_no_url == 1

    def test_all_valid_url_items_reported(
        self,
        sample_curated_item: CuratedItem,
        sample_digest_metadata: DigestMetadata,
        tmp_path: Path,
    ) -> None:
        """items_rendered matches count of items with valid URLs."""
        result = _make_result(
            items=[sample_curated_item, sample_curated_item],
            metadata=sample_digest_metadata,
        )
        r = RenderingAgent(output_dir=tmp_path).render(result)
        assert r.items_rendered == 2
        assert r.items_dropped_no_url == 0

    def test_md_file_is_utf8(
        self,
        sample_curated_item: CuratedItem,
        sample_digest_metadata: DigestMetadata,
        tmp_path: Path,
    ) -> None:
        """Markdown file written as UTF-8 (SRC-138)."""
        result = _make_result(
            items=[sample_curated_item], metadata=sample_digest_metadata
        )
        r = RenderingAgent(output_dir=tmp_path).render(result)
        content = r.markdown_path.read_text(encoding="utf-8")
        assert len(content) > 0

    def test_json_file_is_valid(
        self,
        sample_curated_item: CuratedItem,
        sample_digest_metadata: DigestMetadata,
        tmp_path: Path,
    ) -> None:
        """Written JSON file is valid JSON."""
        result = _make_result(
            items=[sample_curated_item], metadata=sample_digest_metadata
        )
        r = RenderingAgent(output_dir=tmp_path).render(result)
        data = json.loads(r.json_path.read_text(encoding="utf-8"))
        assert "schema_version" in data

    def test_returns_rendering_result_dataclass(
        self,
        sample_curated_item: CuratedItem,
        sample_digest_metadata: DigestMetadata,
        tmp_path: Path,
    ) -> None:
        """render() returns a RenderingResult dataclass."""
        result = _make_result(
            items=[sample_curated_item], metadata=sample_digest_metadata
        )
        r = RenderingAgent(output_dir=tmp_path).render(result)
        assert isinstance(r, RenderingResult)


# ---------------------------------------------------------------------------
# DigestRecord path update (SRC-145 — portal download links)
# ---------------------------------------------------------------------------

class TestRenderingAgentStore:
    """
    render_and_update_store() must populate DigestRecord.md_path / html_path /
    json_path so the portal can serve download links.

    Traces: SRC-145 (DigestRecord paths → portal download links)
    """

    def _make_mock_store(
        self, existing_record: Any = None
    ) -> MagicMock:
        store = MagicMock()
        store.get_digest.return_value = existing_record
        return store

    def test_updates_digest_paths_when_record_exists(
        self,
        sample_curated_item: CuratedItem,
        sample_digest_metadata: DigestMetadata,
        tmp_path: Path,
    ) -> None:
        """render_and_update_store() calls upsert_digest with populated paths."""
        from ai_news_agent.storage.models import DigestRecord

        existing = DigestRecord(
            agent_id="test-agent",
            cadence="daily",
            run_date=date(2026, 5, 10),
            window_start=datetime(2026, 5, 9, 0, 0, tzinfo=UTC),
            window_end=datetime(2026, 5, 9, 23, 59, tzinfo=UTC),
            prompt_version="sha256:abc123def456",
            llm_provider="openai",
            llm_model="gpt-4o",
            items_considered=20,
            items_included=5,
            items_by_tier={"1b": 3},
            items_by_source_class={"web": 5},
            twitter_signal_available=True,
            tweet_api_call_count=9,
            token_usage=4200,
        )
        store = self._make_mock_store(existing_record=existing)

        result = _make_result(
            items=[sample_curated_item], metadata=sample_digest_metadata
        )
        agent = RenderingAgent(output_dir=tmp_path)
        agent.render_and_update_store(result, store)

        store.upsert_digest.assert_called_once()
        updated = store.upsert_digest.call_args[0][0]
        assert updated.md_path is not None
        assert updated.html_path is not None
        assert updated.json_path is not None
        assert updated.md_path.endswith(".md")
        assert updated.html_path.endswith(".html")
        assert updated.json_path.endswith(".json")

    def test_noop_when_no_record_exists(
        self,
        sample_curated_item: CuratedItem,
        sample_digest_metadata: DigestMetadata,
        tmp_path: Path,
    ) -> None:
        """No upsert called when DigestRecord doesn't exist (e.g. dry-run curation)."""
        store = self._make_mock_store(existing_record=None)
        result = _make_result(
            items=[sample_curated_item], metadata=sample_digest_metadata
        )
        agent = RenderingAgent(output_dir=tmp_path)
        agent.render_and_update_store(result, store)

        store.upsert_digest.assert_not_called()

    def test_render_still_writes_files_when_no_record(
        self,
        sample_curated_item: CuratedItem,
        sample_digest_metadata: DigestMetadata,
        tmp_path: Path,
    ) -> None:
        """Files are written even when DigestRecord doesn't exist."""
        store = self._make_mock_store(existing_record=None)
        result = _make_result(
            items=[sample_curated_item], metadata=sample_digest_metadata
        )
        agent = RenderingAgent(output_dir=tmp_path)
        r = agent.render_and_update_store(result, store)

        assert r.markdown_path.exists()
        assert r.html_path.exists()
        assert r.json_path.exists()


# ---------------------------------------------------------------------------
# Dry-run mode (SRC-102)
# ---------------------------------------------------------------------------

class TestRenderingAgentDryRun:
    """
    render_dry_run() renders all formats but does not write to the configured
    output directory — used for CI smoke tests.

    Traces: SRC-102 (smoke test dry-run mode)
    """

    def test_dry_run_does_not_write_to_output_dir(
        self,
        sample_curated_item: CuratedItem,
        sample_digest_metadata: DigestMetadata,
        tmp_path: Path,
    ) -> None:
        """No files written to output_dir in dry-run mode (SRC-102)."""
        result = _make_result(
            items=[sample_curated_item], metadata=sample_digest_metadata
        )
        agent = RenderingAgent(output_dir=tmp_path)
        agent.render_dry_run(result)

        # Nothing written to the configured output dir
        assert not list(tmp_path.glob("*.md"))
        assert not list(tmp_path.glob("*.html"))
        assert not list(tmp_path.glob("*.json"))

    def test_dry_run_returns_rendering_result(
        self,
        sample_curated_item: CuratedItem,
        sample_digest_metadata: DigestMetadata,
        tmp_path: Path,
    ) -> None:
        """render_dry_run returns a RenderingResult with rendered counts."""
        result = _make_result(
            items=[sample_curated_item], metadata=sample_digest_metadata
        )
        r = RenderingAgent(output_dir=tmp_path).render_dry_run(result)
        assert isinstance(r, RenderingResult)
        assert r.items_rendered >= 0

    def test_dry_run_renders_all_three_formats(
        self,
        sample_curated_item: CuratedItem,
        sample_digest_metadata: DigestMetadata,
        tmp_path: Path,
    ) -> None:
        """Dry-run mode exercises all three renderers without error (SRC-102)."""
        result = _make_result(
            items=[sample_curated_item], metadata=sample_digest_metadata
        )
        # Should complete without raising
        r = RenderingAgent(output_dir=tmp_path).render_dry_run(result)
        assert r.items_rendered == 1


# ---------------------------------------------------------------------------
# Filename convention — thin distribution layer support (SRC-145, SRC-140)
# ---------------------------------------------------------------------------

class TestFilenameConvention:
    """
    The filename pattern ``{YYYY-MM-DD}-{cadence}.{ext}`` is designed so that
    a future thin distribution layer can ingest the output directory without
    parsing filenames for agent ID (that's encoded in the directory path).

    Traces: SRC-140 (naming convention supports future distribution layer),
            SRC-145 (idempotent date-stamped filenames)
    """

    @pytest.mark.parametrize(("cadence", "run_date", "expected"), [
        ("daily",   date(2026, 5, 10),  "2026-05-10-daily"),
        ("weekly",  date(2026, 5, 10),  "2026-05-10-weekly"),
        ("monthly", date(2026, 5,  1),  "2026-05-01-monthly"),
        ("annual",  date(2026, 1,  1),  "2026-01-01-annual"),
    ])
    def test_filename_stem_all_cadences(
        self, cadence: str, run_date: date, expected: str
    ) -> None:
        """Filename stem is {YYYY-MM-DD}-{cadence} for all cadences."""
        meta = _make_meta(cadence=cadence, run_date=run_date)
        for renderer_cls, ext in (
            (MarkdownRenderer, ".md"),
            (HtmlRenderer, ".html"),
            (JsonRenderer, ".json"),
        ):
            fn = renderer_cls.filename(meta)
            assert fn == f"{expected}{ext}", f"Unexpected filename from {renderer_cls.__name__}: {fn}"

    def test_all_three_renderers_consistent_basename(self) -> None:
        """All three renderers produce the same basename stem — just differ in extension."""
        meta = _make_meta(cadence="daily", run_date=date(2026, 5, 10))
        md_fn   = MarkdownRenderer.filename(meta)
        html_fn = HtmlRenderer.filename(meta)
        json_fn = JsonRenderer.filename(meta)

        assert md_fn.rsplit(".", 1)[0] == html_fn.rsplit(".", 1)[0] == json_fn.rsplit(".", 1)[0]

    def test_filename_only_contains_ascii(self) -> None:
        """Filenames are ASCII-only — safe on all filesystems."""
        for cadence in ("daily", "weekly", "monthly", "annual"):
            meta = _make_meta(cadence=cadence)
            for fn in (
                MarkdownRenderer.filename(meta),
                HtmlRenderer.filename(meta),
                JsonRenderer.filename(meta),
            ):
                fn.encode("ascii")  # raises UnicodeEncodeError if non-ASCII


# ---------------------------------------------------------------------------
# Twitter degradation note (SRC-148)
# ---------------------------------------------------------------------------

class TestTwitterDegradation:
    """
    When Twitter API is unavailable, all three output formats must include a
    clear degradation note informing readers that influencer signal was absent.

    Traces: SRC-148 (Twitter as signal, not hard dependency; digest notes degradation)
    """

    NOTE = "⚠️ Twitter/X influencer signal was unavailable for this run."

    def test_markdown_includes_degradation_note(self) -> None:
        """Markdown includes degradation banner when Twitter unavailable (SRC-148)."""
        result = _make_result(twitter_degradation_note=self.NOTE)
        md = MarkdownRenderer().render(result)
        assert "influencer signal was unavailable" in md

    def test_html_includes_degradation_note(self) -> None:
        """HTML includes degradation banner when Twitter unavailable (SRC-148)."""
        result = _make_result(twitter_degradation_note=self.NOTE)
        html = HtmlRenderer().render(result)
        assert "influencer signal was unavailable" in html

    def test_json_includes_degradation_note(self) -> None:
        """JSON includes twitter_degradation_note key when unavailable (SRC-148)."""
        result = _make_result(twitter_degradation_note=self.NOTE)
        data = json.loads(JsonRenderer().render(result))
        assert "twitter_degradation_note" in data

    def test_no_degradation_note_when_available(self) -> None:
        """No degradation banner when Twitter signal is available (SRC-148)."""
        result = _make_result(twitter_degradation_note=None)
        md = MarkdownRenderer().render(result)
        html = HtmlRenderer().render(result)
        data = json.loads(JsonRenderer().render(result))

        assert "influencer signal was unavailable" not in md
        assert "influencer signal was unavailable" not in html
        assert "twitter_degradation_note" not in data

    def test_degradation_note_escaped_in_html(self) -> None:
        """Degradation note is HTML-escaped to prevent injection."""
        malicious_note = '<script>alert("xss")</script>'
        result = _make_result(twitter_degradation_note=malicious_note)
        html = HtmlRenderer().render(result)
        assert "<script>alert" not in html


# ---------------------------------------------------------------------------
# Cross-references (SRC-048)
# ---------------------------------------------------------------------------

class TestCrossRefs:
    """
    Each curated item may include optional cross_refs list with related URLs.
    These are rendered in all three formats.

    Traces: SRC-048 (optional cross-references to related items)
    """

    def test_markdown_renders_multiple_cross_refs(self) -> None:
        """Multiple cross-refs all rendered (SRC-048)."""
        item = _make_item(
            cross_refs=[
                "https://related.example.com/article-a",
                "https://related.example.com/article-b",
            ]
        )
        md = MarkdownRenderer().render(_make_result(items=[item]))
        assert "https://related.example.com/article-a" in md
        assert "https://related.example.com/article-b" in md

    def test_html_renders_multiple_cross_refs(self) -> None:
        """Multiple cross-refs rendered in HTML (SRC-048)."""
        item = _make_item(
            cross_refs=[
                "https://related.example.com/a",
                "https://related.example.com/b",
            ]
        )
        html = HtmlRenderer().render(_make_result(items=[item]))
        assert "https://related.example.com/a" in html
        assert "https://related.example.com/b" in html

    def test_json_preserves_all_cross_refs(self) -> None:
        """Cross-refs list preserved exactly in JSON output (SRC-048)."""
        refs = [
            "https://related.example.com/a",
            "https://related.example.com/b",
        ]
        item = _make_item(cross_refs=refs)
        data = json.loads(JsonRenderer().render(_make_result(items=[item])))
        assert data["items"][0]["cross_refs"] == refs


# ---------------------------------------------------------------------------
# Cadence edge cases
# ---------------------------------------------------------------------------

class TestCadenceEdgeCases:
    """Edge cases for empty states and missing optional sections."""

    def test_daily_with_no_items(self) -> None:
        """Daily with no items renders informative empty-state (all formats)."""
        result = _make_result(items=[], metadata=_make_meta(cadence="daily"))
        md   = MarkdownRenderer().render(result)
        html = HtmlRenderer().render(result)
        data = json.loads(JsonRenderer().render(result))

        assert "No articles" in md
        assert "No articles" in html
        assert data["items"] == []

    def test_weekly_with_no_themes_or_outlook(self) -> None:
        """Weekly with empty themes and outlook still renders stories correctly."""
        meta = _make_meta(cadence="weekly")
        result = _make_result(metadata=meta, themes=[], outlook="")
        md = MarkdownRenderer().render(result)
        assert "Top Stories This Week" in md
        assert "This Week's Themes" not in md
        assert "Looking Ahead" not in md

    def test_monthly_with_no_themes_or_outlook(self) -> None:
        """Monthly with empty themes and outlook still renders stories."""
        meta = _make_meta(cadence="monthly")
        result = _make_result(metadata=meta, themes=[], outlook="")
        md = MarkdownRenderer().render(result)
        assert "Top Stories This Month" in md
        assert "Monthly Themes" not in md
        assert "What to Watch" not in md

    def test_annual_with_no_themes_predictions_or_items(self) -> None:
        """Annual with all empty fields renders gracefully."""
        meta = _make_meta(cadence="annual")
        result = _make_result(
            metadata=meta, items=[], themes=[], predictions=[]
        )
        md   = MarkdownRenderer().render(result)
        html = HtmlRenderer().render(result)
        data = json.loads(JsonRenderer().render(result))

        assert "Top 10 Stories of the Year" in md
        assert "Top 10 Stories of the Year" in html
        assert data["items"] == []
        assert data["predictions"] == []

    def test_unknown_cadence_renders_without_error(self) -> None:
        """Unknown cadence value does not crash renderers."""
        # Replace cadence with unknown value directly on metadata
        meta = replace(_make_meta(), cadence="quarterly")
        result = _make_result(metadata=meta)
        # Should not raise
        MarkdownRenderer().render(result)
        HtmlRenderer().render(result)
        JsonRenderer().render(result)


# ---------------------------------------------------------------------------
# Monitoring fields (SRC-150)
# ---------------------------------------------------------------------------

class TestMonitoringFields:
    """
    All three renderers must include quality-monitoring fields from SRC-150
    (items_considered, items_included, llm_model, prompt_version, etc.).

    Traces: SRC-150 (quality monitoring), SRC-129 (prompt_version)
    """

    def test_markdown_footer_has_all_monitoring_fields(
        self, sample_digest_metadata: DigestMetadata
    ) -> None:
        """Markdown footer contains all SRC-150 fields."""
        md = MarkdownRenderer().render(_make_result(metadata=sample_digest_metadata))
        assert "Digest Metadata" in md
        assert "Agent ID" in md
        assert "LLM Model" in md
        assert "Prompt Version" in md
        assert "Items Considered" in md
        assert "Items Included" in md
        assert "Token Usage" in md

    def test_html_footer_has_monitoring_fields(
        self, sample_digest_metadata: DigestMetadata
    ) -> None:
        """HTML footer contains monitoring fields."""
        html = HtmlRenderer().render(_make_result(metadata=sample_digest_metadata))
        assert "footer-meta" in html
        assert "gpt-4o" in html
        assert "sha256:abc123def456" in html

    def test_json_metadata_token_usage_is_int(
        self, sample_digest_metadata: DigestMetadata
    ) -> None:
        """token_usage in JSON metadata is an integer."""
        data = json.loads(
            JsonRenderer().render(_make_result(metadata=sample_digest_metadata))
        )
        assert isinstance(data["metadata"]["token_usage"], int)

    def test_json_metadata_items_counts_match(
        self, sample_digest_metadata: DigestMetadata
    ) -> None:
        """items_considered ≥ items_included (sanity check)."""
        data = json.loads(
            JsonRenderer().render(_make_result(metadata=sample_digest_metadata))
        )
        meta = data["metadata"]
        assert meta["items_considered"] >= meta["items_included"]

    def test_markdown_formats_token_usage_with_commas(self) -> None:
        """Large token counts formatted with thousands separator for readability."""
        meta = replace(
            _make_meta(),
            token_usage=1_234_567,
        )
        md = MarkdownRenderer().render(_make_result(metadata=meta))
        assert "1,234,567" in md
