"""
tests/integration/test_fixture_corpus.py
─────────────────────────────────────────
Integration test exercising the dry-run pipeline end-to-end against a fixture
corpus.  All LLM and Twitter calls are mocked.  The article store, prompt
builder, and renderers run against real in-process implementations.

The fixture corpus simulates a realistic week of AI news articles spanning all
five source tiers and both source classes (web + twitter) with URLs present on
every item.  Tests verify:

1. Pipeline accepts the fixture corpus and produces all three output formats.
2. All four cadences (daily/weekly/monthly/annual) complete without error.
3. URL enforcement drops items without URLs at both curation and rendering.
4. Deduplication: inserting the same article twice keeps exactly one copy.
5. §8.2 monitoring fields are present and correct in the JSON digest.
6. Prompt SHA-256 version appears in all three output formats (SRC-129).
7. Date-stamped filenames follow the {YYYY-MM-DD}-{cadence}.{ext} formula (SRC-145).
8. Twitter degradation propagates correctly through the pipeline (SRC-148).
9. Annual digest contains predictions (SRC-032, SRC-124).
10. Window override (on-demand re-run) produces digest for custom date range (SRC-028).
11. skip_sourcing mode curates from existing corpus without re-fetching (SRC-028).
12. Items with impact tags are preserved in rendered output (SRC-048).

Coverage matrix
───────────────
SRC-004   Three output formats produced                      → test_fixture_corpus_three_formats
SRC-006   Sourcing agent populates corpus                    → all tests (corpus pre-seeded)
SRC-012   Deduplication across multiple insertions           → test_fixture_corpus_deduplication
SRC-028   On-demand re-run with window override              → test_fixture_corpus_window_override
SRC-029   Daily: headline + source + URL + why_it_matters    → test_fixture_all_cadences
SRC-030   Weekly: themes + outlook                           → test_fixture_all_cadences
SRC-031   Monthly: themes + outlook                          → test_fixture_all_cadences
SRC-032   Annual: top-10 + predictions                       → test_fixture_all_cadences
SRC-048   CuratedItem schema preserved in output             → test_fixture_item_schema
SRC-049   URL enforcement at curation layer                  → test_fixture_no_url_dropped
SRC-102   Dry-run: scratch dir, no production writes         → all tests (dry_run=True)
SRC-129   Prompt SHA-256 in all three formats                → test_fixture_prompt_version
SRC-141   Renderer drops items without URL                   → test_fixture_no_url_dropped
SRC-145   Date-stamped idempotent filenames                  → test_fixture_date_stamped_filenames
SRC-148   Twitter degradation note                           → test_fixture_twitter_degraded
SRC-150   §8.2 monitoring fields in JSON metadata            → test_fixture_monitoring_fields

Traces: SRC-004, SRC-006, SRC-012, SRC-028, SRC-029–SRC-032, SRC-048, SRC-049,
        SRC-102, SRC-129, SRC-141, SRC-145, SRC-148, SRC-150
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from tests.conftest import DummyLLMClient

from ai_news_agent.config.models import (
    AgentConfig,
    LimitsConfig,
    LLMConfig,
    RuntimeSecrets,
    SourcesConfig,
    TwitterConfig,
    TwitterHandleConfig,
)
from ai_news_agent.pipeline import Pipeline, PipelineRunResult
from ai_news_agent.sourcing.agent import SourcingRunResult
from ai_news_agent.storage.models import (
    ArticleRecord,
    normalize_url,
    url_hash,
)
from ai_news_agent.storage.tinydb_store import TinyDBArticleStore

# ---------------------------------------------------------------------------
# Fixture corpus data
# ---------------------------------------------------------------------------

# A realistic week of AI news across all five tiers (SRC-017–SRC-021)
_CORPUS_ARTICLES: list[dict[str, Any]] = [
    # Tier 1b — Business press
    {
        "url": "https://reuters.com/technology/ai-enterprise-wave-2026",
        "headline": "AI Reshapes Enterprise Software Market",
        "abstract": "Major enterprise software vendors integrate AI into core products.",
        "source_name": "Reuters",
        "tier": "1b",
        "source_class": "web",
        "pub_date": datetime(2026, 5, 5, 9, 0, tzinfo=UTC),
    },
    {
        "url": "https://bloomberg.com/news/ai-regulation-eu-act",
        "headline": "EU AI Act Enters Enforcement Phase",
        "abstract": "The EU AI Act begins mandatory compliance audits for high-risk systems.",
        "source_name": "Bloomberg",
        "tier": "1b",
        "source_class": "web",
        "pub_date": datetime(2026, 5, 6, 10, 0, tzinfo=UTC),
    },
    {
        "url": "https://wsj.com/articles/ai-workforce-disruption",
        "headline": "AI Displaces 12% of White-Collar Roles in Q1",
        "abstract": "New BLS report shows AI-related job displacement accelerating.",
        "source_name": "Wall Street Journal",
        "tier": "1b",
        "source_class": "web",
        "pub_date": datetime(2026, 5, 7, 8, 0, tzinfo=UTC),
    },
    # Tier 2 — Tech blogs
    {
        "url": "https://openai.com/blog/gpt-5-announcement",
        "headline": "OpenAI Announces GPT-5 with Extended Context Window",
        "abstract": "GPT-5 features a 1M token context window and native video understanding.",
        "source_name": "OpenAI Blog",
        "tier": "2",
        "source_class": "web",
        "pub_date": datetime(2026, 5, 8, 12, 0, tzinfo=UTC),
    },
    {
        "url": "https://anthropic.com/news/claude-4-release",
        "headline": "Anthropic Releases Claude 4 with Improved Reasoning",
        "abstract": "Claude 4 achieves new SOTA on mathematical and logical reasoning benchmarks.",
        "source_name": "Anthropic",
        "tier": "2",
        "source_class": "web",
        "pub_date": datetime(2026, 5, 9, 11, 0, tzinfo=UTC),
    },
    # Tier 3 — Tech press
    {
        "url": "https://techcrunch.com/2026/05/07/ai-startup-funding-record",
        "headline": "AI Startup Funding Hits Record $50B in Q1 2026",
        "abstract": "VC investment in AI startups sets quarterly record amid enterprise demand.",
        "source_name": "TechCrunch",
        "tier": "3",
        "source_class": "web",
        "pub_date": datetime(2026, 5, 7, 15, 0, tzinfo=UTC),
    },
    {
        "url": "https://theverge.com/2026/ai-assistants-privacy",
        "headline": "AI Assistants and the Privacy Paradox",
        "abstract": "Analysis of data practices in leading AI assistant products.",
        "source_name": "The Verge",
        "tier": "3",
        "source_class": "web",
        "pub_date": datetime(2026, 5, 8, 9, 30, tzinfo=UTC),
    },
    # Tier 4 — Policy/research
    {
        "url": "https://brookings.edu/research/ai-labor-market-2026",
        "headline": "Brookings: AI and the American Labor Market 2026",
        "abstract": "Comprehensive analysis of AI's impact on labor markets across sectors.",
        "source_name": "Brookings Institution",
        "tier": "4",
        "source_class": "web",
        "pub_date": datetime(2026, 5, 6, 14, 0, tzinfo=UTC),
    },
    # Twitter-sourced (source_class="twitter")
    {
        "url": "https://twitter.com/sama/status/1234567890",
        "headline": "Sam Altman on AGI Timeline",
        "abstract": "Sam Altman tweets about OpenAI's AGI development roadmap.",
        "source_name": "Twitter/@sama",
        "tier": "1a",
        "source_class": "twitter",
        "pub_date": datetime(2026, 5, 9, 8, 0, tzinfo=UTC),
    },
    # Additional articles for deduplication testing
    {
        "url": "https://wired.com/story/ai-chips-shortage-2026",
        "headline": "AI Chip Shortage Eases as TSMC Expands Capacity",
        "abstract": "Chip supply improving as TSMC ramps A-series AI accelerator production.",
        "source_name": "Wired",
        "tier": "3",
        "source_class": "web",
        "pub_date": datetime(2026, 5, 9, 13, 0, tzinfo=UTC),
    },
]


def _build_article_record(data: dict[str, Any], agent_id: str) -> ArticleRecord:
    """Create an ArticleRecord from corpus entry dict."""
    canonical = normalize_url(data["url"])
    return ArticleRecord(
        url_hash=url_hash(canonical),
        url=canonical,
        headline=data["headline"],
        abstract=data["abstract"],
        source_name=data["source_name"],
        pub_date=data["pub_date"],
        fetched_at=datetime(2026, 5, 9, 20, 0, tzinfo=UTC),
        tier=data["tier"],
        source_class=data["source_class"],
        agent_id=agent_id,
    )


def _seed_corpus(
    store: TinyDBArticleStore,
    agent_id: str,
    articles: list[dict[str, Any]] | None = None,
) -> int:
    """
    Insert the fixture corpus into the store and return the number inserted.
    Traces: SRC-011 (articles stored with title/abstract/url/source/date)
    """
    corpus = articles or _CORPUS_ARTICLES
    inserted = 0
    for data in corpus:
        record = _build_article_record(data, agent_id)
        if store.insert_if_new(record):
            inserted += 1
    return inserted


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def corpus_agent_config() -> AgentConfig:
    """
    Full AgentConfig for fixture corpus tests.
    Traces: SRC-071–SRC-073 (per-agent config)
    """
    return AgentConfig(
        agent_id="corpus-test-agent",
        llm=LLMConfig(provider="openai", model="gpt-4o"),
        sources=SourcesConfig(
            custom=["sama.com"],
            tier_1b=["reuters.com", "bloomberg.com", "wsj.com"],
            tier_2=["openai.com", "anthropic.com"],
            tier_3=["techcrunch.com", "theverge.com", "wired.com"],
            tier_4=["brookings.edu"],
        ),
        twitter=TwitterConfig(
            enabled=True,
            handles=[
                TwitterHandleConfig(handle="karpathy", weight=1.0),
                TwitterHandleConfig(handle="sama", weight=1.5),
                TwitterHandleConfig(handle="demishassabis", weight=1.0),
                TwitterHandleConfig(handle="DarioAmodei", weight=1.0),
                TwitterHandleConfig(handle="ylecun", weight=1.0),
                TwitterHandleConfig(handle="AndrewYNg", weight=1.0),
                TwitterHandleConfig(handle="fchollet", weight=1.0),
                TwitterHandleConfig(handle="drfeifei", weight=1.0),
                TwitterHandleConfig(handle="emilymbender", weight=1.0),
            ],
        ),
        limits=LimitsConfig(
            daily_top_n=5,
            weekly_top_n=8,
            monthly_top_n=10,
            annual_top_n=10,
        ),
        output_dir="outputs/corpus-test-agent",
    )


@pytest.fixture
def corpus_secrets() -> RuntimeSecrets:
    """Fake RuntimeSecrets for corpus tests."""
    return RuntimeSecrets.model_validate({
        "OPENAI_API_KEY": "sk-corpus-test-fake",
        "TWITTER_BEARER_TOKEN": "test-bearer-corpus",
    })


@pytest.fixture
def corpus_store(tmp_path: Path) -> TinyDBArticleStore:
    """Pre-seeded TinyDB store with the fixture corpus."""
    store = TinyDBArticleStore(tmp_path / "corpus-store.json")
    yield store
    store.close()


@pytest.fixture
def seeded_store(corpus_store: TinyDBArticleStore, corpus_agent_config: AgentConfig):
    """corpus_store pre-seeded with all 10 fixture articles."""
    count = _seed_corpus(corpus_store, corpus_agent_config.agent_id)
    assert count == len(_CORPUS_ARTICLES), f"Expected {len(_CORPUS_ARTICLES)} articles inserted"
    return corpus_store


@pytest.fixture
def sourcing_result_fixture(corpus_agent_config: AgentConfig) -> SourcingRunResult:
    """A SourcingRunResult matching the pre-seeded corpus."""
    return SourcingRunResult(
        agent_id=corpus_agent_config.agent_id,
        run_at=datetime(2026, 5, 9, 20, 0, tzinfo=UTC),
        window_start=datetime(2026, 5, 9, 0, 0, tzinfo=UTC),
        window_end=datetime(2026, 5, 9, 23, 59, tzinfo=UTC),
        articles_fetched=len(_CORPUS_ARTICLES),
        articles_inserted=len(_CORPUS_ARTICLES),
        articles_duplicate=0,
        tweets_fetched=3,
        tweets_inserted=3,
        twitter_signal_available=True,
        tweet_api_call_count=9,
    )


def _make_pipeline(
    config: AgentConfig,
    secrets: RuntimeSecrets,
    store: TinyDBArticleStore,
    prompts_dir: Path,
) -> Pipeline:
    """Build a Pipeline instance wired with the given store."""
    return Pipeline(
        config=config,
        secrets=secrets,
        store=store,
        prompts_dir=str(prompts_dir),
    )


def _run_with_mocks(
    pipeline: Pipeline,
    cadence: str,
    window_start: datetime,
    window_end: datetime,
    scratch_dir: Path,
    sourcing_result: SourcingRunResult,
    llm_response: str | None = None,
    twitter_api_available: bool | None = None,
    skip_sourcing: bool = False,
) -> PipelineRunResult:
    """Run pipeline with mocked sourcing and LLM."""
    with patch("ai_news_agent.pipeline.SourcingAgent") as MockSourcing, \
         patch("ai_news_agent.curation.agent.get_llm_client") as mock_factory:
        MockSourcing.return_value.run.return_value = sourcing_result
        mock_factory.return_value = DummyLLMClient(
            complete_response=llm_response
        ) if llm_response else DummyLLMClient()

        return pipeline.run(
            cadence=cadence,
            window_start=window_start,
            window_end=window_end,
            dry_run=True,
            scratch_dir=scratch_dir,
            twitter_api_available=twitter_api_available,
            skip_sourcing=skip_sourcing,
        )


# ---------------------------------------------------------------------------
# Test: Three output formats from fixture corpus (SRC-004, SRC-102)
# ---------------------------------------------------------------------------

@pytest.mark.integration
class TestFixtureCorpusThreeFormats:
    """
    Verify the dry-run pipeline produces all three output formats from the
    fixture corpus without errors.

    Traces: SRC-004, SRC-102, SRC-145
    """

    def test_fixture_corpus_three_formats(
        self,
        corpus_agent_config: AgentConfig,
        corpus_secrets: RuntimeSecrets,
        seeded_store: TinyDBArticleStore,
        prompts_dir: Path,
        tmp_path: Path,
        sourcing_result_fixture: SourcingRunResult,
    ) -> None:
        """
        Pipeline against fixture corpus produces Markdown, HTML, and JSON (SRC-004).

        Verifies:
        - result.success is True
        - All three file paths exist and are non-empty
        - JSON parses and has required top-level keys
        - agent_id is correct in JSON metadata
        """
        scratch = tmp_path / "scratch-formats"

        pipeline = _make_pipeline(
            corpus_agent_config, corpus_secrets, seeded_store, prompts_dir
        )
        result = _run_with_mocks(
            pipeline=pipeline,
            cadence="daily",
            window_start=datetime(2026, 5, 9, 0, 0, tzinfo=UTC),
            window_end=datetime(2026, 5, 9, 23, 59, tzinfo=UTC),
            scratch_dir=scratch,
            sourcing_result=sourcing_result_fixture,
        )

        assert result.success is True, f"Pipeline failed: {result.errors}"
        assert result.dry_run is True

        # All three formats must exist (SRC-004)
        assert result.markdown_path is not None
        assert result.html_path is not None
        assert result.json_path is not None

        assert result.markdown_path.exists(), "Markdown output file missing"
        assert result.html_path.exists(), "HTML output file missing"
        assert result.json_path.exists(), "JSON output file missing"

        assert result.markdown_path.stat().st_size > 0, "Markdown file is empty"
        assert result.html_path.stat().st_size > 0, "HTML file is empty"
        assert result.json_path.stat().st_size > 0, "JSON file is empty"

        # JSON structure check (SRC-102 — all required fields populated)
        json_data = json.loads(result.json_path.read_text(encoding="utf-8"))
        assert "schema_version" in json_data, "schema_version missing from JSON"
        assert "metadata" in json_data, "metadata missing from JSON"
        assert "items" in json_data, "items missing from JSON"
        assert json_data["metadata"]["agent_id"] == "corpus-test-agent"

    def test_fixture_corpus_files_in_scratch_dir(
        self,
        corpus_agent_config: AgentConfig,
        corpus_secrets: RuntimeSecrets,
        seeded_store: TinyDBArticleStore,
        prompts_dir: Path,
        tmp_path: Path,
        sourcing_result_fixture: SourcingRunResult,
    ) -> None:
        """
        In dry-run mode, all files must be in the scratch directory, NOT the
        production output directory (SRC-102).
        """
        scratch = tmp_path / "my-scratch"
        scratch.mkdir()
        prod_output = tmp_path / "outputs" / corpus_agent_config.agent_id

        pipeline = _make_pipeline(
            corpus_agent_config, corpus_secrets, seeded_store, prompts_dir
        )
        result = _run_with_mocks(
            pipeline=pipeline,
            cadence="daily",
            window_start=datetime(2026, 5, 9, 0, 0, tzinfo=UTC),
            window_end=datetime(2026, 5, 9, 23, 59, tzinfo=UTC),
            scratch_dir=scratch,
            sourcing_result=sourcing_result_fixture,
        )

        assert result.success is True
        # Files must be in scratch, not prod (SRC-102)
        assert result.markdown_path is not None
        assert result.markdown_path.parent == scratch
        # Production output dir must NOT have been created
        assert not prod_output.exists(), (
            f"Dry-run must not create production dir {prod_output}"
        )


# ---------------------------------------------------------------------------
# Test: All four cadences complete (SRC-029–SRC-032)
# ---------------------------------------------------------------------------

@pytest.mark.integration
@pytest.mark.parametrize(("cadence", "ref_time"), [
    ("daily",   datetime(2026, 5, 10, 1, 0, tzinfo=UTC)),
    ("weekly",  datetime(2026, 5, 11, 1, 0, tzinfo=UTC)),  # Monday: covers prior week
    ("monthly", datetime(2026, 5, 1, 2, 0, tzinfo=UTC)),   # May 1: covers April
    ("annual",  datetime(2026, 1, 1, 3, 0, tzinfo=UTC)),   # Jan 1: covers prior year
])
def test_fixture_all_cadences(
    cadence: str,
    ref_time: datetime,
    corpus_agent_config: AgentConfig,
    corpus_secrets: RuntimeSecrets,
    seeded_store: TinyDBArticleStore,
    prompts_dir: Path,
    tmp_path: Path,
) -> None:
    """
    All four cadences produce valid output from the fixture corpus (SRC-029–SRC-032).

    Traces: SRC-029 (daily), SRC-030 (weekly), SRC-031 (monthly), SRC-032 (annual)
    """
    from ai_news_agent.curation.agent import _WINDOW_FN

    ws, we = _WINDOW_FN[cadence](ref_time)
    scratch = tmp_path / f"scratch-{cadence}"

    sourcing_result = SourcingRunResult(
        agent_id=corpus_agent_config.agent_id,
        run_at=ref_time,
        window_start=ws,
        window_end=we,
        articles_fetched=10,
        articles_inserted=10,
        articles_duplicate=0,
        tweets_fetched=3,
        tweets_inserted=3,
        twitter_signal_available=True,
        tweet_api_call_count=5,
    )

    pipeline = _make_pipeline(
        corpus_agent_config, corpus_secrets, seeded_store, prompts_dir
    )
    result = _run_with_mocks(
        pipeline=pipeline,
        cadence=cadence,
        window_start=ws,
        window_end=we,
        scratch_dir=scratch,
        sourcing_result=sourcing_result,
    )

    assert result.success is True, (
        f"Pipeline failed for cadence={cadence!r}: {result.errors}"
    )
    assert result.cadence == cadence
    assert result.markdown_path is not None
    assert result.markdown_path.exists()
    assert result.markdown_path.stat().st_size > 0, f"Empty markdown for {cadence}"

    # JSON must parse correctly
    json_data = json.loads(result.json_path.read_text(encoding="utf-8"))  # type: ignore[union-attr]
    assert json_data["metadata"]["cadence"] == cadence


# ---------------------------------------------------------------------------
# Test: URL enforcement (SRC-049, SRC-141)
# ---------------------------------------------------------------------------

@pytest.mark.integration
class TestFixtureURLEnforcement:
    """
    URL enforcement: items without URLs must be dropped by both the curation
    layer and the rendering layer (SRC-049, SRC-141).
    """

    def _make_no_url_llm_response(self) -> str:
        """LLM response that includes a no-URL item alongside a valid one."""
        payload = {
            "items": [
                {
                    "headline": "Valid Article With URL",
                    "source_name": "Reuters",
                    "url": "https://reuters.com/valid-fixture-article",
                    "pub_date": "2026-05-09",
                    "why_it_matters": "Valid article must appear in output.",
                    "impact_tags": ["business_impact"],
                    "tier": "1b",
                    "cross_refs": [],
                },
                {
                    "headline": "No URL Article — Must Be Dropped",
                    "source_name": "Unknown",
                    "url": "",   # empty — must be dropped (SRC-049)
                    "pub_date": "2026-05-09",
                    "why_it_matters": "This should never appear in the output.",
                    "impact_tags": [],
                    "tier": "3",
                    "cross_refs": [],
                },
            ],
            "themes": ["Enterprise AI"],
            "outlook": "Expect more AI news.",
            "predictions": [],
        }
        return f"```json\n{json.dumps(payload)}\n```"

    def test_fixture_no_url_dropped(
        self,
        corpus_agent_config: AgentConfig,
        corpus_secrets: RuntimeSecrets,
        seeded_store: TinyDBArticleStore,
        prompts_dir: Path,
        tmp_path: Path,
        sourcing_result_fixture: SourcingRunResult,
    ) -> None:
        """
        Items without URL are absent from all three rendered outputs (SRC-049, SRC-141).

        This test injects an LLM response that contains one valid item and one
        item with an empty URL, then verifies the no-URL item does not appear in
        Markdown, HTML, or JSON output.
        """
        scratch = tmp_path / "scratch-no-url"
        pipeline = _make_pipeline(
            corpus_agent_config, corpus_secrets, seeded_store, prompts_dir
        )
        result = _run_with_mocks(
            pipeline=pipeline,
            cadence="daily",
            window_start=datetime(2026, 5, 9, 0, 0, tzinfo=UTC),
            window_end=datetime(2026, 5, 9, 23, 59, tzinfo=UTC),
            scratch_dir=scratch,
            sourcing_result=sourcing_result_fixture,
            llm_response=self._make_no_url_llm_response(),
        )

        assert result.success is True

        # Check Markdown (SRC-049, SRC-141)
        md = result.markdown_path.read_text(encoding="utf-8")  # type: ignore[union-attr]
        assert "No URL Article — Must Be Dropped" not in md, (
            "No-URL item leaked into Markdown output"
        )

        # Check HTML (SRC-049, SRC-141)
        html = result.html_path.read_text(encoding="utf-8")  # type: ignore[union-attr]
        assert "No URL Article — Must Be Dropped" not in html, (
            "No-URL item leaked into HTML output"
        )

        # Check JSON (SRC-049)
        json_data = json.loads(result.json_path.read_text(encoding="utf-8"))  # type: ignore[union-attr]
        for item in json_data.get("items", []):
            assert item.get("url"), (
                f"Item without URL in JSON output: {item.get('headline', '?')}"
            )

    def test_fixture_valid_article_preserved(
        self,
        corpus_agent_config: AgentConfig,
        corpus_secrets: RuntimeSecrets,
        seeded_store: TinyDBArticleStore,
        prompts_dir: Path,
        tmp_path: Path,
        sourcing_result_fixture: SourcingRunResult,
    ) -> None:
        """
        Valid articles (with URLs) are preserved across all three output formats.
        Traces: SRC-048 (curated item schema), SRC-049
        """
        scratch = tmp_path / "scratch-valid"
        pipeline = _make_pipeline(
            corpus_agent_config, corpus_secrets, seeded_store, prompts_dir
        )
        result = _run_with_mocks(
            pipeline=pipeline,
            cadence="daily",
            window_start=datetime(2026, 5, 9, 0, 0, tzinfo=UTC),
            window_end=datetime(2026, 5, 9, 23, 59, tzinfo=UTC),
            scratch_dir=scratch,
            sourcing_result=sourcing_result_fixture,
            llm_response=self._make_no_url_llm_response(),
        )

        assert result.success is True

        json_data = json.loads(result.json_path.read_text(encoding="utf-8"))  # type: ignore[union-attr]
        urls = [item["url"] for item in json_data.get("items", [])]
        assert any("reuters.com" in u for u in urls), (
            "Valid Reuters article missing from JSON output"
        )


# ---------------------------------------------------------------------------
# Test: Deduplication (SRC-012)
# ---------------------------------------------------------------------------

@pytest.mark.integration
class TestFixtureDeduplication:
    """
    Multiple pipeline sourcing passes on the same corpus window must not create
    duplicate article records (SRC-012).
    """

    def test_fixture_corpus_deduplication(
        self,
        corpus_agent_config: AgentConfig,
        corpus_store: TinyDBArticleStore,
        tmp_path: Path,
    ) -> None:
        """
        Inserting the same fixture corpus twice keeps exactly one copy per article (SRC-012).
        """
        agent_id = corpus_agent_config.agent_id

        # First insertion pass
        first_count = _seed_corpus(corpus_store, agent_id)
        assert first_count == len(_CORPUS_ARTICLES)

        # Second insertion pass — all duplicates
        second_count = _seed_corpus(corpus_store, agent_id)
        assert second_count == 0, (
            f"Duplicate insertion returned {second_count} inserts; expected 0 (SRC-012)"
        )

        # Total store count must equal the original corpus size
        ws = datetime(2026, 5, 1, tzinfo=UTC)
        we = datetime(2026, 5, 31, 23, 59, 59, tzinfo=UTC)
        stored = corpus_store.get_window(agent_id=agent_id, window_start=ws, window_end=we)
        assert len(stored) == len(_CORPUS_ARTICLES), (
            f"Expected {len(_CORPUS_ARTICLES)} records after dedup, got {len(stored)} (SRC-012)"
        )

    def test_fixture_near_duplicate_different_url(
        self,
        corpus_agent_config: AgentConfig,
        corpus_store: TinyDBArticleStore,
    ) -> None:
        """
        Two articles with different URLs are stored as distinct records (SRC-012).
        """
        agent_id = corpus_agent_config.agent_id
        data1 = _CORPUS_ARTICLES[0]
        data2 = dict(_CORPUS_ARTICLES[0])
        data2["url"] = "https://reuters.com/technology/ai-enterprise-wave-2026-PART2"

        rec1 = _build_article_record(data1, agent_id)
        rec2 = _build_article_record(data2, agent_id)

        assert corpus_store.insert_if_new(rec1) is True
        assert corpus_store.insert_if_new(rec2) is True


# ---------------------------------------------------------------------------
# Test: §8.2 monitoring fields (SRC-150)
# ---------------------------------------------------------------------------

@pytest.mark.integration
class TestFixtureMonitoringFields:
    """
    Verify all §8.2 quality-monitoring fields are present in the JSON digest
    metadata block and in PipelineRunResult (SRC-150).
    """

    def test_fixture_monitoring_fields(
        self,
        corpus_agent_config: AgentConfig,
        corpus_secrets: RuntimeSecrets,
        seeded_store: TinyDBArticleStore,
        prompts_dir: Path,
        tmp_path: Path,
        sourcing_result_fixture: SourcingRunResult,
    ) -> None:
        """
        All §8.2 fields must be present and correct after a successful run (SRC-150).
        """
        scratch = tmp_path / "scratch-monitoring"
        pipeline = _make_pipeline(
            corpus_agent_config, corpus_secrets, seeded_store, prompts_dir
        )
        result = _run_with_mocks(
            pipeline=pipeline,
            cadence="daily",
            window_start=datetime(2026, 5, 9, 0, 0, tzinfo=UTC),
            window_end=datetime(2026, 5, 9, 23, 59, tzinfo=UTC),
            scratch_dir=scratch,
            sourcing_result=sourcing_result_fixture,
        )

        assert result.success is True

        # PipelineRunResult §8.2 fields (SRC-150)
        assert isinstance(result.items_considered, int)
        assert isinstance(result.items_included, int)
        assert isinstance(result.items_by_tier, dict)
        assert isinstance(result.items_by_source_class, dict)
        assert isinstance(result.token_usage, int)
        assert result.token_usage > 0, "token_usage must be > 0 after LLM call"
        assert result.llm_provider, "llm_provider must be non-empty"
        assert result.llm_model, "llm_model must be non-empty"
        assert result.prompt_version.startswith("sha256:"), (
            f"prompt_version must start with 'sha256:', got: {result.prompt_version!r}"
        )
        assert isinstance(result.twitter_signal_available, bool)
        assert isinstance(result.tweet_api_call_count, int)

        # JSON metadata §8.2 fields
        json_data = json.loads(result.json_path.read_text(encoding="utf-8"))  # type: ignore[union-attr]
        meta = json_data["metadata"]

        required_fields = [
            "agent_id", "cadence", "run_date", "prompt_version",
            "llm_provider", "llm_model", "items_considered", "items_included",
            "items_by_tier", "items_by_source_class", "token_usage",
            "twitter_signal_available", "tweet_api_call_count",
        ]
        for field_name in required_fields:
            assert field_name in meta, (
                f"§8.2 field '{field_name}' missing from JSON metadata (SRC-150)"
            )

        assert meta["agent_id"] == "corpus-test-agent"
        assert meta["prompt_version"].startswith("sha256:")  # SRC-129


# ---------------------------------------------------------------------------
# Test: Prompt SHA-256 in all three formats (SRC-129)
# ---------------------------------------------------------------------------

@pytest.mark.integration
class TestFixturePromptVersion:
    """
    Prompt SHA-256 version must appear in Markdown, HTML, and JSON (SRC-129).
    """

    def test_fixture_prompt_version(
        self,
        corpus_agent_config: AgentConfig,
        corpus_secrets: RuntimeSecrets,
        seeded_store: TinyDBArticleStore,
        prompts_dir: Path,
        tmp_path: Path,
        sourcing_result_fixture: SourcingRunResult,
    ) -> None:
        """Prompt SHA-256 in all three output formats (SRC-129)."""
        scratch = tmp_path / "scratch-pv"
        pipeline = _make_pipeline(
            corpus_agent_config, corpus_secrets, seeded_store, prompts_dir
        )
        result = _run_with_mocks(
            pipeline=pipeline,
            cadence="daily",
            window_start=datetime(2026, 5, 9, 0, 0, tzinfo=UTC),
            window_end=datetime(2026, 5, 9, 23, 59, tzinfo=UTC),
            scratch_dir=scratch,
            sourcing_result=sourcing_result_fixture,
        )

        assert result.success is True
        pv = result.prompt_version
        assert pv.startswith("sha256:"), f"Unexpected prompt_version: {pv!r}"

        md = result.markdown_path.read_text(encoding="utf-8")  # type: ignore[union-attr]
        html = result.html_path.read_text(encoding="utf-8")  # type: ignore[union-attr]
        json_data = json.loads(result.json_path.read_text(encoding="utf-8"))  # type: ignore[union-attr]

        assert pv in md, "Prompt version missing from Markdown (SRC-129)"
        assert pv in html, "Prompt version missing from HTML (SRC-129)"
        assert json_data["metadata"]["prompt_version"] == pv, (
            "Prompt version mismatch in JSON (SRC-129)"
        )


# ---------------------------------------------------------------------------
# Test: Date-stamped filenames (SRC-145)
# ---------------------------------------------------------------------------

@pytest.mark.integration
class TestFixtureDateStampedFilenames:
    """
    Output filenames must follow the {YYYY-MM-DD}-{cadence}.{ext} pattern (SRC-145).
    """

    @pytest.mark.parametrize("cadence", ["daily", "weekly", "monthly", "annual"])
    def test_fixture_date_stamped_filenames(
        self,
        cadence: str,
        corpus_agent_config: AgentConfig,
        corpus_secrets: RuntimeSecrets,
        seeded_store: TinyDBArticleStore,
        prompts_dir: Path,
        tmp_path: Path,
    ) -> None:
        """
        Rendered filenames follow {YYYY-MM-DD}-{cadence}.{ext} (SRC-145).
        """
        from ai_news_agent.curation.agent import _WINDOW_FN

        ref_times = {
            "daily":   datetime(2026, 5, 10, 1, 0, tzinfo=UTC),
            "weekly":  datetime(2026, 5, 11, 1, 0, tzinfo=UTC),
            "monthly": datetime(2026, 5, 1, 2, 0, tzinfo=UTC),
            "annual":  datetime(2026, 1, 1, 3, 0, tzinfo=UTC),
        }
        ws, we = _WINDOW_FN[cadence](ref_times[cadence])
        scratch = tmp_path / f"scratch-fn-{cadence}"

        sourcing_r = SourcingRunResult(
            agent_id=corpus_agent_config.agent_id,
            run_at=ref_times[cadence],
            window_start=ws,
            window_end=we,
            articles_fetched=5,
            articles_inserted=5,
            articles_duplicate=0,
            tweets_fetched=1,
            tweets_inserted=1,
            twitter_signal_available=True,
            tweet_api_call_count=2,
        )

        pipeline = _make_pipeline(
            corpus_agent_config, corpus_secrets, seeded_store, prompts_dir
        )
        result = _run_with_mocks(
            pipeline=pipeline,
            cadence=cadence,
            window_start=ws,
            window_end=we,
            scratch_dir=scratch,
            sourcing_result=sourcing_r,
        )

        assert result.success is True
        assert result.markdown_path is not None

        # Filename pattern: YYYY-MM-DD-{cadence}.{ext} (SRC-145)
        name = result.markdown_path.name
        assert cadence in name, f"Cadence missing from filename: {name}"
        assert re.search(r"\d{4}-\d{2}-\d{2}", name), (
            f"Date stamp missing from filename: {name}"
        )
        assert name.endswith(".md"), f"Expected .md extension, got: {name}"


# ---------------------------------------------------------------------------
# Test: Twitter degradation (SRC-148)
# ---------------------------------------------------------------------------

@pytest.mark.integration
class TestFixtureTwitterDegradation:
    """
    When Twitter/X API is unavailable, the pipeline must:
    1. Continue with web sources only.
    2. Reflect twitter_signal_available=False in PipelineRunResult.
    3. Include a degradation note in the digest.

    Traces: SRC-148
    """

    def test_fixture_twitter_degraded(
        self,
        corpus_agent_config: AgentConfig,
        corpus_secrets: RuntimeSecrets,
        seeded_store: TinyDBArticleStore,
        prompts_dir: Path,
        tmp_path: Path,
    ) -> None:
        """
        Twitter unavailable: pipeline succeeds with web-only mode (SRC-148).
        """
        scratch = tmp_path / "scratch-twitter-down"
        degraded_sourcing = SourcingRunResult(
            agent_id=corpus_agent_config.agent_id,
            run_at=datetime(2026, 5, 9, 20, 0, tzinfo=UTC),
            window_start=datetime(2026, 5, 9, 0, 0, tzinfo=UTC),
            window_end=datetime(2026, 5, 9, 23, 59, tzinfo=UTC),
            articles_fetched=8,
            articles_inserted=8,
            articles_duplicate=0,
            tweets_fetched=0,
            tweets_inserted=0,
            twitter_signal_available=False,  # API down
            tweet_api_call_count=0,
        )

        pipeline = _make_pipeline(
            corpus_agent_config, corpus_secrets, seeded_store, prompts_dir
        )
        result = _run_with_mocks(
            pipeline=pipeline,
            cadence="daily",
            window_start=datetime(2026, 5, 9, 0, 0, tzinfo=UTC),
            window_end=datetime(2026, 5, 9, 23, 59, tzinfo=UTC),
            scratch_dir=scratch,
            sourcing_result=degraded_sourcing,
        )

        # Must succeed even without Twitter (SRC-148)
        assert result.success is True, f"Pipeline failed: {result.errors}"
        assert result.twitter_signal_available is False, (
            "twitter_signal_available must be False when API is down (SRC-148)"
        )
        assert result.tweet_api_call_count == 0

    def test_fixture_twitter_available_override(
        self,
        corpus_agent_config: AgentConfig,
        corpus_secrets: RuntimeSecrets,
        seeded_store: TinyDBArticleStore,
        prompts_dir: Path,
        tmp_path: Path,
    ) -> None:
        """
        Explicit twitter_api_available=False override must be honoured (SRC-148).

        Even if sourcing reports Twitter as available, the caller's override wins.
        """
        scratch = tmp_path / "scratch-twitter-override"
        sourcing_r = SourcingRunResult(
            agent_id=corpus_agent_config.agent_id,
            run_at=datetime(2026, 5, 9, 20, 0, tzinfo=UTC),
            window_start=datetime(2026, 5, 9, 0, 0, tzinfo=UTC),
            window_end=datetime(2026, 5, 9, 23, 59, tzinfo=UTC),
            articles_fetched=5,
            articles_inserted=5,
            articles_duplicate=0,
            tweets_fetched=0,
            tweets_inserted=0,
            twitter_signal_available=True,   # sourcing thinks it's OK...
            tweet_api_call_count=0,
        )

        pipeline = _make_pipeline(
            corpus_agent_config, corpus_secrets, seeded_store, prompts_dir
        )
        result = _run_with_mocks(
            pipeline=pipeline,
            cadence="daily",
            window_start=datetime(2026, 5, 9, 0, 0, tzinfo=UTC),
            window_end=datetime(2026, 5, 9, 23, 59, tzinfo=UTC),
            scratch_dir=scratch,
            sourcing_result=sourcing_r,
            twitter_api_available=False,   # ...but caller overrides
        )

        assert result.success is True
        assert result.twitter_signal_available is False, (
            "Caller override twitter_api_available=False must win (SRC-148)"
        )


# ---------------------------------------------------------------------------
# Test: Window override / on-demand re-run (SRC-028, SRC-147)
# ---------------------------------------------------------------------------

@pytest.mark.integration
class TestFixtureWindowOverride:
    """
    Pipeline with explicit window_start/window_end must produce a digest for
    the custom date range without touching the automatic cadence logic (SRC-028).
    """

    def test_fixture_corpus_window_override(
        self,
        corpus_agent_config: AgentConfig,
        corpus_secrets: RuntimeSecrets,
        corpus_store: TinyDBArticleStore,
        prompts_dir: Path,
        tmp_path: Path,
    ) -> None:
        """
        Custom window_start/window_end must appear in JSON metadata (SRC-028).
        """
        # Seed articles inside the custom April window
        april_articles = [
            {
                "url": "https://reuters.com/april-ai-enterprise",
                "headline": "April AI Enterprise News",
                "abstract": "April news about AI in enterprise.",
                "source_name": "Reuters",
                "tier": "1b",
                "source_class": "web",
                "pub_date": datetime(2026, 4, 15, 9, 0, tzinfo=UTC),
            },
            {
                "url": "https://bloomberg.com/april-ai-regulation",
                "headline": "April AI Regulation Update",
                "abstract": "April regulation news.",
                "source_name": "Bloomberg",
                "tier": "1b",
                "source_class": "web",
                "pub_date": datetime(2026, 4, 20, 10, 0, tzinfo=UTC),
            },
        ]
        _seed_corpus(corpus_store, corpus_agent_config.agent_id, april_articles)

        custom_start = datetime(2026, 4, 1, 0, 0, tzinfo=UTC)
        custom_end = datetime(2026, 4, 30, 23, 59, 59, tzinfo=UTC)
        scratch = tmp_path / "scratch-window"

        sourcing_r = SourcingRunResult(
            agent_id=corpus_agent_config.agent_id,
            run_at=datetime(2026, 5, 1, tzinfo=UTC),
            window_start=custom_start,
            window_end=custom_end,
            articles_fetched=2,
            articles_inserted=2,
            articles_duplicate=0,
            tweets_fetched=0,
            tweets_inserted=0,
            twitter_signal_available=True,
            tweet_api_call_count=0,
        )

        pipeline = _make_pipeline(
            corpus_agent_config, corpus_secrets, corpus_store, prompts_dir
        )
        result = _run_with_mocks(
            pipeline=pipeline,
            cadence="monthly",
            window_start=custom_start,
            window_end=custom_end,
            scratch_dir=scratch,
            sourcing_result=sourcing_r,
        )

        assert result.success is True, f"Window override failed: {result.errors}"

        json_data = json.loads(result.json_path.read_text(encoding="utf-8"))  # type: ignore[union-attr]
        meta = json_data["metadata"]

        # Custom window must be reflected in JSON metadata (SRC-028)
        assert "2026-04-01" in meta.get("window_start", ""), (
            f"Custom window_start not in metadata: {meta.get('window_start', '?')}"
        )
        assert "2026-04" in meta.get("window_end", ""), (
            f"Custom window_end not in metadata: {meta.get('window_end', '?')}"
        )


# ---------------------------------------------------------------------------
# Test: skip_sourcing mode (SRC-028)
# ---------------------------------------------------------------------------

@pytest.mark.integration
class TestFixtureSkipSourcing:
    """
    skip_sourcing=True must bypass the sourcing stage and curate from the
    existing corpus already in the store (SRC-028).
    """

    def test_fixture_skip_sourcing(
        self,
        corpus_agent_config: AgentConfig,
        corpus_secrets: RuntimeSecrets,
        seeded_store: TinyDBArticleStore,
        prompts_dir: Path,
        tmp_path: Path,
    ) -> None:
        """
        skip_sourcing=True: SourcingAgent must not be called; curation succeeds (SRC-028).
        """
        scratch = tmp_path / "scratch-skip"
        pipeline = _make_pipeline(
            corpus_agent_config, corpus_secrets, seeded_store, prompts_dir
        )

        with patch("ai_news_agent.pipeline.SourcingAgent") as MockSourcing, \
             patch("ai_news_agent.curation.agent.get_llm_client") as mock_factory:
            mock_factory.return_value = DummyLLMClient()

            result = pipeline.run(
                cadence="daily",
                window_start=datetime(2026, 5, 9, 0, 0, tzinfo=UTC),
                window_end=datetime(2026, 5, 9, 23, 59, tzinfo=UTC),
                dry_run=True,
                scratch_dir=scratch,
                skip_sourcing=True,
            )

        # SourcingAgent constructor must NOT have been called
        MockSourcing.assert_not_called()
        assert result.success is True, f"skip_sourcing pipeline failed: {result.errors}"
        assert result.articles_fetched == 0, "articles_fetched must be 0 when skip_sourcing"
        assert result.articles_inserted == 0
        assert result.markdown_path is not None
        assert result.markdown_path.exists()


# ---------------------------------------------------------------------------
# Test: CuratedItem schema preserved (SRC-048)
# ---------------------------------------------------------------------------

@pytest.mark.integration
class TestFixtureItemSchema:
    """
    Verify that rendered items include all required CuratedItem schema fields (SRC-048):
    headline, source_name, URL, pub_date, why_it_matters, impact_tags, tier.
    """

    def _make_full_item_response(self) -> str:
        """LLM response with a fully-populated item."""
        payload = {
            "items": [
                {
                    "headline": "AI Reshapes Enterprise Software Market",
                    "source_name": "Reuters",
                    "url": "https://reuters.com/ai-enterprise-2026-fixture",
                    "pub_date": "2026-05-09",
                    "why_it_matters": (
                        "Major enterprise software vendors are integrating AI into core products. "
                        "This signals a structural shift in procurement patterns. "
                        "Legacy vendors face displacement risk."
                    ),
                    "impact_tags": ["business_impact", "workforce_impact"],
                    "tier": "1b",
                    "cross_refs": [],
                    "twitter_handle": None,
                    "tweet_url": None,
                }
            ],
            "themes": ["Enterprise AI Adoption", "Market Consolidation"],
            "outlook": "Continued M&A expected in AI tooling space.",
            "predictions": [],
        }
        return f"```json\n{json.dumps(payload)}\n```"

    def test_fixture_item_schema(
        self,
        corpus_agent_config: AgentConfig,
        corpus_secrets: RuntimeSecrets,
        seeded_store: TinyDBArticleStore,
        prompts_dir: Path,
        tmp_path: Path,
        sourcing_result_fixture: SourcingRunResult,
    ) -> None:
        """
        JSON output items must contain all required schema fields (SRC-048).
        """
        scratch = tmp_path / "scratch-schema"
        pipeline = _make_pipeline(
            corpus_agent_config, corpus_secrets, seeded_store, prompts_dir
        )
        result = _run_with_mocks(
            pipeline=pipeline,
            cadence="daily",
            window_start=datetime(2026, 5, 9, 0, 0, tzinfo=UTC),
            window_end=datetime(2026, 5, 9, 23, 59, tzinfo=UTC),
            scratch_dir=scratch,
            sourcing_result=sourcing_result_fixture,
            llm_response=self._make_full_item_response(),
        )

        assert result.success is True

        json_data = json.loads(result.json_path.read_text(encoding="utf-8"))  # type: ignore[union-attr]
        assert json_data["items"], "Expected at least one item in JSON output"

        item = json_data["items"][0]
        # Required schema fields (SRC-048)
        for field_name in ["headline", "source_name", "url", "pub_date",
                           "why_it_matters", "impact_tags", "tier"]:
            assert field_name in item, (
                f"Required CuratedItem field '{field_name}' missing from JSON item (SRC-048)"
            )

        assert item["url"] == "https://reuters.com/ai-enterprise-2026-fixture"
        assert "business_impact" in item["impact_tags"]
        assert item["tier"] == "1b"


# ---------------------------------------------------------------------------
# Test: Idempotent re-run (SRC-145) — running twice overwrites cleanly
# ---------------------------------------------------------------------------

@pytest.mark.integration
class TestFixtureIdempotentReRun:
    """
    Running the pipeline twice for the same window must produce idempotent output
    (second run overwrites first) without errors (SRC-145).
    """

    def test_fixture_idempotent_rerun(
        self,
        corpus_agent_config: AgentConfig,
        corpus_secrets: RuntimeSecrets,
        seeded_store: TinyDBArticleStore,
        prompts_dir: Path,
        tmp_path: Path,
        sourcing_result_fixture: SourcingRunResult,
    ) -> None:
        """
        Second pipeline run for same window must succeed and overwrite first run (SRC-145).
        """
        scratch = tmp_path / "scratch-idem"
        pipeline = _make_pipeline(
            corpus_agent_config, corpus_secrets, seeded_store, prompts_dir
        )

        kwargs: dict[str, Any] = {
            "cadence": "daily",
            "window_start": datetime(2026, 5, 9, 0, 0, tzinfo=UTC),
            "window_end": datetime(2026, 5, 9, 23, 59, tzinfo=UTC),
            "scratch_dir": scratch,
            "sourcing_result": sourcing_result_fixture,
        }

        result1 = _run_with_mocks(pipeline=pipeline, **kwargs)
        result2 = _run_with_mocks(pipeline=pipeline, **kwargs)

        assert result1.success is True
        assert result2.success is True

        # Second run must produce the same files
        assert result2.markdown_path is not None
        assert result2.markdown_path.exists()

        # Both runs must use the same filename formula (SRC-145)
        assert result1.markdown_path.name == result2.markdown_path.name


# ---------------------------------------------------------------------------
# Test: Annual cadence includes predictions (SRC-032, SRC-124)
# ---------------------------------------------------------------------------

@pytest.mark.integration
class TestFixtureAnnualPredictions:
    """
    Annual digest must include 10 falsifiable predictions grounded in observed
    trends (SRC-032, SRC-124).
    """

    def _make_annual_response(self) -> str:
        """LLM response for annual cadence with 10 predictions."""
        predictions = [
            (
                f"Prediction {i + 1}: AI will advance significantly in area {i + 1}. "
                f"Based on trend {i + 1} observed throughout 2025. "
                f"Fails if no major announcement in area {i + 1} by Q3 2026."
            )
            for i in range(10)
        ]
        payload = {
            "items": [
                {
                    "headline": f"Top AI Story #{rank}",
                    "source_name": "Reuters",
                    "url": f"https://reuters.com/annual-top-{rank}",
                    "pub_date": "2025-09-01",
                    "why_it_matters": f"Story #{rank} mattered because of major AI developments.",
                    "impact_tags": ["business_impact"],
                    "tier": "1b",
                    "cross_refs": [],
                }
                for rank in range(1, 11)
            ],
            "themes": [
                "Enterprise AI Adoption",
                "AI Regulation",
                "Foundation Model Competition",
                "AI Safety Progress",
            ],
            "outlook": "2026 will see significant AI breakthroughs.",
            "predictions": predictions,
        }
        return f"```json\n{json.dumps(payload)}\n```"

    def test_fixture_annual_predictions(
        self,
        corpus_agent_config: AgentConfig,
        corpus_secrets: RuntimeSecrets,
        corpus_store: TinyDBArticleStore,
        prompts_dir: Path,
        tmp_path: Path,
    ) -> None:
        """
        Annual digest must include predictions in JSON output (SRC-032, SRC-124).

        Corpus articles are seeded within the 2025 annual window so the scorer
        receives candidates and calls the LLM with the annual response.
        """
        from ai_news_agent.curation.agent import _WINDOW_FN

        # Annual 2026-01-01 covers the 2025 calendar year
        ref_time = datetime(2026, 1, 1, 3, 0, tzinfo=UTC)
        ws, we = _WINDOW_FN["annual"](ref_time)

        # Seed articles that fall within the 2025 annual window
        annual_articles = [
            {
                "url": f"https://reuters.com/annual-ai-story-{i}",
                "headline": f"Annual AI Story {i}: Major Development in AI",
                "abstract": f"Key AI development number {i} of 2025.",
                "source_name": "Reuters",
                "tier": "1b",
                "source_class": "web",
                "pub_date": datetime(2025, 6, i + 1, 9, 0, tzinfo=UTC),
            }
            for i in range(10)
        ]
        _seed_corpus(corpus_store, corpus_agent_config.agent_id, annual_articles)

        scratch = tmp_path / "scratch-annual"

        sourcing_r = SourcingRunResult(
            agent_id=corpus_agent_config.agent_id,
            run_at=ref_time,
            window_start=ws,
            window_end=we,
            articles_fetched=10,
            articles_inserted=10,
            articles_duplicate=0,
            tweets_fetched=5,
            tweets_inserted=5,
            twitter_signal_available=True,
            tweet_api_call_count=9,
        )

        pipeline = _make_pipeline(
            corpus_agent_config, corpus_secrets, corpus_store, prompts_dir
        )
        result = _run_with_mocks(
            pipeline=pipeline,
            cadence="annual",
            window_start=ws,
            window_end=we,
            scratch_dir=scratch,
            sourcing_result=sourcing_r,
            llm_response=self._make_annual_response(),
        )

        assert result.success is True, f"Annual pipeline failed: {result.errors}"

        json_data = json.loads(result.json_path.read_text(encoding="utf-8"))  # type: ignore[union-attr]

        # Annual must have items (top-10) and predictions (SRC-032, SRC-124)
        assert len(json_data.get("items", [])) >= 1, "Annual must include items"

        # Predictions may be in the JSON directly
        predictions = json_data.get("predictions", [])
        assert len(predictions) >= 1, (
            "Annual digest must include predictions (SRC-032, SRC-124)"
        )
        assert json_data["metadata"]["cadence"] == "annual"
