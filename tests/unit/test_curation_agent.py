"""
tests/unit/test_curation_agent.py — Comprehensive Curation Agent test suite.

Covers all requirements from §2.2 and §6 of docs/requirements/requirements.md:
- SRC-014–SRC-032  curation agent behaviour, all four cadences
- SRC-027          LLM-powered prioritisation
- SRC-028          on-demand re-run with explicit window override
- SRC-029          daily: headline + source + URL + why_it_matters
- SRC-030          weekly: themes + look-ahead outlook
- SRC-031          monthly: bigger-picture themes + anticipated news
- SRC-032          annual: top-10 + 10 predictions + inflection points
- SRC-047–SRC-049  Twitter signal role, URL enforcement
- SRC-054          research LLM model selection for monthly/annual
- SRC-061          structured output parsing from JSON block
- SRC-102          dry-run mode for CI smoke tests
- SRC-113          prompts directory
- SRC-115–SRC-124  prompt structure: ISO dates, disqualifiers, inclusion criteria,
                   Twitter section, output format, search budget,
                   why-it-matters, URL requirement, annual predictions
- SRC-124          annual predictions grounded in observed trends
- SRC-129          prompt_version SHA-256 in every output
- SRC-141          URL enforcement — items lacking URLs dropped
- SRC-145          idempotent DigestRecord upsert (re-runs overwrite cleanly)
- SRC-148          Twitter degradation note and API-down vs quiet-window distinction
- SRC-150          quality monitoring: token_usage, items_by_tier,
                   items_by_source_class, tweet_api_call_count
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from typing import TYPE_CHECKING
from unittest.mock import MagicMock, patch

import pytest
from tests.conftest import DummyLLMClient

from ai_news_agent.config.models import (
    AgentConfig,
    LimitsConfig,
    LLMCadenceOverride,
    LLMConfig,
)
from ai_news_agent.curation.agent import (
    CurationAgent,
    CurationRunResult,
    _annual_window,
    _daily_window,
    _monthly_window,
    _weekly_window,
)
from ai_news_agent.curation.prompt_builder import PromptBuilder
from ai_news_agent.curation.scorer import Scorer, ScorerResult
from ai_news_agent.storage.models import (
    ArticleRecord,
    TweetSignal,
    normalize_url,
    url_hash,
)

if TYPE_CHECKING:
    from pathlib import Path


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_article(
    headline: str = "Default Headline",
    source_name: str = "Reuters",
    url: str = "https://reuters.com/default",
    tier: str = "1b",
    source_class: str = "web",
    agent_id: str = "test-agent",
    pub_date: datetime | None = None,
    twitter_handle: str | None = None,
    tweet_url: str | None = None,
) -> ArticleRecord:
    """Create a test ArticleRecord with defaults."""
    canonical = normalize_url(url)
    ts = pub_date or datetime(2026, 5, 9, 10, 0, tzinfo=UTC)
    return ArticleRecord(
        url_hash=url_hash(canonical),
        url=canonical,
        headline=headline,
        abstract=f"Abstract for {headline}.",
        source_name=source_name,
        pub_date=ts,
        fetched_at=datetime(2026, 5, 10, 0, 0, tzinfo=UTC),
        tier=tier,
        source_class=source_class,
        agent_id=agent_id,
        twitter_handle=twitter_handle,
        tweet_url=tweet_url,
    )


def _make_tweet(handle: str = "karpathy", agent_id: str = "test-agent") -> TweetSignal:
    """Create a test TweetSignal."""
    return TweetSignal(
        tweet_id=f"tweet-{handle}-001",
        handle=handle,
        text=f"Interesting AI development from @{handle}",
        created_at=datetime(2026, 5, 9, 14, 0, tzinfo=UTC),
        linked_urls=[f"https://example.com/{handle}-article"],
        agent_id=agent_id,
        fetched_at=datetime(2026, 5, 9, 14, 5, tzinfo=UTC),
        weight=1.0,
    )


def _build_llm_payload(
    items: list[dict] | None = None,
    themes: list[str] | None = None,
    outlook: str = "",
    predictions: list[str] | None = None,
) -> str:
    """Build a valid LLM JSON response string."""
    payload = {
        "items": items
        or [
            {
                "headline": "AI Reshapes Enterprise Software",
                "source_name": "Reuters",
                "url": "https://reuters.com/ai-enterprise-2026",
                "pub_date": "2026-05-09",
                "why_it_matters": (
                    "Major vendors integrating AI into core products. "
                    "Structural shift in business software. Legacy vendors at risk."
                ),
                "impact_tags": ["business_impact"],
                "tier": "1b",
                "cross_refs": [],
                "twitter_handle": None,
                "tweet_url": None,
            }
        ],
        "themes": themes or [],
        "outlook": outlook,
        "predictions": predictions or [],
    }
    return f"```json\n{json.dumps(payload)}\n```"


# ---------------------------------------------------------------------------
# Part 1: Window computation helpers (SRC-009, SRC-028–SRC-032)
# ---------------------------------------------------------------------------


class TestWindowHelpers:
    """
    Verify that each window helper computes the correct UTC date range.
    Traces: SRC-009, SRC-028–SRC-032
    """

    # SRC-029: rolling 24–48h window — yesterday 00:00 UTC through reference (now).
    # Wider than a strict prior calendar day so a fresh same-day pipeline run can
    # still curate articles whose pub_date fell back to fetched_at (SRC-011).
    def test_daily_window_starts_yesterday_00_00(self) -> None:
        ref = datetime(2026, 5, 10, 6, 0, tzinfo=UTC)
        start, _ = _daily_window(ref)
        assert start.date() == date(2026, 5, 9)
        assert start.hour == 0
        assert start.minute == 0
        assert start.second == 0

    def test_daily_window_ends_at_reference(self) -> None:
        ref = datetime(2026, 5, 10, 6, 30, 15, tzinfo=UTC)
        _, end = _daily_window(ref)
        assert end == ref

    def test_daily_window_utc_aware(self) -> None:
        ref = datetime(2026, 5, 10, tzinfo=UTC)
        start, end = _daily_window(ref)
        assert start.tzinfo is not None
        assert end.tzinfo is not None

    # SRC-030: previous Sunday–Saturday
    def test_weekly_window_spans_7_days(self) -> None:
        ref = datetime(2026, 5, 11, tzinfo=UTC)  # Monday
        start, end = _weekly_window(ref)
        delta_days = (end.date() - start.date()).days
        assert delta_days == 6, f"Expected 6-day span, got {delta_days}"

    def test_weekly_window_utc_aware(self) -> None:
        ref = datetime(2026, 5, 10, tzinfo=UTC)
        start, end = _weekly_window(ref)
        assert start.tzinfo is not None
        assert end.tzinfo is not None

    def test_weekly_window_end_is_saturday(self) -> None:
        """The end of the weekly window is always a Saturday."""
        ref = datetime(2026, 5, 11, tzinfo=UTC)  # Monday
        _, end = _weekly_window(ref)
        # isoweekday(): 6 = Saturday
        assert end.isoweekday() == 6, (
            f"Expected Saturday (6), got {end.isoweekday()} ({end.date()})"
        )

    # SRC-031: previous calendar month
    def test_monthly_window_is_previous_month(self) -> None:
        ref = datetime(2026, 5, 1, tzinfo=UTC)  # May 1 → April window
        start, end = _monthly_window(ref)
        assert start.month == 4
        assert end.month == 4

    def test_monthly_window_starts_on_first(self) -> None:
        ref = datetime(2026, 5, 1, tzinfo=UTC)
        start, _ = _monthly_window(ref)
        assert start.day == 1

    def test_monthly_window_ends_on_last_day(self) -> None:
        ref = datetime(2026, 5, 1, tzinfo=UTC)
        _, end = _monthly_window(ref)
        assert end.day == 30  # April has 30 days

    def test_monthly_window_utc_aware(self) -> None:
        ref = datetime(2026, 5, 1, tzinfo=UTC)
        start, end = _monthly_window(ref)
        assert start.tzinfo is not None
        assert end.tzinfo is not None

    # SRC-032: previous calendar year
    def test_annual_window_is_previous_year(self) -> None:
        ref = datetime(2026, 1, 1, tzinfo=UTC)
        start, end = _annual_window(ref)
        assert start.year == 2025
        assert end.year == 2025

    def test_annual_window_spans_full_year(self) -> None:
        ref = datetime(2026, 1, 1, tzinfo=UTC)
        start, end = _annual_window(ref)
        assert start.month == 1
        assert start.day == 1
        assert end.month == 12
        assert end.day == 31

    def test_annual_window_utc_aware(self) -> None:
        ref = datetime(2026, 1, 1, tzinfo=UTC)
        start, end = _annual_window(ref)
        assert start.tzinfo is not None
        assert end.tzinfo is not None


# ---------------------------------------------------------------------------
# Part 2: ScorerResult — new return type
# ---------------------------------------------------------------------------


class TestScorerResult:
    """
    Verify that score_and_rank returns a ScorerResult with all cadence-specific fields.
    Traces: SRC-029–SRC-032, SRC-048, SRC-124, SRC-150
    """

    def test_scorer_result_has_items(self, sample_article: ArticleRecord) -> None:
        """ScorerResult.items contains ranked CuratedItem objects (SRC-027)."""
        payload = _build_llm_payload()
        llm = DummyLLMClient(complete_response=payload)
        scorer = Scorer(llm_client=llm)
        result = scorer.score_and_rank(
            prompt="Test prompt",
            model="gpt-4o",
            candidates=[sample_article],
            tweet_signals=[],
            top_n=5,
            prompt_version="sha256:test",
            cadence="daily",
        )
        assert isinstance(result, ScorerResult)
        assert isinstance(result.items, list)

    def test_scorer_result_has_themes_weekly(self, sample_article: ArticleRecord) -> None:
        """ScorerResult.themes populated for weekly cadence (SRC-030)."""
        payload = _build_llm_payload(
            themes=["Enterprise AI Adoption", "Regulatory Shifts"],
            outlook="Expect continued M&A activity next week.",
        )
        llm = DummyLLMClient(complete_response=payload)
        scorer = Scorer(llm_client=llm)
        result = scorer.score_and_rank(
            prompt="Weekly prompt",
            model="gpt-4o",
            candidates=[sample_article],
            tweet_signals=[],
            top_n=7,
            prompt_version="sha256:weekly",
            cadence="weekly",
        )
        assert len(result.themes) == 2
        assert result.themes[0] == "Enterprise AI Adoption"
        assert result.outlook == "Expect continued M&A activity next week."

    def test_scorer_result_has_themes_monthly(self, sample_article: ArticleRecord) -> None:
        """ScorerResult.themes populated for monthly cadence (SRC-031)."""
        payload = _build_llm_payload(
            themes=["LLM Commoditisation", "Regulation Acceleration"],
            outlook="May will likely bring new EU enforcement actions.",
        )
        llm = DummyLLMClient(complete_response=payload)
        scorer = Scorer(llm_client=llm)
        result = scorer.score_and_rank(
            prompt="Monthly prompt",
            model="gpt-4o",
            candidates=[sample_article],
            tweet_signals=[],
            top_n=10,
            prompt_version="sha256:monthly",
            cadence="monthly",
        )
        assert len(result.themes) == 2
        assert len(result.outlook) > 0  # outlook populated from LLM (SRC-031)

    def test_scorer_result_has_predictions_annual(self, sample_article: ArticleRecord) -> None:
        """ScorerResult.predictions populated for annual cadence (SRC-032, SRC-124)."""
        preds = [
            "AI agents will handle >20% of customer service calls by year-end.",
            "At least one G7 nation will pass binding AI liability legislation.",
            "Open-weight models will close the gap with frontier within 6 months.",
        ]
        payload = _build_llm_payload(
            themes=["Agentic AI", "Regulatory Consolidation"],
            predictions=preds,
        )
        llm = DummyLLMClient(complete_response=payload)
        scorer = Scorer(llm_client=llm)
        result = scorer.score_and_rank(
            prompt="Annual prompt",
            model="o3",
            candidates=[sample_article],
            tweet_signals=[],
            top_n=10,
            prompt_version="sha256:annual",
            cadence="annual",
        )
        assert len(result.predictions) == 3
        assert "AI agents" in result.predictions[0]

    def test_scorer_result_token_usage_populated(self, sample_article: ArticleRecord) -> None:
        """ScorerResult.token_usage > 0 after a real LLM call (SRC-150)."""
        payload = _build_llm_payload()
        llm = DummyLLMClient(complete_response=payload)
        scorer = Scorer(llm_client=llm)
        result = scorer.score_and_rank(
            prompt="A fairly long system prompt to ensure token estimate is non-trivial.",
            model="gpt-4o",
            candidates=[sample_article],
            tweet_signals=[],
            top_n=5,
            prompt_version="sha256:test",
            cadence="daily",
        )
        assert result.token_usage > 0, "token_usage must be positive (SRC-150)"

    def test_scorer_result_raw_response_captured(self, sample_article: ArticleRecord) -> None:
        """ScorerResult.raw_response contains the LLM's raw output."""
        payload = _build_llm_payload()
        llm = DummyLLMClient(complete_response=payload)
        scorer = Scorer(llm_client=llm)
        result = scorer.score_and_rank(
            prompt="prompt",
            model="gpt-4o",
            candidates=[sample_article],
            tweet_signals=[],
            top_n=5,
            prompt_version="sha256:test",
            cadence="daily",
        )
        assert len(result.raw_response) > 0

    def test_scorer_result_empty_on_no_candidates(self) -> None:
        """Empty candidate list returns empty ScorerResult without error (SRC-015)."""
        llm = DummyLLMClient()
        scorer = Scorer(llm_client=llm)
        result = scorer.score_and_rank(
            prompt="prompt",
            model="gpt-4o",
            candidates=[],
            tweet_signals=[],
            top_n=5,
            prompt_version="sha256:test",
            cadence="daily",
        )
        assert isinstance(result, ScorerResult)
        assert result.items == []
        assert result.themes == []
        assert result.predictions == []
        assert result.token_usage == 0

    def test_scorer_result_parse_error_returns_empty_items(self) -> None:
        """Malformed LLM output returns ScorerResult with empty items (SRC-061)."""
        llm = DummyLLMClient(complete_response="this is not valid json at all %%%")
        scorer = Scorer(llm_client=llm)
        article = _make_article()
        result = scorer.score_and_rank(
            prompt="prompt",
            model="gpt-4o",
            candidates=[article],
            tweet_signals=[],
            top_n=5,
            prompt_version="sha256:test",
            cadence="daily",
        )
        assert result.items == []


