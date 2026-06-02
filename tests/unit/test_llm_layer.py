"""
tests/unit/test_llm_layer.py — Comprehensive tests for the LLM abstraction layer.

Coverage:
  - AbstractLLMClient contract (provider-agnostic interface)
  - AbstractSearchTool contract (provider-agnostic search interface)
  - parse_structured: JSON-block extraction, lenient fallbacks, error cases (SRC-061)
  - OpenAILLMClient: complete() routing (Chat vs Responses API), search() delegation
  - AnthropicLLMClient: complete(), system-message extraction, thinking flag
  - GoogleLLMClient: complete(), grounding flag, fallback search
  - Factory: get_llm_client(), get_search_tool() for all three providers
  - retry decorator: exponential backoff, retryable vs non-retryable classification
  - BraveSearchTool: response parsing, error handling
  - TavilySearchTool: response parsing, error handling
  - SearchResult: data model invariants
  - Token usage tracking (SRC-150)

Traces: SRC-055–SRC-061 (provider-agnostic design), SRC-057 (OpenAI default),
        SRC-060 (abstract tool use), SRC-061 (output parsing),
        SRC-098 (unit tests with mocked LLM/Twitter), SRC-144 (retry/backoff),
        SRC-150 (token usage logging)
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from ai_news_agent.config.models import LLMConfig, RuntimeSecrets
from ai_news_agent.llm.base import AbstractLLMClient, SearchResult
from ai_news_agent.llm.retry import LLMError, _is_retryable, with_retry
from ai_news_agent.llm.search_tools import (
    AbstractSearchTool,
    BraveSearchTool,
    TavilySearchTool,
)
from ai_news_agent.storage.models import CurationResponse

# ---------------------------------------------------------------------------
# Helpers / sample data
# ---------------------------------------------------------------------------


def _make_curation_json(items: list[dict] | None = None) -> str:
    """Build a valid ```json...``` LLM response block for CurationResponse."""
    payload = {
        "items": items
        or [
            {
                "headline": "Test Headline",
                "source_name": "Reuters",
                "url": "https://reuters.com/test-story",
                "pub_date": "2026-05-10",
                "why_it_matters": "It matters because...",
                "impact_tags": ["business_impact"],
                "tier": "1b",
                "cross_refs": [],
                "twitter_handle": None,
                "tweet_url": None,
            }
        ],
        "themes": ["Enterprise AI"],
        "outlook": "Continued growth.",
        "predictions": [],
    }
    return f"```json\n{json.dumps(payload)}\n```"


def _make_secrets(
    openai_key: str = "sk-test-fake",
    web_key: str | None = None,
    web_provider: str | None = None,
    anthropic_key: str | None = None,
    google_key: str | None = None,
) -> RuntimeSecrets:
    return RuntimeSecrets.model_construct(
        openai_api_key=openai_key,
        twitter_bearer_token="bearer-fake",
        web_search_api_key=web_key,
        web_search_provider=web_provider,
        anthropic_api_key=anthropic_key,
        google_api_key=google_key,
        scheduler_api_key=None,
    )


def _make_llm_config(provider: str = "openai", model: str = "gpt-4o") -> LLMConfig:
    return LLMConfig(provider=provider, model=model)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# TestSearchResult — data model contract (SRC-060)
# ---------------------------------------------------------------------------


class TestSearchResult:
    """Traces: SRC-060 (uniform SearchResult shape across providers)."""

    def test_all_fields_accessible(self) -> None:
        sr = SearchResult(
            url="https://reuters.com/test",
            title="Test Article",
            snippet="A short snippet about AI.",
            source="reuters.com",
        )
        assert sr.url == "https://reuters.com/test"
        assert sr.title == "Test Article"
        assert sr.snippet == "A short snippet about AI."
        assert sr.source == "reuters.com"

    def test_empty_snippet_is_valid(self) -> None:
        """Snippet may be empty — not all search tools return snippets."""
        sr = SearchResult(url="https://example.com", title="T", snippet="", source="example.com")
        assert sr.snippet == ""

    def test_url_is_not_normalised_by_dataclass(self) -> None:
        """SearchResult stores the URL as-is; normalisation is the store's job."""
        raw = "https://reuters.com/test?utm_source=feed"
        sr = SearchResult(url=raw, title="X", snippet="", source="reuters.com")
        assert sr.url == raw


# ---------------------------------------------------------------------------
# TestParseStructuredImpl — shared JSON-block extraction (SRC-061)
# ---------------------------------------------------------------------------


