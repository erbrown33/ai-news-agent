"""
storage/sqlite_store.py — SQLite concrete implementation of AbstractArticleStore.

SQLite is the **production-recommended** document store for deployments where:
  - Performance matters (indexed queries vs TinyDB's full-file scan)
  - You want transactional safety for concurrent agent runs
  - The output directory is a persistent volume or cloud-synced storage

Why SQLite over TinyDB in production:
  - Indexed ``(url_hash, agent_id)`` dedup check: O(log n) vs O(n) full scan
  - Indexed ``(agent_id, pub_date)`` window queries: same efficiency gain
  - SQLite WAL mode supports concurrent reads while a write is in progress
  - Storage is more compact for large article volumes (binary vs pretty JSON)
  - SQLite is in Python's stdlib — zero additional dependency

To activate: set ``store_backend: sqlite`` in the agent YAML config.
The store file will be placed at ``outputs/{agent_id}/store.db``.

Traces: SRC-008–SRC-013 (lookback windows, dedup, sourcing-only rule),
        SRC-028–SRC-032 (cadence window queries),
        SRC-053 (pluggable document store — swap path),
        SRC-072 (agent_id scoping — one file per agent),
        SRC-129 (digest prompt_version),
        SRC-145 (idempotent digest upsert — INSERT OR REPLACE),
        SRC-150 (get_stats for monitoring)
"""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import UTC, date, datetime
from pathlib import Path

from ai_news_agent.storage.base import AbstractArticleStore, StoreStats
from ai_news_agent.storage.models import (
    ArticleRecord,
    Cadence,
    DigestRecord,
    TweetSignal,
    headline_similarity,
    lookback_window,
)

logger = logging.getLogger(__name__)

_HEADLINE_SIM_THRESHOLD = 0.85

# ---------------------------------------------------------------------------
# DDL — create tables + indexes if they do not exist
# ---------------------------------------------------------------------------

_DDL = """
-- Articles table (SRC-011, SRC-012)
CREATE TABLE IF NOT EXISTS articles (
    url_hash       TEXT    NOT NULL,
    agent_id       TEXT    NOT NULL,
    url            TEXT    NOT NULL,
    headline       TEXT    NOT NULL,
    abstract       TEXT,
    source_name    TEXT    NOT NULL,
    pub_date       TEXT    NOT NULL,  -- ISO-8601 UTC
    fetched_at     TEXT    NOT NULL,  -- ISO-8601 UTC
    tier           TEXT    NOT NULL,
    source_class   TEXT    NOT NULL,
    twitter_handle TEXT,
    tweet_url      TEXT,
    PRIMARY KEY (url_hash, agent_id)   -- dedup key (SRC-012)
);

-- Index for window queries on pub_date per agent (SRC-008–SRC-010)
CREATE INDEX IF NOT EXISTS idx_articles_agent_pubdate
    ON articles (agent_id, pub_date);

-- Tweets table (SRC-047, SRC-067)
CREATE TABLE IF NOT EXISTS tweets (
    tweet_id   TEXT    NOT NULL,
    agent_id   TEXT    NOT NULL,
    handle     TEXT    NOT NULL,
    text       TEXT    NOT NULL,
    created_at TEXT    NOT NULL,  -- ISO-8601 UTC
    fetched_at TEXT    NOT NULL,  -- ISO-8601 UTC
    linked_urls TEXT   NOT NULL,  -- JSON array
    weight     REAL    NOT NULL DEFAULT 1.0,
    PRIMARY KEY (tweet_id, agent_id)
);

CREATE INDEX IF NOT EXISTS idx_tweets_agent_created
    ON tweets (agent_id, created_at);

-- Digests table (SRC-129, SRC-145, SRC-150)
CREATE TABLE IF NOT EXISTS digests (
    agent_id                 TEXT    NOT NULL,
    cadence                  TEXT    NOT NULL,
    run_date                 TEXT    NOT NULL,  -- YYYY-MM-DD
    window_start             TEXT    NOT NULL,
    window_end               TEXT    NOT NULL,
    prompt_version           TEXT    NOT NULL,
    llm_provider             TEXT    NOT NULL,
    llm_model                TEXT    NOT NULL,
    items_considered         INTEGER NOT NULL DEFAULT 0,
    items_included           INTEGER NOT NULL DEFAULT 0,
    items_by_tier            TEXT    NOT NULL DEFAULT '{}',  -- JSON
    items_by_source_class    TEXT    NOT NULL DEFAULT '{}',  -- JSON
    twitter_signal_available INTEGER NOT NULL DEFAULT 1,     -- bool
    tweet_api_call_count     INTEGER NOT NULL DEFAULT 0,
    token_usage              INTEGER NOT NULL DEFAULT 0,
    md_path                  TEXT,
    html_path                TEXT,
    json_path                TEXT,
    PRIMARY KEY (agent_id, cadence, run_date)  -- idempotent (SRC-145)
);

CREATE INDEX IF NOT EXISTS idx_digests_agent_cadence
    ON digests (agent_id, cadence, run_date DESC);
"""