# ---------------------------------------------------------------------------
# Part 3: URL enforcement at Scorer layer (SRC-049, SRC-141)
# ---------------------------------------------------------------------------


class TestURLEnforcement:
    """
    Verify that items lacking a URL are unconditionally dropped.
    Traces: SRC-049, SRC-141
    """

    def test_all_returned_items_have_url(self, sample_article: ArticleRecord) -> None:
        """Every item in ScorerResult.items has a non-empty URL (SRC-049)."""
        llm = DummyLLMClient()
        scorer = Scorer(llm_client=llm)
        result = scorer.score_and_rank(
            prompt="prompt",
            model="gpt-4o",
            candidates=[sample_article],
            tweet_signals=[],
            top_n=5,
            prompt_version="sha256:test",
            cadence="daily",
        )
        for item in result.items:
            assert item.url, f"Item without URL should have been dropped: {item.headline!r}"

    def test_no_url_item_is_dropped(self) -> None:
        """Item with empty URL string is dropped (SRC-049, SRC-141)."""
        payload = _build_llm_payload(
            items=[
                {
                    "headline": "Has URL",
                    "source_name": "Reuters",
                    "url": "https://reuters.com/real",
                    "pub_date": "2026-05-09",
                    "why_it_matters": "Real item.",
                    "impact_tags": [],
                    "tier": "1b",
                    "cross_refs": [],
                },
                {
                    "headline": "No URL item",
                    "source_name": "Unknown",
                    "url": "",  # MUST be dropped
                    "pub_date": "2026-05-09",
                    "why_it_matters": "Should be dropped.",
                    "impact_tags": [],
                    "tier": "3",
                    "cross_refs": [],
                },
            ]
        )
        llm = DummyLLMClient(complete_response=payload)
        scorer = Scorer(llm_client=llm)
        article = _make_article()
        result = scorer.score_and_rank(
            prompt="prompt",
            model="gpt-4o",
            candidates=[article],
            tweet_signals=[],
            top_n=5,
            prompt_version="sha256:test",
            cadence="daily",
        )
        assert len(result.items) == 1
        assert result.items[0].url == "https://reuters.com/real"

    def test_whitespace_only_url_is_dropped(self) -> None:
        """Whitespace-only URL string is dropped (SRC-049)."""
        payload = _build_llm_payload(
            items=[
                {
                    "headline": "Whitespace URL",
                    "source_name": "Reuters",
                    "url": "   ",
                    "pub_date": "2026-05-09",
                    "why_it_matters": "Whitespace URL.",
                    "impact_tags": [],
                    "tier": "1b",
                    "cross_refs": [],
                },
            ]
        )
        llm = DummyLLMClient(complete_response=payload)
        scorer = Scorer(llm_client=llm)
        article = _make_article()
        result = scorer.score_and_rank(
            prompt="prompt",
            model="gpt-4o",
            candidates=[article],
            tweet_signals=[],
            top_n=5,
            prompt_version="sha256:test",
            cadence="daily",
        )
        assert result.items == [], "Whitespace-URL items must be dropped"


# ---------------------------------------------------------------------------
# Part 4: CuratedItem schema compliance (SRC-048)
# ---------------------------------------------------------------------------


class TestCuratedItemSchema:
    """
    Verify that every returned CuratedItem has all SRC-048 mandatory fields.
    Traces: SRC-048
    """

    def test_daily_item_fields(self, sample_article: ArticleRecord) -> None:
        """Daily items: headline, source, URL, pub_date, why_it_matters (SRC-029, SRC-048)."""
        llm = DummyLLMClient()
        scorer = Scorer(llm_client=llm)
        result = scorer.score_and_rank(
            prompt="prompt",
            model="gpt-4o",
            candidates=[sample_article],
            tweet_signals=[],
            top_n=5,
            prompt_version="sha256:test",
            cadence="daily",
        )
        for item in result.items:
            assert item.headline, "headline required (SRC-048)"
            assert item.source_name, "source_name required (SRC-048)"
            assert item.url, "url required (SRC-048, SRC-049)"
            assert item.pub_date, "pub_date required (SRC-048)"
            assert item.why_it_matters, "why_it_matters required (SRC-048, SRC-122)"
            assert isinstance(item.impact_tags, list), "impact_tags must be list (SRC-048)"
            assert item.tier, "tier required (SRC-048)"
            assert isinstance(item.cross_refs, list), "cross_refs must be list (SRC-048)"

    def test_twitter_sourced_item_has_handle_and_tweet_url(self) -> None:
        """Twitter-sourced items include originating handle + tweet URL (SRC-048)."""
        payload = _build_llm_payload(
            items=[
                {
                    "headline": "CEO Announces Product on X",
                    "source_name": "Twitter/X",
                    "url": "https://twitter.com/sama/status/999",
                    "pub_date": "2026-05-09",
                    "why_it_matters": "Executive announcement before press coverage.",
                    "impact_tags": ["business_impact"],
                    "tier": "1b",
                    "cross_refs": [],
                    "twitter_handle": "sama",
                    "tweet_url": "https://twitter.com/sama/status/999",
                },
            ]
        )
        llm = DummyLLMClient(complete_response=payload)
        scorer = Scorer(llm_client=llm)
        article = _make_article()
        result = scorer.score_and_rank(
            prompt="prompt",
            model="gpt-4o",
            candidates=[article],
            tweet_signals=[],
            top_n=5,
            prompt_version="sha256:test",
            cadence="daily",
        )
        assert len(result.items) == 1
        item = result.items[0]
        assert item.twitter_handle == "sama", "twitter_handle required for Twitter-sourced items"
        assert item.tweet_url == "https://twitter.com/sama/status/999"

    def test_web_sourced_item_has_no_twitter_fields(self, sample_article: ArticleRecord) -> None:
        """Web-sourced items have None for twitter_handle and tweet_url (SRC-048)."""
        llm = DummyLLMClient()
        scorer = Scorer(llm_client=llm)
        result = scorer.score_and_rank(
            prompt="prompt",
            model="gpt-4o",
            candidates=[sample_article],
            tweet_signals=[],
            top_n=5,
            prompt_version="sha256:test",
            cadence="daily",
        )
        for item in result.items:
            if item.twitter_handle is None:
                assert item.tweet_url is None

    def test_prompt_version_on_all_items(self, sample_article: ArticleRecord) -> None:
        """prompt_version is set on every CuratedItem (SRC-129)."""
        version = "sha256:abc123def456"
        llm = DummyLLMClient()
        scorer = Scorer(llm_client=llm)
        result = scorer.score_and_rank(
            prompt="prompt",
            model="gpt-4o",
            candidates=[sample_article],
            tweet_signals=[],
            top_n=5,
            prompt_version=version,
            cadence="daily",
        )
        for item in result.items:
            assert item.prompt_version == version, (
                f"Every item must carry prompt_version (SRC-129): {item.headline!r}"
            )

    def test_top_n_enforced(self, sample_article: ArticleRecord) -> None:
        """Items truncated to top_n (SRC-029–SRC-032)."""
        many = [
            {
                "headline": f"Item {i}",
                "source_name": "Reuters",
                "url": f"https://reuters.com/article-{i}",
                "pub_date": "2026-05-09",
                "why_it_matters": "Matters.",
                "impact_tags": ["business_impact"],
                "tier": "1b",
                "cross_refs": [],
            }
            for i in range(30)
        ]
        payload = _build_llm_payload(items=many)
        llm = DummyLLMClient(complete_response=payload)
        scorer = Scorer(llm_client=llm)
        result = scorer.score_and_rank(
            prompt="prompt",
            model="gpt-4o",
            candidates=[sample_article],
            tweet_signals=[],
            top_n=7,
            prompt_version="sha256:test",
            cadence="weekly",
        )
        assert len(result.items) <= 7, "Scorer must truncate to top_n"

    def test_tier_weight_ordering(self) -> None:
        """Higher tiers appear before lower tiers (SRC-016–SRC-021)."""
        # LLM returns tier_1b first then tier_3 — sorting should preserve 1b above 3
        items_from_llm = [
            {
                "headline": "Tier 3 Article",
                "source_name": "TechCrunch",
                "url": "https://techcrunch.com/a",
                "pub_date": "2026-05-09",
                "why_it_matters": "Interesting.",
                "impact_tags": [],
                "tier": "3",
                "cross_refs": [],
            },
            {
                "headline": "Tier 1b Article",
                "source_name": "Reuters",
                "url": "https://reuters.com/b",
                "pub_date": "2026-05-09",
                "why_it_matters": "Very important.",
                "impact_tags": ["business_impact"],
                "tier": "1b",
                "cross_refs": [],
            },
        ]
        payload = _build_llm_payload(items=items_from_llm)
        llm = DummyLLMClient(complete_response=payload)
        scorer = Scorer(llm_client=llm)
        article = _make_article()
        result = scorer.score_and_rank(
            prompt="prompt",
            model="gpt-4o",
            candidates=[article],
            tweet_signals=[],
            top_n=10,
            prompt_version="sha256:test",
            cadence="daily",
        )
        assert len(result.items) == 2
        # 1b (weight=0.9) must come before 3 (weight=0.7)
        assert result.items[0].tier == "1b"
        assert result.items[1].tier == "3"


