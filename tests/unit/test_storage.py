"""
tests/unit/test_storage.py — Document store: models, dedup, lookback queries,
cadence windows, stats, digest records, factory, and SQLite parity.

Traces: SRC-008–SRC-013 (lookback windows, deduplication, sourcing rule),
        SRC-028–SRC-032 (daily/weekly/monthly/annual cadence windows),
        SRC-048–SRC-049 (curated item schema, URL enforcement),
        SRC-053 (pluggable document store — TinyDB + SQLite),
        SRC-072 (agent_id scoping),
        SRC-098 (unit tests — mock filesystem only),
        SRC-129 (digest prompt_version),
        SRC-145 (idempotent digest upsert),
        SRC-150 (get_stats monitoring)
"""

from __future__ import annotations

import datetime
import logging
from dataclasses import replace
from datetime import UTC, timedelta
from pathlib import Path

import pytest

from ai_news_agent.storage import (
    ArticleRecord,
    Cadence,
    DigestRecord,
    SQLiteArticleStore,
    StoreFactory,
    StoreStats,
    TinyDBArticleStore,
    TweetSignal,
    headline_similarity,
    lookback_window,
    normalize_url,
    url_hash,
)
from ai_news_agent.storage.base import AbstractArticleStore

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_NOW = datetime.datetime(2026, 5, 11, 6, 0, 0, tzinfo=UTC)


def _make_article(
    agent_id: str = "test-agent",
    url_suffix: str = "a",
    pub_date: datetime.datetime | None = None,
    tier: str = "1b",
    source_class: str = "web",
    headline: str | None = None,
) -> ArticleRecord:
    raw_url   = f"https://reuters.com/article-{url_suffix}"
    canonical = normalize_url(raw_url)
    return ArticleRecord(
        url_hash=url_hash(canonical),
        url=canonical,
        headline=headline or f"Article {url_suffix}",
        abstract="Test abstract.",
        source_name="Reuters",
        pub_date=pub_date or _NOW,
        fetched_at=_NOW,
        tier=tier,
        source_class=source_class,
        agent_id=agent_id,
    )


def _make_tweet(
    agent_id: str = "test-agent",
    tweet_id: str = "1001",
    created_at: datetime.datetime | None = None,
) -> TweetSignal:
    return TweetSignal(
        tweet_id=tweet_id,
        handle="karpathy",
        text="Fascinating paper on enterprise AI just dropped.",
        created_at=created_at or _NOW,
        linked_urls=["https://arxiv.org/abs/2026.test"],
        agent_id=agent_id,
        fetched_at=_NOW,
        weight=1.0,
    )


def _make_digest(
    agent_id: str = "test-agent",
    cadence: str = "daily",
    run_date: datetime.date | None = None,
) -> DigestRecord:
    rd = run_date or _NOW.date()
    return DigestRecord(
        agent_id=agent_id,
        cadence=cadence,
        run_date=rd,
        window_start=_NOW - timedelta(days=1),
        window_end=_NOW,
        prompt_version="sha256:abc123",
        llm_provider="openai",
        llm_model="gpt-4o",
        items_considered=20,
        items_included=5,
        items_by_tier={"1b": 3, "2": 2},
        items_by_source_class={"web": 4, "twitter": 1},
        twitter_signal_available=True,
        tweet_api_call_count=9,
        token_usage=4200,
    )


# ---------------------------------------------------------------------------
# Fixtures — both TinyDB and SQLite backed stores
# ---------------------------------------------------------------------------

@pytest.fixture
def tiny_store(tmp_path: Path) -> TinyDBArticleStore:
    """TinyDB store backed by a temp file.  Traces: SRC-053 (TinyDB default)."""
    store = TinyDBArticleStore(tmp_path / "store.json")
    yield store
    store.close()


@pytest.fixture
def sqlite_store(tmp_path: Path) -> SQLiteArticleStore:
    """SQLite store backed by a temp file.  Traces: SRC-053 (pluggable SQLite)."""
    store = SQLiteArticleStore(tmp_path / "store.db")
    yield store
    store.close()


@pytest.fixture(params=["tinydb", "sqlite"])
def store(request: pytest.FixtureRequest, tmp_path: Path) -> AbstractArticleStore:
    """
    Parametrised fixture — runs each test with BOTH TinyDB and SQLite.
    Ensures both implementations satisfy the AbstractArticleStore contract.
    Traces: SRC-053 (pluggable store parity)
    """
    suffix = request.param
    db_path = tmp_path / f"store_{suffix}.{'json' if suffix == 'tinydb' else 'db'}"
    s = StoreFactory.from_backend(suffix, db_path)
    yield s
    s.close()


# ---------------------------------------------------------------------------
# Backward-compat alias used by conftest.py and other test modules
# ---------------------------------------------------------------------------

@pytest.fixture
def tiny_db_store(tmp_path: Path) -> TinyDBArticleStore:
    """Alias kept for backward compat with conftest fixtures."""
    store = TinyDBArticleStore(tmp_path / "store.json")
    yield store
    store.close()


# ===========================================================================
# Section 1: URL normalisation and hashing (SRC-012)
# ===========================================================================

class TestUrlNormalisation:
    """Traces: SRC-012 (canonical URL deduplication)."""

    def test_tracking_params_stripped(self) -> None:
        url = "https://reuters.com/article?utm_source=twitter&utm_campaign=test"
        canonical = normalize_url(url)
        assert "utm_source" not in canonical
        assert "utm_campaign" not in canonical

    def test_fbclid_stripped(self) -> None:
        url = "https://bloomberg.com/story?fbclid=abc123"
        assert "fbclid" not in normalize_url(url)

    def test_non_tracking_params_preserved(self) -> None:
        url = "https://example.com/article?page=2&sort=date"
        canonical = normalize_url(url)
        assert "page=2" in canonical
        assert "sort=date" in canonical

    def test_trailing_slash_stripped(self) -> None:
        assert normalize_url("https://reuters.com/article/") == "https://reuters.com/article"

    def test_scheme_lowercased(self) -> None:
        assert normalize_url("HTTP://REUTERS.COM/article").startswith("http://")

    def test_netloc_lowercased(self) -> None:
        assert "reuters.com" in normalize_url("https://REUTERS.COM/article")

    def test_same_url_same_hash(self) -> None:
        a = "https://reuters.com/article"
        b = "https://reuters.com/article/"
        assert url_hash(normalize_url(a)) == url_hash(normalize_url(b))

    def test_url_hash_is_sha256_hex(self) -> None:
        h = url_hash("https://example.com")
        assert len(h) == 64
        assert all(c in "0123456789abcdef" for c in h)

    def test_different_urls_different_hash(self) -> None:
        h1 = url_hash(normalize_url("https://reuters.com/a"))
        h2 = url_hash(normalize_url("https://reuters.com/b"))
        assert h1 != h2