class TestParseStructuredImpl:
    """
    Tests for ``_parse_structured_impl`` — the shared output parsing algorithm.
    Must work identically for all provider clients. (SRC-061)

    Traces: SRC-061 (Markdown + JSON block; not provider schema-enforcement)
    """

    def _parse(self, raw: str) -> CurationResponse:
        from ai_news_agent.llm.openai_client import _parse_structured_impl

        return _parse_structured_impl(raw, CurationResponse)

    def test_json_fenced_block_extracted(self) -> None:
        """Happy path: ```json ... ``` fenced block with valid CurationResponse."""
        raw = _make_curation_json()
        result = self._parse(raw)
        assert isinstance(result, CurationResponse)
        assert len(result.items) == 1
        assert result.items[0].headline == "Test Headline"
        assert result.items[0].url == "https://reuters.com/test-story"

    def test_prose_before_and_after_block_is_ignored(self) -> None:
        """Prose before/after the JSON block must be ignored. (SRC-061)"""
        raw = (
            "Here are the curated items for your review:\n\n"
            + _make_curation_json()
            + "\n\nLet me know if you need anything else."
        )
        result = self._parse(raw)
        assert result.themes == ["Enterprise AI"]

    def test_fallback_no_fence_plain_json_object(self) -> None:
        """Lenient fallback: plain JSON object without ```json``` fencing. (SRC-061)"""
        payload = {
            "items": [],
            "themes": ["Policy"],
            "outlook": "Watch for regulation.",
            "predictions": [],
        }
        result = self._parse(json.dumps(payload))
        assert result.outlook == "Watch for regulation."

    def test_fallback_object_buried_in_prose(self) -> None:
        """Lenient fallback: JSON object buried between prose lines."""
        obj = json.dumps(
            {
                "items": [],
                "themes": ["Safety"],
                "outlook": "",
                "predictions": [],
            }
        )
        raw = f"Summary of findings:\n{obj}\nEnd of report."
        result = self._parse(raw)
        assert "Safety" in result.themes

    def test_case_insensitive_fence_label(self) -> None:
        """```JSON``` with uppercase should also be parsed. (SRC-061)"""
        payload = {"items": [], "themes": [], "outlook": "ok", "predictions": []}
        raw = f"```JSON\n{json.dumps(payload)}\n```"
        result = self._parse(raw)
        assert result.outlook == "ok"

    def test_multiple_json_blocks_uses_first(self) -> None:
        """When multiple JSON blocks exist, the first one is used."""
        first = json.dumps({"items": [], "themes": ["First"], "outlook": "", "predictions": []})
        second = json.dumps({"items": [], "themes": ["Second"], "outlook": "", "predictions": []})
        raw = f"```json\n{first}\n```\n\nSome text\n\n```json\n{second}\n```"
        result = self._parse(raw)
        assert result.themes == ["First"]

    def test_empty_items_list(self) -> None:
        """Empty items list is valid — LLM may return no candidates."""
        payload = {"items": [], "themes": [], "outlook": "", "predictions": []}
        result = self._parse(f"```json\n{json.dumps(payload)}\n```")
        assert result.items == []

    def test_extra_fields_ignored(self) -> None:
        """Extra fields in the JSON block are silently ignored (model_config extra=ignore)."""
        payload = {
            "items": [],
            "themes": ["test"],
            "outlook": "",
            "predictions": [],
            "unknown_future_field": "ignored",
        }
        result = self._parse(f"```json\n{json.dumps(payload)}\n```")
        assert result.themes == ["test"]

    def test_raises_on_invalid_json(self) -> None:
        """Malformed JSON with no recoverable fallback must raise."""
        with pytest.raises((json.JSONDecodeError, ValueError, Exception)):  # noqa: PT011
            self._parse("This is not JSON at all, nothing to extract")

    def test_predictions_populated_annual(self) -> None:
        """predictions field is populated for annual responses. (SRC-124)"""
        payload = {
            "items": [],
            "themes": ["Inflection Point"],
            "outlook": "Major shifts ahead.",
            "predictions": ["LLM commoditisation", "Regulation wave"],
        }
        result = self._parse(f"```json\n{json.dumps(payload)}\n```")
        assert len(result.predictions) == 2
        assert result.predictions[0] == "LLM commoditisation"


# ---------------------------------------------------------------------------
# TestRetryDecorator — exponential backoff (SRC-144)
# ---------------------------------------------------------------------------


class TestRetryDecorator:
    """Traces: SRC-144 (3 retries, exponential backoff 30 → 60 → 120 s)."""

    def test_success_on_first_try(self) -> None:
        """No retries needed when function succeeds immediately."""
        call_count = 0

        @with_retry(max_retries=3, backoff_base=0.001)
        def succeed() -> str:
            nonlocal call_count
            call_count += 1
            return "ok"

        assert succeed() == "ok"
        assert call_count == 1

    def test_success_after_two_failures(self) -> None:
        """Should succeed on the third attempt after two transient failures."""
        call_count = 0

        @with_retry(max_retries=3, backoff_base=0.001)
        def succeed_on_third() -> str:
            nonlocal call_count
            call_count += 1
            if call_count < 3:
                # Simulate a rate-limit error (retryable)
                raise Exception("429 rate limit exceeded")
            return "success"

        result = succeed_on_third()
        assert result == "success"
        assert call_count == 3

    def test_exhausts_retries_raises_llm_error(self) -> None:
        """After max_retries exhausted, LLMError is raised."""

        @with_retry(max_retries=2, backoff_base=0.001)
        def always_fail() -> str:
            raise Exception("503 service unavailable")

        with pytest.raises(LLMError) as exc_info:
            always_fail()
        assert "always_fail" in str(exc_info.value)

    def test_non_retryable_raises_immediately(self) -> None:
        """Non-retryable errors (e.g. 401 auth) should raise immediately as LLMError."""
        call_count = 0

        @with_retry(max_retries=3, backoff_base=0.001)
        def auth_fail() -> str:
            nonlocal call_count
            call_count += 1
            raise Exception("401 Unauthorized — invalid API key")

        with pytest.raises(LLMError):
            auth_fail()
        # Non-retryable: only one call
        assert call_count == 1

    def test_llm_error_not_rewrapped(self) -> None:
        """LLMError raised inside decorated function propagates without re-wrapping."""

        @with_retry(max_retries=3, backoff_base=0.001)
        def raise_llm_error() -> str:
            raise LLMError("already normalised")

        with pytest.raises(LLMError) as exc_info:
            raise_llm_error()
        assert "already normalised" in str(exc_info.value)

    def test_backoff_timing(self) -> None:
        """Verify sleep is called with the correct backoff values."""
        call_count = 0

        @with_retry(max_retries=2, backoff_base=10.0)
        def always_rate_limited() -> str:
            nonlocal call_count
            call_count += 1
            raise Exception("429 Too Many Requests")

        sleep_calls: list[float] = []

        with (
            patch("ai_news_agent.llm.retry.time.sleep", side_effect=sleep_calls.append),
            pytest.raises(LLMError),
        ):
            always_rate_limited()

        # backoff_base=10, attempts: 10*2^0=10, 10*2^1=20
        assert sleep_calls == [10.0, 20.0]