# ---------------------------------------------------------------------------
# Part 5: CurationAgent integration — daily cadence (SRC-029)
# ---------------------------------------------------------------------------


class TestCurationAgentDaily:
    """
    Traces: SRC-014–SRC-015, SRC-027, SRC-029, SRC-048, SRC-129, SRC-148, SRC-150
    """

    def test_daily_run_returns_correct_result_type(
        self,
        sample_agent_config: AgentConfig,
        sample_secrets,
        prompts_dir: Path,
        tmp_path: Path,
    ) -> None:
        """CurationAgent.run() returns CurationRunResult (SRC-014)."""
        from ai_news_agent.storage.tinydb_store import TinyDBArticleStore

        store = TinyDBArticleStore(tmp_path / "store.json")
        with patch("ai_news_agent.curation.agent.get_llm_client") as mock_factory:
            mock_factory.return_value = DummyLLMClient()
            agent = CurationAgent(
                config=sample_agent_config,
                secrets=sample_secrets,
                store=store,
                prompts_dir=str(prompts_dir),
            )
            result = agent.run(
                cadence="daily",
                reference_time=datetime(2026, 5, 10, tzinfo=UTC),
            )
        assert isinstance(result, CurationRunResult)

    def test_daily_metadata_cadence_correct(
        self,
        sample_agent_config: AgentConfig,
        sample_secrets,
        prompts_dir: Path,
        tmp_path: Path,
    ) -> None:
        """DigestMetadata.cadence == 'daily' for daily runs (SRC-029)."""
        from ai_news_agent.storage.tinydb_store import TinyDBArticleStore

        store = TinyDBArticleStore(tmp_path / "store.json")
        with patch("ai_news_agent.curation.agent.get_llm_client") as mock_factory:
            mock_factory.return_value = DummyLLMClient()
            agent = CurationAgent(
                config=sample_agent_config,
                secrets=sample_secrets,
                store=store,
                prompts_dir=str(prompts_dir),
            )
            result = agent.run(
                cadence="daily",
                reference_time=datetime(2026, 5, 10, tzinfo=UTC),
            )
        assert result.metadata.cadence == "daily"

    def test_daily_metadata_prompt_version_sha256(
        self,
        sample_agent_config: AgentConfig,
        sample_secrets,
        prompts_dir: Path,
        tmp_path: Path,
    ) -> None:
        """DigestMetadata.prompt_version starts with 'sha256:' (SRC-129)."""
        from ai_news_agent.storage.tinydb_store import TinyDBArticleStore

        store = TinyDBArticleStore(tmp_path / "store.json")
        with patch("ai_news_agent.curation.agent.get_llm_client") as mock_factory:
            mock_factory.return_value = DummyLLMClient()
            agent = CurationAgent(
                config=sample_agent_config,
                secrets=sample_secrets,
                store=store,
                prompts_dir=str(prompts_dir),
            )
            result = agent.run(
                cadence="daily",
                reference_time=datetime(2026, 5, 10, tzinfo=UTC),
            )
        assert result.metadata.prompt_version.startswith("sha256:"), (
            "DigestMetadata.prompt_version must be SHA-256 hash (SRC-129)"
        )

    def test_daily_metadata_contains_llm_provider_and_model(
        self,
        sample_agent_config: AgentConfig,
        sample_secrets,
        prompts_dir: Path,
        tmp_path: Path,
    ) -> None:
        """DigestMetadata records LLM provider + model for quality monitoring (SRC-150)."""
        from ai_news_agent.storage.tinydb_store import TinyDBArticleStore

        store = TinyDBArticleStore(tmp_path / "store.json")
        with patch("ai_news_agent.curation.agent.get_llm_client") as mock_factory:
            mock_factory.return_value = DummyLLMClient()
            agent = CurationAgent(
                config=sample_agent_config,
                secrets=sample_secrets,
                store=store,
                prompts_dir=str(prompts_dir),
            )
            result = agent.run(
                cadence="daily",
                reference_time=datetime(2026, 5, 10, tzinfo=UTC),
            )
        assert result.metadata.llm_provider, "llm_provider required (SRC-150)"
        assert result.metadata.llm_model, "llm_model required (SRC-150)"

    def test_daily_run_with_articles_in_window(
        self,
        sample_agent_config: AgentConfig,
        sample_secrets,
        prompts_dir: Path,
        tmp_path: Path,
    ) -> None:
        """Articles inserted for the previous day appear as candidates (SRC-008–SRC-010)."""
        from ai_news_agent.storage.tinydb_store import TinyDBArticleStore

        store = TinyDBArticleStore(tmp_path / "store.json")

        # Insert an article on May 9 — within the daily window for a May 10 run
        article = _make_article(
            headline="Test Article in Window",
            url="https://reuters.com/test-in-window",
            agent_id=sample_agent_config.agent_id,
            pub_date=datetime(2026, 5, 9, 10, 0, tzinfo=UTC),
        )
        store.insert_if_new(article)

        payload = _build_llm_payload(
            items=[
                {
                    "headline": "Test Article in Window",
                    "source_name": "Reuters",
                    "url": "https://reuters.com/test-in-window",
                    "pub_date": "2026-05-09",
                    "why_it_matters": "It was in the window.",
                    "impact_tags": ["business_impact"],
                    "tier": "1b",
                    "cross_refs": [],
                }
            ]
        )
        with patch("ai_news_agent.curation.agent.get_llm_client") as mock_factory:
            mock_factory.return_value = DummyLLMClient(complete_response=payload)
            agent = CurationAgent(
                config=sample_agent_config,
                secrets=sample_secrets,
                store=store,
                prompts_dir=str(prompts_dir),
            )
            result = agent.run(
                cadence="daily",
                reference_time=datetime(2026, 5, 10, tzinfo=UTC),
            )

        assert result.metadata.items_considered >= 1, "Candidates in window must be counted"
        assert result.metadata.items_included >= 1

    def test_diagnostics_when_store_empty(
        self,
        sample_agent_config: AgentConfig,
        sample_secrets,
        prompts_dir: Path,
        tmp_path: Path,
    ) -> None:
        """Empty store → diagnostics block with the 'store is empty' reason."""
        from ai_news_agent.storage.tinydb_store import TinyDBArticleStore

        store = TinyDBArticleStore(tmp_path / "store.json")
        with patch("ai_news_agent.curation.agent.get_llm_client") as mock_factory:
            mock_factory.return_value = DummyLLMClient()
            agent = CurationAgent(
                config=sample_agent_config,
                secrets=sample_secrets,
                store=store,
                prompts_dir=str(prompts_dir),
            )
            result = agent.run(
                cadence="daily",
                reference_time=datetime(2026, 5, 10, tzinfo=UTC),
            )

        assert result.diagnostics is not None, (
            "Empty digest must carry a CurationDiagnostics explanation"
        )
        diag = result.diagnostics
        assert diag.articles_in_store == 0
        assert diag.articles_in_window == 0
        assert diag.reasons, "Must have at least one reason"
        assert any("store is empty" in r.lower() for r in diag.reasons)

    def test_diagnostics_when_articles_in_store_but_not_in_window(
        self,
        sample_agent_config: AgentConfig,
        sample_secrets,
        prompts_dir: Path,
        tmp_path: Path,
    ) -> None:
        """
        Store has articles but their pub_date falls outside the curation window.
        Diagnostics must call out the window mismatch — the original bug that
        motivated this feature.
        """
        from ai_news_agent.storage.tinydb_store import TinyDBArticleStore

        store = TinyDBArticleStore(tmp_path / "store.json")

        # Insert an article 5 days before the reference — outside the daily window.
        out_of_window = _make_article(
            headline="Out of window",
            url="https://example.com/old",
            agent_id=sample_agent_config.agent_id,
            pub_date=datetime(2026, 5, 4, 12, 0, tzinfo=UTC),
        )
        store.insert_if_new(out_of_window)

        with patch("ai_news_agent.curation.agent.get_llm_client") as mock_factory:
            mock_factory.return_value = DummyLLMClient()
            agent = CurationAgent(
                config=sample_agent_config,
                secrets=sample_secrets,
                store=store,
                prompts_dir=str(prompts_dir),
            )
            result = agent.run(
                cadence="daily",
                reference_time=datetime(2026, 5, 10, tzinfo=UTC),
            )

        assert result.diagnostics is not None
        diag = result.diagnostics
        assert diag.articles_in_store == 1
        assert diag.articles_in_window == 0
        assert any("window" in r.lower() for r in diag.reasons)

    def test_diagnostics_omitted_for_normal_run(
        self,
        sample_agent_config: AgentConfig,
        sample_secrets,
        prompts_dir: Path,
        tmp_path: Path,
    ) -> None:
        """When items_included >= threshold, no diagnostics block is attached."""
        from ai_news_agent.storage.tinydb_store import TinyDBArticleStore

        store = TinyDBArticleStore(tmp_path / "store.json")

        # Stock the store and have the LLM return four items so we clear the
        # sparse-digest threshold (3).
        items_payload = []
        for i in range(4):
            url = f"https://reuters.com/article-{i}"
            store.insert_if_new(
                _make_article(
                    headline=f"Article {i}",
                    url=url,
                    agent_id=sample_agent_config.agent_id,
                    pub_date=datetime(2026, 5, 9, 10, 0, tzinfo=UTC),
                )
            )
            items_payload.append(
                {
                    "headline": f"Article {i}",
                    "source_name": "Reuters",
                    "url": url,
                    "pub_date": "2026-05-09",
                    "why_it_matters": "Material development.",
                    "impact_tags": ["business_impact"],
                    "tier": "1b",
                    "cross_refs": [],
                }
            )

        payload = _build_llm_payload(items=items_payload)
        with patch("ai_news_agent.curation.agent.get_llm_client") as mock_factory:
            mock_factory.return_value = DummyLLMClient(complete_response=payload)
            agent = CurationAgent(
                config=sample_agent_config,
                secrets=sample_secrets,
                store=store,
                prompts_dir=str(prompts_dir),
            )
            result = agent.run(
                cadence="daily",
                reference_time=datetime(2026, 5, 10, tzinfo=UTC),
            )

        assert result.metadata.items_included >= 3, (
            "Test setup requires >= 3 items to verify diagnostics are omitted"
        )
        assert result.diagnostics is None, (
            "Diagnostics must not be attached when the digest is well-populated"
        )

    def test_daily_twitter_degradation_when_api_down(
        self,
        sample_agent_config: AgentConfig,
        sample_secrets,
        prompts_dir: Path,
        tmp_path: Path,
    ) -> None:
        """When twitter_api_available=False, degradation note is set (SRC-148)."""
        from ai_news_agent.storage.tinydb_store import TinyDBArticleStore

        store = TinyDBArticleStore(tmp_path / "store.json")
        with patch("ai_news_agent.curation.agent.get_llm_client") as mock_factory:
            mock_factory.return_value = DummyLLMClient()
            agent = CurationAgent(
                config=sample_agent_config,
                secrets=sample_secrets,
                store=store,
                prompts_dir=str(prompts_dir),
            )
            result = agent.run(
                cadence="daily",
                reference_time=datetime(2026, 5, 10, tzinfo=UTC),
                twitter_api_available=False,
            )
        assert result.twitter_degradation_note is not None
        assert len(result.twitter_degradation_note) > 0
        assert result.metadata.twitter_signal_available is False

    def test_daily_twitter_available_when_signals_present(
        self,
        sample_agent_config: AgentConfig,
        sample_secrets,
        prompts_dir: Path,
        tmp_path: Path,
    ) -> None:
        """When tweet signals in store, twitter_signal_available=True (SRC-148)."""
        from ai_news_agent.storage.tinydb_store import TinyDBArticleStore

        store = TinyDBArticleStore(tmp_path / "store.json")

        signal = TweetSignal(
            tweet_id="tweet-001",
            handle="karpathy",
            text="Interesting AI development",
            created_at=datetime(2026, 5, 9, 10, 0, tzinfo=UTC),
            linked_urls=["https://example.com/paper"],
            agent_id=sample_agent_config.agent_id,
            fetched_at=datetime(2026, 5, 9, 10, 5, tzinfo=UTC),
            weight=1.0,
        )
        store.insert_tweet_signal(signal)

        with patch("ai_news_agent.curation.agent.get_llm_client") as mock_factory:
            mock_factory.return_value = DummyLLMClient()
            agent = CurationAgent(
                config=sample_agent_config,
                secrets=sample_secrets,
                store=store,
                prompts_dir=str(prompts_dir),
            )
            result = agent.run(
                cadence="daily",
                reference_time=datetime(2026, 5, 10, tzinfo=UTC),
            )

        assert result.metadata.twitter_signal_available is True
        assert result.twitter_degradation_note is None

    def test_daily_metadata_items_considered_count(
        self,
        sample_agent_config: AgentConfig,
        sample_secrets,
        prompts_dir: Path,
        tmp_path: Path,
    ) -> None:
        """metadata.items_considered reflects total candidates retrieved (SRC-150)."""
        from ai_news_agent.storage.tinydb_store import TinyDBArticleStore

        store = TinyDBArticleStore(tmp_path / "store.json")

        for i in range(3):
            store.insert_if_new(
                _make_article(
                    headline=f"Article {i}",
                    url=f"https://reuters.com/article-{i}",
                    agent_id=sample_agent_config.agent_id,
                    pub_date=datetime(2026, 5, 9, i + 8, 0, tzinfo=UTC),
                )
            )

        with patch("ai_news_agent.curation.agent.get_llm_client") as mock_factory:
            mock_factory.return_value = DummyLLMClient()
            agent = CurationAgent(
                config=sample_agent_config,
                secrets=sample_secrets,
                store=store,
                prompts_dir=str(prompts_dir),
            )
            result = agent.run(
                cadence="daily",
                reference_time=datetime(2026, 5, 10, tzinfo=UTC),
            )
        assert result.metadata.items_considered == 3


