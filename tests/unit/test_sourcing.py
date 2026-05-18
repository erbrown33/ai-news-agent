"""
tests/unit/test_sourcing.py — Sourcing Agent: full behaviour coverage.

Covers:
  - Tier classification (SRC-016–SRC-021)
  - URL enforcement: no-URL articles dropped (SRC-049)
  - Intra-run deduplication (SRC-012)
  - Pub-date extraction from snippets (SRC-011)
  - Tier query construction, including Tier 1a custom sources (SRC-017)
  - WebFetcher.fetch_all: tier refinement, per-run dedup (SRC-012, SRC-016–SRC-021)
  - WebFetcher.fetch_from_tweet_urls: tweet lead-generation pipeline (SRC-069–SRC-070)
  - Headline extraction (_extract_headline) (SRC-011)
  - SourcingAgent.run: happy path, duplicate detection, Twitter degradation (SRC-012, SRC-148)
  - SourcingAgent.run: tweet URL enrichment wired into web fetch (SRC-069–SRC-070)
  - SourcingAgent.run: monitoring log fields (SRC-150)
  - Strict sourcing-only contract: no curation calls (SRC-013)
  - CLI entry point: argument parsing (SRC-076–SRC-077)

Traces: SRC-006–SRC-013, SRC-016–SRC-021, SRC-033–SRC-053, SRC-047, SRC-049,
        SRC-062–SRC-070, SRC-098 (mocked HTTP/LLM), SRC-148, SRC-150
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING
from unittest.mock import MagicMock, patch

import pytest

from ai_news_agent.sourcing.agent import SourcingAgent, SourcingRunResult
from ai_news_agent.sourcing.web_fetcher import (
    WebFetcher,
    _classify_tier,
    _extract_headline,
    _extract_pub_date,
    _search_result_to_record,
)

if TYPE_CHECKING:
    from ai_news_agent.config.models import AgentConfig
    from ai_news_agent.storage.models import ArticleRecord, TweetSignal


# ===========================================================================
# Tier classification (SRC-016–SRC-021)
# ===========================================================================

class TestTierClassification:
    """
    Validate URL-to-tier mapping across all five tiers and edge cases.
    Traces: SRC-016–SRC-021
    """

    def test_reuters_is_tier_1b(self, sample_agent_config: AgentConfig) -> None:
        """reuters.com maps to Tier 1b — popular business press (SRC-018)."""
        assert _classify_tier("https://reuters.com/article", sample_agent_config) == "1b"

    def test_bloomberg_is_tier_1b(self, sample_agent_config: AgentConfig) -> None:
        """bloomberg.com maps to Tier 1b (SRC-018)."""
        assert _classify_tier("https://bloomberg.com/ai-news", sample_agent_config) == "1b"

    def test_www_prefix_stripped_before_match(self, sample_agent_config: AgentConfig) -> None:
        """www. prefix is stripped so www.reuters.com still matches reuters.com."""
        assert _classify_tier("https://www.reuters.com/article", sample_agent_config) == "1b"

    def test_subdomain_matches_tier_domain(self, sample_agent_config: AgentConfig) -> None:
        """Subdomain (e.g. blog.reuters.com) still matches base domain (SRC-018)."""
        assert _classify_tier("https://blog.reuters.com/post", sample_agent_config) == "1b"

    def test_openai_blog_is_tier_2(self, sample_agent_config: AgentConfig) -> None:
        """openai.com maps to Tier 2 — top AI blogs (SRC-019)."""
        assert _classify_tier("https://openai.com/blog/gpt5", sample_agent_config) == "2"

    def test_anthropic_is_tier_2(self, sample_agent_config: AgentConfig) -> None:
        """anthropic.com maps to Tier 2 (SRC-019)."""
        assert _classify_tier("https://anthropic.com/research", sample_agent_config) == "2"

    def test_techcrunch_is_tier_3(self, sample_agent_config: AgentConfig) -> None:
        """techcrunch.com maps to Tier 3 — tech business press (SRC-020)."""
        assert _classify_tier("https://techcrunch.com/article", sample_agent_config) == "3"

    def test_brookings_is_tier_4(self, sample_agent_config: AgentConfig) -> None:
        """brookings.edu maps to Tier 4 — policy/research (SRC-021)."""
        assert _classify_tier("https://brookings.edu/report", sample_agent_config) == "4"

    def test_unknown_domain_returns_unknown(self, sample_agent_config: AgentConfig) -> None:
        """Unrecognised domain returns 'unknown' (not an error — just unclassified)."""
        assert _classify_tier("https://obscure-blog.example.com/post", sample_agent_config) == "unknown"

    def test_custom_tier_1a_takes_priority(self, sample_agent_config: AgentConfig) -> None:
        """Tier 1a custom sources have highest priority over all other tiers (SRC-017)."""
        # Set a custom domain that could also match another tier
        new_sources = sample_agent_config.sources.model_copy(
            update={"custom": ["custom.example.com", "reuters.com"]}
        )
        config = sample_agent_config.model_copy(update={"sources": new_sources})
        # reuters.com is in both custom (1a) and tier_1b — 1a wins
        assert _classify_tier("https://reuters.com/article", config) == "1a"

    def test_custom_domain_classified_as_1a(self, sample_agent_config: AgentConfig) -> None:
        """Non-standard domain in custom list → Tier 1a (SRC-017)."""
        new_sources = sample_agent_config.sources.model_copy(
            update={"custom": ["myblog.example.com"]}
        )
        config = sample_agent_config.model_copy(update={"sources": new_sources})
        assert _classify_tier("https://myblog.example.com/post/ai-news", config) == "1a"

    def test_malformed_url_returns_unknown(self, sample_agent_config: AgentConfig) -> None:
        """Malformed URL (no netloc) gracefully returns 'unknown' without raising."""
        assert _classify_tier("not-a-url", sample_agent_config) == "unknown"

    def test_empty_url_returns_unknown(self, sample_agent_config: AgentConfig) -> None:
        """Empty string URL → 'unknown' (defensive — SRC-049 blocks before storage)."""
        assert _classify_tier("", sample_agent_config) == "unknown"


# ===========================================================================
# Publication date extraction (SRC-011)
# ===========================================================================

class TestPubDateExtraction:
    """
    Validate that _extract_pub_date correctly parses dates from snippets.
    Traces: SRC-011 (pub_date must be non-null on every ArticleRecord)
    """

    def _dt(self, year: int, month: int, day: int) -> datetime:
        return datetime(year, month, day, tzinfo=UTC)

    def test_iso_date_extracted_from_snippet(self) -> None:
        """ISO 8601 date in snippet → parsed into UTC datetime (SRC-011)."""
        fetched = datetime(2026, 5, 10, tzinfo=UTC)
        result = _extract_pub_date("Published 2026-05-09. Major AI announcement.", fetched)
        assert result == self._dt(2026, 5, 9)

    def test_natural_language_date_extracted(self) -> None:
        """Natural-language date (e.g. 'May 8, 2026') extracted correctly (SRC-011)."""
        fetched = datetime(2026, 5, 10, tzinfo=UTC)
        result = _extract_pub_date("Published May 8, 2026. AI reshapes industry.", fetched)
        assert result == self._dt(2026, 5, 8)

    def test_fallback_to_fetched_at_when_no_date(self) -> None:
        """Snippet with no parseable date → fallback to fetched_at (SRC-011)."""
        fetched = datetime(2026, 5, 10, 9, 30, tzinfo=UTC)
        result = _extract_pub_date("No date information here.", fetched)
        assert result == fetched

    def test_none_snippet_falls_back_to_fetched_at(self) -> None:
        """None snippet → fetched_at (SRC-011 — pub_date must always be set)."""
        fetched = datetime(2026, 5, 10, tzinfo=UTC)
        result = _extract_pub_date(None, fetched)
        assert result == fetched

    def test_invalid_date_falls_back_to_fetched_at(self) -> None:
        """Snippet with invalid date values → fetched_at fallback (SRC-011)."""
        fetched = datetime(2026, 5, 10, tzinfo=UTC)
        result = _extract_pub_date("Published 2026-99-99.", fetched)
        assert result == fetched

    def test_date_is_utc_aware(self) -> None:
        """Extracted pub_date is always UTC-aware (SRC-009 — UTC throughout)."""
        fetched = datetime(2026, 5, 10, tzinfo=UTC)
        result = _extract_pub_date("2026-05-09 news about AI.", fetched)
        assert result.tzinfo is not None


# ===========================================================================
# Headline extraction
# ===========================================================================

class TestHeadlineExtraction:
    """
    Validate headline extraction from HTML content.
    Traces: SRC-011 (headline field on ArticleRecord)
    """

    def test_extracts_html_title_tag(self) -> None:
        """<title> tag content used as headline."""
        html = "<html><head><title>OpenAI Releases GPT-5</title></head><body></body></html>"
        result = _extract_headline(html, "https://openai.com/blog")
        assert result == "OpenAI Releases GPT-5"

    def test_extracts_markdown_heading(self) -> None:
        """First Markdown # heading used when no <title> tag found."""
        md = "# AI Market Report\n\nContent here..."
        result = _extract_headline(md, "https://example.com")
        assert result == "AI Market Report"

    def test_falls_back_to_url_when_no_content(self) -> None:
        """Empty content → URL used as fallback headline (non-empty guarantee)."""
        result = _extract_headline(None, "https://reuters.com/article-123")
        assert result == "https://reuters.com/article-123"

    def test_truncates_long_headlines_to_200_chars(self) -> None:
        """Headlines longer than 200 characters are truncated."""
        long_title = "A" * 250
        html = f"<title>{long_title}</title>"
        result = _extract_headline(html, "https://example.com")
        assert len(result) <= 200

    def test_strips_excess_whitespace_from_title(self) -> None:
        """Whitespace in <title> tag is normalised by re.sub(r'\\s+', ' ')."""
        html = "<title>AI   News   Today</title>"
        result = _extract_headline(html, "https://example.com")
        # re.sub(r'\s+', ' ') collapses repeated whitespace to single space
        assert result == "AI News Today"