# ===========================================================================
# Section 2: headline_similarity (SRC-012 secondary dedup)
# ===========================================================================

class TestHeadlineSimilarity:
    """Traces: SRC-012 (secondary dedup — Levenshtein headline signal)."""

    def test_identical_headlines_score_one(self) -> None:
        assert headline_similarity("AI reshapes enterprise", "AI reshapes enterprise") == 1.0

    def test_completely_different_headlines_score_low(self) -> None:
        sim = headline_similarity("AI reshapes enterprise", "Football championship results")
        assert sim < 0.4

    def test_near_duplicate_above_threshold(self) -> None:
        # AMP variant or minor headline edit
        a = "EU AI Act Enters Enforcement Phase"
        b = "EU AI Act Enters the Enforcement Phase"
        assert headline_similarity(a, b) >= 0.85

    def test_case_insensitive(self) -> None:
        assert headline_similarity("AI NEWS", "ai news") == 1.0


# ===========================================================================
# Section 3: lookback_window helpers (SRC-008–SRC-010, SRC-028–SRC-032)
# ===========================================================================

class TestLookbackWindow:
    """Traces: SRC-008 (lookback windows), SRC-009 (daily), SRC-028–SRC-032."""

    # Reference: 2026-05-11 06:00:00 UTC (Monday)
    REF = datetime.datetime(2026, 5, 11, 6, 0, 0, tzinfo=UTC)

    def test_daily_window_is_yesterday(self) -> None:
        """Daily window = 2026-05-10 00:00 → 23:59:59 UTC.  Traces: SRC-009."""
        start, end = lookback_window(Cadence.DAILY, self.REF)
        assert start.date() == datetime.date(2026, 5, 10)
        assert start.hour == 0
        assert start.minute == 0
        assert end.date() == datetime.date(2026, 5, 10)
        assert end.hour == 23
        assert end.minute == 59

    def test_daily_window_utc_aware(self) -> None:
        start, end = lookback_window(Cadence.DAILY, self.REF)
        assert start.tzinfo is not None
        assert end.tzinfo is not None

    def test_daily_window_start_before_end(self) -> None:
        start, end = lookback_window(Cadence.DAILY, self.REF)
        assert start < end

    def test_weekly_window_covers_sunday_to_saturday(self) -> None:
        """Weekly window = previous Sunday–Saturday.  Traces: SRC-030."""
        # REF is Monday 2026-05-11 → previous week is Sun 2026-05-03 → Sat 2026-05-09
        start, end = lookback_window(Cadence.WEEKLY, self.REF)
        assert start.weekday() == 6   # Sunday (Python: Mon=0, Sun=6)
        assert end.weekday() == 5     # Saturday
        assert end > start

    def test_weekly_window_is_7_days(self) -> None:
        start, end = lookback_window(Cadence.WEEKLY, self.REF)
        delta = end - start
        # ≈ 6 days + 23:59:59 = almost 7 days
        assert 6 <= delta.days <= 7

    def test_monthly_window_covers_previous_month(self) -> None:
        """Monthly window = 2026-04-01 → 2026-04-30.  Traces: SRC-031."""
        # REF is May 2026
        start, end = lookback_window(Cadence.MONTHLY, self.REF)
        assert start.month == 4
        assert start.day == 1
        assert end.month == 4

    def test_monthly_window_wraps_year_boundary(self) -> None:
        """Jan 2026 reference → December 2025 window."""
        ref = datetime.datetime(2026, 1, 15, 0, 0, tzinfo=UTC)
        start, end = lookback_window(Cadence.MONTHLY, ref)
        assert start.year == 2025
        assert start.month == 12
        assert end.year == 2025
        assert end.month == 12

    def test_annual_window_is_previous_year(self) -> None:
        """Annual window = 2025-01-01 → 2025-12-31.  Traces: SRC-032."""
        start, end = lookback_window(Cadence.ANNUAL, self.REF)
        assert start.year == 2025
        assert start.month == 1
        assert start.day == 1
        assert end.year == 2025
        assert end.month == 12
        assert end.day == 31

    def test_cadence_enum_string_coercion(self) -> None:
        """Cadence enum is also a str subclass."""
        assert Cadence.DAILY == "daily"
        assert Cadence.WEEKLY == "weekly"
        assert Cadence.MONTHLY == "monthly"
        assert Cadence.ANNUAL == "annual"

    def test_unknown_cadence_raises(self) -> None:
        with pytest.raises(ValueError, match="quarterly"):
            lookback_window("quarterly")  # type: ignore[arg-type]


# ===========================================================================
# Section 4: ArticleRecord deduplication (SRC-012) — both backends
# ===========================================================================

class TestInsertIfNew:
    """Traces: SRC-012 (no same article stored twice).  Both backends."""

    def test_new_article_inserted(self, store: AbstractArticleStore) -> None:
        article = _make_article()
        assert store.insert_if_new(article) is True

    def test_duplicate_not_inserted(self, store: AbstractArticleStore) -> None:
        article = _make_article()
        store.insert_if_new(article)
        assert store.insert_if_new(article) is False

    def test_same_url_different_agent_inserted(self, store: AbstractArticleStore) -> None:
        """
        Same URL under a different agent_id is a different record.
        Traces: SRC-072 (agent_id scoping)
        """
        a = _make_article("agent-a")
        b = replace(a, agent_id="agent-b")
        store.insert_if_new(a)
        assert store.insert_if_new(b) is True

    def test_url_hash_is_dedup_key_not_headline(self, store: AbstractArticleStore) -> None:
        """
        Same URL, different headline → still a duplicate (url_hash matches).
        Traces: SRC-012
        """
        a = _make_article(headline="Original Headline")
        b = replace(a, headline="Edited Headline")
        store.insert_if_new(a)
        assert store.insert_if_new(b) is False

    def test_different_urls_same_headline_both_inserted(self, store: AbstractArticleStore) -> None:
        """Two genuinely different articles with same headline are stored separately."""
        a = _make_article(url_suffix="a")
        b = _make_article(url_suffix="b")
        store.insert_if_new(a)
        assert store.insert_if_new(b) is True

    def test_multiple_new_articles_all_inserted(self, store: AbstractArticleStore) -> None:
        articles = [_make_article(url_suffix=str(i)) for i in range(10)]
        for art in articles:
            store.insert_if_new(art)
        assert store.count_articles("test-agent") == 10