# ---------------------------------------------------------------------------
# Part 6: Weekly curation — themes + look-ahead (SRC-030)
# ---------------------------------------------------------------------------


class TestCurationAgentWeekly:
    """
    Traces: SRC-030 (weekly: themes + look-ahead + top articles)
    """

    def test_weekly_result_has_themes(
        self,
        sample_agent_config: AgentConfig,
        sample_secrets,
        prompts_dir: Path,
        tmp_path: Path,
    ) -> None:
        """Weekly result includes themes from LLM response (SRC-030)."""
        from ai_news_agent.storage.tinydb_store import TinyDBArticleStore

        store = TinyDBArticleStore(tmp_path / "store.json")
        payload = _build_llm_payload(
            themes=["Enterprise AI Integration", "LLM Cost Decline"],
            outlook="Next week: expect OpenAI GPT-5 announcement coverage.",
        )
        with patch("ai_news_agent.curation.agent.get_llm_client") as mock_factory:
            mock_factory.return_value = DummyLLMClient(complete_response=payload)
            agent = CurationAgent(
                config=sample_agent_config,
                secrets=sample_secrets,
                store=store,
                prompts_dir=str(prompts_dir),
            )
            result = agent.run(
                cadence="weekly",
                reference_time=datetime(2026, 5, 11, tzinfo=UTC),  # Monday
            )
        assert result.metadata.cadence == "weekly"
        assert isinstance(result.themes, list)
        assert isinstance(result.outlook, str)

    def test_weekly_result_themes_and_outlook_populated(
        self,
        sample_agent_config: AgentConfig,
        sample_secrets,
        prompts_dir: Path,
        tmp_path: Path,
    ) -> None:
        """Themes and outlook from the LLM are propagated into CurationRunResult (SRC-030)."""
        from ai_news_agent.storage.tinydb_store import TinyDBArticleStore

        # Insert a candidate in the weekly window
        store = TinyDBArticleStore(tmp_path / "store.json")
        # A Monday reference means window is prev-Sun through prev-Sat
        # Insert article in that window
        ref = datetime(2026, 5, 11, tzinfo=UTC)  # Monday May 11
        start, end = _weekly_window(ref)
        article = _make_article(
            agent_id=sample_agent_config.agent_id,
            pub_date=start + timedelta(hours=4),
        )
        store.insert_if_new(article)

        payload = _build_llm_payload(
            themes=["Enterprise AI Integration", "Open-Weight Model Surge"],
            outlook="Watch for new regulation proposals from the EU next week.",
        )
        with patch("ai_news_agent.curation.agent.get_llm_client") as mock_factory:
            mock_factory.return_value = DummyLLMClient(complete_response=payload)
            agent = CurationAgent(
                config=sample_agent_config,
                secrets=sample_secrets,
                store=store,
                prompts_dir=str(prompts_dir),
            )
            result = agent.run(
                cadence="weekly",
                reference_time=ref,
            )

        assert len(result.themes) == 2
        assert "EU" in result.outlook or len(result.outlook) > 0


# ---------------------------------------------------------------------------
# Part 7: Monthly curation — bigger-picture themes + anticipated news (SRC-031)
# ---------------------------------------------------------------------------


class TestCurationAgentMonthly:
    """
    Traces: SRC-031, SRC-054 (research LLM for monthly)
    """

    def test_monthly_result_has_themes_and_outlook(
        self,
        sample_agent_config: AgentConfig,
        sample_secrets,
        prompts_dir: Path,
        tmp_path: Path,
    ) -> None:
        """Monthly digest includes themes and outlook (SRC-031)."""
        from ai_news_agent.storage.tinydb_store import TinyDBArticleStore

        store = TinyDBArticleStore(tmp_path / "store.json")
        payload = _build_llm_payload(
            themes=["AI Regulation Acceleration", "Model Commoditisation"],
            outlook="May will see EU enforcement under the AI Act.",
        )
        with patch("ai_news_agent.curation.agent.get_llm_client") as mock_factory:
            mock_factory.return_value = DummyLLMClient(complete_response=payload)
            agent = CurationAgent(
                config=sample_agent_config,
                secrets=sample_secrets,
                store=store,
                prompts_dir=str(prompts_dir),
            )
            result = agent.run(
                cadence="monthly",
                reference_time=datetime(2026, 5, 1, tzinfo=UTC),
            )
        assert result.metadata.cadence == "monthly"
        assert isinstance(result.themes, list)
        assert isinstance(result.outlook, str)

    def test_monthly_research_model_override(
        self,
        sample_secrets,
        prompts_dir: Path,
        tmp_path: Path,
    ) -> None:
        """Monthly uses configured cadence override model (SRC-054)."""
        from ai_news_agent.config.models import SourcesConfig, TwitterConfig
        from ai_news_agent.storage.tinydb_store import TinyDBArticleStore

        config = AgentConfig(
            agent_id="test-monthly",
            llm=LLMConfig(
                provider="openai",
                model="gpt-4o",
                cadence_overrides={
                    "monthly": LLMCadenceOverride(model="o3", thinking=False),
                },
            ),
            sources=SourcesConfig(),
            twitter=TwitterConfig(enabled=False, handles=[]),
            limits=LimitsConfig(),
            output_dir=str(tmp_path / "outputs/test-monthly"),
        )
        store = TinyDBArticleStore(tmp_path / "store.json")
        with patch("ai_news_agent.curation.agent.get_llm_client") as mock_factory:
            mock_factory.return_value = DummyLLMClient()
            agent = CurationAgent(
                config=config,
                secrets=sample_secrets,
                store=store,
                prompts_dir=str(prompts_dir),
            )
            result = agent.run(
                cadence="monthly",
                reference_time=datetime(2026, 5, 1, tzinfo=UTC),
            )
        assert result.metadata.llm_model == "o3"


