"""
sourcing/agent.py — SourcingAgent orchestrator and CLI entry point.

The Sourcing Agent's only responsibility is to fetch candidate articles from
configured sources and persist them in the store.  It **does not** score,
rank, filter, or summarise — those are Curation Agent responsibilities (SRC-013).

Pipeline (SRC-008–SRC-013):
  1.  Fetch tweet signals from configured influencer handles  (SRC-067–SRC-069)
      → Graceful degradation if Twitter API is unavailable   (SRC-148)
  1b. Convert standalone tweets (no linked URLs) to          (SRC-047 exception)
      ArticleRecord candidates with source_class="twitter"
  2.  Fetch web articles from all source tiers               (SRC-053, SRC-060)
  3.  Enrich with primary articles from tweet-linked URLs    (SRC-069–SRC-070)
  4.  Deduplicate by (url_hash, agent_id); insert new only   (SRC-012)
  5.  Log SourcingRunResult for quality monitoring           (SRC-150)

Lookback window defaults (SRC-009):
  - ``window_start``: 00:00 UTC today
  - ``window_end``:   ``datetime.now(UTC)`` (current moment)

Multiple runs per day are safe — each run adds only new articles not yet in
the store; duplicates are silently counted and logged (SRC-010, SRC-012).

CLI entry point: ``ai-news-source`` (registered in pyproject.toml)

Traces: SRC-006–SRC-013, SRC-033–SRC-053, SRC-062–SRC-070, SRC-148, SRC-150
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import structlog

from ai_news_agent.config.loader import ConfigError, load_agent_config
from ai_news_agent.config.models import AgentConfig, RuntimeSecrets
from ai_news_agent.llm.factory import get_llm_client, get_search_tool
from ai_news_agent.sourcing.twitter_fetcher import TwitterFetcher
from ai_news_agent.sourcing.web_fetcher import WebFetcher
from ai_news_agent.storage.models import ArticleRecord, normalize_url, url_hash
from ai_news_agent.storage.tinydb_store import TinyDBArticleStore

if TYPE_CHECKING:
    from ai_news_agent.storage.base import AbstractArticleStore

log = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# SourcingRunResult — quality monitoring payload (SRC-150)
# ---------------------------------------------------------------------------


@dataclass
class SourcingRunResult:
    """
    Summary of a single sourcing run — emitted as a structured log event and
    returned to the caller for quality monitoring (SRC-150).

    All count fields are non-negative integers.  ``twitter_signal_available``
    reflects whether the Twitter API responded successfully for this run (SRC-148).

    Traces: SRC-012 (new vs duplicate counts), SRC-148 (twitter_signal_available),
            SRC-150 (monitoring fields: items_by_tier, items_by_source_class,
                     tweet_api_call_count)
    """

    agent_id: str
    run_at: datetime
    window_start: datetime
    window_end: datetime

    # Article counters (SRC-012, SRC-150)
    articles_fetched: int  # total raw candidates examined
    articles_inserted: int  # new (non-duplicate) insertions
    articles_duplicate: int  # duplicates silently skipped

    # Tweet signal counters (SRC-067, SRC-150)
    tweets_fetched: int  # raw tweet signals collected from all handles
    tweets_inserted: int  # new (non-duplicate) tweet signals stored

    # Twitter API availability (SRC-148)
    twitter_signal_available: bool  # False → web-only degraded mode

    # Quality monitoring fields (SRC-150)
    tweet_api_call_count: int  # 0 when degraded (SRC-150)
    items_by_tier: dict[str, int] = field(default_factory=dict)  # SRC-150
    items_by_source_class: dict[str, int] = field(default_factory=dict)  # SRC-150
    errors: list[str] = field(default_factory=list)  # non-fatal error messages


# ---------------------------------------------------------------------------
# SourcingAgent
# ---------------------------------------------------------------------------


class SourcingAgent:
    """
    Sourcing Agent — fetches candidate articles from web and Twitter/X,
    deduplicates, and stores them.

    **IMPORTANT**: This agent's job is **strictly to source** — curation,
    ranking, and summarisation happen later in the CurationAgent (SRC-013).
    No LLM scoring, no article selection — raw fetch and persist only.

    Key behaviours:
    - Lookback window starts at 00:00 UTC of the current day by default (SRC-009).
    - Multiple runs per day are safe and additive — only new records are
      inserted; duplicates are counted and logged (SRC-010, SRC-012).
    - Articles are stored with title / abstract / url + unique identifiers (SRC-011).
    - Deduplication primary key: ``(url_hash, agent_id)`` (SRC-012).
    - Twitter content is signal/lead-gen only — not stored as primary news (SRC-047).
    - Tweet-linked URLs are fetched as web articles to create primary source
      records for the curation LLM to reference (SRC-069–SRC-070).
    - If Twitter API is unavailable, continues with web sources alone (SRC-148).

    Traces: SRC-006–SRC-013, SRC-033–SRC-053, SRC-062–SRC-070, SRC-148, SRC-150
    """

    def __init__(
        self,
        config: AgentConfig,
        secrets: RuntimeSecrets,
        store: AbstractArticleStore | None = None,
    ) -> None:
        """
        Initialise the Sourcing Agent and all sub-components.

        Args:
            config:  Per-agent configuration loaded from YAML (SRC-071–SRC-073).
            secrets: Runtime secrets from environment variables — NEVER from YAML
                     (SRC-073).
            store:   Article store; defaults to TinyDBArticleStore at
                     ``{output_dir}/store.json`` (SRC-053).  Inject a mock or
                     in-memory store for testing.
        """
        self._config = config
        self._secrets = secrets
        self._store: AbstractArticleStore = store or TinyDBArticleStore(
            f"{config.output_dir}/store.json"
        )
        # LLM client — used by WebFetcher for prompt-assisted sourcing (future)
        self._llm = get_llm_client(config.llm, secrets)
        # Search tool — optional; web fetch is skipped gracefully when None.
        # Anthropic/Google users without WEB_SEARCH_API_KEY still get Twitter.
        try:
            search_tool = get_search_tool(config.llm, secrets)
        except ConfigError:
            search_tool = None
            log.warning(
                "sourcing_no_search_tool",
                provider=config.llm.provider,
                msg="No web search tool configured — web sourcing disabled, Twitter only. "
                "Set WEB_SEARCH_API_KEY (brave or tavily) to enable web sourcing.",
            )
        self._web_fetcher = WebFetcher(
            config=config,
            llm_client=self._llm,
            search_tool=search_tool,
        )
        self._twitter_fetcher = TwitterFetcher(
            config=config,
            bearer_token=secrets.twitter_bearer_token,
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def run(
        self,
        window_start: datetime | None = None,
        window_end: datetime | None = None,
    ) -> SourcingRunResult:
        """
        Execute a full sourcing run for the given lookback window.

        Defaults (SRC-009):
        - ``window_start``: 00:00 UTC today (start of the current day).
        - ``window_end``:   now (current UTC moment).

        Steps:
        1. **Twitter signal fetch** (SRC-067–SRC-069):
           - Fetch tweet signals from all configured handles.
           - Graceful degradation if Twitter is unavailable → web-only (SRC-148).
           - Store new :class:`TweetSignal` objects (dedup by tweet_id + agent_id).
           - Collect hydrated linked URLs for step 3.
        2. **Web article fetch** (SRC-053, SRC-060):
           - Run searches across all tiers using the configured search tool.
           - Converts each result to an :class:`ArticleRecord`.
        3. **Tweet URL enrichment** (SRC-069–SRC-070):
           - For each unique URL extracted from tweet signals, fetch the primary
             article via ``fetch_from_tweet_urls``.
           - Adds primary-source records for influencer-signalled stories.
        4. **Deduplication and storage** (SRC-012):
           - Calls ``store.insert_if_new`` on all candidates.
           - Duplicate ``(url_hash, agent_id)`` pairs are counted and skipped.
        5. **Quality monitoring log** (SRC-150):
           - Emits a structured log event with all monitoring fields.
           - Returns :class:`SourcingRunResult` to the caller.

        Args:
            window_start: Lookback window start (UTC-aware datetime).  If
                          ``None``, defaults to 00:00 UTC today (SRC-009).
            window_end:   Lookback window end (UTC-aware datetime).  If
                          ``None``, defaults to the current UTC time.

        Returns:
            :class:`SourcingRunResult` with complete monitoring metadata.

        Traces: SRC-008–SRC-013, SRC-033–SRC-053, SRC-067–SRC-070, SRC-148, SRC-150
        """
        now = datetime.now(UTC)

        # Default window: today 00:00 UTC → now (SRC-009)
        if window_start is None:
            window_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        if window_end is None:
            window_end = now

        log.info(
            "sourcing_run_start",
            agent_id=self._config.agent_id,
            window_start=window_start.isoformat(),
            window_end=window_end.isoformat(),
        )

        result = SourcingRunResult(
            agent_id=self._config.agent_id,
            run_at=now,
            window_start=window_start,
            window_end=window_end,
            articles_fetched=0,
            articles_inserted=0,
            articles_duplicate=0,
            tweets_fetched=0,
            tweets_inserted=0,
            twitter_signal_available=True,
            tweet_api_call_count=0,
        )

        # ------------------------------------------------------------------
        # Step 1: Twitter/X signal fetch (SRC-067–SRC-069, SRC-148)
        # ------------------------------------------------------------------
        tweet_linked_urls: list[str] = []

        if self._config.twitter.enabled:
            signals, twitter_ok = self._twitter_fetcher.fetch(
                window_start=window_start,
                window_end=window_end,
                agent_id=self._config.agent_id,
            )
            result.twitter_signal_available = twitter_ok
            result.tweets_fetched = len(signals)
            result.tweet_api_call_count = len(self._config.twitter.handles) if twitter_ok else 0

            for signal in signals:
                # Store new tweet signals (dedup by tweet_id + agent_id)
                inserted = self._store.insert_tweet_signal(signal)
                if inserted:
                    result.tweets_inserted += 1
                # Collect linked URLs for step 3 (SRC-069)
                tweet_linked_urls.extend(signal.linked_urls)
        else:
            result.twitter_signal_available = False

        if not result.twitter_signal_available:
            log.warning(
                "twitter_signal_unavailable",
                agent_id=self._config.agent_id,
                msg=(
                    "Continuing with web sources alone — a degradation note will be "
                    "appended to the digest (SRC-148)."
                ),
            )

        # ------------------------------------------------------------------
        # Step 1b: Standalone-tweet article records (SRC-047 exception path)
        #
        # Tweets with no external linked URLs are potential "tweet IS the news"
        # candidates (e.g. an executive announcement before press coverage).
        # We build ArticleRecords so the Curation Agent can evaluate the tweet
        # content on its merits alongside web articles. source_class="twitter"
        # distinguishes them; the curation prompt instructs the LLM accordingly.
        # ------------------------------------------------------------------
        standalone_tweet_articles: list[ArticleRecord] = []
        for signal in signals:
            if not signal.linked_urls:
                tweet_url_str = f"https://x.com/{signal.handle}/status/{signal.tweet_id}"
                canonical = normalize_url(tweet_url_str)
                if len(signal.text) > 120:
                    truncated = signal.text[:120]
                    last_space = truncated.rfind(" ")
                    headline = truncated[:last_space] if last_space > 60 else truncated
                else:
                    headline = signal.text
                standalone_tweet_articles.append(
                    ArticleRecord(
                        url_hash=url_hash(canonical),
                        url=canonical,
                        headline=headline,
                        abstract=signal.text,
                        source_name=f"@{signal.handle} on X",
                        pub_date=signal.created_at,
                        fetched_at=signal.fetched_at,
                        tier="2",
                        source_class="twitter",
                        agent_id=signal.agent_id,
                        twitter_handle=signal.handle,
                        tweet_url=canonical,
                    )
                )
        if standalone_tweet_articles:
            log.debug(
                "sourcing_standalone_tweet_articles",
                agent_id=self._config.agent_id,
                count=len(standalone_tweet_articles),
            )

        # ------------------------------------------------------------------
        # Step 2: Web article fetch (SRC-053, SRC-060)
        # ------------------------------------------------------------------
        web_articles = self._web_fetcher.fetch_all(
            window_start=window_start,
            window_end=window_end,
        )

        # ------------------------------------------------------------------
        # Step 3: Tweet URL enrichment (SRC-069–SRC-070)
        #
        # For each unique URL from tweet signals, fetch the primary article.
        # These are stored as "web" source_class records — they are primary
        # sources discovered via Twitter lead-generation, not tweets themselves.
        # ------------------------------------------------------------------
        tweet_url_articles = []
        if tweet_linked_urls:
            unique_tweet_urls = list(dict.fromkeys(u for u in tweet_linked_urls if u))
            tweet_url_articles = self._web_fetcher.fetch_from_tweet_urls(
                urls=unique_tweet_urls,
                agent_id=self._config.agent_id,
            )
            log.debug(
                "sourcing_tweet_url_articles",
                agent_id=self._config.agent_id,
                tweet_url_count=len(unique_tweet_urls),
                articles_produced=len(tweet_url_articles),
            )

        all_articles = web_articles + tweet_url_articles + standalone_tweet_articles
        result.articles_fetched = len(all_articles)

        # ------------------------------------------------------------------
        # Step 4: Deduplication and storage (SRC-012)
        # ------------------------------------------------------------------
        tier_counts: dict[str, int] = {}
        source_class_counts: dict[str, int] = {}

        for article in all_articles:
            inserted = self._store.insert_if_new(article)
            if inserted:
                result.articles_inserted += 1
                tier_counts[article.tier] = tier_counts.get(article.tier, 0) + 1
                source_class_counts[article.source_class] = (
                    source_class_counts.get(article.source_class, 0) + 1
                )
            else:
                result.articles_duplicate += 1

        result.items_by_tier = tier_counts
        result.items_by_source_class = source_class_counts

        # ------------------------------------------------------------------
        # Step 5: Quality monitoring log (SRC-150)
        # ------------------------------------------------------------------
        log.info(
            "sourcing_run_complete",
            agent_id=self._config.agent_id,
            articles_fetched=result.articles_fetched,
            articles_inserted=result.articles_inserted,
            articles_duplicate=result.articles_duplicate,
            tweets_fetched=result.tweets_fetched,
            tweets_inserted=result.tweets_inserted,
            twitter_available=result.twitter_signal_available,
            tweet_api_call_count=result.tweet_api_call_count,
            items_by_tier=result.items_by_tier,
            items_by_source_class=result.items_by_source_class,
        )

        return result


# ---------------------------------------------------------------------------
# CLI entry point (SRC-076–SRC-077: local dev manual trigger)
# ---------------------------------------------------------------------------


def cli_main() -> None:
    """
    Command-line entry point: ``ai-news-source``.

    Runs the Sourcing Agent for a single agent configuration and prints a
    summary to stderr.  Exit code 0 on success, 1 on error.

    Usage::

        # Default: today's window, default agent config
        ai-news-source

        # Explicit agent config and extended lookback
        ai-news-source --agent configs/default-agent.yaml --window-days 2

        # Short custom window
        ai-news-source --agent configs/tech-agent.yaml --window-days 1

    Flags:
        --agent PATH         Path to per-agent YAML config (default: configs/default-agent.yaml)
        --window-days N      Lookback window in days (default: 1 = today from 00:00 UTC)
        --scheduler PATH     Path to scheduler.yaml (used only for validation, optional)

    Traces: SRC-076 (local dev Phase 1), SRC-077 (manual trigger for backfills)
    """
    parser = argparse.ArgumentParser(
        prog="ai-news-source",
        description=(
            "Run the AI News Sourcing Agent for a single agent configuration.\n"
            "Fetches candidate articles from configured web sources and Twitter/X,\n"
            "deduplicates, and persists them to the store."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--agent",
        default="configs/default-agent.yaml",
        metavar="PATH",
        help="Path to per-agent YAML config file (default: configs/default-agent.yaml)",
    )
    parser.add_argument(
        "--window-days",
        type=int,
        default=1,
        metavar="N",
        help=(
            "Lookback window in full days (default: 1 = today from 00:00 UTC). "
            "Use 2 for yesterday + today, etc."
        ),
    )
    parser.add_argument(
        "--scheduler",
        default="configs/scheduler.yaml",
        metavar="PATH",
        help="Path to root scheduler.yaml (used only for validation; optional)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "Fetch and dedup articles but do NOT persist to the store. "
            "Useful for testing search tool connectivity. (SRC-102)"
        ),
    )
    args = parser.parse_args()

    try:
        config = load_agent_config(args.agent)
        secrets = RuntimeSecrets()  # type: ignore[call-arg]
    except Exception as exc:  # noqa: BLE001
        print(f"ERROR: Failed to load configuration: {exc}", file=sys.stderr)
        sys.exit(1)

    now = datetime.now(UTC)
    window_start = (now - timedelta(days=args.window_days)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )

    if args.dry_run:
        print(
            f"DRY RUN — fetching for agent '{config.agent_id}' "
            f"window {window_start.date()} → {now.date()} (no writes)",
            file=sys.stderr,
        )
        # In dry-run mode, use an in-memory store that discards after run
        import os
        import tempfile  # noqa: E401

        from ai_news_agent.storage.tinydb_store import TinyDBArticleStore

        with tempfile.TemporaryDirectory() as tmpdir:
            store = TinyDBArticleStore(os.path.join(tmpdir, "dry-run.json"))
            agent = SourcingAgent(config=config, secrets=secrets, store=store)
            result = agent.run(window_start=window_start, window_end=now)
            store.close()
    else:
        agent = SourcingAgent(config=config, secrets=secrets)
        result = agent.run(window_start=window_start, window_end=now)

    print(
        f"Sourcing complete — agent: {result.agent_id}\n"
        f"  Window:              {result.window_start.date()} → {result.window_end.date()}\n"
        f"  Articles fetched:    {result.articles_fetched}\n"
        f"  Articles inserted:   {result.articles_inserted}\n"
        f"  Duplicates skipped:  {result.articles_duplicate}\n"
        f"  Tweet signals:       {result.tweets_fetched} fetched / "
        f"{result.tweets_inserted} new\n"
        f"  Twitter available:   {result.twitter_signal_available}\n"
        f"  By tier:             {result.items_by_tier}\n"
        f"  By source class:     {result.items_by_source_class}",
        file=sys.stderr,
    )
    sys.exit(0)


if __name__ == "__main__":
    cli_main()