def _iso(dt: datetime) -> str:
    """Ensure the datetime is UTC-aware and return ISO-8601 string."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.isoformat()


def _parse_dt(value: str | datetime) -> datetime:
    """Coerce stored ISO-8601 string → UTC-aware datetime."""
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed


def _parse_date(value: str | date) -> date:
    """Coerce stored YYYY-MM-DD string → date."""
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, datetime):
        return value.date()
    return date.fromisoformat(value)


class SQLiteArticleStore(AbstractArticleStore):
    """
    SQLite-backed article store — production-grade alternative to TinyDB.

    **Schema:**
    - ``articles``  table — :class:`ArticleRecord`; PRIMARY KEY ``(url_hash, agent_id)``
    - ``tweets``    table — :class:`TweetSignal`; PRIMARY KEY ``(tweet_id, agent_id)``
    - ``digests``   table — :class:`DigestRecord`; PRIMARY KEY ``(agent_id, cadence, run_date)``

    **WAL mode** is enabled at connection time for concurrent-read safety.
    All writes use transactions with implicit ``BEGIN``/``COMMIT``.

    Traces: SRC-012 (dedup via PRIMARY KEY constraint),
            SRC-053 (pluggable store),
            SRC-072 (one file per agent — caller provides db_path),
            SRC-145 (INSERT OR REPLACE for idempotent digest upsert),
            SRC-150 (get_stats via SQL aggregation)
    """

    def __init__(self, db_path: str | Path) -> None:
        resolved = Path(db_path)
        resolved.parent.mkdir(parents=True, exist_ok=True)
        self._db_path = resolved
        self._conn = sqlite3.connect(
            str(resolved),
            detect_types=sqlite3.PARSE_DECLTYPES | sqlite3.PARSE_COLNAMES,
            check_same_thread=False,
        )
        self._conn.row_factory = sqlite3.Row
        # WAL mode for concurrent-read safety in multi-agent containers
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.executescript(_DDL)
        self._conn.commit()

    # ------------------------------------------------------------------
    # Resource lifecycle
    # ------------------------------------------------------------------

    def close(self) -> None:
        """Commit any pending transaction and close the SQLite connection."""
        try:
            self._conn.commit()
        finally:
            self._conn.close()

    # ------------------------------------------------------------------
    # ArticleRecord operations (SRC-011–SRC-012)
    # ------------------------------------------------------------------

    def insert_if_new(self, article: ArticleRecord) -> bool:
        """
        Insert ``article`` using INSERT OR IGNORE — the PRIMARY KEY constraint
        on ``(url_hash, agent_id)`` silently rejects duplicates.

        Returns ``True`` if a row was actually inserted, ``False`` otherwise.

        Secondary near-duplicate check: if the URL hash is novel but a stored
        headline in the same agent has similarity ≥ 0.85, logs a WARNING
        (SRC-012 architecture §3.3).

        Traces: SRC-010 (multiple runs — add new only),
                SRC-012 (primary dedup by url_hash via PRIMARY KEY)
        """
        cur = self._conn.execute(
            """
            INSERT OR IGNORE INTO articles
              (url_hash, agent_id, url, headline, abstract, source_name,
               pub_date, fetched_at, tier, source_class, twitter_handle, tweet_url)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                article.url_hash,
                article.agent_id,
                article.url,
                article.headline,
                article.abstract,
                article.source_name,
                _iso(article.pub_date),
                _iso(article.fetched_at),
                article.tier,
                article.source_class,
                article.twitter_handle,
                article.tweet_url,
            ),
        )
        self._conn.commit()

        inserted = cur.rowcount > 0
        if inserted:
            self._check_near_duplicate(article)
        return inserted

    def _check_near_duplicate(self, candidate: ArticleRecord) -> None:
        """
        Scan recent stored headlines for near-duplicates.
        SQLite version: fetch the last 500 headlines for the agent
        (bounded scan) and apply Levenshtein similarity.
        Traces: SRC-012 (secondary dedup signal)
        """
        cur = self._conn.execute(
            """
            SELECT url, headline FROM articles
            WHERE agent_id = ?
              AND url_hash != ?
            ORDER BY pub_date DESC
            LIMIT 500
            """,
            (candidate.agent_id, candidate.url_hash),
        )
        for row in cur.fetchall():
            sim = headline_similarity(candidate.headline, row["headline"])
            if sim >= _HEADLINE_SIM_THRESHOLD:
                logger.warning(
                    "near_duplicate_detected",
                    extra={
                        "agent_id": candidate.agent_id,
                        "new_url": candidate.url,
                        "existing_url": row["url"],
                        "similarity": round(sim, 3),
                        "new_headline": candidate.headline,
                        "existing_headline": row["headline"],
                    },
                )
                break

    def get_window(
        self,
        agent_id: str,
        window_start: datetime,
        window_end: datetime,
    ) -> list[ArticleRecord]:
        """
        Return all articles for ``agent_id`` within ``[window_start, window_end]``,
        ordered by ``pub_date`` ascending.  Uses the ``idx_articles_agent_pubdate``
        index for O(log n) performance.
        Traces: SRC-008–SRC-010 (lookback window queries)
        """
        cur = self._conn.execute(
            """
            SELECT * FROM articles
            WHERE agent_id = ?
              AND pub_date >= ?
              AND pub_date <= ?
            ORDER BY pub_date ASC
            """,
            (agent_id, _iso(window_start), _iso(window_end)),
        )
        return [self._row_to_article(row) for row in cur.fetchall()]

    def get_window_by_cadence(
        self,
        agent_id: str,
        cadence: Cadence,
        reference: datetime | None = None,
    ) -> list[ArticleRecord]:
        """
        Convenience wrapper — computes window from cadence + reference.
        Traces: SRC-009 (daily), SRC-028–SRC-032 (all cadence windows)
        """
        start, end = lookback_window(cadence, reference)
        return self.get_window(agent_id, start, end)

    def count_articles(self, agent_id: str) -> int:
        """Total article count for ``agent_id`` across all time."""
        cur = self._conn.execute(
            "SELECT COUNT(*) FROM articles WHERE agent_id = ?",
            (agent_id,),
        )
        return int(cur.fetchone()[0])

    def count_window(
        self,
        agent_id: str,
        window_start: datetime,
        window_end: datetime,
    ) -> int:
        """
        Return the count of articles for ``agent_id`` with ``pub_date`` in
        ``[window_start, window_end]``. Uses the
        ``idx_articles_agent_pubdate`` index for O(log n) range scans.
        """
        cur = self._conn.execute(
            """
            SELECT COUNT(*) FROM articles
            WHERE agent_id = ?
              AND pub_date >= ?
              AND pub_date <= ?
            """,
            (agent_id, _iso(window_start), _iso(window_end)),
        )
        return int(cur.fetchone()[0])

    def get_stats(
        self,
        agent_id: str,
        window_start: datetime,
        window_end: datetime,
    ) -> StoreStats:
        """
        Return aggregated :class:`StoreStats` for articles in the window.
        Uses SQL GROUP BY for efficiency instead of Python-side aggregation.
        Traces: SRC-150 (items_by_tier, items_by_source_class)
        """
        # Total in window
        cur = self._conn.execute(
            """
            SELECT COUNT(*) FROM articles
            WHERE agent_id = ?
              AND pub_date >= ?
              AND pub_date <= ?
            """,
            (agent_id, _iso(window_start), _iso(window_end)),
        )
        total = int(cur.fetchone()[0])

        # By tier
        cur = self._conn.execute(
            """
            SELECT tier, COUNT(*) as cnt FROM articles
            WHERE agent_id = ?
              AND pub_date >= ?
              AND pub_date <= ?
            GROUP BY tier
            """,
            (agent_id, _iso(window_start), _iso(window_end)),
        )
        by_tier = {row["tier"]: row["cnt"] for row in cur.fetchall()}

        # By source class
        cur = self._conn.execute(
            """
            SELECT source_class, COUNT(*) as cnt FROM articles
            WHERE agent_id = ?
              AND pub_date >= ?
              AND pub_date <= ?
            GROUP BY source_class
            """,
            (agent_id, _iso(window_start), _iso(window_end)),
        )
        by_class = {row["source_class"]: row["cnt"] for row in cur.fetchall()}

        return StoreStats(total=total, by_tier=by_tier, by_source_class=by_class)

    def delete_older_than(self, agent_id: str, cutoff: datetime) -> int:
        """
        Delete all articles for ``agent_id`` with ``pub_date`` < ``cutoff``.
        Returns number of rows deleted.
        """
        cur = self._conn.execute(
            "DELETE FROM articles WHERE agent_id = ? AND pub_date < ?",
            (agent_id, _iso(cutoff)),
        )
        self._conn.commit()
        return cur.rowcount

    @staticmethod
    def _row_to_article(row: sqlite3.Row) -> ArticleRecord:
        """Convert a sqlite3.Row to an ArticleRecord."""
        return ArticleRecord(
            url_hash=row["url_hash"],
            url=row["url"],
            headline=row["headline"],
            abstract=row["abstract"],
            source_name=row["source_name"],
            pub_date=_parse_dt(row["pub_date"]),
            fetched_at=_parse_dt(row["fetched_at"]),
            tier=row["tier"],
            source_class=row["source_class"],
            agent_id=row["agent_id"],
            twitter_handle=row["twitter_handle"],
            tweet_url=row["tweet_url"],
        )

    # ------------------------------------------------------------------
    # TweetSignal operations (SRC-047, SRC-067–SRC-069)
    # ------------------------------------------------------------------

    def insert_tweet_signal(self, signal: TweetSignal) -> bool:
        """
        Insert ``signal`` using INSERT OR IGNORE — PRIMARY KEY ``(tweet_id, agent_id)``
        rejects duplicates.  Returns ``True`` if inserted.
        Traces: SRC-067
        """
        cur = self._conn.execute(
            """
            INSERT OR IGNORE INTO tweets
              (tweet_id, agent_id, handle, text, created_at, fetched_at, linked_urls, weight)
            VALUES (?,?,?,?,?,?,?,?)
            """,
            (
                signal.tweet_id,
                signal.agent_id,
                signal.handle,
                signal.text,
                _iso(signal.created_at),
                _iso(signal.fetched_at),
                json.dumps(signal.linked_urls),
                signal.weight,
            ),
        )
        self._conn.commit()
        return cur.rowcount > 0

    def get_tweet_signals(
        self,
        agent_id: str,
        window_start: datetime,
        window_end: datetime,
    ) -> list[TweetSignal]:
        """
        Return all tweet signals for ``agent_id`` within the window,
        sorted by ``created_at`` ascending.
        Traces: SRC-047, SRC-070
        """
        cur = self._conn.execute(
            """
            SELECT * FROM tweets
            WHERE agent_id = ?
              AND created_at >= ?
              AND created_at <= ?
            ORDER BY created_at ASC
            """,
            (agent_id, _iso(window_start), _iso(window_end)),
        )
        return [self._row_to_tweet(row) for row in cur.fetchall()]

    @staticmethod
    def _row_to_tweet(row: sqlite3.Row) -> TweetSignal:
        """Convert a sqlite3.Row to a TweetSignal."""
        return TweetSignal(
            tweet_id=row["tweet_id"],
            handle=row["handle"],
            text=row["text"],
            created_at=_parse_dt(row["created_at"]),
            linked_urls=json.loads(row["linked_urls"]),
            agent_id=row["agent_id"],
            fetched_at=_parse_dt(row["fetched_at"]),
            weight=row["weight"],
        )

    # ------------------------------------------------------------------
    # DigestRecord operations (SRC-129, SRC-145, SRC-150)
    # ------------------------------------------------------------------

    def upsert_digest(self, record: DigestRecord) -> None:
        """
        INSERT OR REPLACE — the PRIMARY KEY ``(agent_id, cadence, run_date)``
        guarantees idempotent re-run behaviour (SRC-145).
        Traces: SRC-129 (prompt_version), SRC-145, SRC-150
        """
        self._conn.execute(
            """
            INSERT OR REPLACE INTO digests
              (agent_id, cadence, run_date, window_start, window_end,
               prompt_version, llm_provider, llm_model,
               items_considered, items_included,
               items_by_tier, items_by_source_class,
               twitter_signal_available, tweet_api_call_count, token_usage,
               md_path, html_path, json_path)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                record.agent_id,
                record.cadence,
                record.run_date.isoformat(),
                _iso(record.window_start),
                _iso(record.window_end),
                record.prompt_version,
                record.llm_provider,
                record.llm_model,
                record.items_considered,
                record.items_included,
                json.dumps(record.items_by_tier),
                json.dumps(record.items_by_source_class),
                int(record.twitter_signal_available),
                record.tweet_api_call_count,
                record.token_usage,
                record.md_path,
                record.html_path,
                record.json_path,
            ),
        )
        self._conn.commit()

    def get_digest(
        self,
        agent_id: str,
        cadence: str,
        run_date: date | None = None,
    ) -> DigestRecord | None:
        """
        Retrieve a digest record.  If ``run_date`` is ``None``, returns the
        most recent one for ``(agent_id, cadence)``.

        ``run_date`` matches :attr:`DigestRecord.run_date` (a :class:`date`).
        A :class:`datetime` is also accepted and coerced via ``.date()``.

        Traces: SRC-145 (portal listing)
        """
        if run_date is not None:
            run_date_key = run_date.date() if isinstance(run_date, datetime) else run_date
            cur = self._conn.execute(
                """
                SELECT * FROM digests
                WHERE agent_id = ? AND cadence = ? AND run_date = ?
                """,
                (agent_id, cadence, run_date_key.isoformat()),
            )
        else:
            cur = self._conn.execute(
                """
                SELECT * FROM digests
                WHERE agent_id = ? AND cadence = ?
                ORDER BY run_date DESC
                LIMIT 1
                """,
                (agent_id, cadence),
            )
        row = cur.fetchone()
        if row is None:
            return None
        return self._row_to_digest(row)

    def list_digests(
        self,
        agent_id: str,
        cadence: str | None = None,
        limit: int = 50,
    ) -> list[DigestRecord]:
        """
        Return up to ``limit`` digest records, most-recent first.
        Traces: SRC-133–SRC-134 (portal index listing)
        """
        if cadence is not None:
            cur = self._conn.execute(
                """
                SELECT * FROM digests
                WHERE agent_id = ? AND cadence = ?
                ORDER BY run_date DESC
                LIMIT ?
                """,
                (agent_id, cadence, limit),
            )
        else:
            cur = self._conn.execute(
                """
                SELECT * FROM digests
                WHERE agent_id = ?
                ORDER BY run_date DESC
                LIMIT ?
                """,
                (agent_id, limit),
            )
        return [self._row_to_digest(row) for row in cur.fetchall()]

    @staticmethod
    def _row_to_digest(row: sqlite3.Row) -> DigestRecord:
        """Convert a sqlite3.Row to a DigestRecord."""
        return DigestRecord(
            agent_id=row["agent_id"],
            cadence=row["cadence"],
            run_date=_parse_date(row["run_date"]),
            window_start=_parse_dt(row["window_start"]),
            window_end=_parse_dt(row["window_end"]),
            prompt_version=row["prompt_version"],
            llm_provider=row["llm_provider"],
            llm_model=row["llm_model"],
            items_considered=row["items_considered"],
            items_included=row["items_included"],
            items_by_tier=json.loads(row["items_by_tier"]),
            items_by_source_class=json.loads(row["items_by_source_class"]),
            twitter_signal_available=bool(row["twitter_signal_available"]),
            tweet_api_call_count=row["tweet_api_call_count"],
            token_usage=row["token_usage"],
            md_path=row["md_path"],
            html_path=row["html_path"],
            json_path=row["json_path"],
        )