# ---------------------------------------------------------------------------
# Part 8: Annual curation — top-10 + 10 predictions (SRC-032, SRC-124)
# ---------------------------------------------------------------------------


class TestCurationAgentAnnual:
    """
    Traces: SRC-032, SRC-054, SRC-124
    """

    def test_annual_result_has_predictions(
        self,
        sample_agent_config: AgentConfig,
        sample_secrets,
        prompts_dir: Path,
        tmp_path: Path,
    ) -> None:
        """Annual digest includes predictions list (SRC-032, SRC-124)."""
        from ai_news_agent.storage.tinydb_store import TinyDBArticleStore

        store = TinyDBArticleStore(tmp_path / "store.json")

        # Insert an article in the annual window (2025) so scorer isn't short-circuited
        store.insert_if_new(
            _make_article(
                headline="Annual Article 2025",
                url="https://reuters.com/annual-article-2025",
                agent_id=sample_agent_config.agent_id,
                pub_date=datetime(2025, 6, 15, 10, 0, tzinfo=UTC),
            )
        )

        preds = [
            "AI agents will handle >20% of enterprise customer service calls.",
            "Open-weight models will match closed frontier within 6 months.",
            "At least one G7 nation will pass binding AI liability legislation.",
        ]
        payload = _build_llm_payload(
            themes=["Agentic AI", "Regulatory Consolidation"],
            predictions=preds,
        )
        with patch("ai_news_agent.curation.agent.get_llm_client") as mock_factory:
            mock_factory.return_value = DummyLLMClient(complete_response=payload)
            agent = CurationAgent(
                config=sample_agent_config,
                secrets=sample_secrets,
                store=store,
                prompts_dir=str(prompts_dir),
            )
            result = agent.run(
                cadence="annual",
                reference_time=datetime(2026, 1, 1, tzinfo=UTC),
            )
        assert result.metadata.cadence == "annual"
        assert len(result.predictions) == 3
        assert all(len(p) > 0 for p in result.predictions)

    def test_annual_uses_research_model_override(
        self,
        sample_secrets,
        prompts_dir: Path,
        tmp_path: Path,
    ) -> None:
        """Annual cadence uses o3+thinking when configured (SRC-032, SRC-054)."""
        from ai_news_agent.config.models import SourcesConfig, TwitterConfig
        from ai_news_agent.storage.tinydb_store import TinyDBArticleStore

        config = AgentConfig(
            agent_id="test-annual",
            llm=LLMConfig(
                provider="openai",
                model="gpt-4o",
                cadence_overrides={
                    "annual": LLMCadenceOverride(model="o3", thinking=True),
                },
            ),
            sources=SourcesConfig(),
            twitter=TwitterConfig(enabled=False, handles=[]),
            limits=LimitsConfig(annual_top_n=10),
            output_dir=str(tmp_path / "outputs/test-annual"),
        )
        store = TinyDBArticleStore(tmp_path / "store.json")
        dummy = DummyLLMClient()
        with patch("ai_news_agent.curation.agent.get_llm_client") as mock_factory:
            mock_factory.return_value = dummy
            agent = CurationAgent(
                config=config,
                secrets=sample_secrets,
                store=store,
                prompts_dir=str(prompts_dir),
            )
            result = agent.run(
                cadence="annual",
                reference_time=datetime(2026, 1, 1, tzinfo=UTC),
            )

        assert result.metadata.llm_model == "o3"

    def test_annual_thinking_flag_passed_to_scorer(
        self,
        sample_secrets,
        prompts_dir: Path,
        tmp_path: Path,
    ) -> None:
        """thinking=True is forwarded to the Scorer for o3 annual runs (SRC-032, SRC-054)."""
        from ai_news_agent.config.models import SourcesConfig, TwitterConfig
        from ai_news_agent.storage.tinydb_store import TinyDBArticleStore

        config = AgentConfig(
            agent_id="test-annual-thinking",
            llm=LLMConfig(
                provider="openai",
                model="gpt-4o",
                cadence_overrides={
                    "annual": LLMCadenceOverride(model="o3", thinking=True),
                },
            ),
            sources=SourcesConfig(),
            twitter=TwitterConfig(enabled=False, handles=[]),
            limits=LimitsConfig(annual_top_n=10),
            output_dir=str(tmp_path / "outputs/test-annual-thinking"),
        )
        store = TinyDBArticleStore(tmp_path / "store.json")
        dummy = DummyLLMClient()

        with patch("ai_news_agent.curation.agent.get_llm_client") as mock_factory:
            mock_factory.return_value = dummy
            # Patch the Scorer to capture the thinking flag
            with patch("ai_news_agent.curation.agent.Scorer") as MockScorer:
                mock_scorer_instance = MagicMock()
                mock_scorer_instance.score_and_rank.return_value = ScorerResult(
                    items=[], themes=[], outlook="", predictions=[], token_usage=100
                )
                MockScorer.return_value = mock_scorer_instance

                agent = CurationAgent(
                    config=config,
                    secrets=sample_secrets,
                    store=store,
                    prompts_dir=str(prompts_dir),
                )
                agent.run(
                    cadence="annual",
                    reference_time=datetime(2026, 1, 1, tzinfo=UTC),
                )

        call_kwargs = mock_scorer_instance.score_and_rank.call_args
        assert call_kwargs is not None
        assert call_kwargs.kwargs.get("thinking", False) is True, (
            "thinking=True must be forwarded to scorer for annual o3 runs (SRC-054)"
        )


# ---------------------------------------------------------------------------
# Part 9: On-demand re-run with explicit window override (SRC-028)
# ---------------------------------------------------------------------------


class TestOnDemandRerun:
    """
    Traces: SRC-028 (curation can be rerun for any window on demand),
            SRC-145 (re-runs overwrite cleanly)
    """

    def test_explicit_window_overrides_automatic(
        self,
        sample_agent_config: AgentConfig,
        sample_secrets,
        prompts_dir: Path,
        tmp_path: Path,
    ) -> None:
        """
        Explicit window_start/window_end override the automatic window computation.
        Allows historical re-runs without touching reference_time (SRC-028).
        """
        from ai_news_agent.storage.tinydb_store import TinyDBArticleStore

        store = TinyDBArticleStore(tmp_path / "store.json")

        # Insert article in the explicit window (March 2026)
        march_start = datetime(2026, 3, 1, tzinfo=UTC)
        march_end = datetime(2026, 3, 31, 23, 59, 59, tzinfo=UTC)
        article = _make_article(
            headline="March Article",
            url="https://reuters.com/march-article",
            agent_id=sample_agent_config.agent_id,
            pub_date=datetime(2026, 3, 15, 10, 0, tzinfo=UTC),
        )
        store.insert_if_new(article)

        with patch("ai_news_agent.curation.agent.get_llm_client") as mock_factory:
            mock_factory.return_value = DummyLLMClient()
            agent = CurationAgent(
                config=sample_agent_config,
                secrets=sample_secrets,
                store=store,
                prompts_dir=str(prompts_dir),
            )
            result = agent.run(
                cadence="monthly",
                reference_time=datetime(2026, 5, 1, tzinfo=UTC),  # Would normally give April
                window_start=march_start,
                window_end=march_end,
            )

        assert result.metadata.window_start == march_start
        assert result.metadata.window_end == march_end
        assert result.metadata.items_considered >= 1, (
            "Explicit window must retrieve articles within that range (SRC-028)"
        )

    def test_explicit_window_does_not_include_out_of_window_articles(
        self,
        sample_agent_config: AgentConfig,
        sample_secrets,
        prompts_dir: Path,
        tmp_path: Path,
    ) -> None:
        """Articles outside the explicit window are not retrieved (SRC-008)."""
        from ai_news_agent.storage.tinydb_store import TinyDBArticleStore

        store = TinyDBArticleStore(tmp_path / "store.json")

        # Insert article in May — outside March window
        store.insert_if_new(
            _make_article(
                headline="May Article",
                url="https://reuters.com/may-article",
                agent_id=sample_agent_config.agent_id,
                pub_date=datetime(2026, 5, 9, 10, 0, tzinfo=UTC),
            )
        )

        march_start = datetime(2026, 3, 1, tzinfo=UTC)
        march_end = datetime(2026, 3, 31, 23, 59, 59, tzinfo=UTC)

        with patch("ai_news_agent.curation.agent.get_llm_client") as mock_factory:
            mock_factory.return_value = DummyLLMClient()
            agent = CurationAgent(
                config=sample_agent_config,
                secrets=sample_secrets,
                store=store,
                prompts_dir=str(prompts_dir),
            )
            result = agent.run(
                cadence="monthly",
                window_start=march_start,
                window_end=march_end,
            )

        assert result.metadata.items_considered == 0, (
            "May article must NOT appear in a March window (SRC-008)"
        )

    def test_rerun_overwrites_digest_record(
        self,
        sample_agent_config: AgentConfig,
        sample_secrets,
        prompts_dir: Path,
        tmp_path: Path,
    ) -> None:
        """Running curation twice for the same date+cadence produces one DigestRecord (SRC-145)."""
        from ai_news_agent.storage.tinydb_store import TinyDBArticleStore

        store = TinyDBArticleStore(tmp_path / "store.json")
        ref = datetime(2026, 5, 10, tzinfo=UTC)

        with patch("ai_news_agent.curation.agent.get_llm_client") as mock_factory:
            mock_factory.return_value = DummyLLMClient()
            agent = CurationAgent(
                config=sample_agent_config,
                secrets=sample_secrets,
                store=store,
                prompts_dir=str(prompts_dir),
            )
            agent.run(cadence="daily", reference_time=ref)
            agent.run(cadence="daily", reference_time=ref)  # re-run

        digests = store.list_digests(agent_id=sample_agent_config.agent_id, cadence="daily")
        assert len(digests) == 1, (
            "Re-running for the same date+cadence must produce exactly one DigestRecord (SRC-145)"
        )


# ---------------------------------------------------------------------------
# Part 10: Dry-run mode (SRC-102)
# ---------------------------------------------------------------------------