# ===========================================================================
# SearchResult → ArticleRecord conversion (SRC-011, SRC-012, SRC-049)
# ===========================================================================

class TestSearchResultToRecord:
    """
    Validate the _search_result_to_record conversion function.
    Traces: SRC-011 (storage fields), SRC-012 (url_hash), SRC-049 (URL enforcement)
    """

    def _make_result(self, url: str, title: str = "Title", snippet: str = ""):
        from ai_news_agent.llm.base import SearchResult
        return SearchResult(url=url, title=title, snippet=snippet, source="example.com")

    def test_valid_result_produces_article_record(self) -> None:
        """Valid SearchResult with URL → ArticleRecord with all required fields."""
        from ai_news_agent.storage.models import ArticleRecord
        fetched = datetime(2026, 5, 10, tzinfo=UTC)
        result = _search_result_to_record(
            self._make_result("https://reuters.com/article"),
            tier="1b",
            agent_id="test-agent",
            fetched_at=fetched,
        )
        assert isinstance(result, ArticleRecord)
        assert result.url != ""
        assert result.url_hash != ""
        assert result.tier == "1b"
        assert result.agent_id == "test-agent"
        assert result.source_class == "web"

    def test_empty_url_returns_none(self) -> None:
        """SearchResult with empty URL → None (SRC-049 — URL enforcement)."""
        fetched = datetime(2026, 5, 10, tzinfo=UTC)
        result = _search_result_to_record(
            self._make_result(""),
            tier="1b",
            agent_id="test-agent",
            fetched_at=fetched,
        )
        assert result is None

    def test_tracking_params_stripped_from_url(self) -> None:
        """URL tracking params are stripped for canonical dedup key (SRC-012)."""
        fetched = datetime(2026, 5, 10, tzinfo=UTC)
        record_with_tracking = _search_result_to_record(
            self._make_result("https://reuters.com/article?utm_source=rss&utm_campaign=feed"),
            tier="1b",
            agent_id="test-agent",
            fetched_at=fetched,
        )
        record_clean = _search_result_to_record(
            self._make_result("https://reuters.com/article"),
            tier="1b",
            agent_id="test-agent",
            fetched_at=fetched,
        )
        assert record_with_tracking is not None
        assert record_clean is not None
        # Both should produce the same url_hash after tracking param stripping
        assert record_with_tracking.url_hash == record_clean.url_hash

    def test_pub_date_extracted_from_snippet(self) -> None:
        """Pub date from snippet overrides fetched_at (SRC-011)."""
        fetched = datetime(2026, 5, 10, tzinfo=UTC)
        record = _search_result_to_record(
            self._make_result(
                "https://reuters.com/article",
                snippet="Published 2026-05-08. AI news.",
            ),
            tier="1b",
            agent_id="test-agent",
            fetched_at=fetched,
        )
        assert record is not None
        assert record.pub_date.year == 2026
        assert record.pub_date.month == 5
        assert record.pub_date.day == 8