class TestIsRetryable:
    """Tests for the ``_is_retryable`` classifier. Traces: SRC-144."""

    @pytest.mark.parametrize(
        ("exc_name", "expected"),
        [
            ("RateLimitError", True),
            ("APIConnectionError", True),
            ("APITimeoutError", True),
            ("InternalServerError", True),
            ("ServiceUnavailableError", True),
            ("AuthenticationError", False),
            ("BadRequestError", False),
            ("ValueError", False),
        ],
    )
    def test_by_exception_class_name(self, exc_name: str, expected: bool) -> None:
        exc = type(exc_name, (Exception,), {})("test message")
        assert _is_retryable(exc) == expected

    @pytest.mark.parametrize(
        ("msg", "expected"),
        [
            ("HTTP 429 rate limit exceeded", True),
            ("connection timed out after 30s", True),
            ("503 service unavailable", True),
            ("400 bad request schema error", False),
            ("401 Unauthorized invalid key", False),
            ("JSON decode error at line 3", False),
        ],
    )
    def test_by_message_content(self, msg: str, expected: bool) -> None:
        exc = Exception(msg)
        assert _is_retryable(exc) == expected


# ---------------------------------------------------------------------------
# TestLLMError
# ---------------------------------------------------------------------------


class TestLLMError:
    """Tests for the LLMError normalisation class. Traces: SRC-056, SRC-144."""

    def test_message_accessible(self) -> None:
        err = LLMError("something went wrong")
        assert "something went wrong" in str(err)

    def test_cause_attached(self) -> None:
        original = ValueError("original error")
        err = LLMError("wrapped", cause=original)
        assert err.__cause__ is original

    def test_is_runtime_error(self) -> None:
        """LLMError is a RuntimeError for easy catch in non-critical paths."""
        assert isinstance(LLMError("x"), RuntimeError)


# ---------------------------------------------------------------------------
# TestOpenAILLMClient — complete() routing and search() delegation
# ---------------------------------------------------------------------------


class TestOpenAILLMClient:
    """
    Tests for OpenAILLMClient using mocked openai SDK.

    Traces: SRC-057 (OpenAI default), SRC-059 (plain prompts),
            SRC-060 (search delegation), SRC-061 (parse_structured),
            SRC-150 (token usage)
    """

    def _make_client(self, search_results: list[SearchResult] | None = None):
        """Build an OpenAILLMClient with mocked openai and search_tool."""
        from ai_news_agent.llm.openai_client import OpenAILLMClient

        mock_search_tool = MagicMock(spec=AbstractSearchTool)
        mock_search_tool.search.return_value = search_results or [
            SearchResult("https://reuters.com", "Title", "Snippet", "reuters.com")
        ]

        mock_openai_client = MagicMock()
        with patch("ai_news_agent.llm.openai_client.openai") as mock_openai:
            mock_openai.OpenAI.return_value = mock_openai_client
            client = OpenAILLMClient(api_key="sk-fake", search_tool=mock_search_tool)

        return client, mock_search_tool

    def test_search_delegates_to_search_tool(self) -> None:
        """search() must delegate to AbstractSearchTool — not call OpenAI directly. (SRC-060)"""
        client, mock_tool = self._make_client()
        results = client.search("AI news", n_results=5)
        mock_tool.search.assert_called_once_with("AI news", 5)
        assert isinstance(results, list)

    def test_search_deep_budget_multiplies_n(self) -> None:
        """budget_hint='deep' should request 3× results. (SRC-121)"""
        client, mock_tool = self._make_client()
        client.search("AI news", n_results=10, budget_hint="deep")
        mock_tool.search.assert_called_once_with("AI news", 30)

    def test_parse_structured_extracts_json_block(self) -> None:
        """parse_structured must extract ```json block and validate schema. (SRC-061)"""
        client, _ = self._make_client()
        raw = _make_curation_json()
        result = client.parse_structured(raw, CurationResponse)
        assert isinstance(result, CurationResponse)
        assert len(result.items) == 1
        assert result.items[0].headline == "Test Headline"

    def test_parse_structured_fallback_no_fence(self) -> None:
        """Lenient fallback when no fenced block present. (SRC-061)"""
        client, _ = self._make_client()
        payload = {"items": [], "themes": ["X"], "outlook": "", "predictions": []}
        result = client.parse_structured(json.dumps(payload), CurationResponse)
        assert result.themes == ["X"]

    def test_last_token_usage_initially_zero(self) -> None:
        """token_usage starts at 0 before any complete() call. (SRC-150)"""
        client, _ = self._make_client()
        assert client.last_token_usage == 0

    def test_complete_uses_chat_completions_for_gpt4o(self) -> None:
        """gpt-4o should route to Chat Completions API, not Responses API."""
        from ai_news_agent.llm.openai_client import _uses_responses_api

        assert not _uses_responses_api("gpt-4o")

    def test_complete_uses_responses_api_for_o3(self) -> None:
        """o3 and other o-series should route to Responses API."""
        from ai_news_agent.llm.openai_client import _uses_responses_api

        assert _uses_responses_api("o3")
        assert _uses_responses_api("o1")
        assert _uses_responses_api("o4-mini")

    def test_responses_api_not_triggered_for_gpt_models(self) -> None:
        """gpt-4o, gpt-4o-mini etc. should NOT use Responses API."""
        from ai_news_agent.llm.openai_client import _uses_responses_api

        assert not _uses_responses_api("gpt-4o")
        assert not _uses_responses_api("gpt-4o-mini")
        assert not _uses_responses_api("gpt-4-turbo")

    def _make_full_client(
        self,
        mock_client_instance: MagicMock | None = None,
    ) -> tuple[Any, MagicMock]:
        """
        Build an OpenAILLMClient with full mocking of the openai module.
        Returns (client, mock_openai_client_instance).
        """
        from ai_news_agent.llm.openai_client import OpenAILLMClient

        mock_search = MagicMock(spec=AbstractSearchTool)
        if mock_client_instance is None:
            mock_client_instance = MagicMock()

        with patch("ai_news_agent.llm.openai_client.openai") as mock_openai:
            mock_openai.OpenAI.return_value = mock_client_instance
            mock_openai.OpenAIError = Exception  # so isinstance checks work
            client = OpenAILLMClient(api_key="sk-fake", search_tool=mock_search)

        return client, mock_client_instance

    def test_complete_chat_path(self) -> None:
        """complete() for gpt-4o should call chat.completions.create."""
        mock_oa_client = MagicMock()
        mock_response = MagicMock()
        mock_response.choices[0].message.content = _make_curation_json()
        mock_response.usage.total_tokens = 1234
        mock_response.usage.prompt_tokens = 800
        mock_response.usage.completion_tokens = 434
        mock_oa_client.chat.completions.create.return_value = mock_response

        client, mock_oa_client = self._make_full_client(mock_oa_client)

        with patch("ai_news_agent.llm.openai_client.openai") as mock_openai:
            mock_openai.OpenAIError = Exception
            client._client = mock_oa_client
            result = client.complete(
                messages=[{"role": "user", "content": "hello"}],
                model="gpt-4o",
                temperature=0.2,
            )

        mock_oa_client.chat.completions.create.assert_called_once()
        assert result == _make_curation_json()
        assert client.last_token_usage == 1234

    def test_complete_with_thinking_kwarg_for_gpt4o(self) -> None:
        """thinking=True for non-o-series model is silently ignored (uses Chat Completions)."""
        mock_oa_client = MagicMock()
        mock_response = MagicMock()
        mock_response.choices[0].message.content = "some text"
        mock_response.usage.total_tokens = 100
        mock_response.usage.prompt_tokens = 50
        mock_response.usage.completion_tokens = 50
        mock_oa_client.chat.completions.create.return_value = mock_response

        client, mock_oa_client = self._make_full_client(mock_oa_client)

        with patch("ai_news_agent.llm.openai_client.openai") as mock_openai:
            mock_openai.OpenAIError = Exception
            client._client = mock_oa_client
            client.complete(
                messages=[{"role": "user", "content": "test"}],
                model="gpt-4o",
                thinking=True,  # should be silently ignored for non-o-series
            )

        # Must route to Chat Completions, not Responses API
        mock_oa_client.chat.completions.create.assert_called_once()
        mock_oa_client.responses.create.assert_not_called()

    def test_complete_responses_path_with_o3(self) -> None:
        """complete() for o3 should call responses.create with reasoning_effort."""
        mock_oa_client = MagicMock()
        mock_text_block = MagicMock()
        mock_text_block.text = "response text from o3"
        mock_output_item = MagicMock()
        mock_output_item.content = [mock_text_block]
        mock_response = MagicMock()
        mock_response.output = [mock_output_item]
        mock_response.usage.total_tokens = None
        mock_response.usage.input_tokens = 600
        mock_response.usage.output_tokens = 200
        mock_oa_client.responses.create.return_value = mock_response

        client, mock_oa_client = self._make_full_client(mock_oa_client)

        with patch("ai_news_agent.llm.openai_client.openai") as mock_openai:
            mock_openai.OpenAIError = Exception
            client._client = mock_oa_client
            client.complete(
                messages=[
                    {"role": "system", "content": "Be precise."},
                    {"role": "user", "content": "Analyse AI news"},
                ],
                model="o3",
                thinking=True,
            )

        call_kwargs = mock_oa_client.responses.create.call_args[1]
        assert call_kwargs["model"] == "o3"
        assert call_kwargs["reasoning"]["effort"] == "high"
        assert "SYSTEM INSTRUCTIONS" in call_kwargs["input"]
        assert "Analyse AI news" in call_kwargs["input"]
        assert client.last_token_usage == 800

    def test_complete_o3_without_thinking_uses_medium_effort(self) -> None:
        """o3 with thinking=False should use reasoning_effort='medium'."""
        mock_oa_client = MagicMock()
        mock_response = MagicMock()
        mock_response.output = []
        mock_response.usage = None
        mock_oa_client.responses.create.return_value = mock_response

        client, mock_oa_client = self._make_full_client(mock_oa_client)

        with patch("ai_news_agent.llm.openai_client.openai") as mock_openai:
            mock_openai.OpenAIError = Exception
            client._client = mock_oa_client
            client.complete(
                messages=[{"role": "user", "content": "test"}],
                model="o3",
                thinking=False,
            )

        call_kwargs = mock_oa_client.responses.create.call_args[1]
        assert call_kwargs["reasoning"]["effort"] == "medium"