class TestDryRun:
    """
    Traces: SRC-102 (dry-run mode: produce digest but skip store writes)
    """

    def test_dry_run_flag_set_on_result(
        self,
        sample_agent_config: AgentConfig,
        sample_secrets,
        prompts_dir: Path,
        tmp_path: Path,
    ) -> None:
        """dry_run=True is reflected in CurationRunResult.dry_run (SRC-102)."""
        from ai_news_agent.storage.tinydb_store import TinyDBArticleStore

        store = TinyDBArticleStore(tmp_path / "store.json")
        with patch("ai_news_agent.curation.agent.get_llm_client") as mock_factory:
            mock_factory.return_value = DummyLLMClient()
            agent = CurationAgent(
                config=sample_agent_config,
                secrets=sample_secrets,
                store=store,
                prompts_dir=str(prompts_dir),
            )
            result = agent.run(cadence="daily", dry_run=True)
        assert result.dry_run is True

    def test_dry_run_skips_digest_record_write(
        self,
        sample_agent_config: AgentConfig,
        sample_secrets,
        prompts_dir: Path,
        tmp_path: Path,
    ) -> None:
        """Dry-run mode does not persist DigestRecord to the store (SRC-102)."""
        from ai_news_agent.storage.tinydb_store import TinyDBArticleStore

        store = TinyDBArticleStore(tmp_path / "store.json")
        with patch("ai_news_agent.curation.agent.get_llm_client") as mock_factory:
            mock_factory.return_value = DummyLLMClient()
            agent = CurationAgent(
                config=sample_agent_config,
                secrets=sample_secrets,
                store=store,
                prompts_dir=str(prompts_dir),
            )
            agent.run(cadence="daily", dry_run=True)

        digests = store.list_digests(agent_id=sample_agent_config.agent_id)
        assert len(digests) == 0, "Dry-run must not persist DigestRecord (SRC-102)"

    def test_dry_run_still_returns_complete_result(
        self,
        sample_agent_config: AgentConfig,
        sample_secrets,
        prompts_dir: Path,
        tmp_path: Path,
    ) -> None:
        """Dry-run produces a complete CurationRunResult for CI verification (SRC-102)."""
        from ai_news_agent.storage.tinydb_store import TinyDBArticleStore

        store = TinyDBArticleStore(tmp_path / "store.json")
        with patch("ai_news_agent.curation.agent.get_llm_client") as mock_factory:
            mock_factory.return_value = DummyLLMClient()
            agent = CurationAgent(
                config=sample_agent_config,
                secrets=sample_secrets,
                store=store,
                prompts_dir=str(prompts_dir),
            )
            result = agent.run(cadence="daily", dry_run=True)

        assert isinstance(result, CurationRunResult)
        assert result.metadata is not None
        assert result.metadata.cadence == "daily"
        assert result.metadata.prompt_version.startswith("sha256:")

    def test_non_dry_run_persists_digest_record(
        self,
        sample_agent_config: AgentConfig,
        sample_secrets,
        prompts_dir: Path,
        tmp_path: Path,
    ) -> None:
        """Normal (non-dry-run) mode persists DigestRecord (SRC-145)."""
        from ai_news_agent.storage.tinydb_store import TinyDBArticleStore

        store = TinyDBArticleStore(tmp_path / "store.json")
        with patch("ai_news_agent.curation.agent.get_llm_client") as mock_factory:
            mock_factory.return_value = DummyLLMClient()
            agent = CurationAgent(
                config=sample_agent_config,
                secrets=sample_secrets,
                store=store,
                prompts_dir=str(prompts_dir),
            )
            result = agent.run(
                cadence="daily",
                reference_time=datetime(2026, 5, 10, tzinfo=UTC),
                dry_run=False,
            )

        assert result.dry_run is False
        digests = store.list_digests(agent_id=sample_agent_config.agent_id)
        assert len(digests) == 1, "Normal run must persist DigestRecord (SRC-145)"


# ---------------------------------------------------------------------------
# Part 11: Quality monitoring metadata (SRC-150)
# ---------------------------------------------------------------------------


class TestQualityMonitoring:
    """
    Traces: SRC-150 (items_by_tier, items_by_source_class, token_usage,
                     tweet_api_call_count, llm_provider, llm_model)
    """

    def test_items_by_tier_populated(
        self,
        sample_agent_config: AgentConfig,
        sample_secrets,
        prompts_dir: Path,
        tmp_path: Path,
    ) -> None:
        """metadata.items_by_tier reflects tier distribution of returned items (SRC-150)."""
        from ai_news_agent.storage.tinydb_store import TinyDBArticleStore

        store = TinyDBArticleStore(tmp_path / "store.json")
        payload = _build_llm_payload(
            items=[
                {
                    "headline": "1b Article",
                    "source_name": "Reuters",
                    "url": "https://reuters.com/a",
                    "pub_date": "2026-05-09",
                    "why_it_matters": "Matters.",
                    "impact_tags": ["business_impact"],
                    "tier": "1b",
                    "cross_refs": [],
                },
                {
                    "headline": "2 Article",
                    "source_name": "OpenAI Blog",
                    "url": "https://openai.com/blog/b",
                    "pub_date": "2026-05-09",
                    "why_it_matters": "Also matters.",
                    "impact_tags": ["business_impact"],
                    "tier": "2",
                    "cross_refs": [],
                },
            ]
        )
        with patch("ai_news_agent.curation.agent.get_llm_client") as mock_factory:
            mock_factory.return_value = DummyLLMClient(complete_response=payload)
            agent = CurationAgent(
                config=sample_agent_config,
                secrets=sample_secrets,
                store=store,
                prompts_dir=str(prompts_dir),
            )
            result = agent.run(
                cadence="daily",
                reference_time=datetime(2026, 5, 10, tzinfo=UTC),
            )
        by_tier = result.metadata.items_by_tier
        assert isinstance(by_tier, dict), "items_by_tier must be a dict (SRC-150)"

    def test_items_by_source_class_populated(
        self,
        sample_agent_config: AgentConfig,
        sample_secrets,
        prompts_dir: Path,
        tmp_path: Path,
    ) -> None:
        """metadata.items_by_source_class tracks web vs twitter (SRC-150)."""
        from ai_news_agent.storage.tinydb_store import TinyDBArticleStore

        store = TinyDBArticleStore(tmp_path / "store.json")

        # Insert a candidate in the daily window so scorer is not short-circuited
        store.insert_if_new(
            _make_article(
                agent_id=sample_agent_config.agent_id,
                pub_date=datetime(2026, 5, 9, 10, 0, tzinfo=UTC),
            )
        )

        payload = _build_llm_payload(
            items=[
                {
                    "headline": "Web Article",
                    "source_name": "Reuters",
                    "url": "https://reuters.com/web",
                    "pub_date": "2026-05-09",
                    "why_it_matters": "Matters.",
                    "impact_tags": ["business_impact"],
                    "tier": "1b",
                    "cross_refs": [],
                    "twitter_handle": None,
                    "tweet_url": None,
                },
                {
                    "headline": "Twitter Article",
                    "source_name": "Twitter/X",
                    "url": "https://twitter.com/sama/status/1",
                    "pub_date": "2026-05-09",
                    "why_it_matters": "Executive announcement.",
                    "impact_tags": ["business_impact"],
                    "tier": "1b",
                    "cross_refs": [],
                    "twitter_handle": "sama",
                    "tweet_url": "https://twitter.com/sama/status/1",
                },
            ]
        )
        with patch("ai_news_agent.curation.agent.get_llm_client") as mock_factory:
            mock_factory.return_value = DummyLLMClient(complete_response=payload)
            agent = CurationAgent(
                config=sample_agent_config,
                secrets=sample_secrets,
                store=store,
                prompts_dir=str(prompts_dir),
            )
            result = agent.run(
                cadence="daily",
                reference_time=datetime(2026, 5, 10, tzinfo=UTC),
            )

        by_class = result.metadata.items_by_source_class
        assert isinstance(by_class, dict)
        # Two items returned: one web (handle=None → "web"), one twitter (handle="sama")
        assert "twitter" in by_class, (
            "Twitter-sourced item (twitter_handle='sama') must be counted under 'twitter' (SRC-150)"
        )
        assert "web" in by_class, (
            "Web-sourced item (twitter_handle=None) must be counted under 'web' (SRC-150)"
        )

    def test_tweet_api_call_count_reflects_signals(
        self,
        sample_agent_config: AgentConfig,
        sample_secrets,
        prompts_dir: Path,
        tmp_path: Path,
    ) -> None:
        """tweet_api_call_count equals the number of tweet signals in the window (SRC-150)."""
        from ai_news_agent.storage.tinydb_store import TinyDBArticleStore

        store = TinyDBArticleStore(tmp_path / "store.json")

        for i in range(3):
            store.insert_tweet_signal(
                TweetSignal(
                    tweet_id=f"tweet-{i}",
                    handle="karpathy",
                    text=f"Tweet {i}",
                    created_at=datetime(2026, 5, 9, 10 + i, 0, tzinfo=UTC),
                    linked_urls=[],
                    agent_id=sample_agent_config.agent_id,
                    fetched_at=datetime(2026, 5, 9, 10 + i, 5, tzinfo=UTC),
                    weight=1.0,
                )
            )

        with patch("ai_news_agent.curation.agent.get_llm_client") as mock_factory:
            mock_factory.return_value = DummyLLMClient()
            agent = CurationAgent(
                config=sample_agent_config,
                secrets=sample_secrets,
                store=store,
                prompts_dir=str(prompts_dir),
            )
            result = agent.run(
                cadence="daily",
                reference_time=datetime(2026, 5, 10, tzinfo=UTC),
            )

        assert result.metadata.tweet_api_call_count == 3, (
            "tweet_api_call_count must equal the number of tweet signals retrieved (SRC-150)"
        )

    def test_token_usage_populated(
        self,
        sample_agent_config: AgentConfig,
        sample_secrets,
        prompts_dir: Path,
        tmp_path: Path,
    ) -> None:
        """metadata.token_usage > 0 after a real LLM call (SRC-150)."""
        from ai_news_agent.storage.tinydb_store import TinyDBArticleStore

        store = TinyDBArticleStore(tmp_path / "store.json")
        store.insert_if_new(
            _make_article(
                agent_id=sample_agent_config.agent_id,
                pub_date=datetime(2026, 5, 9, 10, 0, tzinfo=UTC),
            )
        )

        with patch("ai_news_agent.curation.agent.get_llm_client") as mock_factory:
            mock_factory.return_value = DummyLLMClient()
            agent = CurationAgent(
                config=sample_agent_config,
                secrets=sample_secrets,
                store=store,
                prompts_dir=str(prompts_dir),
            )
            result = agent.run(
                cadence="daily",
                reference_time=datetime(2026, 5, 10, tzinfo=UTC),
            )

        assert result.metadata.token_usage > 0, (
            "metadata.token_usage must be positive when LLM is called (SRC-150)"
        )


# ---------------------------------------------------------------------------
# Part 12: DigestRecord persistence and idempotency (SRC-145)
# ---------------------------------------------------------------------------