# ===========================================================================
# WebFetcher tier query construction (SRC-016–SRC-021, SRC-116)
# ===========================================================================

class TestTierQueryConstruction:
    """
    Validate that _build_tier_queries generates correct tier labels and
    appends the date suffix (SRC-116).
    Traces: SRC-016–SRC-021 (tier hierarchy), SRC-116 (concrete date range in queries)
    """

    def _make_fetcher(self, config: AgentConfig, dummy_llm) -> WebFetcher:
        from ai_news_agent.llm.search_tools import AbstractSearchTool
        mock_tool = MagicMock(spec=AbstractSearchTool)
        return WebFetcher(config=config, llm_client=dummy_llm, search_tool=mock_tool)

    def test_standard_tiers_1b_to_4_present(
        self, sample_agent_config: AgentConfig, dummy_llm
    ) -> None:
        """All four standard tiers (1b, 2, 3, 4) appear in queries (SRC-018–SRC-021)."""
        fetcher = self._make_fetcher(sample_agent_config, dummy_llm)
        queries = fetcher._build_tier_queries("since:2026-05-09 until:2026-05-10")
        tier_labels = {tier for tier, _ in queries}
        assert "1b" in tier_labels
        assert "2" in tier_labels
        assert "3" in tier_labels
        assert "4" in tier_labels

    def test_date_suffix_appended_to_all_queries(
        self, sample_agent_config: AgentConfig, dummy_llm
    ) -> None:
        """Date suffix appears in every generated query (SRC-116)."""
        fetcher = self._make_fetcher(sample_agent_config, dummy_llm)
        suffix = "since:2026-05-09 until:2026-05-10"
        queries = fetcher._build_tier_queries(suffix)
        for _, q in queries:
            assert suffix in q, f"Date suffix missing from query: {q}"

    def test_custom_domain_generates_tier_1a_site_query(
        self, sample_agent_config: AgentConfig, dummy_llm
    ) -> None:
        """Custom domain in sources.custom → Tier 1a site: query (SRC-017)."""
        new_sources = sample_agent_config.sources.model_copy(
            update={"custom": ["myblog.example.com"]}
        )
        config = sample_agent_config.model_copy(update={"sources": new_sources})
        fetcher = self._make_fetcher(config, dummy_llm)
        queries = fetcher._build_tier_queries("since:2026-05-09 until:2026-05-10")
        tier_1a = [(t, q) for t, q in queries if t == "1a"]
        assert len(tier_1a) == 1
        assert "site:myblog.example.com" in tier_1a[0][1]

    def test_multiple_custom_domains_each_get_own_query(
        self, sample_agent_config: AgentConfig, dummy_llm
    ) -> None:
        """Multiple custom domains each generate a separate Tier 1a query (SRC-017)."""
        new_sources = sample_agent_config.sources.model_copy(
            update={"custom": ["blog-a.com", "blog-b.com"]}
        )
        config = sample_agent_config.model_copy(update={"sources": new_sources})
        fetcher = self._make_fetcher(config, dummy_llm)
        queries = fetcher._build_tier_queries("since:2026-05-09 until:2026-05-10")
        tier_1a = [(t, q) for t, q in queries if t == "1a"]
        assert len(tier_1a) == 2

    def test_no_custom_domains_means_no_tier_1a(
        self, sample_agent_config: AgentConfig, dummy_llm
    ) -> None:
        """Empty custom list → no Tier 1a queries generated (SRC-017)."""
        fetcher = self._make_fetcher(sample_agent_config, dummy_llm)  # custom=[]
        queries = fetcher._build_tier_queries("since:2026-05-09 until:2026-05-10")
        tier_1a = [(t, q) for t, q in queries if t == "1a"]
        assert len(tier_1a) == 0


# ===========================================================================
# WebFetcher.fetch_all (SRC-053, SRC-060, SRC-012, SRC-016–SRC-021, SRC-049)
# ===========================================================================