# ===========================================================================
# Section 5: Near-duplicate headline warning (SRC-012 §3.3)
# ===========================================================================

class TestNearDuplicateWarning:
    """
    Secondary dedup: articles with ≥0.85 headline similarity log a WARNING
    but are still inserted (URL hash is distinct).
    Traces: SRC-012 (architecture §3.3)
    """

    def test_near_duplicate_logged_tinydb(
        self, tiny_store: TinyDBArticleStore, caplog: pytest.LogCaptureFixture
    ) -> None:
        a = _make_article(url_suffix="orig", headline="EU AI Act Enters Enforcement Phase")
        b = _make_article(url_suffix="amp",  headline="EU AI Act Enters the Enforcement Phase")
        tiny_store.insert_if_new(a)
        with caplog.at_level(logging.WARNING):
            inserted = tiny_store.insert_if_new(b)
        assert inserted is True  # Still inserted — not blocked
        assert any("near_duplicate" in r.message.lower() for r in caplog.records)

    def test_near_duplicate_logged_sqlite(
        self, sqlite_store: SQLiteArticleStore, caplog: pytest.LogCaptureFixture
    ) -> None:
        a = _make_article(url_suffix="orig", headline="EU AI Act Enters Enforcement Phase")
        b = _make_article(url_suffix="amp",  headline="EU AI Act Enters the Enforcement Phase")
        sqlite_store.insert_if_new(a)
        with caplog.at_level(logging.WARNING):
            inserted = sqlite_store.insert_if_new(b)
        assert inserted is True
        assert any("near_duplicate" in r.message.lower() for r in caplog.records)

    def test_unrelated_headline_no_warning(
        self, tiny_store: TinyDBArticleStore, caplog: pytest.LogCaptureFixture
    ) -> None:
        a = _make_article(url_suffix="x", headline="AI reshapes enterprise software")
        b = _make_article(url_suffix="y", headline="Football championship results announced")
        tiny_store.insert_if_new(a)
        with caplog.at_level(logging.WARNING):
            tiny_store.insert_if_new(b)
        assert not any("near_duplicate" in r.message.lower() for r in caplog.records)


# ===========================================================================
# Section 6: get_window / lookback queries (SRC-008–SRC-010)
# ===========================================================================

class TestGetWindow:
    """Traces: SRC-008–SRC-010 (lookback window queries).  Both backends."""

    def test_articles_within_window_returned(self, store: AbstractArticleStore) -> None:
        in_window = _make_article(pub_date=_NOW - timedelta(hours=2))
        store.insert_if_new(in_window)

        results = store.get_window(
            "test-agent",
            window_start=_NOW - timedelta(days=1),
            window_end=_NOW,
        )
        assert len(results) == 1

    def test_articles_outside_window_excluded(self, store: AbstractArticleStore) -> None:
        outside = _make_article(url_suffix="old", pub_date=_NOW - timedelta(days=3))
        store.insert_if_new(outside)

        results = store.get_window(
            "test-agent",
            window_start=_NOW - timedelta(days=1),
            window_end=_NOW,
        )
        assert len(results) == 0

    def test_window_boundary_inclusive(self, store: AbstractArticleStore) -> None:
        """Articles exactly at window_start and window_end are included."""
        at_start = _make_article(url_suffix="start", pub_date=_NOW - timedelta(days=1))
        at_end   = _make_article(url_suffix="end",   pub_date=_NOW)
        store.insert_if_new(at_start)
        store.insert_if_new(at_end)

        results = store.get_window(
            "test-agent",
            window_start=_NOW - timedelta(days=1),
            window_end=_NOW,
        )
        assert len(results) == 2

    def test_multiple_runs_idempotent(self, store: AbstractArticleStore) -> None:
        """Re-inserting the same article produces exactly one record.  Traces: SRC-010, SRC-012."""
        article = _make_article(pub_date=_NOW - timedelta(hours=1))
        store.insert_if_new(article)
        store.insert_if_new(article)  # second run — must not duplicate

        results = store.get_window(
            "test-agent",
            window_start=_NOW - timedelta(days=1),
            window_end=_NOW,
        )
        assert len(results) == 1

    def test_results_sorted_by_pub_date_ascending(self, store: AbstractArticleStore) -> None:
        a = _make_article(url_suffix="old", pub_date=_NOW - timedelta(hours=5))
        b = _make_article(url_suffix="mid", pub_date=_NOW - timedelta(hours=3))
        c = _make_article(url_suffix="new", pub_date=_NOW - timedelta(hours=1))
        # Insert out of order
        store.insert_if_new(c)
        store.insert_if_new(a)
        store.insert_if_new(b)

        results = store.get_window(
            "test-agent",
            window_start=_NOW - timedelta(days=1),
            window_end=_NOW,
        )
        assert len(results) == 3
        assert results[0].url == a.url
        assert results[1].url == b.url
        assert results[2].url == c.url

    def test_different_agents_isolated(self, store: AbstractArticleStore) -> None:
        """Each agent's records are completely isolated.  Traces: SRC-072."""
        a = _make_article("agent-a", url_suffix="aa", pub_date=_NOW)
        b = _make_article("agent-b", url_suffix="bb", pub_date=_NOW)
        store.insert_if_new(a)
        store.insert_if_new(b)

        results_a = store.get_window("agent-a", _NOW - timedelta(hours=1), _NOW + timedelta(hours=1))
        results_b = store.get_window("agent-b", _NOW - timedelta(hours=1), _NOW + timedelta(hours=1))
        assert len(results_a) == 1
        assert results_a[0].agent_id == "agent-a"
        assert len(results_b) == 1
        assert results_b[0].agent_id == "agent-b"

    def test_empty_store_returns_empty_list(self, store: AbstractArticleStore) -> None:
        results = store.get_window("test-agent", _NOW - timedelta(days=7), _NOW)
        assert results == []