# ---------------------------------------------------------------------------
# TestAnthropicLLMClient
# ---------------------------------------------------------------------------


class TestAnthropicLLMClient:
    """
    Tests for AnthropicLLMClient using mocked anthropic SDK.

    Traces: SRC-055–SRC-056 (provider-agnostic), SRC-059 (plain prompts),
            SRC-060 (search via injected tool), SRC-061 (parse_structured),
            SRC-150 (token usage)
    """

    def _make_client(
        self,
        search_tool: AbstractSearchTool | None = None,
        mock_response_text: str = "response text",
        input_tokens: int = 100,
        output_tokens: int = 50,
    ):
        """Build AnthropicLLMClient with mocked anthropic SDK."""
        from ai_news_agent.llm.anthropic_client import AnthropicLLMClient

        mock_anthropic_module = MagicMock()
        mock_anthropic_client = MagicMock()
        mock_anthropic_module.Anthropic.return_value = mock_anthropic_client

        # Build mock response
        mock_text_block = MagicMock()
        mock_text_block.type = "text"
        mock_text_block.text = mock_response_text
        mock_resp = MagicMock()
        mock_resp.content = [mock_text_block]
        mock_resp.usage.input_tokens = input_tokens
        mock_resp.usage.output_tokens = output_tokens
        mock_anthropic_client.messages.create.return_value = mock_resp

        with patch.dict("sys.modules", {"anthropic": mock_anthropic_module}):
            # Also patch __init__'s import attempt
            client = AnthropicLLMClient.__new__(AnthropicLLMClient)
            client._client = mock_anthropic_client
            client._anthropic = mock_anthropic_module
            client._search_tool = search_tool
            client._last_token_usage = 0

        return client, mock_anthropic_client, mock_anthropic_module

    def test_complete_returns_text_content(self) -> None:
        """complete() must return the text blocks from the Anthropic response."""
        client, mock_api, _ = self._make_client(mock_response_text="curated output")
        result = client.complete(
            messages=[{"role": "user", "content": "test"}],
            model="claude-3-7-sonnet-20250219",
        )
        assert result == "curated output"

    def test_system_messages_extracted_to_system_param(self) -> None:
        """System messages must be passed via system= param, not in messages list."""
        client, mock_api, _ = self._make_client()
        client.complete(
            messages=[
                {"role": "system", "content": "You are a curator."},
                {"role": "user", "content": "Curate AI news."},
            ],
            model="claude-3-7-sonnet-20250219",
        )
        call_kwargs = mock_api.messages.create.call_args[1]
        assert call_kwargs["system"] == "You are a curator."
        # The user message should be in messages, not the system message
        msg_roles = [m["role"] for m in call_kwargs["messages"]]
        assert "system" not in msg_roles
        assert "user" in msg_roles

    def test_thinking_flag_sets_thinking_block(self) -> None:
        """thinking=True on a supported model must set the thinking config."""
        client, mock_api, _ = self._make_client()
        client.complete(
            messages=[{"role": "user", "content": "analyse"}],
            model="claude-3-7-sonnet-20250219",
            thinking=True,
        )
        call_kwargs = mock_api.messages.create.call_args[1]
        assert "thinking" in call_kwargs
        assert call_kwargs["thinking"]["type"] == "enabled"
        # Extended thinking requires temperature=1
        assert call_kwargs["temperature"] == 1

    def test_thinking_flag_ignored_for_unsupported_model(self) -> None:
        """thinking=True on claude-3-5-haiku (unsupported) must be ignored."""
        client, mock_api, _ = self._make_client()
        client.complete(
            messages=[{"role": "user", "content": "analyse"}],
            model="claude-3-5-haiku-20241022",
            thinking=True,
        )
        call_kwargs = mock_api.messages.create.call_args[1]
        # Should NOT have thinking block for unsupported models
        assert "thinking" not in call_kwargs or call_kwargs.get("thinking") is None

    def test_token_usage_tracked(self) -> None:
        """last_token_usage should sum input + output tokens. (SRC-150)"""
        client, _, _ = self._make_client(input_tokens=300, output_tokens=150)
        client.complete(
            messages=[{"role": "user", "content": "test"}],
            model="claude-3-7-sonnet-20250219",
        )
        assert client.last_token_usage == 450

    def test_search_delegates_to_injected_tool(self) -> None:
        """search() must delegate to the injected AbstractSearchTool. (SRC-060)"""
        mock_search = MagicMock(spec=AbstractSearchTool)
        mock_search.search.return_value = [
            SearchResult("https://reuters.com", "T", "S", "reuters.com")
        ]
        client, _, _ = self._make_client(search_tool=mock_search)
        results = client.search("AI news", n_results=5)
        mock_search.search.assert_called_once_with("AI news", 5)
        assert len(results) == 1

    def test_search_raises_without_tool(self) -> None:
        """search() without an injected tool must raise LLMError. (SRC-060)"""
        client, _, _ = self._make_client(search_tool=None)
        with pytest.raises(LLMError):
            client.search("AI news")

    def test_search_deep_budget_multiplies_n(self) -> None:
        """budget_hint='deep' → 3× results. (SRC-121)"""
        mock_search = MagicMock(spec=AbstractSearchTool)
        mock_search.search.return_value = []
        client, _, _ = self._make_client(search_tool=mock_search)
        client.search("deep search", n_results=10, budget_hint="deep")
        mock_search.search.assert_called_once_with("deep search", 30)

    def test_parse_structured_uses_shared_impl(self) -> None:
        """parse_structured must use the same algorithm as OpenAI client. (SRC-061)"""
        client, _, _ = self._make_client()
        raw = _make_curation_json()
        result = client.parse_structured(raw, CurationResponse)
        assert isinstance(result, CurationResponse)
        assert len(result.items) == 1

    def test_parse_structured_fallback_no_fence(self) -> None:
        """Lenient fallback works on Anthropic responses too. (SRC-061)"""
        client, _, _ = self._make_client()
        payload = {"items": [], "themes": ["test"], "outlook": "ok", "predictions": []}
        result = client.parse_structured(json.dumps(payload), CurationResponse)
        assert result.themes == ["test"]

    def test_import_error_raised_when_package_missing(self) -> None:
        """ImportError with helpful message when anthropic is not installed."""
        # Verify that AnthropicLLMClient raises ImportError when the anthropic
        # package is not available. We check this by calling __init__ with a
        # None-patched module, forcing the deferred import to fail.
        from ai_news_agent.llm.anthropic_client import AnthropicLLMClient

        client = AnthropicLLMClient.__new__(AnthropicLLMClient)
        with (
            patch.dict("sys.modules", {"anthropic": None}),
            pytest.raises((ImportError, AttributeError)),
        ):  # type: ignore[dict-item]  # noqa: PT011
            client.__init__(api_key="test")  # type: ignore[misc]


