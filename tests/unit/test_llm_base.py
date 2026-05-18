"""
tests/unit/test_llm_base.py — AbstractLLMClient contract tests.
Traces: SRC-056 (provider-agnostic interface), SRC-059 (plain language prompts),
        SRC-060 (abstract tool use), SRC-061 (output parsing contract), SRC-098 (unit tests)
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from ai_news_agent.llm.base import SearchResult

if TYPE_CHECKING:
    from tests.conftest import DummyLLMClient


class TestAbstractLLMClientContract:
    """
    Contract tests for AbstractLLMClient.
    DummyLLMClient must satisfy the same contract as any concrete provider.
    Traces: SRC-056 (provider-agnostic contract)
    """

    def test_complete_returns_string(self, dummy_llm: DummyLLMClient) -> None:
        result = dummy_llm.complete(
            messages=[{"role": "user", "content": "test"}],
            model="gpt-4o",
        )
        assert isinstance(result, str)
        assert len(result) > 0

    def test_search_returns_list_of_search_results(self, dummy_llm: DummyLLMClient) -> None:
        results = dummy_llm.search(query="AI news", n_results=5)
        assert isinstance(results, list)
        for r in results:
            assert isinstance(r, SearchResult)
            assert r.url
            assert r.title

    def test_parse_structured_extracts_json_block(self, dummy_llm: DummyLLMClient) -> None:
        """parse_structured extracts ```json block and validates schema (SRC-061)."""
        from ai_news_agent.storage.models import CurationResponse
        raw = '```json\n{"items": [], "themes": ["test"], "outlook": "", "predictions": []}\n```'
        result = dummy_llm.parse_structured(raw, CurationResponse)
        assert isinstance(result, CurationResponse)
        assert result.themes == ["test"]

    def test_parse_structured_fallback_no_code_fence(self, dummy_llm: DummyLLMClient) -> None:
        """Lenient fallback: parse JSON object without ```json fencing (SRC-061)."""
        from ai_news_agent.storage.models import CurationResponse
        raw = '{"items": [], "themes": [], "outlook": "test", "predictions": []}'
        result = dummy_llm.parse_structured(raw, CurationResponse)
        assert result.outlook == "test"

    def test_complete_records_call(self, dummy_llm: DummyLLMClient) -> None:
        """Verify complete() is recorded for assertion in tests."""
        dummy_llm.complete(
            messages=[{"role": "user", "content": "hello"}],
            model="gpt-4o",
        )
        assert len(dummy_llm.complete_calls) == 1
        assert dummy_llm.complete_calls[0]["model"] == "gpt-4o"

    def test_search_records_query(self, dummy_llm: DummyLLMClient) -> None:
        dummy_llm.search("enterprise AI news", n_results=3)
        assert "enterprise AI news" in dummy_llm.search_calls


class TestSearchResult:
    """Traces: SRC-060 (uniform SearchResult shape across providers)."""

    def test_search_result_fields(self) -> None:
        sr = SearchResult(
            url="https://reuters.com/test",
            title="Test Article",
            snippet="A short snippet.",
            source="reuters.com",
        )
        assert sr.url == "https://reuters.com/test"
        assert sr.source == "reuters.com"


class TestParseStructuredEdgeCases:
    """Edge cases for the JSON parsing algorithm (SRC-061)."""

    def test_parse_with_prose_before_json(self, dummy_llm: DummyLLMClient) -> None:
        from ai_news_agent.storage.models import CurationResponse
        # Prose before the JSON block
        raw = (
            "Here are the curated items:\n"
            "```json\n"
            '{"items": [], "themes": ["emerging"], "outlook": "", "predictions": []}\n'
            "```\n"
            "Hope this helps."
        )
        result = dummy_llm.parse_structured(raw, CurationResponse)
        assert "emerging" in result.themes

    def test_parse_structured_with_items(self, dummy_llm: DummyLLMClient) -> None:
        from ai_news_agent.storage.models import CurationResponse
        payload = {
            "items": [
                {
                    "headline": "Test Headline",
                    "source_name": "Reuters",
                    "url": "https://reuters.com/test",
                    "pub_date": "2026-05-10",
                    "why_it_matters": "It matters because...",
                    "impact_tags": ["business_impact"],
                    "tier": "1b",
                    "cross_refs": [],
                }
            ],
            "themes": [],
            "outlook": "",
            "predictions": [],
        }
        raw = f"```json\n{json.dumps(payload)}\n```"
        result = dummy_llm.parse_structured(raw, CurationResponse)
        assert len(result.items) == 1
        assert result.items[0].headline == "Test Headline"
        assert result.items[0].url == "https://reuters.com/test"
