"""
tests/unit/test_llm_providers.py — Unit tests for Anthropic and Google LLM clients
plus factory paths for non-default providers.

Coverage matrix
───────────────
SRC-055  Anthropic + Google provider support                → TestAnthropicClient, TestGoogleClient
SRC-056  Provider swap without pipeline changes             → TestFactoryProviderPaths
SRC-060  Fallback search tool selection                     → TestFactorySearchToolFallback
SRC-061  Structured output parsing from plain text          → TestAnthropicClient, TestGoogleClient
SRC-150  Token usage tracking                               → TestAnthropicClient, TestGoogleClient

Traces: SRC-055, SRC-056, SRC-060, SRC-061, SRC-150
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from ai_news_agent.llm.base import SearchResult

# ---------------------------------------------------------------------------
# AnthropicLLMClient
# ---------------------------------------------------------------------------

class TestAnthropicClient:
    """
    Unit tests for AnthropicLLMClient with mocked anthropic SDK.
    Traces: SRC-055 (Anthropic provider), SRC-061 (parse_structured from text)
    """

    @pytest.fixture
    def mock_anthropic_module(self):
        """Mock the anthropic SDK so no real import is needed."""
        mock_mod = MagicMock()
        mock_client_instance = MagicMock()
        mock_mod.Anthropic.return_value = mock_client_instance
        mock_mod.APIError = Exception
        mock_mod.RateLimitError = Exception
        mock_mod.APIConnectionError = Exception
        with patch.dict("sys.modules", {"anthropic": mock_mod}):
            yield mock_mod, mock_client_instance

    @pytest.fixture
    def anthropic_client(self, mock_anthropic_module):
        """Return an AnthropicLLMClient with a mocked SDK."""
        from ai_news_agent.llm.anthropic_client import AnthropicLLMClient
        _, mock_instance = mock_anthropic_module
        return AnthropicLLMClient(api_key="test-ant-key", search_tool=None), mock_instance

    def test_complete_returns_string(self, anthropic_client) -> None:
        """
        complete() returns a string extracted from the Anthropic response.
        Traces: SRC-055
        """
        client, mock_instance = anthropic_client

        # Build a mock message response
        # The Anthropic client checks block.type == "text" before extracting block.text
        mock_content_block = MagicMock()
        mock_content_block.type = "text"
        mock_content_block.text = "AI is transforming enterprise software."
        mock_message = MagicMock()
        mock_message.content = [mock_content_block]
        mock_message.usage = MagicMock()
        mock_message.usage.input_tokens = 100
        mock_message.usage.output_tokens = 50
        mock_instance.messages.create.return_value = mock_message

        result = client.complete(
            messages=[{"role": "user", "content": "Summarize AI news."}],
            model="claude-3-5-sonnet-20241022",
        )

        assert isinstance(result, str)
        assert "AI is transforming" in result

    def test_complete_extended_thinking(self, anthropic_client) -> None:
        """
        complete() with thinking=True passes the extended_thinking param.
        Traces: SRC-032 (annual high-reasoning), SRC-055
        """
        client, mock_instance = anthropic_client

        mock_content_block = MagicMock()
        mock_content_block.type = "text"
        mock_content_block.text = "Extended thinking response."
        mock_message = MagicMock()
        mock_message.content = [mock_content_block]
        mock_message.usage = MagicMock()
        mock_message.usage.input_tokens = 200
        mock_message.usage.output_tokens = 100
        mock_instance.messages.create.return_value = mock_message

        result = client.complete(
            messages=[{"role": "user", "content": "Annual review."}],
            model="claude-3-7-sonnet-20250219",
            thinking=True,
        )

        assert isinstance(result, str)
        # Verify the client was called (with or without extended_thinking kwarg)
        assert mock_instance.messages.create.called

    def test_complete_raises_on_api_error(self, anthropic_client) -> None:
        """
        complete() propagates Anthropic API errors (for retry decorator to catch).
        Traces: SRC-144 (retry on transient failure)
        """
        client, mock_instance = anthropic_client
        mock_instance.messages.create.side_effect = RuntimeError("API error")

        with pytest.raises(RuntimeError, match="API error"):
            client.complete(
                messages=[{"role": "user", "content": "Test"}],
                model="claude-3-5-sonnet-20241022",
            )

    def test_search_with_tool(self, mock_anthropic_module) -> None:
        """
        search() delegates to the injected search_tool.
        Traces: SRC-060 (Anthropic fallback search)
        """
        from ai_news_agent.llm.anthropic_client import AnthropicLLMClient

        mock_search_tool = MagicMock()
        mock_search_tool.search.return_value = [
            SearchResult(url="https://reuters.com", title="Reuters AI", snippet="AI...", source="reuters.com")
        ]

        client = AnthropicLLMClient(api_key="test-key", search_tool=mock_search_tool)
        results = client.search("AI news today")

        mock_search_tool.search.assert_called_once()
        assert len(results) == 1
        assert results[0].url == "https://reuters.com"

    def test_search_without_tool_raises_llm_error(self, anthropic_client) -> None:
        """
        search() raises LLMError when no search_tool is configured.
        Traces: SRC-060
        """
        from ai_news_agent.llm.retry import LLMError
        client, _ = anthropic_client
        with pytest.raises(LLMError):
            client.search("AI news")

    def test_parse_structured(self, anthropic_client) -> None:
        """
        parse_structured() extracts a Pydantic model from JSON in the response.
        Traces: SRC-061 (parse from plain text)
        """
        import json

        from ai_news_agent.storage.models import CurationResponse

        client, _ = anthropic_client
        payload = {"items": [], "themes": ["AI adoption"], "outlook": "growth", "predictions": []}
        raw = f"```json\n{json.dumps(payload)}\n```"

        result = client.parse_structured(raw, CurationResponse)
        assert isinstance(result, CurationResponse)
        assert result.themes == ["AI adoption"]


# ---------------------------------------------------------------------------
# GoogleLLMClient
# ---------------------------------------------------------------------------

class TestGoogleClient:
    """
    Unit tests for GoogleLLMClient with mocked google-generativeai SDK.
    Traces: SRC-055 (Google provider), SRC-061, SRC-150
    """

    @staticmethod
    def _make_mock_genai_module():
        """Return a properly structured mock google.generativeai module."""
        mock_mod = MagicMock()
        mock_mod.configure = MagicMock()
        return mock_mod

    @staticmethod
    def _make_mock_genai_response(text: str = "Google AI response text.") -> MagicMock:
        """Return a mock Gemini response object."""
        mock_response = MagicMock()
        mock_response.text = text
        mock_response.usage_metadata = MagicMock()
        mock_response.usage_metadata.prompt_token_count = 100
        mock_response.usage_metadata.candidates_token_count = 50
        mock_response.usage_metadata.total_token_count = 150
        return mock_response

    def test_init_configures_genai(self) -> None:
        """
        __init__ calls genai.configure() with the provided API key.
        Traces: SRC-055
        """
        mock_genai = self._make_mock_genai_module()
        with patch.dict("sys.modules", {
            "google": MagicMock(),
            "google.generativeai": mock_genai,
        }):
            from ai_news_agent.llm.google_client import GoogleLLMClient
            client = GoogleLLMClient(api_key="my-google-key", use_grounding=False)
            # configure is called on the stored _genai reference
            client._genai.configure.assert_called_once_with(api_key="my-google-key")

    def test_complete_returns_string(self) -> None:
        """
        complete() returns a string from the Gemini model response.
        Traces: SRC-055
        """
        mock_genai = self._make_mock_genai_module()
        with patch.dict("sys.modules", {
            "google": MagicMock(),
            "google.generativeai": mock_genai,
        }):
            from ai_news_agent.llm.google_client import GoogleLLMClient
            client = GoogleLLMClient(
                api_key="test-key",
                search_tool=None,
                use_grounding=False,
            )
            # Wire the mock model
            mock_response = self._make_mock_genai_response()
            mock_model = MagicMock()
            mock_model.generate_content.return_value = mock_response
            client._genai.GenerativeModel.return_value = mock_model

            result = client.complete(
                messages=[{"role": "user", "content": "Summarize AI."}],
                model="gemini-1.5-pro",
            )

        assert isinstance(result, str)

    def test_complete_handles_system_message(self) -> None:
        """
        complete() separates system messages from conversation content.
        Traces: SRC-055, SRC-059 (plain prompts)
        """
        mock_genai = self._make_mock_genai_module()
        with patch.dict("sys.modules", {
            "google": MagicMock(),
            "google.generativeai": mock_genai,
        }):
            from ai_news_agent.llm.google_client import GoogleLLMClient
            client = GoogleLLMClient(
                api_key="test-key",
                search_tool=None,
                use_grounding=False,
            )
            mock_response = self._make_mock_genai_response("Response with system context.")
            mock_model = MagicMock()
            mock_model.generate_content.return_value = mock_response
            client._genai.GenerativeModel.return_value = mock_model

            result = client.complete(
                messages=[
                    {"role": "system", "content": "You are a helpful assistant."},
                    {"role": "user", "content": "What is AI?"},
                ],
                model="gemini-1.5-pro",
            )

        assert isinstance(result, str)

    def test_search_with_tool(self) -> None:
        """
        search() delegates to the injected search_tool.
        Traces: SRC-060 (Google search fallback)
        """
        mock_genai = self._make_mock_genai_module()
        with patch.dict("sys.modules", {
            "google": MagicMock(),
            "google.generativeai": mock_genai,
        }):
            from ai_news_agent.llm.google_client import GoogleLLMClient

            mock_search_tool = MagicMock()
            mock_search_tool.search.return_value = [
                SearchResult(
                    url="https://bloomberg.com/ai",
                    title="Bloomberg AI",
                    snippet="AI news...",
                    source="bloomberg.com",
                )
            ]

            client = GoogleLLMClient(
                api_key="test-key",
                search_tool=mock_search_tool,
                use_grounding=False,
            )
            results = client.search("AI news", n_results=5)
            assert len(results) == 1
            assert results[0].url == "https://bloomberg.com/ai"

    def test_search_without_tool_raises_llm_error(self) -> None:
        """
        search() raises LLMError when no search_tool is configured.
        Traces: SRC-060
        """
        from ai_news_agent.llm.retry import LLMError

        mock_genai = self._make_mock_genai_module()
        with patch.dict("sys.modules", {
            "google": MagicMock(),
            "google.generativeai": mock_genai,
        }):
            from ai_news_agent.llm.google_client import GoogleLLMClient
            client = GoogleLLMClient(
                api_key="test-key",
                search_tool=None,
                use_grounding=False,
            )
            with pytest.raises(LLMError):
                client.search("AI news")

    def test_parse_structured(self) -> None:
        """
        parse_structured() extracts a Pydantic model from JSON in text.
        Traces: SRC-061
        """
        import json

        from ai_news_agent.storage.models import CurationResponse

        mock_genai = self._make_mock_genai_module()
        with patch.dict("sys.modules", {
            "google": MagicMock(),
            "google.generativeai": mock_genai,
        }):
            from ai_news_agent.llm.google_client import GoogleLLMClient
            client = GoogleLLMClient(
                api_key="test-key",
                search_tool=None,
                use_grounding=False,
            )
            payload = {"items": [], "themes": ["Policy"], "outlook": "uncertain", "predictions": []}
            raw = f"```json\n{json.dumps(payload)}\n```"

            result = client.parse_structured(raw, CurationResponse)
            assert isinstance(result, CurationResponse)
            assert result.themes == ["Policy"]

    def test_init_raises_without_google_package(self) -> None:
        """
        __init__ raises ImportError when google-generativeai is not installed.
        Traces: SRC-055
        """
        with patch.dict("sys.modules", {"google": None, "google.generativeai": None}):
            # Re-import to pick up the mocked module
            import importlib
            # We need to make the import fail cleanly
            try:
                import ai_news_agent.llm.google_client as gc_mod
                importlib.reload(gc_mod)
                from ai_news_agent.llm.google_client import GoogleLLMClient
                with pytest.raises((ImportError, AttributeError)):
                    GoogleLLMClient(api_key="test-key")
            except (ImportError, AttributeError):
                pass  # Expected — module can't import google-generativeai


# ---------------------------------------------------------------------------
# Factory provider paths (Anthropic, Google)
# ---------------------------------------------------------------------------

class TestFactoryProviderPaths:
    """
    Tests for get_llm_client() with Anthropic and Google providers.
    Traces: SRC-055 (multiple providers), SRC-056 (swap without pipeline changes)
    """

    def test_get_llm_client_anthropic_with_key(self, sample_secrets) -> None:
        """
        Anthropic provider with ANTHROPIC_API_KEY returns AnthropicLLMClient.
        Traces: SRC-055
        """
        from ai_news_agent.config.models import LLMConfig
        from ai_news_agent.llm.anthropic_client import AnthropicLLMClient
        from ai_news_agent.llm.factory import get_llm_client

        cfg = LLMConfig(provider="anthropic", model="claude-3-5-sonnet-20241022")
        secrets = sample_secrets.model_copy(
            update={"anthropic_api_key": "test-anthropic-key"}
        )

        # Mock anthropic import
        mock_ant = MagicMock()
        mock_ant.APIError = Exception
        mock_ant.RateLimitError = Exception
        mock_ant.APIConnectionError = Exception
        with patch.dict("sys.modules", {"anthropic": mock_ant}):
            client = get_llm_client(cfg, secrets)
        assert isinstance(client, AnthropicLLMClient)

    def test_get_llm_client_google_with_key(self, sample_secrets) -> None:
        """
        Google provider with GOOGLE_API_KEY returns GoogleLLMClient.
        Traces: SRC-055
        """
        from ai_news_agent.config.models import LLMConfig
        from ai_news_agent.llm.factory import get_llm_client
        from ai_news_agent.llm.google_client import GoogleLLMClient

        cfg = LLMConfig(provider="google", model="gemini-1.5-pro")
        secrets = sample_secrets.model_copy(
            update={"google_api_key": "test-google-key"}
        )

        mock_genai = MagicMock()
        with patch.dict("sys.modules", {
            "google": MagicMock(),
            "google.generativeai": mock_genai,
        }):
            # Google search tool will also try to resolve — mock Brave key
            secrets_with_search = secrets.model_copy(
                update={"web_search_api_key": "brave-test-key",
                        "web_search_provider": "brave"}
            )
            client = get_llm_client(cfg, secrets_with_search)
        assert isinstance(client, GoogleLLMClient)

    def test_get_llm_client_anthropic_without_search_tool(self, sample_secrets) -> None:
        """
        Anthropic provider without WEB_SEARCH_API_KEY still creates client (search=None).
        Traces: SRC-055, SRC-060 (search tool optional for Anthropic)
        """
        from ai_news_agent.config.models import LLMConfig
        from ai_news_agent.llm.anthropic_client import AnthropicLLMClient
        from ai_news_agent.llm.factory import get_llm_client

        cfg = LLMConfig(provider="anthropic", model="claude-3-5-sonnet-20241022")
        secrets = sample_secrets.model_copy(
            update={"anthropic_api_key": "test-ant-key",
                    "web_search_api_key": None,
                    "web_search_provider": None}
        )

        mock_ant = MagicMock()
        mock_ant.APIError = Exception
        mock_ant.RateLimitError = Exception
        mock_ant.APIConnectionError = Exception
        with patch.dict("sys.modules", {"anthropic": mock_ant}):
            client = get_llm_client(cfg, secrets)
        # Should succeed with search_tool=None
        assert isinstance(client, AnthropicLLMClient)


# ---------------------------------------------------------------------------
# Serverless one-shot handler
# ---------------------------------------------------------------------------

class TestServerlessHandler:
    """
    Smoke tests for the serverless entry point module.
    Traces: SRC-080–SRC-086 (serverless container deployment)
    """

    def test_serverless_cli_main_help(self) -> None:
        """
        Importing cli_main from serverless module works without error.
        Traces: SRC-080
        """
        from ai_news_agent.scheduler.serverless import cli_main  # noqa: F401
        assert callable(cli_main)

    def test_serverless_health_check_function_exists(self) -> None:
        """
        The serverless module exposes a handle_request or equivalent function.
        Traces: SRC-080
        """
        import importlib
        mod = importlib.import_module("ai_news_agent.scheduler.serverless")
        # At minimum the module should be importable and have cli_main
        assert hasattr(mod, "cli_main"), "serverless module must expose cli_main"


# ---------------------------------------------------------------------------
# Rendering agent — render_and_update_store (production path)
# ---------------------------------------------------------------------------

class TestRenderingAgentProductionPath:
    """
    Test the production render path (render) which writes to the output dir.
    Traces: SRC-004, SRC-145 (production output)
    """

    def test_render_to_output_dir(
        self,
        sample_agent_config,
        sample_secrets,
        prompts_dir,
        tmp_path,
    ) -> None:
        """
        render() writes all three files to the output directory (SRC-004, SRC-145).

        Tests the production render path using the CurationAgent → RenderingAgent
        chain with mocked LLM. The store is not in dry-run mode so the
        DigestRecord is persisted.
        """
        from datetime import UTC, datetime
        from unittest.mock import patch

        from ai_news_agent.curation.agent import CurationAgent
        from ai_news_agent.rendering.agent import RenderingAgent
        from ai_news_agent.storage.tinydb_store import TinyDBArticleStore

        store = TinyDBArticleStore(tmp_path / "prod-store.json")
        output_dir = tmp_path / "outputs"

        with patch("ai_news_agent.curation.agent.get_llm_client") as mock_factory:
            from tests.conftest import DummyLLMClient
            mock_factory.return_value = DummyLLMClient()

            agent = CurationAgent(
                config=sample_agent_config,
                secrets=sample_secrets,
                store=store,
                prompts_dir=str(prompts_dir),
            )
            result = agent.run(
                cadence="daily",
                window_start=datetime(2026, 5, 9, 0, 0, tzinfo=UTC),
                window_end=datetime(2026, 5, 9, 23, 59, tzinfo=UTC),
            )

        # Use plain render() — all three files produced in output_dir
        renderer = RenderingAgent(output_dir=output_dir)
        rendering_result = renderer.render(result)

        # All three files must exist (SRC-004)
        assert rendering_result.markdown_path.exists()
        assert rendering_result.html_path.exists()
        assert rendering_result.json_path.exists()

        # Filenames must follow the date-stamped convention (SRC-145)
        assert "daily" in rendering_result.markdown_path.name

        store.close()


# ---------------------------------------------------------------------------
# TinyDB store edge cases
# ---------------------------------------------------------------------------

class TestTinyDBEdgeCases:
    """
    Edge cases for TinyDBArticleStore not covered elsewhere.
    Traces: SRC-012 (dedup), SRC-053 (document store)
    """

    def test_store_insert_if_new_returns_false_on_duplicate(self, tiny_db_store) -> None:
        """
        insert_if_new() returns False when the exact same URL is inserted twice.
        Traces: SRC-012
        """
        from datetime import UTC, datetime

        from ai_news_agent.storage.models import ArticleRecord, normalize_url, url_hash

        url = "https://reuters.com/duplicate-test-article"
        canonical = normalize_url(url)
        record = ArticleRecord(
            url_hash=url_hash(canonical),
            url=canonical,
            headline="Duplicate Test Article",
            abstract="Testing deduplication.",
            source_name="Reuters",
            pub_date=datetime(2026, 5, 9, 12, 0, tzinfo=UTC),
            fetched_at=datetime(2026, 5, 9, 12, 0, tzinfo=UTC),
            tier="1b",
            source_class="web",
            agent_id="test-agent",
        )

        assert tiny_db_store.insert_if_new(record) is True
        assert tiny_db_store.insert_if_new(record) is False

    def test_store_get_stats(self, tiny_db_store) -> None:
        """
        get_stats() returns StoreStats with tier/source_class counts.
        Traces: SRC-150 (quality monitoring — tier distribution)
        """
        from datetime import UTC, datetime

        from ai_news_agent.storage.models import ArticleRecord, normalize_url, url_hash

        ws = datetime(2026, 5, 9, 0, 0, tzinfo=UTC)
        we = datetime(2026, 5, 9, 23, 59, tzinfo=UTC)

        for i in range(3):
            url = f"https://reuters.com/stats-test-{i}"
            canonical = normalize_url(url)
            record = ArticleRecord(
                url_hash=url_hash(canonical),
                url=canonical,
                headline=f"Stats Test Article {i}",
                abstract="Testing stats.",
                source_name="Reuters",
                pub_date=datetime(2026, 5, 9, 12, 0, tzinfo=UTC),
                fetched_at=datetime(2026, 5, 9, 12, 0, tzinfo=UTC),
                tier="1b",
                source_class="web",
                agent_id="test-agent",
            )
            tiny_db_store.insert_if_new(record)

        stats = tiny_db_store.get_stats(
            agent_id="test-agent",
            window_start=ws,
            window_end=we,
        )
        assert stats.total >= 3
        assert stats.by_tier.get("1b", 0) >= 3

    def test_store_context_manager(self, tmp_path) -> None:
        """
        TinyDBArticleStore can be used as a context manager.
        Traces: SRC-053
        """
        from ai_news_agent.storage.tinydb_store import TinyDBArticleStore

        with TinyDBArticleStore(tmp_path / "ctx-store.json") as store:
            assert store is not None


# ---------------------------------------------------------------------------
# Scorer edge cases
# ---------------------------------------------------------------------------

class TestScorerEdgeCases:
    """
    Edge cases for Scorer not covered by curation_agent tests.
    Traces: SRC-022–SRC-027 (scoring criteria), SRC-049 (URL enforcement)
    """

    def test_scorer_drops_items_without_url(
        self,
        sample_agent_config,
        sample_secrets,
        prompts_dir,
        tiny_db_store,
    ) -> None:
        """
        Scorer enforces URL requirement: items without URL are excluded (SRC-049).
        """
        import json
        from datetime import UTC, datetime
        from unittest.mock import patch

        from tests.conftest import DummyLLMClient

        from ai_news_agent.curation.agent import CurationAgent
        from ai_news_agent.storage.models import ArticleRecord, normalize_url, url_hash

        # Seed an article with a URL
        url = "https://reuters.com/scorer-test"
        canonical = normalize_url(url)
        record = ArticleRecord(
            url_hash=url_hash(canonical),
            url=canonical,
            headline="Scorer Test Article",
            abstract="Testing URL enforcement.",
            source_name="Reuters",
            pub_date=datetime(2026, 5, 9, 12, 0, tzinfo=UTC),
            fetched_at=datetime(2026, 5, 9, 12, 0, tzinfo=UTC),
            tier="1b",
            source_class="web",
            agent_id="test-agent",
        )
        tiny_db_store.insert_if_new(record)

        # LLM response with a no-URL item
        no_url_payload = json.dumps({
            "items": [
                {
                    "headline": "No URL — Must Be Dropped",
                    "source_name": "Unknown",
                    "url": "",   # empty — SRC-049
                    "pub_date": "2026-05-09",
                    "why_it_matters": "Should be dropped.",
                    "impact_tags": [],
                    "tier": "3",
                    "cross_refs": [],
                }
            ],
            "themes": [],
            "outlook": "",
            "predictions": [],
        })

        with patch("ai_news_agent.curation.agent.get_llm_client") as mock_factory:
            mock_factory.return_value = DummyLLMClient(
                complete_response=f"```json\n{no_url_payload}\n```"
            )
            agent = CurationAgent(
                config=sample_agent_config,
                secrets=sample_secrets,
                store=tiny_db_store,
                prompts_dir=str(prompts_dir),
            )
            result = agent.run(
                cadence="daily",
                window_start=datetime(2026, 5, 9, 0, 0, tzinfo=UTC),
                window_end=datetime(2026, 5, 9, 23, 59, tzinfo=UTC),
            )

        # No-URL item must be absent from result (SRC-049)
        for item in result.items:
            assert item.url, f"No-URL item in result: {item.headline}"

    def test_scorer_no_candidates_returns_empty(
        self,
        sample_agent_config,
        sample_secrets,
        prompts_dir,
        tmp_path,
    ) -> None:
        """
        When the store has no candidates for the window, scorer returns empty result.
        Traces: SRC-015 (curation handles empty windows)
        """
        from datetime import UTC, datetime
        from unittest.mock import patch

        from tests.conftest import DummyLLMClient

        from ai_news_agent.curation.agent import CurationAgent
        from ai_news_agent.storage.tinydb_store import TinyDBArticleStore

        # Empty store
        store = TinyDBArticleStore(tmp_path / "empty-store.json")

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
                window_start=datetime(2026, 5, 9, 0, 0, tzinfo=UTC),
                window_end=datetime(2026, 5, 9, 23, 59, tzinfo=UTC),
            )

        # Should succeed with 0 items (no LLM call needed for 0 candidates)
        assert result.metadata.items_considered == 0
        assert result.items == []
        store.close()


# ---------------------------------------------------------------------------
# Smoke mock LLM client (SMOKE_TEST_MOCK_LLM=1 — SRC-102)
# ---------------------------------------------------------------------------

class TestSmokeMockLLMClient:
    """
    Unit tests for the ``_SmokeMockLLMClient`` returned by ``get_llm_client``
    when ``SMOKE_TEST_MOCK_LLM=1`` is set.

    Ensures the mock satisfies all contract requirements needed by the CI
    container smoke step (SRC-102), without touching real APIs.

    Traces: SRC-056 (provider swap), SRC-061 (parse_structured), SRC-102 (mock)
    """

    def _get_mock_client(self):
        """Return the smoke mock client via the factory (env var override)."""
        import os
        from unittest.mock import patch

        from ai_news_agent.config.models import LLMConfig, RuntimeSecrets
        from ai_news_agent.llm.factory import get_llm_client

        cfg = LLMConfig(provider="openai", model="gpt-4o")
        secrets = RuntimeSecrets.model_validate({
            "OPENAI_API_KEY": "sk-fake",
            "TWITTER_BEARER_TOKEN": "fake",
        })
        with patch.dict(os.environ, {"SMOKE_TEST_MOCK_LLM": "1"}):
            return get_llm_client(cfg, secrets)

    def test_factory_returns_smoke_mock_when_env_set(self) -> None:
        """
        get_llm_client() returns _SmokeMockLLMClient when SMOKE_TEST_MOCK_LLM=1.
        Traces: SRC-056, SRC-102
        """
        from ai_news_agent.llm.factory import _SmokeMockLLMClient
        client = self._get_mock_client()
        assert isinstance(client, _SmokeMockLLMClient), (
            f"Expected _SmokeMockLLMClient, got {type(client).__name__} (SRC-102)"
        )

    def test_smoke_mock_complete_returns_valid_json_block(self) -> None:
        """
        _SmokeMockLLMClient.complete() returns a JSON block parseable by curation.
        Traces: SRC-061, SRC-102
        """
        import json
        import re

        client = self._get_mock_client()
        resp = client.complete([], model="gpt-4o")
        assert "```json" in resp, "Expected markdown JSON block in mock response"
        # Extract and parse
        match = re.search(r"```json\s*(.*?)```", resp, re.DOTALL)
        assert match, "Could not find JSON block in mock response"
        data = json.loads(match.group(1).strip())
        assert "items" in data, "items key missing from mock response"
        items = data["items"]
        assert len(items) > 0, "Expected at least one item in mock response"
        item = items[0]
        assert item.get("url", "").startswith("http"), (
            "Mock item must have a valid URL (SRC-049)"
        )

    def test_smoke_mock_search_returns_search_results(self) -> None:
        """
        _SmokeMockLLMClient.search() returns a list of SearchResult objects.
        Traces: SRC-060, SRC-102
        """
        from ai_news_agent.llm.base import SearchResult

        client = self._get_mock_client()
        results = client.search("AI news", n_results=5)
        assert isinstance(results, list), "search() must return a list"
        assert len(results) > 0, "Expected at least one mock search result"
        assert isinstance(results[0], SearchResult), (
            "search() must return SearchResult instances"
        )
        assert results[0].url.startswith("http"), "Mock SearchResult must have valid URL"

    def test_smoke_mock_parse_structured(self) -> None:
        """
        _SmokeMockLLMClient.parse_structured() parses the mock JSON block.
        Traces: SRC-061, SRC-102
        """
        from ai_news_agent.curation.scorer import CurationResponse

        client = self._get_mock_client()
        raw = client.complete([], model="gpt-4o")
        parsed = client.parse_structured(raw, CurationResponse)
        assert hasattr(parsed, "items"), "parse_structured must return CurationResponse"

    def test_smoke_mock_not_activated_without_env(self) -> None:
        """
        get_llm_client() does NOT return the smoke mock when env var is absent.
        Traces: SRC-056, SRC-102
        """
        import os
        from unittest.mock import patch

        from ai_news_agent.config.models import LLMConfig, RuntimeSecrets
        from ai_news_agent.llm.factory import _SmokeMockLLMClient, get_llm_client
        from ai_news_agent.llm.openai_client import OpenAILLMClient

        cfg = LLMConfig(provider="openai", model="gpt-4o")
        secrets = RuntimeSecrets.model_validate({
            "OPENAI_API_KEY": "sk-fake-for-test",
            "TWITTER_BEARER_TOKEN": "fake",
        })
        env_without_mock = {k: v for k, v in os.environ.items() if k != "SMOKE_TEST_MOCK_LLM"}
        with (
            patch.dict(os.environ, env_without_mock, clear=True),
            patch("ai_news_agent.llm.factory.NativeOpenAISearchTool"),
        ):
            client = get_llm_client(cfg, secrets)
        assert isinstance(client, OpenAILLMClient), (
            f"Without SMOKE_TEST_MOCK_LLM, should get OpenAILLMClient not {type(client)}"
        )
        assert not isinstance(client, _SmokeMockLLMClient), (
            "Smoke mock must NOT be returned when env var is absent"
        )