# ---------------------------------------------------------------------------
# TestGoogleLLMClient
# ---------------------------------------------------------------------------


class TestGoogleLLMClient:
    """
    Tests for GoogleLLMClient using mocked google-generativeai SDK.

    Traces: SRC-055–SRC-056 (provider-agnostic), SRC-059 (plain prompts),
            SRC-060 (search via injected tool), SRC-061 (parse_structured)
    """

    def _make_client(
        self,
        search_tool: AbstractSearchTool | None = None,
        response_text: str = "google response",
        use_grounding: bool = False,
    ):
        """Build GoogleLLMClient bypassing actual SDK init via __new__ + direct attribute assignment."""
        from ai_news_agent.llm.google_client import GoogleLLMClient

        mock_genai = MagicMock()
        mock_model = MagicMock()
        mock_genai.GenerativeModel.return_value = mock_model

        mock_response = MagicMock()
        mock_response.text = response_text
        mock_usage = MagicMock()
        mock_usage.prompt_token_count = 100
        mock_usage.candidates_token_count = 50
        mock_response.usage_metadata = mock_usage
        mock_model.generate_content.return_value = mock_response

        # Use __new__ to bypass __init__ (which calls genai.configure())
        client = GoogleLLMClient.__new__(GoogleLLMClient)
        client._genai = mock_genai
        client._api_key = "fake-key"
        client._search_tool = search_tool
        client._use_grounding = use_grounding
        client._last_token_usage = 0

        return client, mock_model, mock_genai

    def test_complete_returns_text(self) -> None:
        """complete() must return the model response text."""
        client, mock_model, _ = self._make_client(response_text="gemini output")
        result = client.complete(
            messages=[{"role": "user", "content": "test"}],
            model="gemini-2.0-flash",
        )
        assert result == "gemini output"

    def test_system_message_extracted_to_system_instruction(self) -> None:
        """System messages must be extracted and passed as system_instruction."""
        client, mock_model, mock_genai = self._make_client()
        client.complete(
            messages=[
                {"role": "system", "content": "Be a news curator."},
                {"role": "user", "content": "Analyse AI news."},
            ],
            model="gemini-2.0-flash",
        )
        call_kwargs = mock_genai.GenerativeModel.call_args[1]
        assert call_kwargs["system_instruction"] == "Be a news curator."

    def test_search_delegates_to_injected_tool(self) -> None:
        """search() must delegate to the injected AbstractSearchTool. (SRC-060)"""
        mock_search = MagicMock(spec=AbstractSearchTool)
        mock_search.search.return_value = []
        client, _, _ = self._make_client(search_tool=mock_search)
        client.search("AI regulation", n_results=7)
        mock_search.search.assert_called_once_with("AI regulation", 7)

    def test_search_raises_without_tool(self) -> None:
        """search() without an injected tool must raise LLMError."""
        client, _, _ = self._make_client(search_tool=None)
        with pytest.raises(LLMError):
            client.search("AI news")

    def test_parse_structured_uses_shared_impl(self) -> None:
        """parse_structured must use the same algorithm as OpenAI/Anthropic clients. (SRC-061)"""
        client, _, _ = self._make_client()
        raw = _make_curation_json()
        result = client.parse_structured(raw, CurationResponse)
        assert isinstance(result, CurationResponse)

    def test_token_usage_tracked(self) -> None:
        """last_token_usage should sum input + output tokens. (SRC-150)"""
        mock_genai = MagicMock()
        mock_model = MagicMock()
        mock_genai.GenerativeModel.return_value = mock_model
        mock_response = MagicMock()
        mock_response.text = "test"
        mock_usage = MagicMock()
        mock_usage.prompt_token_count = 200
        mock_usage.candidates_token_count = 80
        mock_response.usage_metadata = mock_usage
        mock_model.generate_content.return_value = mock_response

        from ai_news_agent.llm.google_client import GoogleLLMClient

        client = GoogleLLMClient.__new__(GoogleLLMClient)
        client._genai = mock_genai
        client._api_key = "fake-key"
        client._search_tool = None
        client._use_grounding = False
        client._last_token_usage = 0

        client.complete(
            messages=[{"role": "user", "content": "test"}],
            model="gemini-2.0-flash",
        )
        assert client.last_token_usage == 280