class TestWebFetcherFetchAll:
    """
    Integration tests for WebFetcher.fetch_all with mocked search tool.
    Traces: SRC-049 (URL enforcement), SRC-053 (configurable fetch),
            SRC-060 (abstract tool use), SRC-012 (intra-run dedup),
            SRC-016–SRC-021 (tier refinement)
    """

    def _make_fetcher(
        self, config: AgentConfig, dummy_llm, search_results=None
    ) -> WebFetcher:
        from ai_news_agent.llm.search_tools import AbstractSearchTool

        mock_tool = MagicMock(spec=AbstractSearchTool)
        if search_results is not None:
            mock_tool.search.return_value = search_results
        else:
            mock_tool.search.return_value = []
        mock_tool.hydrate_url.return_value = None
        return WebFetcher(config=config, llm_client=dummy_llm, search_tool=mock_tool)

    def test_articles_without_url_skipped(
        self, sample_agent_config: AgentConfig, dummy_llm
    ) -> None:
        """SearchResult with empty URL is not converted to ArticleRecord (SRC-049)."""
        from ai_news_agent.llm.base import SearchResult

        fetcher = self._make_fetcher(
            sample_agent_config,
            dummy_llm,
            search_results=[
                SearchResult(url="", title="No URL Article", snippet="...", source="unknown"),
                SearchResult(url="https://reuters.com/valid", title="Valid", snippet="...", source="reuters.com"),
            ],
        )
        articles = fetcher.fetch_all(
            window_start=datetime(2026, 5, 9, tzinfo=UTC),
            window_end=datetime(2026, 5, 9, 23, 59, tzinfo=UTC),
        )
        for article in articles:
            assert article.url != "", "Article with empty URL should have been dropped (SRC-049)"

    def test_duplicate_urls_deduplicated_within_run(
        self, sample_agent_config: AgentConfig, dummy_llm
    ) -> None:
        """Same URL from multiple queries → only one ArticleRecord (SRC-012)."""
        from ai_news_agent.llm.base import SearchResult

        same_url = "https://reuters.com/article-duplicate"
        fetcher = self._make_fetcher(
            sample_agent_config,
            dummy_llm,
            search_results=[
                SearchResult(url=same_url, title="Dup A", snippet="...", source="reuters.com"),
                SearchResult(url=same_url, title="Dup B", snippet="...", source="reuters.com"),
            ],
        )
        articles = fetcher.fetch_all(
            window_start=datetime(2026, 5, 9, tzinfo=UTC),
            window_end=datetime(2026, 5, 9, 23, 59, tzinfo=UTC),
        )
        urls = [a.url for a in articles]
        assert len(urls) == len(set(urls)), "Duplicate URLs must be deduplicated within a run (SRC-012)"

    def test_tier_refinement_applied_to_results(
        self, sample_agent_config: AgentConfig, dummy_llm
    ) -> None:
        """Result URL domain overrides query-context tier (SRC-016–SRC-021)."""
        from ai_news_agent.llm.base import SearchResult

        # The query is for tier "1b" but the URL is openai.com (tier 2)
        from ai_news_agent.llm.search_tools import AbstractSearchTool
        mock_tool = MagicMock(spec=AbstractSearchTool)
        mock_tool.search.return_value = [
            SearchResult(
                url="https://openai.com/blog/update",
                title="OpenAI Update",
                snippet="...",
                source="openai.com",
            )
        ]
        mock_tool.hydrate_url.return_value = None
        fetcher = WebFetcher(config=sample_agent_config, llm_client=dummy_llm, search_tool=mock_tool)

        articles = fetcher.fetch_all(
            window_start=datetime(2026, 5, 9, tzinfo=UTC),
            window_end=datetime(2026, 5, 9, 23, 59, tzinfo=UTC),
        )
        openai_articles = [a for a in articles if "openai.com" in a.url]
        if openai_articles:
            # URL-domain-based tier classification should override query context
            assert openai_articles[0].tier == "2", (
                "openai.com URL should be classified as Tier 2 (SRC-019)"
            )

    def test_search_error_continues_to_next_query(
        self, sample_agent_config: AgentConfig, dummy_llm
    ) -> None:
        """Search tool error for one query doesn't abort the entire fetch (SRC-148)."""
        from ai_news_agent.llm.base import SearchResult
        from ai_news_agent.llm.search_tools import AbstractSearchTool

        call_count = 0

        def mock_search(query: str, n: int):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise RuntimeError("Search API timeout")
            return [SearchResult(url=f"https://reuters.com/article-{call_count}", title="Article", snippet="", source="reuters.com")]

        mock_tool = MagicMock(spec=AbstractSearchTool)
        mock_tool.search.side_effect = mock_search
        mock_tool.hydrate_url.return_value = None

        fetcher = WebFetcher(config=sample_agent_config, llm_client=dummy_llm, search_tool=mock_tool)
        # Should not raise — errors are caught per query
        fetcher.fetch_all(
            window_start=datetime(2026, 5, 9, tzinfo=UTC),
            window_end=datetime(2026, 5, 9, 23, 59, tzinfo=UTC),
        )
        # At least some queries should have succeeded
        assert call_count > 1  # multiple queries were attempted despite first failing

    def test_returns_list_of_article_records(
        self, sample_agent_config: AgentConfig, dummy_llm
    ) -> None:
        """fetch_all always returns a list (may be empty)."""
        from ai_news_agent.storage.models import ArticleRecord
        fetcher = self._make_fetcher(sample_agent_config, dummy_llm, search_results=[])
        articles = fetcher.fetch_all(
            window_start=datetime(2026, 5, 9, tzinfo=UTC),
            window_end=datetime(2026, 5, 9, 23, 59, tzinfo=UTC),
        )
        assert isinstance(articles, list)
        for a in articles:
            assert isinstance(a, ArticleRecord)


# ===========================================================================
# WebFetcher.fetch_from_tweet_urls (SRC-069–SRC-070)
# ===========================================================================