# ===========================================================================
# Section 7: get_window_by_cadence (SRC-009, SRC-028–SRC-032)
# ===========================================================================

class TestGetWindowByCadence:
    """Convenience cadence wrapper — both backends."""

    # Reference: Monday 2026-05-11 — so "previous day" = 2026-05-10
    REF = datetime.datetime(2026, 5, 11, 6, 0, 0, tzinfo=UTC)

    def test_daily_cadence_returns_yesterday(self, store: AbstractArticleStore) -> None:
        yesterday_noon = datetime.datetime(2026, 5, 10, 12, 0, 0, tzinfo=UTC)
        article = _make_article(pub_date=yesterday_noon)
        store.insert_if_new(article)

        results = store.get_window_by_cadence("test-agent", Cadence.DAILY, self.REF)
        assert len(results) == 1

    def test_daily_cadence_excludes_today(self, store: AbstractArticleStore) -> None:
        today_article = _make_article(url_suffix="today", pub_date=self.REF)
        store.insert_if_new(today_article)

        results = store.get_window_by_cadence("test-agent", Cadence.DAILY, self.REF)
        assert len(results) == 0

    def test_weekly_cadence_covers_7_days(self, store: AbstractArticleStore) -> None:
        # Previous week (Sunday 2026-05-03 to Saturday 2026-05-09)
        # Use distinct, long headlines to avoid near-duplicate warnings
        topics = [
            "EU AI Act Enforcement Begins This Quarter",
            "OpenAI Releases GPT-5 Enterprise Edition Globally",
            "Google DeepMind Achieves AGI Safety Milestone",
            "Meta Open-Sources Next-Generation Language Model",
            "Anthropic Raises Record Funding Round From Investors",
            "Microsoft Azure AI Services Expanded To New Regions",
            "Tesla Robotaxi Fleet Deploys Autonomous AI System",
        ]
        articles = [
            _make_article(
                url_suffix=f"weekly{i}",
                headline=topics[i],
                pub_date=datetime.datetime(2026, 5, 3 + i, 12, tzinfo=UTC),
            )
            for i in range(7)
        ]
        for a in articles:
            store.insert_if_new(a)

        results = store.get_window_by_cadence("test-agent", Cadence.WEEKLY, self.REF)
        assert len(results) == 7

    def test_monthly_cadence_covers_previous_month(self, store: AbstractArticleStore) -> None:
        # Previous month = April 2026
        april_mid = datetime.datetime(2026, 4, 15, 12, tzinfo=UTC)
        article = _make_article(pub_date=april_mid)
        store.insert_if_new(article)

        results = store.get_window_by_cadence("test-agent", Cadence.MONTHLY, self.REF)
        assert len(results) == 1

    def test_monthly_cadence_excludes_current_month(self, store: AbstractArticleStore) -> None:
        may_article = _make_article(url_suffix="may", pub_date=datetime.datetime(2026, 5, 5, 12, tzinfo=UTC))
        store.insert_if_new(may_article)

        results = store.get_window_by_cadence("test-agent", Cadence.MONTHLY, self.REF)
        assert len(results) == 0

    def test_annual_cadence_covers_previous_year(self, store: AbstractArticleStore) -> None:
        year_2025 = datetime.datetime(2025, 6, 15, 12, tzinfo=UTC)
        article = _make_article(pub_date=year_2025)
        store.insert_if_new(article)

        results = store.get_window_by_cadence("test-agent", Cadence.ANNUAL, self.REF)
        assert len(results) == 1

    def test_annual_cadence_excludes_current_year(self, store: AbstractArticleStore) -> None:
        year_2026 = _make_article(url_suffix="2026", pub_date=datetime.datetime(2026, 1, 15, 12, tzinfo=UTC))
        store.insert_if_new(year_2026)

        results = store.get_window_by_cadence("test-agent", Cadence.ANNUAL, self.REF)
        assert len(results) == 0


# ===========================================================================
# Section 8: count_articles + get_stats (SRC-150)
# ===========================================================================

class TestCountAndStats:
    """Traces: SRC-150 (quality monitoring — items_by_tier, items_by_source_class)."""

    def test_count_articles_empty(self, store: AbstractArticleStore) -> None:
        assert store.count_articles("test-agent") == 0

    def test_count_articles_after_inserts(self, store: AbstractArticleStore) -> None:
        for i in range(5):
            store.insert_if_new(_make_article(url_suffix=str(i)))
        assert store.count_articles("test-agent") == 5

    def test_count_articles_scoped_to_agent(self, store: AbstractArticleStore) -> None:
        store.insert_if_new(_make_article("agent-a", "x"))
        store.insert_if_new(_make_article("agent-b", "y"))
        assert store.count_articles("agent-a") == 1
        assert store.count_articles("agent-b") == 1
        assert store.count_articles("agent-c") == 0

    def test_count_window_empty(self, store: AbstractArticleStore) -> None:
        """count_window returns 0 when no articles exist."""
        assert store.count_window(
            "test-agent",
            _NOW - timedelta(days=7),
            _NOW,
        ) == 0

    def test_count_window_only_counts_articles_in_range(
        self, store: AbstractArticleStore,
    ) -> None:
        """
        Only articles whose pub_date falls within the inclusive window are
        counted. Used by the Pipeline to decide whether to expand sourcing's
        window for a backfill run.
        """
        in_window = _make_article(
            url_suffix="in",
            pub_date=_NOW - timedelta(days=2),
        )
        out_of_window = _make_article(
            url_suffix="out",
            pub_date=_NOW - timedelta(days=30),
        )
        store.insert_if_new(in_window)
        store.insert_if_new(out_of_window)

        count = store.count_window(
            "test-agent",
            _NOW - timedelta(days=7),
            _NOW,
        )
        assert count == 1

    def test_count_window_scoped_to_agent(
        self, store: AbstractArticleStore,
    ) -> None:
        """count_window is scoped to a single agent_id (SRC-072)."""
        store.insert_if_new(_make_article(
            "agent-a", "x", pub_date=_NOW - timedelta(days=2),
        ))
        store.insert_if_new(_make_article(
            "agent-b", "y", pub_date=_NOW - timedelta(days=2),
        ))
        assert store.count_window(
            "agent-a", _NOW - timedelta(days=7), _NOW,
        ) == 1
        assert store.count_window(
            "agent-c", _NOW - timedelta(days=7), _NOW,
        ) == 0

    def test_get_stats_returns_store_stats(self, store: AbstractArticleStore) -> None:
        store.insert_if_new(_make_article(url_suffix="1", tier="1b", source_class="web"))
        store.insert_if_new(_make_article(url_suffix="2", tier="2", source_class="web"))
        store.insert_if_new(_make_article(url_suffix="3", tier="1b", source_class="twitter"))

        stats = store.get_stats(
            "test-agent",
            window_start=_NOW - timedelta(days=1),
            window_end=_NOW + timedelta(hours=1),
        )
        assert isinstance(stats, StoreStats)
        assert stats.total == 3
        assert stats.by_tier.get("1b") == 2
        assert stats.by_tier.get("2") == 1
        assert stats.by_source_class.get("web") == 2
        assert stats.by_source_class.get("twitter") == 1

    def test_get_stats_empty_window(self, store: AbstractArticleStore) -> None:
        store.insert_if_new(_make_article(pub_date=_NOW - timedelta(days=5)))

        stats = store.get_stats(
            "test-agent",
            window_start=_NOW - timedelta(hours=1),
            window_end=_NOW,
        )
        assert stats.total == 0
        assert stats.by_tier == {}
        assert stats.by_source_class == {}


