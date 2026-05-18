"""
tests/unit/test_pipeline_sourcing_window.py — Pipeline cadence-aware sourcing
window expansion + web_fetcher backfill pub_date fallback.

Covers:
- Pipeline.run() expands sourcing's window to the cadence window when the
  store is sparse for that cadence (weekly/monthly/annual). A populated
  store skips the expansion so we don't burn search quota.
- web_fetcher.fetch_all uses the window midpoint as the pub_date fallback
  when the window ends > 24h ago, so backfilled articles land inside the
  curation window even when snippets carry no parseable date.

Traces: SRC-008–SRC-013 (sourcing windows), SRC-028–SRC-032 (cadence windows),
        SRC-053 (configurable store), SRC-060 (search tool abstraction)
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock, patch

from ai_news_agent.config.models import AgentConfig, RuntimeSecrets
from ai_news_agent.llm.base import SearchResult
from ai_news_agent.pipeline import Pipeline
from ai_news_agent.sourcing.agent import SourcingRunResult
from ai_news_agent.sourcing.web_fetcher import WebFetcher
from ai_news_agent.storage.tinydb_store import TinyDBArticleStore


def _make_sourcing_result() -> SourcingRunResult:
    """Build a successful SourcingRunResult skeleton for mocked calls."""
    now = datetime.now(UTC)
    return SourcingRunResult(
        agent_id="test-agent",
        run_at=now,
        window_start=now,
        window_end=now,
        articles_fetched=0,
        articles_inserted=0,
        articles_duplicate=0,
        tweets_fetched=0,
        tweets_inserted=0,
        twitter_signal_available=True,
        tweet_api_call_count=0,
    )


class TestPipelineSourcingWindowExpansion:
    """Verify that Pipeline expands sourcing's window for sparse stores."""

    def _build_pipeline(
        self,
        sample_agent_config: AgentConfig,
        sample_secrets: RuntimeSecrets,
        tmp_path,
    ) -> tuple[Pipeline, TinyDBArticleStore]:
        store = TinyDBArticleStore(tmp_path / "store.json")
        pipeline = Pipeline(
            config=sample_agent_config,
            secrets=sample_secrets,
            store=store,
        )
        return pipeline, store

    def test_weekly_run_with_empty_store_expands_to_weekly_window(
        self, sample_agent_config: AgentConfig, sample_secrets: RuntimeSecrets, tmp_path,
    ) -> None:
        """
        Fresh-install weekly run: store has zero candidates for the cadence
        window, so the pipeline must hand sourcing the full weekly window
        instead of letting it default to "today only".
        """
        pipeline, _ = self._build_pipeline(
            sample_agent_config, sample_secrets, tmp_path,
        )

        captured: dict = {}

        def fake_run(self, *, window_start=None, window_end=None, **kwargs):
            captured["window_start"] = window_start
            captured["window_end"] = window_end
            return _make_sourcing_result()

        with patch("ai_news_agent.pipeline.SourcingAgent") as mock_sourcing_cls, \
             patch("ai_news_agent.pipeline.CurationAgent") as mock_curation_cls, \
             patch("ai_news_agent.pipeline.RenderingAgent") as mock_rendering_cls:
            mock_sourcing_cls.return_value.run = MagicMock(
                side_effect=lambda **kw: (captured.update(kw) or _make_sourcing_result()),
            )
            mock_curation_cls.return_value.run = MagicMock(
                return_value=MagicMock(
                    metadata=MagicMock(
                        items_considered=0, items_included=0,
                        items_by_tier={}, items_by_source_class={},
                        token_usage=0, llm_provider="openai", llm_model="gpt-4o",
                        prompt_version="sha256:x",
                        twitter_signal_available=True, tweet_api_call_count=0,
                    ),
                    items=[], themes=[], outlook="", predictions=[],
                    twitter_degradation_note=None, dry_run=False,
                    diagnostics=None,
                ),
            )
            mock_rendering_cls.return_value.render_and_update_store = MagicMock(
                return_value=MagicMock(
                    markdown_path="/tmp/x.md",
                    html_path="/tmp/x.html",
                    json_path="/tmp/x.json",
                    items_rendered=0, items_dropped_no_url=0,
                ),
            )

            pipeline.run(cadence="weekly")

        # The captured window must span 7 days (the weekly cadence window),
        # not just "today 00:00 → now".
        delta_days = (captured["window_end"] - captured["window_start"]).days
        assert delta_days >= 6, (
            f"Sourcing window must span the weekly cadence (≥6 days), "
            f"got {delta_days} days"
        )

    def test_weekly_run_with_populated_store_does_not_expand(
        self, sample_agent_config: AgentConfig, sample_secrets: RuntimeSecrets, tmp_path,
    ) -> None:
        """
        Populated store: pipeline must NOT expand sourcing's window — leave
        it to the sourcing agent's default ("today 00:00 → now") so we don't
        burn extra search calls every run.
        """
        from ai_news_agent.curation.agent import _weekly_window
        from ai_news_agent.storage.models import (
            ArticleRecord,
            normalize_url,
            url_hash,
        )

        pipeline, store = self._build_pipeline(
            sample_agent_config, sample_secrets, tmp_path,
        )

        # Insert top_n+ articles inside this week's cadence window so the
        # "is store sparse?" check returns False.
        now = datetime.now(UTC)
        week_start, week_end = _weekly_window(now)
        sample_pub = week_start + (week_end - week_start) / 2
        for i in range(sample_agent_config.limits.weekly_top_n + 2):
            raw = f"https://reuters.com/article-{i}"
            canonical = normalize_url(raw)
            store.insert_if_new(ArticleRecord(
                url_hash=url_hash(canonical),
                url=canonical,
                headline=f"Article {i}",
                abstract=None,
                source_name="reuters.com",
                pub_date=sample_pub,
                fetched_at=sample_pub,
                tier="1b",
                source_class="web",
                agent_id="test-agent",
                twitter_handle=None,
                tweet_url=None,
            ))

        captured: dict = {}

        with patch("ai_news_agent.pipeline.SourcingAgent") as mock_sourcing_cls, \
             patch("ai_news_agent.pipeline.CurationAgent") as mock_curation_cls, \
             patch("ai_news_agent.pipeline.RenderingAgent") as mock_rendering_cls:
            mock_sourcing_cls.return_value.run = MagicMock(
                side_effect=lambda **kw: (captured.update(kw) or _make_sourcing_result()),
            )
            mock_curation_cls.return_value.run = MagicMock(
                return_value=MagicMock(
                    metadata=MagicMock(
                        items_considered=0, items_included=0,
                        items_by_tier={}, items_by_source_class={},
                        token_usage=0, llm_provider="openai", llm_model="gpt-4o",
                        prompt_version="sha256:x",
                        twitter_signal_available=True, tweet_api_call_count=0,
                    ),
                    items=[], themes=[], outlook="", predictions=[],
                    twitter_degradation_note=None, dry_run=False,
                    diagnostics=None,
                ),
            )
            mock_rendering_cls.return_value.render_and_update_store = MagicMock(
                return_value=MagicMock(
                    markdown_path="/tmp/x.md",
                    html_path="/tmp/x.html",
                    json_path="/tmp/x.json",
                    items_rendered=0, items_dropped_no_url=0,
                ),
            )

            pipeline.run(cadence="weekly")

        # Pipeline did not pre-compute a window; sourcing got None for both,
        # which lets the sourcing agent apply its default "today" window.
        assert captured.get("window_start") is None
        assert captured.get("window_end") is None

    def test_daily_run_never_expands_sourcing_window(
        self, sample_agent_config: AgentConfig, sample_secrets: RuntimeSecrets, tmp_path,
    ) -> None:
        """
        Daily cadence keeps the existing behavior — sourcing's own "today"
        default applies. The widened daily curation window (SRC-029) already
        covers "yesterday 00:00 → now" for the curate step.
        """
        pipeline, _ = self._build_pipeline(
            sample_agent_config, sample_secrets, tmp_path,
        )

        captured: dict = {}

        with patch("ai_news_agent.pipeline.SourcingAgent") as mock_sourcing_cls, \
             patch("ai_news_agent.pipeline.CurationAgent") as mock_curation_cls, \
             patch("ai_news_agent.pipeline.RenderingAgent") as mock_rendering_cls:
            mock_sourcing_cls.return_value.run = MagicMock(
                side_effect=lambda **kw: (captured.update(kw) or _make_sourcing_result()),
            )
            mock_curation_cls.return_value.run = MagicMock(
                return_value=MagicMock(
                    metadata=MagicMock(
                        items_considered=0, items_included=0,
                        items_by_tier={}, items_by_source_class={},
                        token_usage=0, llm_provider="openai", llm_model="gpt-4o",
                        prompt_version="sha256:x",
                        twitter_signal_available=True, tweet_api_call_count=0,
                    ),
                    items=[], themes=[], outlook="", predictions=[],
                    twitter_degradation_note=None, dry_run=False,
                    diagnostics=None,
                ),
            )
            mock_rendering_cls.return_value.render_and_update_store = MagicMock(
                return_value=MagicMock(
                    markdown_path="/tmp/x.md",
                    html_path="/tmp/x.html",
                    json_path="/tmp/x.json",
                    items_rendered=0, items_dropped_no_url=0,
                ),
            )

            pipeline.run(cadence="daily")

        assert captured.get("window_start") is None
        assert captured.get("window_end") is None