class TestFetchFromTweetUrls:
    """
    Validate the tweet URL lead-generation pipeline.
    Traces: SRC-069 (hydrate linked tweet URLs), SRC-070 (primary reporting discovery)
    """

    def _make_fetcher(self, config: AgentConfig, dummy_llm) -> WebFetcher:
        from ai_news_agent.llm.search_tools import AbstractSearchTool
        mock_tool = MagicMock(spec=AbstractSearchTool)
        mock_tool.search.return_value = []
        mock_tool.hydrate_url.return_value = "<html><title>AI Breakthrough</title></html>"
        return WebFetcher(config=config, llm_client=dummy_llm, search_tool=mock_tool)

    def test_produces_article_records_from_tweet_urls(
        self, sample_agent_config: AgentConfig, dummy_llm
    ) -> None:
        """Tweet URLs produce ArticleRecord objects (SRC-069–SRC-070)."""
        from ai_news_agent.storage.models import ArticleRecord
        fetcher = self._make_fetcher(sample_agent_config, dummy_llm)
        articles = fetcher.fetch_from_tweet_urls(
            urls=["https://reuters.com/ai-story"],
            agent_id="test-agent",
        )
        assert len(articles) >= 1
        assert all(isinstance(a, ArticleRecord) for a in articles)

    def test_duplicate_tweet_urls_deduplicated(
        self, sample_agent_config: AgentConfig, dummy_llm
    ) -> None:
        """Duplicate URLs in the list → only one ArticleRecord (SRC-012)."""
        fetcher = self._make_fetcher(sample_agent_config, dummy_llm)
        articles = fetcher.fetch_from_tweet_urls(
            urls=["https://reuters.com/ai-story", "https://reuters.com/ai-story"],
            agent_id="test-agent",
        )
        urls = [a.url for a in articles]
        assert len(urls) == len(set(urls)), "Duplicate tweet URLs must be deduplicated"

    def test_empty_urls_skipped(
        self, sample_agent_config: AgentConfig, dummy_llm
    ) -> None:
        """Empty/blank strings in url list are skipped gracefully."""
        fetcher = self._make_fetcher(sample_agent_config, dummy_llm)
        articles = fetcher.fetch_from_tweet_urls(
            urls=["", "   ", "https://reuters.com/valid"],
            agent_id="test-agent",
        )
        for a in articles:
            assert a.url != ""

    def test_hydrate_failure_still_produces_stub_record(
        self, sample_agent_config: AgentConfig, dummy_llm
    ) -> None:
        """If hydrate_url returns None, a stub ArticleRecord is still created from the URL."""
        from ai_news_agent.llm.search_tools import AbstractSearchTool
        mock_tool = MagicMock(spec=AbstractSearchTool)
        mock_tool.search.return_value = []
        mock_tool.hydrate_url.return_value = None  # hydration failed

        fetcher = WebFetcher(config=sample_agent_config, llm_client=dummy_llm, search_tool=mock_tool)
        articles = fetcher.fetch_from_tweet_urls(
            urls=["https://techcrunch.com/ai-story"],
            agent_id="test-agent",
        )
        # URL was valid, so we should still get a stub record
        assert len(articles) == 1
        assert articles[0].url != ""

    def test_tweet_url_article_is_source_class_web(
        self, sample_agent_config: AgentConfig, dummy_llm
    ) -> None:
        """Tweet-sourced primary articles are stored with source_class='web' (SRC-047)."""
        fetcher = self._make_fetcher(sample_agent_config, dummy_llm)
        articles = fetcher.fetch_from_tweet_urls(
            urls=["https://reuters.com/ai-story"],
            agent_id="test-agent",
        )
        assert all(a.source_class == "web" for a in articles), (
            "Primary articles from tweet URLs must be source_class='web' — "
            "they are web articles, not tweets (SRC-047)"
        )

    def test_headline_extracted_from_html_title(
        self, sample_agent_config: AgentConfig, dummy_llm
    ) -> None:
        """HTML <title> tag used as headline when hydrate returns HTML content."""
        from ai_news_agent.llm.search_tools import AbstractSearchTool
        mock_tool = MagicMock(spec=AbstractSearchTool)
        mock_tool.search.return_value = []
        mock_tool.hydrate_url.return_value = "<html><title>Reuters AI Report</title></html>"

        fetcher = WebFetcher(config=sample_agent_config, llm_client=dummy_llm, search_tool=mock_tool)
        articles = fetcher.fetch_from_tweet_urls(
            urls=["https://reuters.com/ai-story"],
            agent_id="test-agent",
        )
        assert len(articles) == 1
        assert articles[0].headline == "Reuters AI Report"

    def test_unclassified_domain_defaults_to_tier_3(
        self, sample_agent_config: AgentConfig, dummy_llm
    ) -> None:
        """Tweet URL from unknown domain defaults to Tier 3 (tech press assumption)."""
        fetcher = self._make_fetcher(sample_agent_config, dummy_llm)
        articles = fetcher.fetch_from_tweet_urls(
            urls=["https://unknown-ai-blog.io/article"],
            agent_id="test-agent",
        )
        assert all(a.tier == "3" for a in articles), (
            "Unclassified tweet URL domains should default to Tier 3"
        )

    def test_tier_classified_correctly_when_known_domain(
        self, sample_agent_config: AgentConfig, dummy_llm
    ) -> None:
        """Known domain in tweet URL is classified to correct tier (SRC-016–SRC-021)."""
        fetcher = self._make_fetcher(sample_agent_config, dummy_llm)
        articles = fetcher.fetch_from_tweet_urls(
            urls=["https://reuters.com/article"],
            agent_id="test-agent",
        )
        assert all(a.tier == "1b" for a in articles), (
            "reuters.com tweet URL should be classified as Tier 1b"
        )


# ===========================================================================
# SourcingAgent (mocked sub-components) (SRC-006–SRC-013, SRC-148, SRC-150)
# ===========================================================================

