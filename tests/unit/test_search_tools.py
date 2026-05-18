"""
tests/unit/test_search_tools.py — Unit tests for LLM search tool implementations.

Coverage matrix
───────────────
SRC-060  AbstractSearchTool interface + provider adapters         → all test classes
SRC-069  hydrate_url for linked tweet URLs                        → TestHydrateUrl
SRC-109  WEB_SEARCH_API_KEY for Brave/Tavily                      → TestBraveSearch, TestTavilySearch
SRC-056  Provider-agnostic: same SearchResult shape from all      → TestSearchResultShape

Traces: SRC-056, SRC-060, SRC-069, SRC-109
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from ai_news_agent.llm.base import SearchResult
from ai_news_agent.llm.search_tools import (
    BraveSearchTool,
    NativeOpenAISearchTool,
    _extract_domain,
)

# ---------------------------------------------------------------------------
# Helper: _extract_domain
# ---------------------------------------------------------------------------


class TestExtractDomain:
    """
    Unit tests for the internal _extract_domain helper.
    Traces: SRC-060 (URL normalisation)
    """

    def test_plain_domain(self) -> None:
        """Standard https URL returns domain without www."""
        assert _extract_domain("https://reuters.com/article/123") == "reuters.com"

    def test_www_stripped(self) -> None:
        """www. prefix is stripped from the returned domain."""
        assert _extract_domain("https://www.bloomberg.com/news") == "bloomberg.com"

    def test_subdomain_preserved(self) -> None:
        """Non-www subdomains are kept intact."""
        assert _extract_domain("https://techcrunch.com/ai") == "techcrunch.com"

    def test_empty_string(self) -> None:
        """Empty URL returns empty string — no exception."""
        assert _extract_domain("") == ""

    def test_invalid_url_returns_empty(self) -> None:
        """Completely unparseable input returns empty string gracefully."""
        result = _extract_domain("not_a_url_at_all")
        assert isinstance(result, str)


# ---------------------------------------------------------------------------
# NativeOpenAISearchTool
# ---------------------------------------------------------------------------


class TestNativeOpenAISearchTool:
    """
    Tests for NativeOpenAISearchTool.

    All external OpenAI API calls are mocked.
    Traces: SRC-060 (native OpenAI search), SRC-069 (hydrate_url)
    """

    def _make_tool(self) -> tuple[NativeOpenAISearchTool, MagicMock]:
        """Return a tool instance with a mocked OpenAI client."""
        mock_client = MagicMock()
        tool = NativeOpenAISearchTool(client=mock_client)
        return tool, mock_client

    # ── search() ────────────────────────────────────────────────────────────

    def test_search_returns_empty_on_api_error(self) -> None:
        """
        When the OpenAI Responses API raises, search() returns [] gracefully.
        Traces: SRC-060
        """
        tool, mock_client = self._make_tool()
        mock_client.responses.create.side_effect = RuntimeError("API unavailable")

        results = tool.search("AI news today", n=5)
        assert results == []

    def test_search_parses_json_output(self) -> None:
        """
        Valid JSON array from the model is parsed into SearchResult objects.
        Traces: SRC-060
        """
        tool, mock_client = self._make_tool()

        # Simulate a Responses API output with a JSON block in text
        json_results = json.dumps(
            [
                {
                    "url": "https://reuters.com/ai-news",
                    "title": "AI Advances",
                    "snippet": "AI is changing...",
                },
                {
                    "url": "https://bloomberg.com/tech",
                    "title": "Tech Wave",
                    "snippet": "Technology...",
                },
            ]
        )
        fake_block = MagicMock()
        fake_block.text = f"```json\n{json_results}\n```"
        fake_content_item = MagicMock()
        fake_content_item.content = [fake_block]
        mock_resp = MagicMock()
        mock_resp.output = [fake_content_item]
        mock_client.responses.create.return_value = mock_resp

        results = tool.search("AI news", n=5)
        assert len(results) >= 1
        assert all(isinstance(r, SearchResult) for r in results)
        assert results[0].url == "https://reuters.com/ai-news"

    def test_search_heuristic_url_extraction(self) -> None:
        """
        When JSON parse fails, heuristic URL extraction still returns results.
        Traces: SRC-060 (fallback parsing)
        """
        tool, mock_client = self._make_tool()

        text_with_urls = (
            "- AI Advances at https://reuters.com/ai-advances\n"
            "- Tech News at https://bloomberg.com/tech-news\n"
        )
        fake_block = MagicMock()
        fake_block.text = text_with_urls
        fake_content_item = MagicMock()
        fake_content_item.content = [fake_block]
        mock_resp = MagicMock()
        mock_resp.output = [fake_content_item]
        mock_client.responses.create.return_value = mock_resp

        results = tool.search("AI news", n=5)
        assert len(results) >= 1
        assert all(r.url.startswith("https://") for r in results)

    def test_search_respects_n_limit(self) -> None:
        """
        search() must not return more than n results.
        Traces: SRC-060
        """
        tool, mock_client = self._make_tool()

        json_results = json.dumps(
            [
                {"url": f"https://example{i}.com", "title": f"Article {i}", "snippet": "Text"}
                for i in range(10)
            ]
        )
        fake_block = MagicMock()
        fake_block.text = f"```json\n{json_results}\n```"
        fake_content_item = MagicMock()
        fake_content_item.content = [fake_block]
        mock_resp = MagicMock()
        mock_resp.output = [fake_content_item]
        mock_client.responses.create.return_value = mock_resp

        results = tool.search("AI news", n=3)
        assert len(results) <= 3

    def test_search_empty_output(self) -> None:
        """
        When the API returns output=None, search() returns [].
        Traces: SRC-060
        """
        tool, mock_client = self._make_tool()
        mock_resp = MagicMock()
        mock_resp.output = None
        mock_client.responses.create.return_value = mock_resp

        results = tool.search("query")
        assert results == []

    def test_search_empty_content_block(self) -> None:
        """
        When content blocks have no text, search() returns [].
        Traces: SRC-060
        """
        tool, mock_client = self._make_tool()

        fake_block = MagicMock()
        fake_block.text = ""
        fake_content_item = MagicMock()
        fake_content_item.content = [fake_block]
        mock_resp = MagicMock()
        mock_resp.output = [fake_content_item]
        mock_client.responses.create.return_value = mock_resp

        results = tool.search("query")
        assert results == []

    # ── _parse_search_block() corner cases ──────────────────────────────────

    def test_parse_block_empty_string(self) -> None:
        """Empty block text returns empty list."""
        tool, _ = self._make_tool()
        assert tool._parse_search_block("") == []

    def test_parse_block_json_without_url(self) -> None:
        """JSON items without 'url' key are skipped."""
        tool, _ = self._make_tool()
        json_data = json.dumps([{"title": "No URL here", "snippet": "..."}])
        results = tool._parse_search_block(f"```json\n{json_data}\n```")
        assert results == []

    def test_parse_block_invalid_json_falls_back(self) -> None:
        """Malformed JSON falls back to regex extraction."""
        tool, _ = self._make_tool()
        text = "Check https://example.com for more information"
        results = tool._parse_search_block(text)
        assert len(results) >= 1
        assert results[0].url == "https://example.com"

    def test_parse_block_markdown_citation_extracts_link_text_as_title(self) -> None:
        """
        ``- **Source:** [Title](url)`` markdown lines must yield ``title=Title``
        (not the malformed ``"Source:** [Title"`` produced by the old line-strip
        heuristic).
        """
        tool, _ = self._make_tool()
        text = (
            "- **Source:** [TechRadar Pro](https://www.techradar.com/pro/example-article)\n"
            "- **Source:** [Reuters](https://www.reuters.com/tech/ai-news)\n"
        )
        results = tool._parse_search_block(text)
        titles = [r.title for r in results]
        urls = [r.url for r in results]
        assert "TechRadar Pro" in titles
        assert "Reuters" in titles
        assert "https://www.techradar.com/pro/example-article" in urls
        assert "https://www.reuters.com/tech/ai-news" in urls

    def test_parse_block_markdown_with_trailing_description_captures_snippet(self) -> None:
        """Description text after a markdown link becomes the snippet."""
        tool, _ = self._make_tool()
        text = (
            "1. [OpenAI launches new model](https://openai.com/blog/example) — "
            "The new model improves coding and reasoning benchmarks.\n"
        )
        results = tool._parse_search_block(text)
        assert len(results) == 1
        assert results[0].title == "OpenAI launches new model"
        assert "improves coding and reasoning" in results[0].snippet

    def test_parse_block_markdown_with_following_description_line(self) -> None:
        """A description on the line after the link becomes the snippet."""
        tool, _ = self._make_tool()
        text = (
            "[Anthropic releases Claude 4.7](https://anthropic.com/news/example)\n"
            "  Anthropic released its newest model focused on long-context reasoning.\n"
        )
        results = tool._parse_search_block(text)
        assert len(results) == 1
        assert results[0].title == "Anthropic releases Claude 4.7"
        assert "long-context reasoning" in results[0].snippet

    def test_parse_block_dedupes_same_url(self) -> None:
        """Repeated URLs in the same block are returned once."""
        tool, _ = self._make_tool()
        text = (
            "- [Title A](https://example.com/article)\n"
            "- [Title A again](https://example.com/article)\n"
        )
        results = tool._parse_search_block(text)
        assert len(results) == 1

    def test_parse_block_markdown_links_array_not_misread_as_json(self) -> None:
        """
        Prose like ``[A](u1) and [B](u2)`` must not be parsed as a JSON array.
        Previously the ``[...]`` regex was too greedy and captured markdown link
        sequences as JSON, then failed silently.
        """
        tool, _ = self._make_tool()
        text = "Results: [Article A](https://example.com/a) and [Article B](https://example.com/b)"
        results = tool._parse_search_block(text)
        titles = [r.title for r in results]
        assert "Article A" in titles
        assert "Article B" in titles

    # ── hydrate_url() ────────────────────────────────────────────────────────

    def test_hydrate_url_returns_none_on_empty(self) -> None:
        """Empty URL returns None without making any HTTP request."""
        tool, _ = self._make_tool()
        assert tool.hydrate_url("") is None

    def test_hydrate_url_success(self) -> None:
        """
        Successful HTTP GET returns first 2000 chars of text.
        Traces: SRC-069 (hydrate linked tweet URLs)
        """
        tool, _ = self._make_tool()
        mock_resp = MagicMock()
        mock_resp.text = "Article content here " * 200
        mock_resp.raise_for_status = MagicMock()

        with patch.object(tool._http, "get", return_value=mock_resp):
            result = tool.hydrate_url("https://reuters.com/article")

        assert result is not None
        assert len(result) <= 2000

    def test_hydrate_url_returns_none_on_error(self) -> None:
        """
        HTTP errors return None gracefully — no exception propagated.
        Traces: SRC-069
        """
        tool, _ = self._make_tool()
        with patch.object(tool._http, "get", side_effect=RuntimeError("timeout")):
            result = tool.hydrate_url("https://reuters.com/article")
        assert result is None

    def test_close(self) -> None:
        """close() closes the underlying httpx client without error."""
        tool, _ = self._make_tool()
        with patch.object(tool._http, "close") as mock_close:
            tool.close()
            mock_close.assert_called_once()


# ---------------------------------------------------------------------------
# BraveSearchTool
# ---------------------------------------------------------------------------


class TestBraveSearchTool:
    """
    Tests for BraveSearchTool (Brave Search API adapter).

    All HTTP calls are mocked via httpx.
    Traces: SRC-060 (Brave fallback), SRC-109 (WEB_SEARCH_API_KEY)
    """

    def _make_tool(self) -> BraveSearchTool:
        return BraveSearchTool(api_key="test-brave-key")

    def _make_web_response(self, results: list[dict[str, Any]]) -> MagicMock:
        """Build a mock httpx Response with Brave 'web' JSON shape."""
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"web": {"results": results}}
        mock_resp.raise_for_status = MagicMock()
        return mock_resp

    # ── search() ────────────────────────────────────────────────────────────

    def test_search_returns_search_results(self) -> None:
        """
        Successful Brave API response returns normalised SearchResult list.
        Traces: SRC-060
        """
        tool = self._make_tool()
        raw = [
            {"url": "https://reuters.com/ai", "title": "AI News", "description": "AI is big"},
            {"url": "https://bloomberg.com/tech", "title": "Tech", "description": "Tech matters"},
        ]
        mock_resp = self._make_web_response(raw)

        with patch.object(tool._http, "get", return_value=mock_resp):
            results = tool.search("AI news", n=5)

        assert len(results) == 2
        assert all(isinstance(r, SearchResult) for r in results)
        assert results[0].url == "https://reuters.com/ai"
        assert results[0].source == "reuters.com"

    def test_search_uses_news_endpoint_for_date_queries(self) -> None:
        """
        Queries with 'since:' or 'until:' use the news endpoint.
        Traces: SRC-060 (recency for news queries)
        """
        tool = self._make_tool()
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"results": []}
        mock_resp.raise_for_status = MagicMock()

        with patch.object(tool._http, "get", return_value=mock_resp) as mock_get:
            tool.search("AI since:2026-05-01", n=5)
            url_called = mock_get.call_args[0][0]
            assert "news" in url_called

    def test_search_uses_web_endpoint_for_general_queries(self) -> None:
        """
        General queries use the standard web search endpoint.
        Traces: SRC-060
        """
        tool = self._make_tool()
        mock_resp = self._make_web_response([])

        with patch.object(tool._http, "get", return_value=mock_resp) as mock_get:
            tool.search("enterprise AI adoption", n=5)
            url_called = mock_get.call_args[0][0]
            assert "web" in url_called

    def test_search_returns_empty_on_api_error(self) -> None:
        """
        HTTP errors return [] gracefully — no exception propagated.
        Traces: SRC-060
        """
        tool = self._make_tool()
        with patch.object(tool._http, "get", side_effect=RuntimeError("connection refused")):
            results = tool.search("AI news", n=5)
        assert results == []

    def test_search_respects_n_limit(self) -> None:
        """
        search() must not return more than n results.
        Traces: SRC-060
        """
        tool = self._make_tool()
        raw = [
            {"url": f"https://example{i}.com", "title": f"Article {i}", "description": "..."}
            for i in range(15)
        ]
        mock_resp = self._make_web_response(raw)

        with patch.object(tool._http, "get", return_value=mock_resp):
            results = tool.search("AI news", n=3)

        assert len(results) <= 3

    def test_search_skips_items_without_url(self) -> None:
        """
        Items without URL or link fields are excluded from results.
        Traces: SRC-049, SRC-060
        """
        tool = self._make_tool()
        raw = [
            {"title": "No URL here", "description": "..."},  # no url / link
            {"url": "https://reuters.com/ai", "title": "Valid"},  # valid
        ]
        mock_resp = self._make_web_response(raw)

        with patch.object(tool._http, "get", return_value=mock_resp):
            results = tool.search("AI news", n=5)

        assert len(results) == 1
        assert results[0].url == "https://reuters.com/ai"

    def test_parse_response_news_endpoint_shape(self) -> None:
        """
        News-endpoint response (top-level 'results' key) is parsed correctly.
        Traces: SRC-060
        """
        tool = self._make_tool()
        data = {
            "results": [
                {
                    "url": "https://techcrunch.com/news",
                    "title": "TC News",
                    "description": "TechCrunch",
                }
            ]
        }
        results = tool._parse_response(data, n=5)
        assert len(results) == 1
        assert results[0].source == "techcrunch.com"

    def test_parse_response_extra_snippets_used(self) -> None:
        """
        'extra_snippets' field is used as fallback for snippet text.
        Traces: SRC-060
        """
        tool = self._make_tool()
        data = {
            "web": {
                "results": [
                    {
                        "url": "https://reuters.com/x",
                        "title": "Reuters X",
                        "extra_snippets": ["Extra snippet text here"],
                    }
                ]
            }
        }
        results = tool._parse_response(data, n=5)
        assert results[0].snippet == "Extra snippet text here"

    # ── hydrate_url() ────────────────────────────────────────────────────────

    def test_hydrate_url_empty_returns_none(self) -> None:
        """Empty URL returns None."""
        tool = self._make_tool()
        assert tool.hydrate_url("") is None

    def test_hydrate_url_success(self) -> None:
        """Successful GET returns truncated page text."""
        tool = self._make_tool()
        mock_resp = MagicMock()
        mock_resp.text = "Article content " * 300
        mock_resp.raise_for_status = MagicMock()

        with patch.object(tool._http, "get", return_value=mock_resp):
            result = tool.hydrate_url("https://reuters.com/article")

        assert result is not None
        assert len(result) <= 2000

    def test_hydrate_url_error_returns_none(self) -> None:
        """HTTP error returns None without raising."""
        tool = self._make_tool()
        with patch.object(tool._http, "get", side_effect=Exception("timeout")):
            assert tool.hydrate_url("https://reuters.com") is None

    def test_close(self) -> None:
        """close() shuts down the httpx client."""
        tool = self._make_tool()
        with patch.object(tool._http, "close") as mock_close:
            tool.close()
            mock_close.assert_called_once()


# ---------------------------------------------------------------------------
# SearchResult shape consistency (SRC-056 — provider-agnostic)
# ---------------------------------------------------------------------------


class TestSearchResultShape:
    """
    Verify that SearchResult has the same shape regardless of provider.
    Traces: SRC-056 (provider-agnostic), SRC-060 (identical shape from all tools)
    """

    def test_search_result_fields(self) -> None:
        """SearchResult has url, title, snippet, source fields."""
        sr = SearchResult(
            url="https://reuters.com/ai",
            title="AI News",
            snippet="AI is growing...",
            source="reuters.com",
        )
        assert sr.url == "https://reuters.com/ai"
        assert sr.title == "AI News"
        assert sr.snippet == "AI is growing..."
        assert sr.source == "reuters.com"

    def test_search_result_all_tools_return_same_type(self) -> None:
        """All search tool results are SearchResult instances."""
        # NativeOpenAI tool
        mock_client = MagicMock()
        mock_resp = MagicMock()
        mock_resp.output = None
        mock_client.responses.create.return_value = mock_resp
        openai_tool = NativeOpenAISearchTool(client=mock_client)
        openai_results = openai_tool.search("query")
        assert isinstance(openai_results, list)

        # BraveTool
        brave_tool = BraveSearchTool(api_key="key")
        with patch.object(brave_tool._http, "get", side_effect=RuntimeError("offline")):
            brave_results = brave_tool.search("query")
        assert isinstance(brave_results, list)


# ---------------------------------------------------------------------------
# LLM Factory — get_search_tool and get_llm_client
# ---------------------------------------------------------------------------


class TestLLMFactory:
    """
    Tests for get_search_tool() and get_llm_client() factory functions.

    Traces: SRC-055 (provider-agnostic), SRC-056 (swap without pipeline changes),
            SRC-057 (OpenAI default), SRC-060 (search tool selection),
            SRC-109 (WEB_SEARCH_API_KEY)
    """

    @pytest.fixture
    def openai_llm_cfg(self, sample_agent_config):
        return sample_agent_config.llm

    @pytest.fixture
    def openai_secrets(self, sample_secrets):
        return sample_secrets

    def test_get_search_tool_openai_native(self, openai_llm_cfg, openai_secrets) -> None:
        """
        Default OpenAI provider returns NativeOpenAISearchTool.
        Traces: SRC-057, SRC-060
        """
        from ai_news_agent.llm.factory import get_search_tool

        tool = get_search_tool(openai_llm_cfg, openai_secrets)
        assert isinstance(tool, NativeOpenAISearchTool)

    def test_get_search_tool_brave_override(self, openai_llm_cfg, openai_secrets) -> None:
        """
        WEB_SEARCH_PROVIDER=brave returns BraveSearchTool.
        Traces: SRC-060, SRC-109
        """
        from ai_news_agent.config.models import RuntimeSecrets
        from ai_news_agent.llm.factory import get_search_tool

        secrets = RuntimeSecrets.model_validate(
            {
                "OPENAI_API_KEY": openai_secrets.openai_api_key,
                "TWITTER_BEARER_TOKEN": openai_secrets.twitter_bearer_token,
                "WEB_SEARCH_API_KEY": "brave-key-123",
                "WEB_SEARCH_PROVIDER": "brave",
            }
        )
        tool = get_search_tool(openai_llm_cfg, secrets)
        assert isinstance(tool, BraveSearchTool)

    def test_get_search_tool_tavily_override(self, openai_llm_cfg, openai_secrets) -> None:
        """
        WEB_SEARCH_PROVIDER=tavily returns TavilySearchTool.
        Traces: SRC-060, SRC-109
        """
        from ai_news_agent.config.models import RuntimeSecrets
        from ai_news_agent.llm.factory import get_search_tool
        from ai_news_agent.llm.search_tools import TavilySearchTool

        # Mock tavily import
        with patch.dict("sys.modules", {"tavily": MagicMock()}):
            secrets = RuntimeSecrets.model_validate(
                {
                    "OPENAI_API_KEY": openai_secrets.openai_api_key,
                    "TWITTER_BEARER_TOKEN": openai_secrets.twitter_bearer_token,
                    "WEB_SEARCH_API_KEY": "tvly-key-123",
                    "WEB_SEARCH_PROVIDER": "tavily",
                }
            )
            tool = get_search_tool(openai_llm_cfg, secrets)
            assert isinstance(tool, TavilySearchTool)

    def test_get_search_tool_no_key_no_provider_google_raises(self, openai_secrets) -> None:
        """
        Google provider without a WEB_SEARCH_API_KEY raises ConfigError.
        Traces: SRC-060
        """
        from ai_news_agent.config.loader import ConfigError
        from ai_news_agent.config.models import LLMConfig, RuntimeSecrets
        from ai_news_agent.llm.factory import get_search_tool

        google_cfg = LLMConfig(provider="google", model="gemini-1.5-pro")
        secrets = RuntimeSecrets.model_validate(
            {
                "OPENAI_API_KEY": openai_secrets.openai_api_key,
                "TWITTER_BEARER_TOKEN": openai_secrets.twitter_bearer_token,
                "GOOGLE_API_KEY": "test-google-key",
            }
        )
        with pytest.raises(ConfigError, match="WEB_SEARCH_API_KEY"):
            get_search_tool(google_cfg, secrets)

    def test_get_search_tool_no_key_raises(self, openai_secrets) -> None:
        """
        Anthropic provider + no search key raises ConfigError.
        Traces: SRC-060, SRC-109
        """
        from ai_news_agent.config.loader import ConfigError
        from ai_news_agent.config.models import LLMConfig, RuntimeSecrets
        from ai_news_agent.llm.factory import get_search_tool

        # Use anthropic provider with no search key — should raise
        anthropic_cfg = LLMConfig(provider="anthropic", model="claude-3-5-sonnet-20241022")
        secrets = RuntimeSecrets.model_validate(
            {
                "OPENAI_API_KEY": openai_secrets.openai_api_key,
                "TWITTER_BEARER_TOKEN": openai_secrets.twitter_bearer_token,
                "ANTHROPIC_API_KEY": "test-ant-key",
            }
        )
        with pytest.raises(ConfigError):
            get_search_tool(anthropic_cfg, secrets)

    def test_get_llm_client_unknown_provider_raises(self, openai_secrets) -> None:
        """
        Unknown provider string raises ConfigError.
        Traces: SRC-055 (supported providers only)
        """
        from ai_news_agent.config.loader import ConfigError
        from ai_news_agent.config.models import LLMConfig
        from ai_news_agent.llm.factory import get_llm_client

        # Build a config object with an unsupported provider via direct dict bypass
        bad_cfg = LLMConfig.model_construct(provider="cohere", model="command-r")  # type: ignore[arg-type]
        with pytest.raises(ConfigError, match="Unknown LLM provider"):
            get_llm_client(bad_cfg, openai_secrets)

    def test_get_llm_client_openai_no_key_raises(self, openai_secrets) -> None:
        """
        OpenAI provider with empty OPENAI_API_KEY raises ConfigError.
        Traces: SRC-057, SRC-107
        """
        from ai_news_agent.config.loader import ConfigError
        from ai_news_agent.config.models import LLMConfig
        from ai_news_agent.llm.factory import get_llm_client

        cfg = LLMConfig(provider="openai", model="gpt-4o")
        # Build a secrets instance where openai_api_key is empty string
        secrets_no_key = openai_secrets.model_copy(update={"openai_api_key": ""})
        with pytest.raises(ConfigError, match="OPENAI_API_KEY"):
            get_llm_client(cfg, secrets_no_key)

    def test_get_llm_client_anthropic_no_key_raises(self, openai_secrets) -> None:
        """
        Anthropic provider without ANTHROPIC_API_KEY raises ConfigError.
        Traces: SRC-055
        """
        from ai_news_agent.config.loader import ConfigError
        from ai_news_agent.config.models import LLMConfig
        from ai_news_agent.llm.factory import get_llm_client

        cfg = LLMConfig(provider="anthropic", model="claude-3-5-sonnet-20241022")
        # anthropic_api_key defaults to None — should raise
        with pytest.raises(ConfigError, match="ANTHROPIC_API_KEY"):
            get_llm_client(cfg, openai_secrets)

    def test_get_llm_client_google_no_key_raises(self, openai_secrets) -> None:
        """
        Google provider without GOOGLE_API_KEY raises ConfigError.
        Traces: SRC-055
        """
        from ai_news_agent.config.loader import ConfigError
        from ai_news_agent.config.models import LLMConfig
        from ai_news_agent.llm.factory import get_llm_client

        cfg = LLMConfig(provider="google", model="gemini-1.5-pro")
        # google_api_key defaults to None — should raise
        with pytest.raises(ConfigError, match="GOOGLE_API_KEY"):
            get_llm_client(cfg, openai_secrets)

    def test_get_search_tool_fallback_to_tavily_when_key_present(
        self, openai_llm_cfg, openai_secrets
    ) -> None:
        """
        When WEB_SEARCH_API_KEY is set with provider=tavily, TavilySearchTool is returned.
        Traces: SRC-060 (Tavily optimised for LLM workloads)
        """
        from ai_news_agent.config.models import RuntimeSecrets
        from ai_news_agent.llm.factory import get_search_tool
        from ai_news_agent.llm.search_tools import TavilySearchTool

        with patch.dict("sys.modules", {"tavily": MagicMock()}):
            secrets = RuntimeSecrets.model_validate(
                {
                    "OPENAI_API_KEY": openai_secrets.openai_api_key,
                    "TWITTER_BEARER_TOKEN": openai_secrets.twitter_bearer_token,
                    "WEB_SEARCH_API_KEY": "tvly-key",
                    "WEB_SEARCH_PROVIDER": "tavily",
                }
            )
            tool = get_search_tool(openai_llm_cfg, secrets)
            assert isinstance(tool, TavilySearchTool)

    def test_get_llm_client_openai_returns_client(self, openai_llm_cfg, openai_secrets) -> None:
        """
        get_llm_client with valid OpenAI secrets returns an OpenAILLMClient.
        Traces: SRC-057
        """
        from ai_news_agent.llm.factory import get_llm_client
        from ai_news_agent.llm.openai_client import OpenAILLMClient

        with patch("openai.OpenAI"):
            client = get_llm_client(openai_llm_cfg, openai_secrets)
        assert isinstance(client, OpenAILLMClient)