# ===========================================================================
# Section 9: delete_older_than (store maintenance)
# ===========================================================================

class TestDeleteOlderThan:
    """Store pruning — bounded file size for long-running deployments."""

    def test_delete_removes_old_records(self, store: AbstractArticleStore) -> None:
        old = _make_article(url_suffix="old", pub_date=_NOW - timedelta(days=500))
        recent = _make_article(url_suffix="new", pub_date=_NOW - timedelta(days=10))
        store.insert_if_new(old)
        store.insert_if_new(recent)

        deleted = store.delete_older_than("test-agent", cutoff=_NOW - timedelta(days=365))
        assert deleted == 1
        assert store.count_articles("test-agent") == 1

    def test_delete_returns_zero_when_nothing_to_delete(self, store: AbstractArticleStore) -> None:
        store.insert_if_new(_make_article(pub_date=_NOW - timedelta(days=10)))
        deleted = store.delete_older_than("test-agent", cutoff=_NOW - timedelta(days=365))
        assert deleted == 0

    def test_delete_does_not_touch_other_agents(self, store: AbstractArticleStore) -> None:
        old_a = _make_article("agent-a", "old", pub_date=_NOW - timedelta(days=500))
        old_b = _make_article("agent-b", "oldb", pub_date=_NOW - timedelta(days=500))
        store.insert_if_new(old_a)
        store.insert_if_new(old_b)

        store.delete_older_than("agent-a", cutoff=_NOW - timedelta(days=365))
        assert store.count_articles("agent-a") == 0
        assert store.count_articles("agent-b") == 1


# ===========================================================================
# Section 10: TweetSignal deduplication (SRC-067)
# ===========================================================================

class TestTweetSignal:
    """Traces: SRC-067 (tweet dedup), SRC-047 (signal role).  Both backends."""

    def test_new_tweet_inserted(self, store: AbstractArticleStore) -> None:
        assert store.insert_tweet_signal(_make_tweet()) is True

    def test_duplicate_tweet_not_inserted(self, store: AbstractArticleStore) -> None:
        tweet = _make_tweet()
        store.insert_tweet_signal(tweet)
        assert store.insert_tweet_signal(tweet) is False

    def test_same_tweet_id_different_agent_inserted(self, store: AbstractArticleStore) -> None:
        """tweet_id is scoped to agent_id.  Traces: SRC-072."""
        a = _make_tweet("agent-a", "9001")
        b = _make_tweet("agent-b", "9001")
        store.insert_tweet_signal(a)
        assert store.insert_tweet_signal(b) is True

    def test_tweet_signals_window_query(self, store: AbstractArticleStore) -> None:
        """Traces: SRC-047, SRC-070 (signals for curation window)."""
        in_window = _make_tweet(created_at=_NOW - timedelta(hours=2))
        out_of_window = _make_tweet(tweet_id="2002", created_at=_NOW - timedelta(days=3))
        store.insert_tweet_signal(in_window)
        store.insert_tweet_signal(out_of_window)

        results = store.get_tweet_signals(
            "test-agent",
            window_start=_NOW - timedelta(days=1),
            window_end=_NOW,
        )
        assert len(results) == 1
        assert results[0].tweet_id == in_window.tweet_id

    def test_tweet_signals_sorted_by_created_at(self, store: AbstractArticleStore) -> None:
        t1 = _make_tweet(tweet_id="1", created_at=_NOW - timedelta(hours=5))
        t2 = _make_tweet(tweet_id="2", created_at=_NOW - timedelta(hours=3))
        t3 = _make_tweet(tweet_id="3", created_at=_NOW - timedelta(hours=1))
        # Insert out of order
        store.insert_tweet_signal(t3)
        store.insert_tweet_signal(t1)
        store.insert_tweet_signal(t2)

        results = store.get_tweet_signals(
            "test-agent",
            window_start=_NOW - timedelta(days=1),
            window_end=_NOW,
        )
        assert [r.tweet_id for r in results] == ["1", "2", "3"]

    def test_tweet_linked_urls_roundtrip(self, store: AbstractArticleStore) -> None:
        """linked_urls list survives serialisation roundtrip (SRC-069)."""
        tweet = _make_tweet()
        store.insert_tweet_signal(tweet)

        results = store.get_tweet_signals(
            "test-agent",
            window_start=_NOW - timedelta(hours=1),
            window_end=_NOW + timedelta(hours=1),
        )
        assert results[0].linked_urls == ["https://arxiv.org/abs/2026.test"]


# ===========================================================================
# Section 11: DigestRecord — upsert / get / list (SRC-129, SRC-145, SRC-150)
# ===========================================================================