class TestSourcingAgent:
    """
    Tests for SourcingAgent.run with mocked WebFetcher and TwitterFetcher.
    Traces: SRC-006–SRC-013 (sourcing agent responsibilities),
            SRC-012 (dedup), SRC-148 (Twitter degradation),
            SRC-150 (quality monitoring)
    """

    @staticmethod
    def _make_agent(
        config: AgentConfig,
        secrets,
        store,
        web_articles=None,
        tweet_signals=None,
        twitter_available: bool = True,
        tweet_url_articles=None,
    ) -> SourcingAgent:
        """
        Construct a SourcingAgent with all sub-components mocked.

        Args:
            tweet_url_articles: Articles returned by fetch_from_tweet_urls;
                                 defaults to [] if None.
        """
        with (
            patch("ai_news_agent.sourcing.agent.get_llm_client"),
            patch("ai_news_agent.sourcing.agent.get_search_tool"),
            patch("ai_news_agent.sourcing.agent.WebFetcher") as MockWeb,
            patch("ai_news_agent.sourcing.agent.TwitterFetcher") as MockTwitter,
        ):
            MockWeb.return_value.fetch_all.return_value = web_articles or []
            MockWeb.return_value.fetch_from_tweet_urls.return_value = tweet_url_articles or []
            MockTwitter.return_value.fetch.return_value = (
                tweet_signals or [],
                twitter_available,
            )
            agent = SourcingAgent(config=config, secrets=secrets, store=store)
            agent._web_fetcher = MockWeb.return_value
            agent._twitter_fetcher = MockTwitter.return_value
        return agent

    def test_run_returns_sourcing_result(
        self,
        sample_agent_config: AgentConfig,
        sample_secrets,
        tiny_db_store,
        sample_article: ArticleRecord,
    ) -> None:
        """SourcingAgent.run returns a SourcingRunResult (SRC-006–SRC-013)."""
        agent = self._make_agent(
            config=sample_agent_config,
            secrets=sample_secrets,
            store=tiny_db_store,
            web_articles=[sample_article],
        )
        result = agent.run(
            window_start=datetime(2026, 5, 9, tzinfo=UTC),
            window_end=datetime(2026, 5, 9, 23, 59, tzinfo=UTC),
        )
        assert isinstance(result, SourcingRunResult)
        assert result.articles_fetched == 1
        assert result.articles_inserted == 1
        assert result.articles_duplicate == 0

    def test_duplicate_article_not_reinserted(
        self,
        sample_agent_config: AgentConfig,
        sample_secrets,
        tiny_db_store,
        sample_article: ArticleRecord,
    ) -> None:
        """Second run with same article increments articles_duplicate (SRC-012)."""
        tiny_db_store.insert_if_new(sample_article)  # pre-insert

        agent = self._make_agent(
            config=sample_agent_config,
            secrets=sample_secrets,
            store=tiny_db_store,
            web_articles=[sample_article],
        )
        result = agent.run(
            window_start=datetime(2026, 5, 9, tzinfo=UTC),
            window_end=datetime(2026, 5, 9, 23, 59, tzinfo=UTC),
        )
        assert result.articles_duplicate == 1
        assert result.articles_inserted == 0

    def test_twitter_unavailable_sets_degradation_flag(
        self,
        sample_agent_config: AgentConfig,
        sample_secrets,
        tiny_db_store,
    ) -> None:
        """SourcingRunResult.twitter_signal_available = False when Twitter fails (SRC-148)."""
        agent = self._make_agent(
            config=sample_agent_config,
            secrets=sample_secrets,
            store=tiny_db_store,
            web_articles=[],
            twitter_available=False,
        )
        result = agent.run(
            window_start=datetime(2026, 5, 9, tzinfo=UTC),
            window_end=datetime(2026, 5, 9, 23, 59, tzinfo=UTC),
        )
        assert result.twitter_signal_available is False

    def test_sourcing_continues_after_twitter_failure(
        self,
        sample_agent_config: AgentConfig,
        sample_secrets,
        tiny_db_store,
        sample_article: ArticleRecord,
    ) -> None:
        """Sourcing run completes normally even when Twitter is unavailable (SRC-148)."""
        agent = self._make_agent(
            config=sample_agent_config,
            secrets=sample_secrets,
            store=tiny_db_store,
            web_articles=[sample_article],
            twitter_available=False,
        )
        # Must not raise
        result = agent.run(
            window_start=datetime(2026, 5, 9, tzinfo=UTC),
            window_end=datetime(2026, 5, 9, 23, 59, tzinfo=UTC),
        )
        assert result.articles_inserted == 1, (
            "Web article must still be inserted even when Twitter is unavailable (SRC-148)"
        )

    def test_sourcing_is_strictly_not_curation(
        self,
        sample_agent_config: AgentConfig,
        sample_secrets,
        tiny_db_store,
        sample_article: ArticleRecord,
    ) -> None:
        """SourcingAgent does not call any curation-related methods (SRC-013)."""
        from ai_news_agent.curation.agent import CurationAgent

        agent = self._make_agent(
            config=sample_agent_config,
            secrets=sample_secrets,
            store=tiny_db_store,
            web_articles=[sample_article],
        )
        with patch.object(CurationAgent, "run") as mock_curate:
            agent.run()

        mock_curate.assert_not_called()

    def test_monitoring_fields_populated(
        self,
        sample_agent_config: AgentConfig,
        sample_secrets,
        tiny_db_store,
        sample_article: ArticleRecord,
    ) -> None:
        """SourcingRunResult monitoring fields are populated (SRC-150)."""
        agent = self._make_agent(
            config=sample_agent_config,
            secrets=sample_secrets,
            store=tiny_db_store,
            web_articles=[sample_article],
            twitter_available=True,
        )
        result = agent.run(
            window_start=datetime(2026, 5, 9, tzinfo=UTC),
            window_end=datetime(2026, 5, 9, 23, 59, tzinfo=UTC),
        )
        # SRC-150 monitoring fields
        assert result.agent_id == "test-agent"
        assert isinstance(result.run_at, datetime)
        assert isinstance(result.items_by_tier, dict)
        assert isinstance(result.items_by_source_class, dict)
        assert result.tweet_api_call_count >= 0

    def test_tweet_signals_stored(
        self,
        sample_agent_config: AgentConfig,
        sample_secrets,
        tiny_db_store,
        sample_tweet_signal: TweetSignal,
    ) -> None:
        """Tweet signals from Twitter fetcher are persisted to the store (SRC-067)."""
        agent = self._make_agent(
            config=sample_agent_config,
            secrets=sample_secrets,
            store=tiny_db_store,
            tweet_signals=[sample_tweet_signal],
            twitter_available=True,
        )
        result = agent.run(
            window_start=datetime(2026, 5, 9, tzinfo=UTC),
            window_end=datetime(2026, 5, 9, 23, 59, tzinfo=UTC),
        )
        assert result.tweets_inserted == 1

    def test_duplicate_tweet_signal_not_reinserted(
        self,
        sample_agent_config: AgentConfig,
        sample_secrets,
        tiny_db_store,
        sample_tweet_signal: TweetSignal,
    ) -> None:
        """Duplicate tweet signal on second run is not reinserted (SRC-012 for tweets)."""
        tiny_db_store.insert_tweet_signal(sample_tweet_signal)  # pre-insert

        agent = self._make_agent(
            config=sample_agent_config,
            secrets=sample_secrets,
            store=tiny_db_store,
            tweet_signals=[sample_tweet_signal],
            twitter_available=True,
        )
        result = agent.run(
            window_start=datetime(2026, 5, 9, tzinfo=UTC),
            window_end=datetime(2026, 5, 9, 23, 59, tzinfo=UTC),
        )
        assert result.tweets_inserted == 0  # duplicate — not re-inserted

    def test_tweet_api_call_count_zero_when_unavailable(
        self,
        sample_agent_config: AgentConfig,
        sample_secrets,
        tiny_db_store,
    ) -> None:
        """tweet_api_call_count is 0 when Twitter API is unavailable (SRC-150)."""
        agent = self._make_agent(
            config=sample_agent_config,
            secrets=sample_secrets,
            store=tiny_db_store,
            twitter_available=False,
        )
        result = agent.run(
            window_start=datetime(2026, 5, 9, tzinfo=UTC),
            window_end=datetime(2026, 5, 9, 23, 59, tzinfo=UTC),
        )
        assert result.tweet_api_call_count == 0

    def test_tweet_url_enrichment_combined_with_web_articles(
        self,
        sample_agent_config: AgentConfig,
        sample_secrets,
        tiny_db_store,
        sample_article: ArticleRecord,
        sample_tweet_signal: TweetSignal,
    ) -> None:
        """
        Tweet-linked URL articles are combined with web articles for total count
        (SRC-069–SRC-070).
        """
        from ai_news_agent.storage.models import ArticleRecord, normalize_url, url_hash

        # Create a distinct article that would come from tweet URL hydration
        tweet_url = "https://techcrunch.com/tweet-sourced-article"
        canonical = normalize_url(tweet_url)
        tweet_article = ArticleRecord(
            url_hash=url_hash(canonical),
            url=canonical,
            headline="Tweet-Sourced Article",
            abstract="Sourced via tweet link",
            source_name="techcrunch.com",
            pub_date=datetime(2026, 5, 9, tzinfo=UTC),
            fetched_at=datetime(2026, 5, 9, 10, 0, tzinfo=UTC),
            tier="3",
            source_class="web",
            agent_id="test-agent",
        )

        agent = self._make_agent(
            config=sample_agent_config,
            secrets=sample_secrets,
            store=tiny_db_store,
            web_articles=[sample_article],
            tweet_signals=[sample_tweet_signal],
            twitter_available=True,
            tweet_url_articles=[tweet_article],
        )
        result = agent.run(
            window_start=datetime(2026, 5, 9, tzinfo=UTC),
            window_end=datetime(2026, 5, 9, 23, 59, tzinfo=UTC),
        )
        # Both web article and tweet-URL article should be counted
        assert result.articles_fetched == 2
        assert result.articles_inserted == 2

    def test_twitter_disabled_skips_twitter_fetch(
        self,
        sample_agent_config: AgentConfig,
        sample_secrets,
        tiny_db_store,
    ) -> None:
        """When twitter.enabled=False, Twitter fetcher is not called (SRC-148)."""
        config = sample_agent_config.model_copy(
            update={"twitter": sample_agent_config.twitter.model_copy(update={"enabled": False})}
        )

        with (
            patch("ai_news_agent.sourcing.agent.get_llm_client"),
            patch("ai_news_agent.sourcing.agent.get_search_tool"),
            patch("ai_news_agent.sourcing.agent.WebFetcher") as MockWeb,
            patch("ai_news_agent.sourcing.agent.TwitterFetcher") as MockTwitter,
        ):
            MockWeb.return_value.fetch_all.return_value = []
            MockWeb.return_value.fetch_from_tweet_urls.return_value = []
            MockTwitter.return_value.fetch.return_value = ([], True)

            agent = SourcingAgent(config=config, secrets=sample_secrets, store=tiny_db_store)
            agent._web_fetcher = MockWeb.return_value
            agent._twitter_fetcher = MockTwitter.return_value
            result = agent.run(
                window_start=datetime(2026, 5, 9, tzinfo=UTC),
                window_end=datetime(2026, 5, 9, 23, 59, tzinfo=UTC),
            )

        MockTwitter.return_value.fetch.assert_not_called()
        assert result.twitter_signal_available is False

    def test_window_defaults_to_today_utc(
        self,
        sample_agent_config: AgentConfig,
        sample_secrets,
        tiny_db_store,
    ) -> None:
        """Default window_start is 00:00 UTC today (SRC-009)."""
        agent = self._make_agent(
            config=sample_agent_config,
            secrets=sample_secrets,
            store=tiny_db_store,
        )
        result = agent.run()  # no explicit window
        now = datetime.now(UTC)
        # window_start should be today's 00:00 UTC
        assert result.window_start.date() == now.date()
        assert result.window_start.hour == 0
        assert result.window_start.minute == 0

    def test_run_result_contains_agent_id(
        self,
        sample_agent_config: AgentConfig,
        sample_secrets,
        tiny_db_store,
    ) -> None:
        """SourcingRunResult.agent_id matches the configured agent_id."""
        agent = self._make_agent(
            config=sample_agent_config,
            secrets=sample_secrets,
            store=tiny_db_store,
        )
        result = agent.run()
        assert result.agent_id == sample_agent_config.agent_id