# ---------------------------------------------------------------------------
# TestSearchToolContracts — AbstractSearchTool contract tests (SRC-060)
# ---------------------------------------------------------------------------


class TestAbstractSearchToolContract:
    """
    Verify the AbstractSearchTool interface contract is satisfied by all concrete implementations
    when instantiated with minimal mocks. (SRC-060)
    """

    def test_brave_search_tool_instantiation(self) -> None:
        """BraveSearchTool can be constructed with an API key."""
        with patch("ai_news_agent.llm.search_tools.httpx.Client"):
            tool = BraveSearchTool(api_key="test-key")
        assert isinstance(tool, AbstractSearchTool)

    def test_tavily_search_tool_import_error(self) -> None:
        """TavilySearchTool raises ImportError with helpful message when tavily not installed."""
        with (
            patch.dict("sys.modules", {"tavily": None}),
            pytest.raises(ImportError, match="tavily-python"),
        ):
            TavilySearchTool(api_key="test-key")


# ---------------------------------------------------------------------------
# TestBraveSearchTool — response parsing and error handling (SRC-060)
# ---------------------------------------------------------------------------


class TestBraveSearchTool:
    """Traces: SRC-060 (Brave Search API fallback)."""

    def _make_tool(self) -> BraveSearchTool:
        with patch("ai_news_agent.llm.search_tools.httpx.Client"):
            return BraveSearchTool(api_key="test-brave-key")

    def test_parse_web_endpoint_response(self) -> None:
        """Parse standard web endpoint response format."""
        tool = self._make_tool()
        data = {
            "web": {
                "results": [
                    {
                        "url": "https://reuters.com/ai-story",
                        "title": "AI Milestone",
                        "description": "A major AI milestone was reached.",
                    },
                    {
                        "url": "https://bloomberg.com/ai-story",
                        "title": "Bloomberg AI",
                        "description": "Bloomberg covers AI growth.",
                    },
                ]
            }
        }
        results = tool._parse_response(data, n=10)
        assert len(results) == 2
        assert results[0].url == "https://reuters.com/ai-story"
        assert results[0].title == "AI Milestone"
        assert results[0].source == "reuters.com"

    def test_parse_news_endpoint_response(self) -> None:
        """Parse news endpoint response format (flat results list)."""
        tool = self._make_tool()
        data = {
            "results": [
                {
                    "url": "https://techcrunch.com/story",
                    "title": "TechCrunch AI Story",
                    "description": "TC covers AI.",
                }
            ]
        }
        results = tool._parse_response(data, n=5)
        assert len(results) == 1
        assert results[0].source == "techcrunch.com"

    def test_parse_respects_n_limit(self) -> None:
        """Results truncated to n even if API returns more."""
        tool = self._make_tool()
        data = {
            "web": {
                "results": [
                    {"url": f"https://example.com/{i}", "title": f"Story {i}", "description": ""}
                    for i in range(20)
                ]
            }
        }
        results = tool._parse_response(data, n=5)
        assert len(results) == 5

    def test_skip_results_without_url(self) -> None:
        """Results without a URL should be skipped."""
        tool = self._make_tool()
        data = {
            "web": {
                "results": [
                    {"url": "", "title": "No URL", "description": ""},
                    {"url": "https://reuters.com/story", "title": "Reuters", "description": ""},
                ]
            }
        }
        results = tool._parse_response(data, n=10)
        assert len(results) == 1
        assert results[0].url == "https://reuters.com/story"

    def test_search_returns_empty_on_http_error(self) -> None:
        """HTTP errors must be caught and empty list returned. (SRC-144)"""
        tool = self._make_tool()
        tool._http = MagicMock()
        tool._http.get.side_effect = Exception("connection refused")
        results = tool.search("AI news", n=5)
        assert results == []

    def test_hydrate_url_returns_text(self) -> None:
        """hydrate_url returns the page text (truncated to 2000 chars). (SRC-069)"""
        tool = self._make_tool()
        mock_resp = MagicMock()
        mock_resp.text = "Article content " * 200  # > 2000 chars
        mock_resp.raise_for_status = MagicMock()
        tool._http = MagicMock()
        tool._http.get.return_value = mock_resp
        result = tool.hydrate_url("https://reuters.com/story")
        assert result is not None
        assert len(result) <= 2000

    def test_hydrate_url_returns_none_on_error(self) -> None:
        """hydrate_url returns None on any fetch failure. (SRC-069)"""
        tool = self._make_tool()
        tool._http = MagicMock()
        tool._http.get.side_effect = Exception("timeout")
        result = tool.hydrate_url("https://reuters.com/story")
        assert result is None

    def test_hydrate_url_returns_none_for_empty(self) -> None:
        """hydrate_url with empty URL returns None immediately."""
        tool = self._make_tool()
        assert tool.hydrate_url("") is None
        assert tool.hydrate_url(None) is None  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# TestFactory — get_llm_client() and get_search_tool() (SRC-055–SRC-057, SRC-060)