class TestDigestRecordPersistence:
    """
    Traces: SRC-129 (prompt_version in DigestRecord),
            SRC-145 (idempotent: re-runs overwrite cleanly)
    """

    def test_digest_record_persisted_after_run(
        self,
        sample_agent_config: AgentConfig,
        sample_secrets,
        prompts_dir: Path,
        tmp_path: Path,
    ) -> None:
        """DigestRecord is written to the store after a successful run (SRC-145)."""
        from ai_news_agent.storage.tinydb_store import TinyDBArticleStore

        store = TinyDBArticleStore(tmp_path / "store.json")
        ref = datetime(2026, 5, 10, tzinfo=UTC)

        with patch("ai_news_agent.curation.agent.get_llm_client") as mock_factory:
            mock_factory.return_value = DummyLLMClient()
            agent = CurationAgent(
                config=sample_agent_config,
                secrets=sample_secrets,
                store=store,
                prompts_dir=str(prompts_dir),
            )
            agent.run(cadence="daily", reference_time=ref)

        record = store.get_digest(
            agent_id=sample_agent_config.agent_id,
            cadence="daily",
        )
        assert record is not None, "DigestRecord must be persisted after a run (SRC-145)"

    def test_digest_record_contains_prompt_version(
        self,
        sample_agent_config: AgentConfig,
        sample_secrets,
        prompts_dir: Path,
        tmp_path: Path,
    ) -> None:
        """DigestRecord.prompt_version is the SHA-256 hash (SRC-129)."""
        from ai_news_agent.storage.tinydb_store import TinyDBArticleStore

        store = TinyDBArticleStore(tmp_path / "store.json")
        ref = datetime(2026, 5, 10, tzinfo=UTC)

        with patch("ai_news_agent.curation.agent.get_llm_client") as mock_factory:
            mock_factory.return_value = DummyLLMClient()
            agent = CurationAgent(
                config=sample_agent_config,
                secrets=sample_secrets,
                store=store,
                prompts_dir=str(prompts_dir),
            )
            agent.run(cadence="daily", reference_time=ref)

        record = store.get_digest(agent_id=sample_agent_config.agent_id, cadence="daily")
        assert record is not None
        assert record.prompt_version.startswith("sha256:"), (
            "DigestRecord.prompt_version must be SHA-256 hash (SRC-129)"
        )

    def test_digest_record_idempotent_on_rerun(
        self,
        sample_agent_config: AgentConfig,
        sample_secrets,
        prompts_dir: Path,
        tmp_path: Path,
    ) -> None:
        """Running curation twice for same date writes only one record (SRC-145)."""
        from ai_news_agent.storage.tinydb_store import TinyDBArticleStore

        store = TinyDBArticleStore(tmp_path / "store.json")
        ref = datetime(2026, 5, 10, tzinfo=UTC)

        with patch("ai_news_agent.curation.agent.get_llm_client") as mock_factory:
            mock_factory.return_value = DummyLLMClient()
            agent = CurationAgent(
                config=sample_agent_config,
                secrets=sample_secrets,
                store=store,
                prompts_dir=str(prompts_dir),
            )
            agent.run(cadence="daily", reference_time=ref)
            agent.run(cadence="daily", reference_time=ref)

        all_digests = store.list_digests(agent_id=sample_agent_config.agent_id, cadence="daily")
        assert len(all_digests) == 1, (
            "Re-running for the same date produces exactly one DigestRecord (SRC-145)"
        )

    def test_different_cadences_produce_separate_records(
        self,
        sample_agent_config: AgentConfig,
        sample_secrets,
        prompts_dir: Path,
        tmp_path: Path,
    ) -> None:
        """Daily and weekly runs produce separate DigestRecords (SRC-029, SRC-030)."""
        from ai_news_agent.storage.tinydb_store import TinyDBArticleStore

        store = TinyDBArticleStore(tmp_path / "store.json")

        with patch("ai_news_agent.curation.agent.get_llm_client") as mock_factory:
            mock_factory.return_value = DummyLLMClient()
            agent = CurationAgent(
                config=sample_agent_config,
                secrets=sample_secrets,
                store=store,
                prompts_dir=str(prompts_dir),
            )
            agent.run(cadence="daily", reference_time=datetime(2026, 5, 10, tzinfo=UTC))
            agent.run(cadence="weekly", reference_time=datetime(2026, 5, 11, tzinfo=UTC))

        all_digests = store.list_digests(agent_id=sample_agent_config.agent_id)
        cadences = {d.cadence for d in all_digests}
        assert "daily" in cadences
        assert "weekly" in cadences


# ---------------------------------------------------------------------------
# Part 13: Twitter degradation — API-down vs quiet-window (SRC-148)
# ---------------------------------------------------------------------------


class TestTwitterDegradation:
    """
    Traces: SRC-148 (Twitter API failure: produce digest from web, add note)
    """

    def test_explicit_false_sets_degradation_note(
        self,
        sample_agent_config: AgentConfig,
        sample_secrets,
        prompts_dir: Path,
        tmp_path: Path,
    ) -> None:
        """Explicit twitter_api_available=False sets degradation note (SRC-148)."""
        from ai_news_agent.storage.tinydb_store import TinyDBArticleStore

        store = TinyDBArticleStore(tmp_path / "store.json")
        with patch("ai_news_agent.curation.agent.get_llm_client") as mock_factory:
            mock_factory.return_value = DummyLLMClient()
            agent = CurationAgent(
                config=sample_agent_config,
                secrets=sample_secrets,
                store=store,
                prompts_dir=str(prompts_dir),
            )
            result = agent.run(
                cadence="daily",
                twitter_api_available=False,
            )
        assert result.twitter_degradation_note is not None
        assert result.metadata.twitter_signal_available is False

    def test_explicit_true_clears_degradation_note(
        self,
        sample_agent_config: AgentConfig,
        sample_secrets,
        prompts_dir: Path,
        tmp_path: Path,
    ) -> None:
        """Explicit twitter_api_available=True clears degradation note (SRC-148)."""
        from ai_news_agent.storage.tinydb_store import TinyDBArticleStore

        store = TinyDBArticleStore(tmp_path / "store.json")
        with patch("ai_news_agent.curation.agent.get_llm_client") as mock_factory:
            mock_factory.return_value = DummyLLMClient()
            agent = CurationAgent(
                config=sample_agent_config,
                secrets=sample_secrets,
                store=store,
                prompts_dir=str(prompts_dir),
            )
            result = agent.run(
                cadence="daily",
                twitter_api_available=True,
            )
        # API explicitly available → no degradation note regardless of signal count
        assert result.twitter_degradation_note is None
        assert result.metadata.twitter_signal_available is True

    def test_inferred_false_when_no_signals_in_store(
        self,
        sample_agent_config: AgentConfig,
        sample_secrets,
        prompts_dir: Path,
        tmp_path: Path,
    ) -> None:
        """No signals in store → inferred as unavailable (quiet window or API down) (SRC-148)."""
        from ai_news_agent.storage.tinydb_store import TinyDBArticleStore

        store = TinyDBArticleStore(tmp_path / "store.json")
        with patch("ai_news_agent.curation.agent.get_llm_client") as mock_factory:
            mock_factory.return_value = DummyLLMClient()
            agent = CurationAgent(
                config=sample_agent_config,
                secrets=sample_secrets,
                store=store,
                prompts_dir=str(prompts_dir),
            )
            result = agent.run(cadence="daily", twitter_api_available=None)
        # No signals → inferred as unavailable
        assert result.metadata.twitter_signal_available is False
        assert result.twitter_degradation_note is not None

    def test_degradation_note_present_in_result_fields(
        self,
        sample_agent_config: AgentConfig,
        sample_secrets,
        prompts_dir: Path,
        tmp_path: Path,
    ) -> None:
        """Degradation note text references SRC-148 or 'unavailable' (SRC-148)."""
        from ai_news_agent.storage.tinydb_store import TinyDBArticleStore

        store = TinyDBArticleStore(tmp_path / "store.json")
        with patch("ai_news_agent.curation.agent.get_llm_client") as mock_factory:
            mock_factory.return_value = DummyLLMClient()
            agent = CurationAgent(
                config=sample_agent_config,
                secrets=sample_secrets,
                store=store,
                prompts_dir=str(prompts_dir),
            )
            result = agent.run(cadence="daily", twitter_api_available=False)
        note = result.twitter_degradation_note
        assert note is not None
        assert "unavailable" in note.lower() or "SRC-148" in note


# ---------------------------------------------------------------------------
# Part 14: Prompt builder integration — all cadences (SRC-115–SRC-124)
# ---------------------------------------------------------------------------