class TestDigestRecord:
    """Traces: SRC-129 (prompt_version), SRC-145 (idempotent), SRC-150 (monitoring)."""

    def test_upsert_then_get(self, store: AbstractArticleStore) -> None:
        record = _make_digest()
        store.upsert_digest(record)

        retrieved = store.get_digest("test-agent", "daily")
        assert retrieved is not None
        assert retrieved.agent_id == "test-agent"
        assert retrieved.cadence == "daily"
        assert retrieved.prompt_version == "sha256:abc123"
        assert retrieved.llm_model == "gpt-4o"

    def test_upsert_is_idempotent(self, store: AbstractArticleStore) -> None:
        """Re-run overwrites cleanly.  Traces: SRC-145."""
        record = _make_digest()
        store.upsert_digest(record)

        updated = replace(record, items_included=10, token_usage=9999)
        store.upsert_digest(updated)

        retrieved = store.get_digest("test-agent", "daily")
        assert retrieved is not None
        assert retrieved.items_included == 10
        assert retrieved.token_usage == 9999

    def test_get_nonexistent_returns_none(self, store: AbstractArticleStore) -> None:
        assert store.get_digest("ghost-agent", "daily") is None

    def test_get_digest_by_run_date(self, store: AbstractArticleStore) -> None:
        d1 = _make_digest(run_date=datetime.date(2026, 5, 10))
        d2 = _make_digest(run_date=datetime.date(2026, 5, 11))
        store.upsert_digest(d1)
        store.upsert_digest(d2)

        ref_dt = datetime.datetime(2026, 5, 10, tzinfo=UTC)
        retrieved = store.get_digest("test-agent", "daily", run_date=ref_dt)
        assert retrieved is not None
        assert retrieved.run_date == datetime.date(2026, 5, 10)

    def test_get_digest_by_run_date_accepts_date(
        self, store: AbstractArticleStore
    ) -> None:
        """
        ``run_date`` matches :attr:`DigestRecord.run_date` (a ``date``).  This
        is the production call path — the Rendering Agent passes
        ``DigestMetadata.run_date`` (also a ``date``) when refreshing rendered
        paths.  Regression for the ``'datetime.date' object has no attribute
        'date'`` AttributeError observed in scheduled runs.
        """
        d1 = _make_digest(run_date=datetime.date(2026, 5, 10))
        d2 = _make_digest(run_date=datetime.date(2026, 5, 11))
        store.upsert_digest(d1)
        store.upsert_digest(d2)

        retrieved = store.get_digest(
            "test-agent", "daily", run_date=datetime.date(2026, 5, 10)
        )
        assert retrieved is not None
        assert retrieved.run_date == datetime.date(2026, 5, 10)

    def test_get_digest_returns_most_recent(self, store: AbstractArticleStore) -> None:
        """Without run_date, returns the most recent record."""
        d1 = _make_digest(run_date=datetime.date(2026, 5, 9))
        d2 = _make_digest(run_date=datetime.date(2026, 5, 10))
        store.upsert_digest(d1)
        store.upsert_digest(d2)

        retrieved = store.get_digest("test-agent", "daily")
        assert retrieved is not None
        assert retrieved.run_date == datetime.date(2026, 5, 10)

    def test_list_digests_ordered_most_recent_first(self, store: AbstractArticleStore) -> None:
        for day in [9, 11, 10]:  # intentionally out of order
            store.upsert_digest(_make_digest(run_date=datetime.date(2026, 5, day)))

        digests = store.list_digests("test-agent", "daily")
        assert len(digests) == 3
        # Most recent first
        assert digests[0].run_date >= digests[1].run_date >= digests[2].run_date

    def test_list_digests_cadence_filter(self, store: AbstractArticleStore) -> None:
        store.upsert_digest(_make_digest(cadence="daily",  run_date=datetime.date(2026, 5, 10)))
        store.upsert_digest(_make_digest(cadence="weekly", run_date=datetime.date(2026, 5, 3)))
        store.upsert_digest(_make_digest(cadence="monthly", run_date=datetime.date(2026, 5, 1)))

        daily   = store.list_digests("test-agent", cadence="daily")
        weekly  = store.list_digests("test-agent", cadence="weekly")
        monthly = store.list_digests("test-agent", cadence="monthly")
        all_   = store.list_digests("test-agent")

        assert len(daily) == 1
        assert daily[0].cadence == "daily"
        assert len(weekly) == 1
        assert weekly[0].cadence == "weekly"
        assert len(monthly) == 1
        assert len(all_) == 3

    def test_list_digests_limit(self, store: AbstractArticleStore) -> None:
        for day in range(1, 21):
            store.upsert_digest(_make_digest(run_date=datetime.date(2026, 5, day)))

        digests = store.list_digests("test-agent", "daily", limit=5)
        assert len(digests) == 5

    def test_digest_items_by_tier_roundtrip(self, store: AbstractArticleStore) -> None:
        """items_by_tier dict survives serialisation.  Traces: SRC-150."""
        record = _make_digest()
        store.upsert_digest(record)
        retrieved = store.get_digest("test-agent", "daily")
        assert retrieved is not None
        assert retrieved.items_by_tier == {"1b": 3, "2": 2}
        assert retrieved.items_by_source_class == {"web": 4, "twitter": 1}

    def test_digest_paths_stored(self, store: AbstractArticleStore) -> None:
        """Output file paths survive roundtrip."""
        record = _make_digest()
        record.md_path   = "outputs/test-agent/2026-05-11-daily.md"
        record.html_path = "outputs/test-agent/2026-05-11-daily.html"
        record.json_path = "outputs/test-agent/2026-05-11-daily.json"
        store.upsert_digest(record)

        retrieved = store.get_digest("test-agent", "daily")
        assert retrieved is not None
        assert retrieved.md_path   == "outputs/test-agent/2026-05-11-daily.md"
        assert retrieved.html_path == "outputs/test-agent/2026-05-11-daily.html"
        assert retrieved.json_path == "outputs/test-agent/2026-05-11-daily.json"

    def test_digest_key_unique(self) -> None:
        """DigestRecord.digest_key is deterministic."""
        record = _make_digest()
        assert record.digest_key == f"test-agent:daily:{record.run_date.isoformat()}"


# ===========================================================================
# Section 12: Context-manager protocol
# ===========================================================================