# ---------------------------------------------------------------------------


class TestFactory:
    """
    Tests for factory functions.

    Traces: SRC-055 (provider-agnostic), SRC-056 (swap without pipeline changes),
            SRC-057 (OpenAI default), SRC-060 (search tool selection)
    """

    def test_get_llm_client_openai_default(self) -> None:
        """provider=openai returns OpenAILLMClient. (SRC-057)"""
        from ai_news_agent.llm.factory import get_llm_client
        from ai_news_agent.llm.openai_client import OpenAILLMClient

        cfg = _make_llm_config("openai")
        secrets = _make_secrets(openai_key="sk-test")

        with patch("ai_news_agent.llm.openai_client.openai") as mock_oa:
            mock_oa.OpenAI.return_value = MagicMock()
            client = get_llm_client(cfg, secrets)

        assert isinstance(client, OpenAILLMClient)

    def test_get_llm_client_unknown_provider_raises(self) -> None:
        """Unknown provider string raises ConfigError. (SRC-056)"""
        from ai_news_agent.config.loader import ConfigError
        from ai_news_agent.llm.factory import get_llm_client

        secrets = _make_secrets()

        # Build a config object with an invalid provider by bypassing Pydantic validation
        bad_cfg = MagicMock()
        bad_cfg.provider = "unknown_provider"

        with pytest.raises(ConfigError, match="Unknown LLM provider"):
            get_llm_client(bad_cfg, secrets)

    def test_get_llm_client_openai_missing_key_raises(self) -> None:
        """provider=openai without OPENAI_API_KEY raises ConfigError."""
        from ai_news_agent.config.loader import ConfigError
        from ai_news_agent.llm.factory import get_llm_client

        bad_cfg = MagicMock()
        bad_cfg.provider = "openai"

        bad_secrets = MagicMock()
        bad_secrets.openai_api_key = ""
        bad_secrets.web_search_api_key = None

        with pytest.raises(ConfigError, match="OPENAI_API_KEY"):
            get_llm_client(bad_cfg, bad_secrets)

    def test_get_search_tool_native_openai_for_openai_provider(self) -> None:
        """provider=openai with no WEB_SEARCH_API_KEY falls back to NativeOpenAISearchTool. (SRC-060)"""
        from ai_news_agent.llm.factory import get_search_tool
        from ai_news_agent.llm.search_tools import NativeOpenAISearchTool

        cfg = _make_llm_config("openai")
        secrets = _make_secrets(openai_key="sk-test")

        mock_openai = MagicMock()
        with (
            patch.dict("sys.modules", {"openai": mock_openai}),
            patch("ai_news_agent.llm.search_tools.httpx.Client"),
        ):
            tool = get_search_tool(cfg, secrets)

        assert isinstance(tool, NativeOpenAISearchTool)

    def test_get_search_tool_tavily_when_key_and_provider_set(self) -> None:
        """WEB_SEARCH_API_KEY + WEB_SEARCH_PROVIDER=tavily → TavilySearchTool. (SRC-060)"""
        from ai_news_agent.llm.factory import get_search_tool

        cfg = _make_llm_config("openai")
        secrets = _make_secrets(web_key="tvly-key", web_provider="tavily")

        with patch("tavily.TavilyClient"):
            try:
                tool = get_search_tool(cfg, secrets)
                assert isinstance(tool, TavilySearchTool)
            except ImportError:
                pytest.skip("tavily-python not installed")

    def test_get_search_tool_brave_when_key_and_provider_set(self) -> None:
        """WEB_SEARCH_API_KEY + WEB_SEARCH_PROVIDER=brave → BraveSearchTool. (SRC-060)"""
        from ai_news_agent.llm.factory import get_search_tool

        cfg = _make_llm_config("openai")
        secrets = _make_secrets(web_key="brave-key", web_provider="brave")

        with patch("ai_news_agent.llm.search_tools.httpx.Client"):
            tool = get_search_tool(cfg, secrets)

        assert isinstance(tool, BraveSearchTool)

    def test_get_search_tool_raises_when_no_tool_available(self) -> None:
        """provider=anthropic with no WEB_SEARCH_API_KEY raises ConfigError. (SRC-060)"""
        from ai_news_agent.config.loader import ConfigError
        from ai_news_agent.llm.factory import get_search_tool

        cfg = _make_llm_config("anthropic")
        secrets = _make_secrets(openai_key="sk-test")  # no web key, no anthropic = no search

        with pytest.raises(ConfigError):
            get_search_tool(cfg, secrets)

    def test_get_search_tool_fallback_to_tavily_if_key_present_no_provider(self) -> None:
        """WEB_SEARCH_API_KEY with no WEB_SEARCH_PROVIDER should default to TavilySearchTool."""
        from ai_news_agent.llm.factory import get_search_tool

        cfg = _make_llm_config("anthropic")
        secrets = _make_secrets(web_key="tvly-key", web_provider=None, openai_key="sk-test")

        with patch("tavily.TavilyClient"):
            try:
                tool = get_search_tool(cfg, secrets)
                assert isinstance(tool, TavilySearchTool)
            except ImportError:
                pytest.skip("tavily-python not installed")

    def test_get_llm_client_anthropic_missing_key_raises(self) -> None:
        """provider=anthropic without ANTHROPIC_API_KEY raises ConfigError."""
        from ai_news_agent.config.loader import ConfigError
        from ai_news_agent.llm.factory import get_llm_client

        bad_cfg = MagicMock()
        bad_cfg.provider = "anthropic"

        bad_secrets = MagicMock()
        bad_secrets.anthropic_api_key = ""
        bad_secrets.web_search_api_key = None

        with pytest.raises(ConfigError, match="ANTHROPIC_API_KEY"):
            get_llm_client(bad_cfg, bad_secrets)

    def test_get_llm_client_google_missing_key_raises(self) -> None:
        """provider=google without GOOGLE_API_KEY raises ConfigError."""
        from ai_news_agent.config.loader import ConfigError
        from ai_news_agent.llm.factory import get_llm_client

        bad_cfg = MagicMock()
        bad_cfg.provider = "google"

        bad_secrets = MagicMock()
        bad_secrets.google_api_key = ""
        bad_secrets.web_search_api_key = None

        with pytest.raises(ConfigError, match="GOOGLE_API_KEY"):
            get_llm_client(bad_cfg, bad_secrets)