class TestWebFetcherBackfillPubDateFallback:
    """
    Verify the backfill-aware pub_date fallback. When the sourcing window
    ends > 24h ago, the web fetcher uses the window midpoint instead of
    ``fetched_at`` so backfilled articles land inside the curation window.
    """

    def _make_fetcher(self, sample_agent_config: AgentConfig) -> tuple[WebFetcher, MagicMock]:
        search_tool = MagicMock()
        llm = MagicMock()
        return WebFetcher(
            config=sample_agent_config,
            llm_client=llm,
            search_tool=search_tool,
        ), search_tool

    def test_pub_date_falls_to_window_midpoint_for_old_window(
        self, sample_agent_config: AgentConfig,
    ) -> None:
        """
        Window ending 5 days ago: pub_date for snippets without a parseable
        date must fall inside the window, not be stamped with ``now``.
        """
        fetcher, search_tool = self._make_fetcher(sample_agent_config)

        # Return a single result with no date in the snippet so the extractor
        # is forced to use the fallback.
        search_tool.search.return_value = [
            SearchResult(
                url="https://reuters.com/example",
                title="Example AI piece",
                snippet="No publication date appears in this snippet text.",
                source="reuters.com",
            ),
        ]

        now = datetime.now(UTC)
        window_start = now - timedelta(days=10)
        window_end = now - timedelta(days=5)

        articles = fetcher.fetch_all(
            window_start=window_start,
            window_end=window_end,
        )

        assert articles, "Expected at least one article from the mocked search"
        # pub_date must land inside the window — not "now".
        for article in articles:
            assert window_start <= article.pub_date <= window_end, (
                f"Backfill pub_date {article.pub_date} must fall in "
                f"[{window_start}, {window_end}]"
            )

    def test_pub_date_falls_to_fetched_at_for_current_window(
        self, sample_agent_config: AgentConfig,
    ) -> None:
        """
        Window ending "now": the fallback stays as ``fetched_at`` so we don't
        rewrite same-day sourcing behavior.
        """
        fetcher, search_tool = self._make_fetcher(sample_agent_config)

        search_tool.search.return_value = [
            SearchResult(
                url="https://reuters.com/today",
                title="Today's AI piece",
                snippet="No publication date appears in this snippet text.",
                source="reuters.com",
            ),
        ]

        now = datetime.now(UTC)
        window_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        window_end = now

        articles = fetcher.fetch_all(
            window_start=window_start,
            window_end=window_end,
        )

        assert articles
        for article in articles:
            # fetched_at and pub_date should both be close to "now" — the
            # backfill heuristic should NOT fire.
            assert abs((article.pub_date - now).total_seconds()) < 5