class TestContextManager:
    """Both stores support ``with`` statement."""

    def test_tinydb_context_manager(self, tmp_path: Path) -> None:
        db = tmp_path / "cm_test.json"
        with TinyDBArticleStore(db) as s:
            s.insert_if_new(_make_article())
            assert s.count_articles("test-agent") == 1
        # Connection closed — re-open verifies persistence
        with TinyDBArticleStore(db) as s2:
            assert s2.count_articles("test-agent") == 1

    def test_sqlite_context_manager(self, tmp_path: Path) -> None:
        db = tmp_path / "cm_test.db"
        with SQLiteArticleStore(db) as s:
            s.insert_if_new(_make_article())
            assert s.count_articles("test-agent") == 1
        with SQLiteArticleStore(db) as s2:
            assert s2.count_articles("test-agent") == 1


# ===========================================================================
# Section 13: Data persistence across close/reopen
# ===========================================================================

class TestPersistence:
    """Writes survive close() + reopen.  Proves data is durable on disk."""

    def test_tinydb_persists_across_close(self, tmp_path: Path) -> None:
        db = tmp_path / "persist.json"
        s = TinyDBArticleStore(db)
        s.insert_if_new(_make_article(url_suffix="persist"))
        s.close()

        s2 = TinyDBArticleStore(db)
        results = s2.get_window("test-agent", _NOW - timedelta(days=1), _NOW + timedelta(hours=1))
        s2.close()
        assert len(results) == 1

    def test_sqlite_persists_across_close(self, tmp_path: Path) -> None:
        db = tmp_path / "persist.db"
        s = SQLiteArticleStore(db)
        s.insert_if_new(_make_article(url_suffix="persist"))
        s.close()

        s2 = SQLiteArticleStore(db)
        results = s2.get_window("test-agent", _NOW - timedelta(days=1), _NOW + timedelta(hours=1))
        s2.close()
        assert len(results) == 1


# ===========================================================================
# Section 14: StoreFactory (SRC-053)
# ===========================================================================

class TestStoreFactory:
    """Traces: SRC-053 (pluggable store factory)."""

    def test_from_backend_tinydb(self, tmp_path: Path) -> None:
        store = StoreFactory.from_backend("tinydb", tmp_path / "s.json")
        assert isinstance(store, TinyDBArticleStore)
        store.close()

    def test_from_backend_sqlite(self, tmp_path: Path) -> None:
        store = StoreFactory.from_backend("sqlite", tmp_path / "s.db")
        assert isinstance(store, SQLiteArticleStore)
        store.close()

    def test_from_backend_case_insensitive(self, tmp_path: Path) -> None:
        store = StoreFactory.from_backend("SQLite", tmp_path / "s.db")
        assert isinstance(store, SQLiteArticleStore)
        store.close()

    def test_from_backend_unknown_raises(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="Unknown store backend"):
            StoreFactory.from_backend("dynamodb", tmp_path / "s.db")

    def test_create_from_agent_config_tinydb_default(
        self, tmp_path: Path, sample_agent_config  # from conftest
    ) -> None:
        """Default backend is TinyDB.  Traces: SRC-053, SRC-072."""
        store = StoreFactory.create(sample_agent_config, output_base=tmp_path)
        assert isinstance(store, TinyDBArticleStore)
        store.close()

    def test_create_creates_output_dir(self, tmp_path: Path, sample_agent_config) -> None:
        StoreFactory.create(sample_agent_config, output_base=tmp_path).close()
        expected = tmp_path / sample_agent_config.agent_id
        assert expected.is_dir()

    def test_register_custom_backend(self, tmp_path: Path) -> None:
        """Custom backend can be registered and retrieved."""
        from ai_news_agent.storage.factory import StoreFactory

        # Use SQLite as a stand-in for a "custom" backend
        StoreFactory.register("custom_test_store", SQLiteArticleStore)
        assert "custom_test_store" in StoreFactory.available_backends()

        store = StoreFactory.from_backend("custom_test_store", tmp_path / "custom.db")
        assert isinstance(store, SQLiteArticleStore)
        store.close()

    def test_register_non_store_raises(self) -> None:
        with pytest.raises(TypeError, match="must be a subclass"):
            StoreFactory.register("bad", object)  # type: ignore[arg-type]

    def test_available_backends_sorted(self) -> None:
        backends = StoreFactory.available_backends()
        assert backends == sorted(backends)
        assert "tinydb" in backends
        assert "sqlite" in backends


# ===========================================================================
# Section 15: ArticleRecord field round-trip (SRC-011)
# ===========================================================================

class TestArticleFieldRoundtrip:
    """
    Every field of ArticleRecord must survive a write→read cycle in both backends.
    Traces: SRC-011 (all storage fields), SRC-048 (twitter provenance fields)
    """

    def _full_article(self) -> ArticleRecord:
        raw_url   = "https://reuters.com/full-roundtrip-test"
        canonical = normalize_url(raw_url)
        return ArticleRecord(
            url_hash=url_hash(canonical),
            url=canonical,
            headline="Full Field Roundtrip Test",
            abstract="Abstract text here.",
            source_name="Reuters",
            pub_date=datetime.datetime(2026, 5, 10, 12, 30, 0, tzinfo=UTC),
            fetched_at=datetime.datetime(2026, 5, 10, 13, 0, 0, tzinfo=UTC),
            tier="1b",
            source_class="twitter",
            agent_id="test-agent",
            twitter_handle="karpathy",
            tweet_url="https://x.com/karpathy/status/9999",
        )

    def test_tinydb_roundtrip(self, tiny_store: TinyDBArticleStore) -> None:
        a = self._full_article()
        tiny_store.insert_if_new(a)
        results = tiny_store.get_window(
            "test-agent",
            window_start=datetime.datetime(2026, 5, 10, 0, tzinfo=UTC),
            window_end=datetime.datetime(2026, 5, 10, 23, 59, tzinfo=UTC),
        )
        assert len(results) == 1
        r = results[0]
        assert r.url_hash == a.url_hash
        assert r.headline == "Full Field Roundtrip Test"
        assert r.abstract == "Abstract text here."
        assert r.source_name == "Reuters"
        assert r.tier == "1b"
        assert r.source_class == "twitter"
        assert r.twitter_handle == "karpathy"
        assert r.tweet_url == "https://x.com/karpathy/status/9999"

    def test_sqlite_roundtrip(self, sqlite_store: SQLiteArticleStore) -> None:
        a = self._full_article()
        sqlite_store.insert_if_new(a)
        results = sqlite_store.get_window(
            "test-agent",
            window_start=datetime.datetime(2026, 5, 10, 0, tzinfo=UTC),
            window_end=datetime.datetime(2026, 5, 10, 23, 59, tzinfo=UTC),
        )
        assert len(results) == 1
        r = results[0]
        assert r.twitter_handle == "karpathy"
        assert r.tweet_url == "https://x.com/karpathy/status/9999"
        assert r.pub_date.tzinfo is not None  # timezone-aware (UTC)