class TestPromptBuilderAllCadences:
    """
    Verify PromptBuilder produces valid prompts for all four cadences.
    Traces: SRC-113, SRC-115–SRC-124, SRC-129
    """

    @pytest.mark.parametrize("cadence", ["daily", "weekly", "monthly", "annual"])
    def test_prompt_built_for_each_cadence(self, cadence: str, prompts_dir: Path) -> None:
        """Prompt builder produces a non-empty prompt for every cadence (SRC-113)."""
        builder = PromptBuilder(prompts_dir=prompts_dir)
        start = datetime(2026, 5, 1, tzinfo=UTC)
        end = datetime(2026, 5, 31, tzinfo=UTC)
        prompt, version = builder.build(
            cadence=cadence,  # type: ignore[arg-type]
            window_start=start,
            window_end=end,
            tweet_signals=[],
            top_n=10,
        )
        assert len(prompt) > 0
        assert version.startswith("sha256:")

    def test_iso_dates_injected(self, prompts_dir: Path) -> None:
        """Concrete ISO dates appear in the prompt — never 'last week' (SRC-116)."""
        builder = PromptBuilder(prompts_dir=prompts_dir)
        start = datetime(2026, 4, 1, tzinfo=UTC)
        end = datetime(2026, 4, 30, tzinfo=UTC)
        prompt, _ = builder.build(
            cadence="monthly",
            window_start=start,
            window_end=end,
            tweet_signals=[],
            top_n=10,
        )
        assert "2026-04-01" in prompt, "window_start_iso must appear in prompt (SRC-116)"
        assert "2026-04-30" in prompt, "window_end_iso must appear in prompt (SRC-116)"
        assert "{{" not in prompt, "All placeholders must be substituted"

    def test_twitter_signal_section_present(
        self, prompts_dir: Path, sample_tweet_signal: TweetSignal
    ) -> None:
        """Twitter signal section appears in prompt (SRC-119)."""
        builder = PromptBuilder(prompts_dir=prompts_dir)
        prompt, _ = builder.build(
            cadence="daily",
            window_start=datetime(2026, 5, 9, tzinfo=UTC),
            window_end=datetime(2026, 5, 9, 23, tzinfo=UTC),
            tweet_signals=[sample_tweet_signal],
            top_n=10,
        )
        assert "Influencer Signal" in prompt, "Influencer section required (SRC-119)"
        assert "karpathy" in prompt

    def test_search_budget_daily_is_normal(self, prompts_dir: Path) -> None:
        """Daily cadence includes normal search budget (SRC-121)."""
        builder = PromptBuilder(prompts_dir=prompts_dir)
        prompt, _ = builder.build(
            cadence="daily",
            window_start=datetime(2026, 5, 9, tzinfo=UTC),
            window_end=datetime(2026, 5, 9, 23, tzinfo=UTC),
            tweet_signals=[],
            top_n=10,
        )
        assert "normal" in prompt.lower(), "Daily budget directive must say 'normal' (SRC-121)"

    def test_search_budget_annual_is_deepest(self, prompts_dir: Path) -> None:
        """Annual cadence includes the deepest search budget (SRC-121)."""
        builder = PromptBuilder(prompts_dir=prompts_dir)
        prompt, _ = builder.build(
            cadence="annual",
            window_start=datetime(2025, 1, 1, tzinfo=UTC),
            window_end=datetime(2025, 12, 31, tzinfo=UTC),
            tweet_signals=[],
            top_n=10,
        )
        assert "deep" in prompt.lower(), "Annual budget directive must mention 'deep' (SRC-121)"

    def test_annual_year_variables(self, prompts_dir: Path) -> None:
        """Annual prompt has both reviewed year and upcoming year (SRC-124)."""
        builder = PromptBuilder(prompts_dir=prompts_dir)
        prompt, _ = builder.build(
            cadence="annual",
            window_start=datetime(2025, 1, 1, tzinfo=UTC),
            window_end=datetime(2025, 12, 31, tzinfo=UTC),
            tweet_signals=[],
            top_n=10,
        )
        assert "2025" in prompt, "Reviewed year must appear in annual prompt (SRC-124)"
        assert "2026" in prompt, "Upcoming year must appear in annual prompt (SRC-124)"

    def test_twitter_api_down_section_distinguishable(self, prompts_dir: Path) -> None:
        """API-down vs quiet-window produces distinct messages (SRC-148)."""
        builder = PromptBuilder(prompts_dir=prompts_dir)

        # API down
        prompt_down, _ = builder.build(
            cadence="daily",
            window_start=datetime(2026, 5, 9, tzinfo=UTC),
            window_end=datetime(2026, 5, 9, 23, tzinfo=UTC),
            tweet_signals=[],
            top_n=10,
            twitter_api_available=False,
        )

        # API up but quiet window
        prompt_quiet, _ = builder.build(
            cadence="daily",
            window_start=datetime(2026, 5, 9, tzinfo=UTC),
            window_end=datetime(2026, 5, 9, 23, tzinfo=UTC),
            tweet_signals=[],
            top_n=10,
            twitter_api_available=True,
        )

        assert prompt_down != prompt_quiet, (
            "API-down and quiet-window prompts must differ (SRC-148)"
        )


# ---------------------------------------------------------------------------
# Part 15: Candidate formatting for LLM context (SRC-027, SRC-048)
# ---------------------------------------------------------------------------


class TestCandidateFormatting:
    """
    Verify the Scorer formats candidates with all required fields for LLM context.
    Traces: SRC-011, SRC-027, SRC-048
    """

    def test_format_includes_all_tiers(self) -> None:
        """All five tiers formatted correctly when present (SRC-016–SRC-021)."""
        llm = DummyLLMClient()
        scorer = Scorer(llm_client=llm)

        articles = [
            _make_article(url=f"https://example.com/a{i}", tier=t)
            for i, t in enumerate(["1a", "1b", "2", "3", "4"])
        ]
        formatted = scorer._format_candidates(articles)
        for tier in ["1A", "1B", "2", "3", "4"]:
            assert tier in formatted, f"Tier {tier} must appear in formatted candidates"

    def test_format_includes_twitter_provenance(self) -> None:
        """Twitter-sourced articles show handle + tweet URL in formatted output (SRC-048)."""
        llm = DummyLLMClient()
        scorer = Scorer(llm_client=llm)
        article = _make_article(
            twitter_handle="karpathy",
            tweet_url="https://twitter.com/karpathy/status/123",
        )
        formatted = scorer._format_candidates([article])
        assert "karpathy" in formatted
        assert "twitter.com" in formatted

    def test_format_includes_url(self) -> None:
        """URL appears in formatted candidate text (SRC-049)."""
        llm = DummyLLMClient()
        scorer = Scorer(llm_client=llm)
        article = _make_article(url="https://reuters.com/specific-article")
        formatted = scorer._format_candidates([article])
        assert "reuters.com/specific-article" in formatted


# ---------------------------------------------------------------------------
# Part 16: CurationAgent construction and configuration
# ---------------------------------------------------------------------------


class TestCurationAgentConstruction:
    """
    Traces: SRC-054 (configurable research LLM), SRC-071–SRC-073 (config system)
    """

    def test_default_store_is_tinydb(
        self,
        sample_agent_config: AgentConfig,
        sample_secrets,
        prompts_dir: Path,
        tmp_path: Path,
    ) -> None:
        """When no store is provided, TinyDBArticleStore is the default (SRC-053)."""
        from ai_news_agent.storage.tinydb_store import TinyDBArticleStore

        config = sample_agent_config.model_copy(
            update={"output_dir": str(tmp_path / "outputs" / "test-agent")}
        )
        with patch("ai_news_agent.curation.agent.get_llm_client") as mock_factory:
            mock_factory.return_value = DummyLLMClient()
            agent = CurationAgent(
                config=config,
                secrets=sample_secrets,
                prompts_dir=str(prompts_dir),
            )
        assert isinstance(agent._store, TinyDBArticleStore)

    def test_custom_store_injected(
        self,
        sample_agent_config: AgentConfig,
        sample_secrets,
        prompts_dir: Path,
        tiny_db_store,
    ) -> None:
        """Custom store is used when injected (SRC-053)."""
        with patch("ai_news_agent.curation.agent.get_llm_client") as mock_factory:
            mock_factory.return_value = DummyLLMClient()
            agent = CurationAgent(
                config=sample_agent_config,
                secrets=sample_secrets,
                store=tiny_db_store,
                prompts_dir=str(prompts_dir),
            )
        assert agent._store is tiny_db_store

    @pytest.mark.parametrize(
        ("cadence", "expected_top_n"),
        [
            ("daily", 5),
            ("weekly", 5),
            ("monthly", 5),
            ("annual", 5),
        ],
    )
    def test_top_n_resolved_from_config(
        self,
        cadence: str,
        expected_top_n: int,
        sample_agent_config: AgentConfig,
        sample_secrets,
        prompts_dir: Path,
        tmp_path: Path,
    ) -> None:
        """top_n is resolved from config per cadence (SRC-029–SRC-032)."""
        from ai_news_agent.storage.tinydb_store import TinyDBArticleStore

        store = TinyDBArticleStore(tmp_path / "store.json")

        captured: dict[str, int] = {}
        original_build = PromptBuilder.build

        def capturing_build(self_pb, **kwargs):  # type: ignore[override]
            captured["top_n"] = kwargs.get("top_n", -1)
            return original_build(self_pb, **kwargs)

        with patch("ai_news_agent.curation.agent.get_llm_client") as mock_factory:
            mock_factory.return_value = DummyLLMClient()
            with patch.object(PromptBuilder, "build", capturing_build):
                agent = CurationAgent(
                    config=sample_agent_config,
                    secrets=sample_secrets,
                    store=store,
                    prompts_dir=str(prompts_dir),
                )
                agent.run(
                    cadence=cadence,
                    reference_time=datetime(2026, 5, 10, tzinfo=UTC),
                )

        assert captured["top_n"] == expected_top_n, (
            f"top_n for {cadence} should be {expected_top_n} per config"
        )


# ---------------------------------------------------------------------------
# Part 17: Cadence-specific model routing — research LLM (SRC-054)
# ---------------------------------------------------------------------------


class TestModelRouting:
    """
    Traces: SRC-054 (configurable research LLM for monthly/annual)
    """

    def test_default_model_used_for_daily(
        self,
        sample_agent_config: AgentConfig,
        sample_secrets,
        prompts_dir: Path,
        tmp_path: Path,
    ) -> None:
        """Daily uses default model (gpt-4o) when no cadence override configured."""
        from ai_news_agent.storage.tinydb_store import TinyDBArticleStore

        store = TinyDBArticleStore(tmp_path / "store.json")
        with patch("ai_news_agent.curation.agent.get_llm_client") as mock_factory:
            mock_factory.return_value = DummyLLMClient()
            agent = CurationAgent(
                config=sample_agent_config,
                secrets=sample_secrets,
                store=store,
                prompts_dir=str(prompts_dir),
            )
            result = agent.run(
                cadence="daily",
                reference_time=datetime(2026, 5, 10, tzinfo=UTC),
            )
        assert result.metadata.llm_model == "gpt-4o"

    def test_override_model_used_for_annual(
        self,
        sample_secrets,
        prompts_dir: Path,
        tmp_path: Path,
    ) -> None:
        """Annual uses o3 override model (SRC-054)."""
        from ai_news_agent.config.models import SourcesConfig, TwitterConfig
        from ai_news_agent.storage.tinydb_store import TinyDBArticleStore

        config = AgentConfig(
            agent_id="test-override",
            llm=LLMConfig(
                provider="openai",
                model="gpt-4o",
                cadence_overrides={"annual": LLMCadenceOverride(model="o3", thinking=True)},
            ),
            sources=SourcesConfig(),
            twitter=TwitterConfig(enabled=False, handles=[]),
            limits=LimitsConfig(),
            output_dir=str(tmp_path / "outputs/test-override"),
        )
        store = TinyDBArticleStore(tmp_path / "store.json")
        with patch("ai_news_agent.curation.agent.get_llm_client") as mock_factory:
            mock_factory.return_value = DummyLLMClient()
            agent = CurationAgent(
                config=config,
                secrets=sample_secrets,
                store=store,
                prompts_dir=str(prompts_dir),
            )
            result = agent.run(
                cadence="annual",
                reference_time=datetime(2026, 1, 1, tzinfo=UTC),
            )
        assert result.metadata.llm_model == "o3"

    def test_no_override_for_weekly_uses_default(
        self,
        sample_agent_config: AgentConfig,
        sample_secrets,
        prompts_dir: Path,
        tmp_path: Path,
    ) -> None:
        """Weekly without override uses default model (SRC-054)."""
        from ai_news_agent.storage.tinydb_store import TinyDBArticleStore

        store = TinyDBArticleStore(tmp_path / "store.json")
        with patch("ai_news_agent.curation.agent.get_llm_client") as mock_factory:
            mock_factory.return_value = DummyLLMClient()
            agent = CurationAgent(
                config=sample_agent_config,
                secrets=sample_secrets,
                store=store,
                prompts_dir=str(prompts_dir),
            )
            result = agent.run(
                cadence="weekly",
                reference_time=datetime(2026, 5, 11, tzinfo=UTC),
            )
        assert result.metadata.llm_model == "gpt-4o"