# ---------------------------------------------------------------------------
# TestProviderAgnosticInterface — contract invariants (SRC-056)
# ---------------------------------------------------------------------------


class TestProviderAgnosticInterface:
    """
    Verify that:
    1. All concrete clients implement the AbstractLLMClient interface.
    2. parse_structured produces identical output regardless of which client is used.
    3. No provider-specific types leak through the interface.

    Traces: SRC-056 (swap without pipeline changes), SRC-059 (plain prompts),
            SRC-061 (identical parse_structured across providers)
    """

    def test_dummy_client_satisfies_abstract_interface(self, dummy_llm: Any) -> None:
        """DummyLLMClient satisfies AbstractLLMClient interface fully."""
        assert isinstance(dummy_llm, AbstractLLMClient)

    def test_all_methods_callable(self, dummy_llm: Any) -> None:
        """All three abstract methods are callable via the interface."""
        # complete()
        result = dummy_llm.complete(
            messages=[{"role": "user", "content": "test"}],
            model="gpt-4o",
        )
        assert isinstance(result, str)

        # search()
        results = dummy_llm.search("AI news", n_results=3)
        assert isinstance(results, list)

        # parse_structured()
        raw = _make_curation_json()
        parsed = dummy_llm.parse_structured(raw, CurationResponse)
        assert isinstance(parsed, CurationResponse)

    def test_parse_structured_same_output_across_clients(self, dummy_llm: Any) -> None:
        """
        All clients produce identical parse_structured output for the same input.
        This verifies provider-agnostic output parsing. (SRC-061)
        """
        from ai_news_agent.llm.openai_client import _parse_structured_impl

        raw = _make_curation_json()
        dummy_result = dummy_llm.parse_structured(raw, CurationResponse)
        direct_result = _parse_structured_impl(raw, CurationResponse)

        assert dummy_result.themes == direct_result.themes
        assert len(dummy_result.items) == len(direct_result.items)
        assert dummy_result.items[0].url == direct_result.items[0].url

    def test_search_result_shape_invariant(self, dummy_llm: Any) -> None:
        """
        All search() calls return SearchResult objects with the expected fields.
        The shape must be identical regardless of which search tool is used. (SRC-060)
        """
        results = dummy_llm.search("AI regulation", n_results=2)
        for r in results:
            assert hasattr(r, "url")
            assert hasattr(r, "title")
            assert hasattr(r, "snippet")
            assert hasattr(r, "source")
            assert isinstance(r.url, str)
            assert isinstance(r.title, str)


# ---------------------------------------------------------------------------
# TestTokenUsageTracking — quality monitoring (SRC-150)
# ---------------------------------------------------------------------------


class TestTokenUsageTracking:
    """
    Verify that all provider clients expose last_token_usage for monitoring.

    Traces: SRC-150 (log token usage per run)
    """

    def test_openai_client_exposes_token_usage_property(self) -> None:
        """OpenAILLMClient must have last_token_usage property."""
        from ai_news_agent.llm.openai_client import OpenAILLMClient

        mock_search = MagicMock(spec=AbstractSearchTool)
        with patch("ai_news_agent.llm.openai_client.openai"):
            client = OpenAILLMClient(api_key="sk-fake", search_tool=mock_search)

        assert hasattr(client, "last_token_usage")
        assert isinstance(client.last_token_usage, int)
        assert client.last_token_usage == 0

    def test_anthropic_client_exposes_token_usage_property(self) -> None:
        """AnthropicLLMClient must have last_token_usage property."""
        from ai_news_agent.llm.anthropic_client import AnthropicLLMClient

        client = AnthropicLLMClient.__new__(AnthropicLLMClient)
        client._last_token_usage = 0
        assert hasattr(client, "last_token_usage")
        assert client.last_token_usage == 0

    def test_google_client_exposes_token_usage_property(self) -> None:
        """GoogleLLMClient must have last_token_usage property."""
        from ai_news_agent.llm.google_client import GoogleLLMClient

        client = GoogleLLMClient.__new__(GoogleLLMClient)
        client._last_token_usage = 0
        assert hasattr(client, "last_token_usage")
        assert client.last_token_usage == 0