# ===========================================================================
# SourcingAgent with multiple articles + tier distribution (SRC-150)
# ===========================================================================

class TestSourcingRunTierCounts:
    """
    Validate that items_by_tier and items_by_source_class are populated correctly.
    Traces: SRC-150 (quality monitoring — items_by_tier, items_by_source_class)
    """

    def test_items_by_tier_counted_correctly(
        self,
        sample_agent_config: AgentConfig,
        sample_secrets,
        tiny_db_store,
    ) -> None:
        """items_by_tier counts newly inserted articles by tier (SRC-150)."""
        from ai_news_agent.storage.models import ArticleRecord, normalize_url, url_hash

        def make_article(url: str, tier: str) -> ArticleRecord:
            c = normalize_url(url)
            return ArticleRecord(
                url_hash=url_hash(c), url=c, headline=f"Article {url}",
                abstract=None, source_name="test", pub_date=datetime(2026, 5, 9, tzinfo=UTC),
                fetched_at=datetime(2026, 5, 9, tzinfo=UTC),
                tier=tier, source_class="web", agent_id="test-agent",
            )

        articles = [
            make_article("https://reuters.com/a1", "1b"),
            make_article("https://reuters.com/a2", "1b"),
            make_article("https://techcrunch.com/a3", "3"),
        ]

        with (
            patch("ai_news_agent.sourcing.agent.get_llm_client"),
            patch("ai_news_agent.sourcing.agent.get_search_tool"),
            patch("ai_news_agent.sourcing.agent.WebFetcher") as MockWeb,
            patch("ai_news_agent.sourcing.agent.TwitterFetcher") as MockTwitter,
        ):
            MockWeb.return_value.fetch_all.return_value = articles
            MockWeb.return_value.fetch_from_tweet_urls.return_value = []
            MockTwitter.return_value.fetch.return_value = ([], True)

            agent = SourcingAgent(config=sample_agent_config, secrets=sample_secrets, store=tiny_db_store)
            agent._web_fetcher = MockWeb.return_value
            agent._twitter_fetcher = MockTwitter.return_value
            result = agent.run(
                window_start=datetime(2026, 5, 9, tzinfo=UTC),
                window_end=datetime(2026, 5, 9, 23, 59, tzinfo=UTC),
            )

        assert result.items_by_tier.get("1b", 0) == 2
        assert result.items_by_tier.get("3", 0) == 1

    def test_items_by_source_class_counts_web_articles(
        self,
        sample_agent_config: AgentConfig,
        sample_secrets,
        tiny_db_store,
        sample_article: ArticleRecord,
    ) -> None:
        """items_by_source_class counts web articles (SRC-150)."""
        with (
            patch("ai_news_agent.sourcing.agent.get_llm_client"),
            patch("ai_news_agent.sourcing.agent.get_search_tool"),
            patch("ai_news_agent.sourcing.agent.WebFetcher") as MockWeb,
            patch("ai_news_agent.sourcing.agent.TwitterFetcher") as MockTwitter,
        ):
            MockWeb.return_value.fetch_all.return_value = [sample_article]
            MockWeb.return_value.fetch_from_tweet_urls.return_value = []
            MockTwitter.return_value.fetch.return_value = ([], True)

            agent = SourcingAgent(config=sample_agent_config, secrets=sample_secrets, store=tiny_db_store)
            agent._web_fetcher = MockWeb.return_value
            agent._twitter_fetcher = MockTwitter.return_value
            result = agent.run(
                window_start=datetime(2026, 5, 9, tzinfo=UTC),
                window_end=datetime(2026, 5, 9, 23, 59, tzinfo=UTC),
            )

        assert "web" in result.items_by_source_class
        assert result.items_by_source_class["web"] >= 1


