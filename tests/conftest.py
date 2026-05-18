"""
tests/conftest.py — Shared fixtures: LLM + Twitter mocks, sample data factories.
Traces: SRC-098 (pytest; mock LLM and Twitter calls), SRC-049 (URL enforcement tests),
        SRC-012 (dedup tests), SRC-061 (output parsing tests)
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from typing import TYPE_CHECKING, Any

import pytest

from ai_news_agent.config.models import (
    AgentConfig,
    LimitsConfig,
    LLMConfig,
    RuntimeSecrets,
    SourcesConfig,
    TwitterConfig,
    TwitterHandleConfig,
)
from ai_news_agent.llm.base import AbstractLLMClient, SearchResult
from ai_news_agent.storage.models import (
    ArticleRecord,
    CuratedItem,
    DigestMetadata,
    TweetSignal,
    normalize_url,
    url_hash,
)

if TYPE_CHECKING:
    from pathlib import Path

# ---------------------------------------------------------------------------
# Dummy LLM client (replaces all real LLM calls in unit tests)
# Traces: SRC-098 (mock LLM calls)
# ---------------------------------------------------------------------------

class DummyLLMClient(AbstractLLMClient):
    """
    Test double for AbstractLLMClient.

    Configured via ``curation_response`` to return a deterministic JSON block.
    Traces: SRC-056 (provider-agnostic), SRC-098 (mocked LLM)
    """

    def __init__(
        self,
        complete_response: str | None = None,
        search_results: list[SearchResult] | None = None,
    ) -> None:
        self.complete_calls: list[dict[str, Any]] = []
        self.search_calls: list[str] = []
        self._complete_response = complete_response or _default_curation_json()
        self._search_results = search_results or _default_search_results()

    def complete(
        self,
        messages: list[dict[str, str]],
        model: str,
        temperature: float = 0.2,
        **kwargs: Any,
    ) -> str:
        self.complete_calls.append(
            {"messages": messages, "model": model, "temperature": temperature, **kwargs}
        )
        return self._complete_response

    def search(
        self,
        query: str,
        n_results: int = 10,
        budget_hint: str = "normal",
    ) -> list[SearchResult]:
        self.search_calls.append(query)
        return self._search_results

    def parse_structured(self, raw: str, schema_cls: type) -> Any:
        """Parse the dummy JSON block — same algorithm as OpenAILLMClient."""
        import re
        json_block_re = re.compile(r"```json\s*(.*?)```", re.DOTALL | re.IGNORECASE)
        match = json_block_re.search(raw)
        json_str = match.group(1).strip() if match else raw.strip()
        try:
            data = json.loads(json_str)
        except json.JSONDecodeError:
            start = json_str.find("{")
            end = json_str.rfind("}") + 1
            data = json.loads(json_str[start:end])
        return schema_cls.model_validate(data)


def _default_curation_json() -> str:
    """A valid LLM response containing a single curated item with a URL."""
    payload = {
        "items": [
            {
                "headline": "AI Reshapes Enterprise Software Market",
                "source_name": "Reuters",
                "url": "https://reuters.com/ai-enterprise-2026",
                "pub_date": "2026-05-10",
                "why_it_matters": (
                    "Major enterprise software vendors are integrating AI into core products, "
                    "accelerating a structural shift in business software procurement. "
                    "The displacement of legacy vendors signals a multi-year transition."
                ),
                "impact_tags": ["business_impact"],
                "tier": "1b",
                "cross_refs": [],
                "twitter_handle": None,
                "tweet_url": None,
            }
        ],
        "themes": ["Enterprise AI adoption", "Market consolidation"],
        "outlook": "Expect continued M&A activity in the AI tooling space.",
        "predictions": [],
    }
    return f"```json\n{json.dumps(payload)}\n```"


def _default_search_results() -> list[SearchResult]:
    return [
        SearchResult(
            url="https://reuters.com/ai-enterprise-2026",
            title="AI Reshapes Enterprise Software Market",
            snippet="Major enterprise software vendors...",
            source="reuters.com",
        ),
        SearchResult(
            url="https://bloomberg.com/ai-regulation-2026",
            title="EU AI Act Enters Enforcement Phase",
            snippet="The EU AI Act is now enforcing...",
            source="bloomberg.com",
        ),
    ]


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def dummy_llm() -> DummyLLMClient:
    """Return a pre-configured DummyLLMClient. Traces: SRC-098."""
    return DummyLLMClient()


@pytest.fixture
def sample_agent_config() -> AgentConfig:
    """
    Minimal AgentConfig for tests — no real YAML needed.
    Traces: SRC-071–SRC-073
    """
    return AgentConfig(
        agent_id="test-agent",
        llm=LLMConfig(provider="openai", model="gpt-4o"),
        sources=SourcesConfig(
            custom=[],
            tier_1b=["reuters.com", "bloomberg.com"],
            tier_2=["openai.com", "anthropic.com"],
            tier_3=["techcrunch.com"],
            tier_4=["brookings.edu"],
        ),
        twitter=TwitterConfig(
            enabled=True,
            handles=[
                TwitterHandleConfig(handle="karpathy", weight=1.0),
                TwitterHandleConfig(handle="sama", weight=1.0),
            ],
        ),
        limits=LimitsConfig(
            daily_top_n=5,
            weekly_top_n=5,
            monthly_top_n=5,
            annual_top_n=5,
        ),
        output_dir="outputs/test-agent",
    )


@pytest.fixture
def sample_secrets() -> RuntimeSecrets:
    """
    Fake RuntimeSecrets for tests — never real keys.
    Traces: SRC-073 (secrets from env vars), SRC-098 (mocked)
    """
    return RuntimeSecrets.model_validate(
        {
            "OPENAI_API_KEY": "sk-test-fake",
            "TWITTER_BEARER_TOKEN": "test-bearer-fake",
            "WEB_SEARCH_API_KEY": None,
            "WEB_SEARCH_PROVIDER": None,
        }
    )


@pytest.fixture
def sample_article() -> ArticleRecord:
    """A valid ArticleRecord with a URL (SRC-011, SRC-012)."""
    raw_url = "https://reuters.com/ai-enterprise-2026"
    canonical = normalize_url(raw_url)
    return ArticleRecord(
        url_hash=url_hash(canonical),
        url=canonical,
        headline="AI Reshapes Enterprise Software Market",
        abstract="Major enterprise software vendors are integrating AI.",
        source_name="Reuters",
        pub_date=datetime(2026, 5, 10, 12, 0, tzinfo=UTC),
        fetched_at=datetime(2026, 5, 10, 13, 0, tzinfo=UTC),
        tier="1b",
        source_class="web",
        agent_id="test-agent",
    )


@pytest.fixture
def sample_article_no_url() -> ArticleRecord:
    """An ArticleRecord with empty URL — should be dropped (SRC-049, SRC-141)."""
    return ArticleRecord(
        url_hash="no-url-hash",
        url="",          # intentionally empty — must be dropped at curation + rendering
        headline="Article With No URL",
        abstract="This should be dropped.",
        source_name="Unknown",
        pub_date=datetime(2026, 5, 10, tzinfo=UTC),
        fetched_at=datetime(2026, 5, 10, tzinfo=UTC),
        tier="3",
        source_class="web",
        agent_id="test-agent",
    )


@pytest.fixture
def sample_tweet_signal() -> TweetSignal:
    """A valid TweetSignal. Traces: SRC-047, SRC-067."""
    return TweetSignal(
        tweet_id="1234567890",
        handle="karpathy",
        text="Fascinating paper on enterprise AI adoption just dropped.",
        created_at=datetime(2026, 5, 10, 10, 0, tzinfo=UTC),
        linked_urls=["https://arxiv.org/abs/2026.test"],
        agent_id="test-agent",
        fetched_at=datetime(2026, 5, 10, 10, 5, tzinfo=UTC),
        weight=1.0,
    )


@pytest.fixture
def sample_curated_item() -> CuratedItem:
    """A valid CuratedItem with URL. Traces: SRC-048."""
    return CuratedItem(
        headline="AI Reshapes Enterprise Software Market",
        source_name="Reuters",
        url="https://reuters.com/ai-enterprise-2026",
        pub_date=date(2026, 5, 10),
        why_it_matters=(
            "Major enterprise software vendors are integrating AI into core products. "
            "This signals a structural shift. Legacy vendors face displacement risk."
        ),
        impact_tags=["business_impact"],
        tier="1b",
        cross_refs=[],
        twitter_handle=None,
        tweet_url=None,
        prompt_version="sha256:abc123",
    )


@pytest.fixture
def sample_curated_item_no_url() -> CuratedItem:
    """A CuratedItem with empty URL — must be dropped (SRC-049, SRC-141)."""
    return CuratedItem(
        headline="Article Without URL",
        source_name="Unknown",
        url="",    # empty — must be dropped
        pub_date=date(2026, 5, 10),
        why_it_matters="This item should be dropped by all renderers.",
        impact_tags=[],
        tier="unknown",
        cross_refs=[],
        twitter_handle=None,
        tweet_url=None,
        prompt_version="sha256:abc123",
    )


@pytest.fixture
def sample_digest_metadata() -> DigestMetadata:
    """A valid DigestMetadata. Traces: SRC-129, SRC-150."""
    return DigestMetadata(
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
        items_by_tier={"1b": 3, "2": 2},
        items_by_source_class={"web": 5},
        twitter_signal_available=True,
        tweet_api_call_count=9,
        token_usage=4200,
    )


@pytest.fixture
def prompts_dir(tmp_path: Path) -> Path:
    """
    Temporary directory containing minimal prompt template files for tests.
    Traces: SRC-113 (prompts directory), SRC-129 (SHA-256 hash)
    """
    prompts = tmp_path / "prompts"
    prompts.mkdir()
    for cadence in ("daily", "weekly", "monthly", "annual"):
        (prompts / f"{cadence}.md").write_text(
            f"# {cadence.title()} Prompt\n"
            f"Window: {{{{window_start_iso}}}} to {{{{window_end_iso}}}}\n"
            f"Twitter signal:\n{{{{twitter_signal_section}}}}\n"
            f"Budget: {{{{search_budget_directive}}}}\n"
            f"Top N: {{{{top_n}}}}\n"
            f"Year: {{{{year}}}}\n"
            f"Year+1: {{{{year_plus_1}}}}\n",
            encoding="utf-8",
        )
    return prompts


@pytest.fixture
def tiny_db_store(tmp_path: Path):
    """
    A real TinyDBArticleStore backed by a temp directory.
    Traces: SRC-053 (TinyDB document store)
    """
    from ai_news_agent.storage.tinydb_store import TinyDBArticleStore
    db_path = tmp_path / "store.json"
    store = TinyDBArticleStore(db_path)
    yield store
    store.close()