# ===========================================================================
# Section 16: AbstractArticleStore contract (interface compliance)
# ===========================================================================

# ===========================================================================
# Section 17: Defensive branch coverage — naive datetime coercion
# ===========================================================================

class TestNaiveDatetimeCoercion:
    """
    Stores accept timezone-naive datetimes and coerce to UTC internally.
    This exercises the defensive branches in _parse_dt / _iso.
    """

    def _make_naive_article(self) -> ArticleRecord:
        raw_url   = "https://reuters.com/naive-dt-test"
        canonical = normalize_url(raw_url)
        # pub_date and fetched_at without tzinfo
        naive_dt = datetime.datetime(2026, 5, 10, 12, 0, 0)  # no tzinfo
        return ArticleRecord(
            url_hash=url_hash(canonical),
            url=canonical,
            headline="Naive Datetime Article",
            abstract=None,
            source_name="Reuters",
            pub_date=naive_dt,
            fetched_at=naive_dt,
            tier="2",
            source_class="web",
            agent_id="test-agent",
        )

    def test_tinydb_accepts_naive_pub_date(self, tiny_store: TinyDBArticleStore) -> None:
        article = self._make_naive_article()
        inserted = tiny_store.insert_if_new(article)
        assert inserted is True
        # Retrieve it — pub_date should be coerced to UTC-aware
        results = tiny_store.get_window(
            "test-agent",
            datetime.datetime(2026, 5, 10, 0, 0, tzinfo=UTC),
            datetime.datetime(2026, 5, 10, 23, 59, tzinfo=UTC),
        )
        assert len(results) == 1
        assert results[0].pub_date.tzinfo is not None

    def test_sqlite_accepts_naive_pub_date(self, sqlite_store: SQLiteArticleStore) -> None:
        article = self._make_naive_article()
        inserted = sqlite_store.insert_if_new(article)
        assert inserted is True
        results = sqlite_store.get_window(
            "test-agent",
            datetime.datetime(2026, 5, 10, 0, 0, tzinfo=UTC),
            datetime.datetime(2026, 5, 10, 23, 59, tzinfo=UTC),
        )
        assert len(results) == 1

    def test_tinydb_naive_tweet_created_at(self, tiny_store: TinyDBArticleStore) -> None:
        """TweetSignal with naive created_at is coerced to UTC."""
        tweet = TweetSignal(
            tweet_id="naive-1",
            handle="karpathy",
            text="Naive datetime tweet.",
            created_at=datetime.datetime(2026, 5, 10, 10, 0, 0),  # naive
            linked_urls=[],
            agent_id="test-agent",
            fetched_at=datetime.datetime(2026, 5, 10, 10, 5, 0),  # naive
            weight=1.0,
        )
        tiny_store.insert_tweet_signal(tweet)
        results = tiny_store.get_tweet_signals(
            "test-agent",
            datetime.datetime(2026, 5, 10, 0, 0, tzinfo=UTC),
            datetime.datetime(2026, 5, 10, 23, 59, tzinfo=UTC),
        )
        assert len(results) == 1


# ===========================================================================
# Section 18: CuratedItemRaw default field init (SRC-048)
# ===========================================================================

class TestCuratedItemRawDefaults:
    """CuratedItemRaw provides empty-list defaults for impact_tags and cross_refs."""

    def test_impact_tags_defaults_to_empty_list(self) -> None:
        from ai_news_agent.storage.models import CuratedItemRaw
        item = CuratedItemRaw(headline="Test", source_name="Reuters")
        assert item.impact_tags == []

    def test_cross_refs_defaults_to_empty_list(self) -> None:
        from ai_news_agent.storage.models import CuratedItemRaw
        item = CuratedItemRaw(headline="Test", source_name="Reuters")
        assert item.cross_refs == []

    def test_explicit_values_preserved(self) -> None:
        from ai_news_agent.storage.models import CuratedItemRaw
        item = CuratedItemRaw(
            headline="Test",
            source_name="Reuters",
            impact_tags=["business_impact"],
            cross_refs=["https://example.com"],
        )
        assert item.impact_tags == ["business_impact"]
        assert item.cross_refs == ["https://example.com"]


class TestAbstractStoreContract:
    """
    Every concrete store must implement ALL abstract methods.
    Instantiating AbstractArticleStore directly must fail.
    """

    def test_cannot_instantiate_abstract_directly(self) -> None:
        with pytest.raises(TypeError):
            AbstractArticleStore()  # type: ignore[abstract]

    def test_tinydb_satisfies_interface(self, tiny_store: TinyDBArticleStore) -> None:
        """TinyDB implements every abstract method."""
        assert isinstance(tiny_store, AbstractArticleStore)
        assert callable(tiny_store.insert_if_new)
        assert callable(tiny_store.get_window)
        assert callable(tiny_store.get_window_by_cadence)
        assert callable(tiny_store.count_articles)
        assert callable(tiny_store.get_stats)
        assert callable(tiny_store.delete_older_than)
        assert callable(tiny_store.insert_tweet_signal)
        assert callable(tiny_store.get_tweet_signals)
        assert callable(tiny_store.upsert_digest)
        assert callable(tiny_store.get_digest)
        assert callable(tiny_store.list_digests)
        assert callable(tiny_store.close)

    def test_sqlite_satisfies_interface(self, sqlite_store: SQLiteArticleStore) -> None:
        """SQLite implements every abstract method."""
        assert isinstance(sqlite_store, AbstractArticleStore)
        assert callable(sqlite_store.insert_if_new)
        assert callable(sqlite_store.get_window)
        assert callable(sqlite_store.get_window_by_cadence)
        assert callable(sqlite_store.count_articles)
        assert callable(sqlite_store.get_stats)
        assert callable(sqlite_store.delete_older_than)
        assert callable(sqlite_store.insert_tweet_signal)
        assert callable(sqlite_store.get_tweet_signals)
        assert callable(sqlite_store.upsert_digest)
        assert callable(sqlite_store.get_digest)
        assert callable(sqlite_store.list_digests)
        assert callable(sqlite_store.close)