# ===========================================================================
# CLI entry point (SRC-076–SRC-077)
# ===========================================================================

class TestCLI:
    """
    Validate CLI argument parsing and exit behaviour.
    Traces: SRC-076 (local dev), SRC-077 (manual trigger)
    """

    def test_cli_exits_0_on_success(
        self,
        sample_agent_config: AgentConfig,
        sample_secrets,
        tmp_path,
    ) -> None:
        """CLI exits with code 0 after a successful sourcing run."""
        with (
            patch("sys.argv", ["ai-news-source", "--agent", "configs/default-agent.yaml"]),
            patch("ai_news_agent.sourcing.agent.load_agent_config") as mock_load,
            patch("ai_news_agent.sourcing.agent.RuntimeSecrets") as mock_secrets,
            patch("ai_news_agent.sourcing.agent.SourcingAgent") as MockAgent,
        ):
            mock_load.return_value = sample_agent_config
            mock_secrets.return_value = sample_secrets
            MockAgent.return_value.run.return_value = SourcingRunResult(
                agent_id="test-cli",
                run_at=datetime(2026, 5, 10, tzinfo=UTC),
                window_start=datetime(2026, 5, 9, tzinfo=UTC),
                window_end=datetime(2026, 5, 10, tzinfo=UTC),
                articles_fetched=5,
                articles_inserted=3,
                articles_duplicate=2,
                tweets_fetched=10,
                tweets_inserted=8,
                twitter_signal_available=True,
                tweet_api_call_count=2,
            )

            from ai_news_agent.sourcing.agent import cli_main
            with pytest.raises(SystemExit) as exc_info:
                cli_main()

        assert exc_info.value.code == 0

    def test_cli_exits_1_on_config_error(self, tmp_path) -> None:
        """CLI exits with code 1 when config loading fails."""
        from ai_news_agent.config.loader import ConfigError
        from ai_news_agent.sourcing.agent import cli_main

        with (
            patch("ai_news_agent.sourcing.agent.load_agent_config") as mock_load,
            patch("sys.argv", ["ai-news-source", "--agent", "nonexistent.yaml"]),
        ):
            mock_load.side_effect = ConfigError("File not found")
            with pytest.raises(SystemExit) as exc_info:
                cli_main()

        assert exc_info.value.code == 1
