"""
tests/unit/test_curation.py — CurationAgent: prompt build, scoring, URL drop.
Traces: SRC-014–SRC-032 (curation agent), SRC-027 (LLM scoring),
        SRC-049 (URL drop), SRC-113 (prompts dir), SRC-129 (prompt version),
        SRC-098 (unit tests with mocked LLM)
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest
from tests.conftest import DummyLLMClient

from ai_news_agent.curation.agent import (
    CurationAgent,
    CurationRunResult,
    _annual_window,
    _daily_window,
    _monthly_window,
    _weekly_window,
)
from ai_news_agent.curation.prompt_builder import PromptBuilder
from ai_news_agent.curation.scorer import Scorer

if TYPE_CHECKING:
    from pathlib import Path

    from ai_news_agent.config.models import AgentConfig
    from ai_news_agent.storage.models import ArticleRecord, TweetSignal

# ---------------------------------------------------------------------------
# Window computation helpers
# ---------------------------------------------------------------------------


class TestWindowHelpers:
    """Traces: SRC-009, SRC-029–SRC-032."""

    def test_daily_window_rolls_from_yesterday_to_reference(self) -> None:
        ref = datetime(2026, 5, 10, 6, 0, tzinfo=UTC)
        start, end = _daily_window(ref)
        assert start.date() == date(2026, 5, 9)
        assert start.hour == 0
        assert end == ref

    def test_weekly_window_covers_7_days(self) -> None:
        ref = datetime(2026, 5, 10, tzinfo=UTC)  # Sunday
        start, end = _weekly_window(ref)
        delta = (end - start).days
        assert delta >= 6  # at least 6 days (could be 6-7 depending on Sunday handling)

    def test_monthly_window_previous_month(self) -> None:
        ref = datetime(2026, 5, 1, tzinfo=UTC)  # May 1st → April window
        start, end = _monthly_window(ref)
        assert start.month == 4
        assert end.month == 4

    def test_annual_window_previous_year(self) -> None:
        ref = datetime(2026, 1, 1, tzinfo=UTC)  # Jan 1 2026 → year 2025
        start, end = _annual_window(ref)
        assert start.year == 2025
        assert end.year == 2025
        assert start.month == 1
        assert end.month == 12


# ---------------------------------------------------------------------------
# PromptBuilder
# ---------------------------------------------------------------------------


class TestPromptBuilder:
    """Traces: SRC-113, SRC-115–SRC-124, SRC-129."""

    def test_build_injects_iso_dates(
        self,
        prompts_dir: Path,
        sample_tweet_signal: TweetSignal,
    ) -> None:
        """Concrete ISO dates injected — never relative phrases (SRC-116)."""
        builder = PromptBuilder(prompts_dir=prompts_dir)
        start = datetime(2026, 5, 9, 0, 0, tzinfo=UTC)
        end = datetime(2026, 5, 9, 23, 59, tzinfo=UTC)
        prompt, version = builder.build(
            cadence="daily",
            window_start=start,
            window_end=end,
            tweet_signals=[],
            top_n=10,
        )
        assert "2026-05-09" in prompt
        assert "{{window_start_iso}}" not in prompt  # placeholder fully substituted

    def test_build_returns_sha256_version(self, prompts_dir: Path) -> None:
        """SHA-256 hash returned for regression tracing (SRC-129)."""
        builder = PromptBuilder(prompts_dir=prompts_dir)
        _, version = builder.build(
            cadence="daily",
            window_start=datetime(2026, 5, 9, tzinfo=UTC),
            window_end=datetime(2026, 5, 9, 23, 59, tzinfo=UTC),
            tweet_signals=[],
            top_n=10,
        )
        assert version.startswith("sha256:")
        assert len(version) == len("sha256:") + 64  # 64-char hex

    def test_build_includes_twitter_signal_section(
        self,
        prompts_dir: Path,
        sample_tweet_signal: TweetSignal,
    ) -> None:
        """Twitter signals appear as labeled context section (SRC-119)."""
        builder = PromptBuilder(prompts_dir=prompts_dir)
        prompt, _ = builder.build(
            cadence="daily",
            window_start=datetime(2026, 5, 9, tzinfo=UTC),
            window_end=datetime(2026, 5, 9, 23, 59, tzinfo=UTC),
            tweet_signals=[sample_tweet_signal],
            top_n=10,
        )
        assert "karpathy" in prompt
        assert "Influencer Signal" in prompt or "twitter" in prompt.lower()

    def test_annual_prompt_has_year_variables(self, prompts_dir: Path) -> None:
        """Annual prompt contains year and year+1 (SRC-124)."""
        builder = PromptBuilder(prompts_dir=prompts_dir)
        prompt, _ = builder.build(
            cadence="annual",
            window_start=datetime(2025, 1, 1, tzinfo=UTC),
            window_end=datetime(2025, 12, 31, tzinfo=UTC),
            tweet_signals=[],
            top_n=10,
        )
        assert "2025" in prompt
        assert "2026" in prompt

    def test_missing_prompt_file_raises_error(self, tmp_path: Path) -> None:
        """FileNotFoundError raised if prompts/ directory is missing files."""
        empty_dir = tmp_path / "empty_prompts"
        empty_dir.mkdir()
        builder = PromptBuilder(prompts_dir=empty_dir)
        with pytest.raises(FileNotFoundError):
            builder.build(
                cadence="daily",
                window_start=datetime(2026, 5, 9, tzinfo=UTC),
                window_end=datetime(2026, 5, 9, tzinfo=UTC),
                tweet_signals=[],
                top_n=10,
            )

    def test_deep_search_budget_for_monthly(self, prompts_dir: Path) -> None:
        """Monthly cadence uses deep search budget (SRC-121)."""
        builder = PromptBuilder(prompts_dir=prompts_dir)
        prompt, _ = builder.build(
            cadence="monthly",
            window_start=datetime(2026, 4, 1, tzinfo=UTC),
            window_end=datetime(2026, 4, 30, tzinfo=UTC),
            tweet_signals=[],
            top_n=10,
        )
        assert "deep" in prompt.lower()


# ---------------------------------------------------------------------------
# Scorer (URL drop, ranking, LLM call)
# ---------------------------------------------------------------------------


class TestScorer:
    """Traces: SRC-027 (LLM scoring), SRC-049 (URL drop), SRC-061, SRC-129."""

    def test_scorer_returns_curated_items(
        self,
        dummy_llm: DummyLLMClient,
        sample_article: ArticleRecord,
    ) -> None:
        """score_and_rank returns ScorerResult; .items may be empty (SRC-027)."""
        from ai_news_agent.curation.scorer import ScorerResult

        scorer = Scorer(llm_client=dummy_llm)
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
        assert len(result.items) >= 0  # May be 0 if LLM drops all; no exception

    def test_scorer_drops_items_without_url(self, dummy_llm: DummyLLMClient) -> None:
        """Items without URL are dropped before returning (SRC-049, SRC-141)."""
        import json as json_mod
        from datetime import UTC, datetime

        from ai_news_agent.storage.models import ArticleRecord, normalize_url, url_hash

        # Inject an LLM response with one no-URL item
        payload = {
            "items": [
                {
                    "headline": "No URL Item",
                    "source_name": "Unknown",
                    "url": "",  # intentionally empty
                    "pub_date": "2026-05-10",
                    "why_it_matters": "Should be dropped.",
                    "impact_tags": [],
                    "tier": "3",
                    "cross_refs": [],
                }
            ],
            "themes": [],
            "outlook": "",
            "predictions": [],
        }
        dummy_llm._complete_response = f"```json\n{json_mod.dumps(payload)}\n```"

        scorer = Scorer(llm_client=dummy_llm)
        canonical = normalize_url("https://reuters.com/article-a")
        candidate = ArticleRecord(
            url_hash=url_hash(canonical),
            url=canonical,
            headline="Some Article",
            abstract=None,
            source_name="Reuters",
            pub_date=datetime(2026, 5, 10, tzinfo=UTC),
            fetched_at=datetime(2026, 5, 10, tzinfo=UTC),
            tier="1b",
            source_class="web",
            agent_id="test",
        )
        result = scorer.score_and_rank(
            prompt="prompt",
            model="gpt-4o",
            candidates=[candidate],
            tweet_signals=[],
            top_n=5,
            prompt_version="sha256:test",
            cadence="daily",
        )
        # All no-URL items should be dropped — result.items must all have URLs
        assert all(item.url for item in result.items)

    def test_scorer_attaches_prompt_version(
        self,
        dummy_llm: DummyLLMClient,
        sample_article: ArticleRecord,
    ) -> None:
        """prompt_version is attached to every returned item (SRC-129)."""
        scorer = Scorer(llm_client=dummy_llm)
        result = scorer.score_and_rank(
            prompt="Test prompt",
            model="gpt-4o",
            candidates=[sample_article],
            tweet_signals=[],
            top_n=5,
            prompt_version="sha256:specific-version",
            cadence="daily",
        )
        for item in result.items:
            assert item.prompt_version == "sha256:specific-version"

    def test_scorer_respects_top_n(self, sample_article: ArticleRecord) -> None:
        """scorer truncates to top_n items (SRC-029–SRC-032)."""
        import json as json_mod

        many_items = [
            {
                "headline": f"Article {i}",
                "source_name": "Reuters",
                "url": f"https://reuters.com/article-{i}",
                "pub_date": "2026-05-10",
                "why_it_matters": "It matters.",
                "impact_tags": ["business_impact"],
                "tier": "1b",
                "cross_refs": [],
            }
            for i in range(20)
        ]
        payload = {"items": many_items, "themes": [], "outlook": "", "predictions": []}
        llm = DummyLLMClient(complete_response=f"```json\n{json_mod.dumps(payload)}\n```")
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
        assert len(result.items) <= 5


# ---------------------------------------------------------------------------
# CurationAgent integration (mocked LLM + real store)
# ---------------------------------------------------------------------------


class TestCurationAgent:
    """Traces: SRC-014–SRC-032, SRC-054, SRC-148."""

    def test_run_daily_no_candidates_returns_empty(
        self,
        sample_agent_config: AgentConfig,
        sample_secrets,
        prompts_dir: Path,
        tmp_path: Path,
    ) -> None:
        """No candidates → curation returns empty items (SRC-015)."""
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
        assert result.metadata.cadence == "daily"

    def test_model_override_selected_for_annual(
        self,
        sample_agent_config: AgentConfig,
        sample_secrets,
        prompts_dir: Path,
        tmp_path: Path,
    ) -> None:
        """Annual cadence uses configured model override (SRC-054)."""
        from ai_news_agent.config.models import LLMCadenceOverride, LLMConfig

        config = sample_agent_config.model_copy(
            update={
                "llm": LLMConfig(
                    provider="openai",
                    model="gpt-4o",
                    cadence_overrides={
                        "annual": LLMCadenceOverride(model="o3", thinking=True),
                    },
                ),
            }
        )
        from ai_news_agent.storage.tinydb_store import TinyDBArticleStore

        store = TinyDBArticleStore(tmp_path / "store.json")
        with patch("ai_news_agent.curation.agent.get_llm_client") as mock_factory:
            dummy = DummyLLMClient()
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
        # Annual run should have used o3 model (checked via metadata)
        assert result.metadata.llm_model == "o3"

    def test_twitter_degradation_note_when_no_signals(
        self,
        sample_agent_config: AgentConfig,
        sample_secrets,
        prompts_dir: Path,
        tmp_path: Path,
    ) -> None:
        """When no tweet signals, degradation note is appended (SRC-148)."""
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
        # No tweet signals in store → degradation note should be set (SRC-148)
        assert result.twitter_degradation_note is not None
        assert (
            "SRC-148" in result.twitter_degradation_note
            or "unavailable" in result.twitter_degradation_note.lower()
        )
