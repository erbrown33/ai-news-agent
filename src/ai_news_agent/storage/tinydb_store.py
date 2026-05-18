"""
storage/tinydb_store.py — TinyDB concrete implementation of AbstractArticleStore.

TinyDB is the **default** document store.  It requires zero external
infrastructure — the entire store lives in a single JSON file at
``outputs/{agent_id}/store.json``.  This is optimal for:
  - Phase 1 local development (SRC-076)
  - Serverless containers where the output directory is mounted or synced
    from cloud storage after each run (SRC-085)
  - Article volumes up to ~50 000 records per agent (well above expected load)

Swap path: subclass ``AbstractArticleStore`` and register in ``StoreFactory``.

Traces: SRC-008–SRC-012 (lookback windows, dedup),
        SRC-028–SRC-032 (cadence window queries),
        SRC-053 (TinyDB document store default),
        SRC-072 (one store file per agent_id),
        SRC-129 (digest prompt_version),
        SRC-145 (idempotent digest upsert),
        SRC-150 (get_stats for monitoring)
"""

from __future__ import annotations

import logging
from dataclasses import asdict
from datetime import UTC, date, datetime
from pathlib import Path

from tinydb import Query, TinyDB

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

# Headline similarity threshold for near-duplicate logging (SRC-012 §3.3)
_HEADLINE_SIM_THRESHOLD = 0.85


def _parse_dt(value: str | datetime) -> datetime:
    """
    Coerce a stored ISO-8601 string back to a timezone-aware :class:`datetime`.
    TinyDB stores datetimes as ISO strings; this restores them with UTC tz.
    """
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed


def _parse_date(value: str | date) -> date:
    """Coerce a stored YYYY-MM-DD string back to a :class:`date`."""
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, datetime):
        return value.date()
    return date.fromisoformat(value)


def _article_to_doc(article: ArticleRecord) -> dict:
    """Serialise an ``ArticleRecord`` to a JSON-safe dict for TinyDB."""
    doc = asdict(article)
    doc["pub_date"]   = article.pub_date.isoformat()
    doc["fetched_at"] = article.fetched_at.isoformat()
    return doc


def _doc_to_article(doc: dict) -> ArticleRecord:
    """Deserialise a TinyDB document back to an ``ArticleRecord``."""
    return ArticleRecord(
        url_hash=doc["url_hash"],
        url=doc["url"],
        headline=doc["headline"],
        abstract=doc.get("abstract"),
        source_name=doc["source_name"],
        pub_date=_parse_dt(doc["pub_date"]),
        fetched_at=_parse_dt(doc["fetched_at"]),
        tier=doc["tier"],
        source_class=doc["source_class"],
        agent_id=doc["agent_id"],
        twitter_handle=doc.get("twitter_handle"),
        tweet_url=doc.get("tweet_url"),
    )


def _tweet_to_doc(signal: TweetSignal) -> dict:
    """Serialise a ``TweetSignal`` to a JSON-safe dict for TinyDB."""
    doc = asdict(signal)
    doc["created_at"] = signal.created_at.isoformat()
    doc["fetched_at"] = signal.fetched_at.isoformat()
    return doc


def _doc_to_tweet(doc: dict) -> TweetSignal:
    """Deserialise a TinyDB document back to a ``TweetSignal``."""
    return TweetSignal(
        tweet_id=doc["tweet_id"],
        handle=doc["handle"],
        text=doc["text"],
        created_at=_parse_dt(doc["created_at"]),
        linked_urls=doc.get("linked_urls", []),
        agent_id=doc["agent_id"],
        fetched_at=_parse_dt(doc["fetched_at"]),
        weight=doc.get("weight", 1.0),
    )


def _digest_to_doc(record: DigestRecord) -> dict:
    """Serialise a ``DigestRecord`` to a JSON-safe dict for TinyDB."""
    return {
        "digest_key":               record.digest_key,
        "agent_id":                 record.agent_id,
        "cadence":                  record.cadence,
        "run_date":                 record.run_date.isoformat(),
        "window_start":             record.window_start.isoformat(),
        "window_end":               record.window_end.isoformat(),
        "prompt_version":           record.prompt_version,
        "llm_provider":             record.llm_provider,
        "llm_model":                record.llm_model,
        "items_considered":         record.items_considered,
        "items_included":           record.items_included,
        "items_by_tier":            record.items_by_tier,
        "items_by_source_class":    record.items_by_source_class,
        "twitter_signal_available": record.twitter_signal_available,
        "tweet_api_call_count":     record.tweet_api_call_count,
        "token_usage":              record.token_usage,
        "md_path":                  record.md_path,
        "html_path":                record.html_path,
        "json_path":                record.json_path,
    }


def _doc_to_digest(doc: dict) -> DigestRecord:
    """Deserialise a TinyDB document back to a ``DigestRecord``."""
    return DigestRecord(
        agent_id=doc["agent_id"],
        cadence=doc["cadence"],
        run_date=_parse_date(doc["run_date"]),
        window_start=_parse_dt(doc["window_start"]),
        window_end=_parse_dt(doc["window_end"]),
        prompt_version=doc["prompt_version"],
        llm_provider=doc["llm_provider"],
        llm_model=doc["llm_model"],
        items_considered=doc["items_considered"],
        items_included=doc["items_included"],
        items_by_tier=doc.get("items_by_tier", {}),
        items_by_source_class=doc.get("items_by_source_class", {}),
        twitter_signal_available=doc.get("twitter_signal_available", True),
        tweet_api_call_count=doc.get("tweet_api_call_count", 0),
        token_usage=doc.get("token_usage", 0),
        md_path=doc.get("md_path"),
        html_path=doc.get("html_path"),
        json_path=doc.get("json_path"),
    )


class TinyDBArticleStore(AbstractArticleStore):
    """
    TinyDB-backed article store.

    **File layout:** One JSON file per agent at the provided ``db_path``
    (typically ``outputs/{agent_id}/store.json``).

    **Tables:**
    - ``articles`` — :class:`ArticleRecord` documents, keyed by
      ``(url_hash, agent_id)``
    - ``tweets``   — :class:`TweetSignal` documents, keyed by
      ``(tweet_id, agent_id)``
    - ``digests``  — :class:`DigestRecord` documents, keyed by
      ``(agent_id, cadence, run_date)``

    Traces: SRC-012 (dedup), SRC-053 (TinyDB document store),
            SRC-072 (one file per agent), SRC-145 (idempotent digest upsert),
            SRC-150 (get_stats monitoring)
    """

    def __init__(self, db_path: str | Path) -> None:
        resolved = Path(db_path)
        resolved.parent.mkdir(parents=True, exist_ok=True)
        self._db_path = resolved
        self._db       = TinyDB(str(resolved))
        self._articles = self._db.table("articles")
        self._tweets   = self._db.table("tweets")
        self._digests  = self._db.table("digests")

    # ------------------------------------------------------------------
    # Resource lifecycle
    # ------------------------------------------------------------------

    def close(self) -> None:
        """Close the underlying TinyDB database and flush all writes."""
        self._db.close()

    # ------------------------------------------------------------------
    # ArticleRecord operations (SRC-011–SRC-012)
    # ------------------------------------------------------------------

    def insert_if_new(self, article: ArticleRecord) -> bool:
        """
        Insert ``article`` only if ``(url_hash, agent_id)`` pair is not present.
        Returns ``True`` if inserted (new), ``False`` if duplicate.

        Secondary dedup check: if the URL hash is new but a stored article for
        this agent has headline similarity ≥ 0.85, logs a WARNING so operators
        can investigate AMP/redirect variations. The new article is still
        inserted — only exact URL-hash matches are rejected automatically.
        (SRC-012 architecture §3.3)

        Traces: SRC-010 (multiple runs per day — add new only),
                SRC-012 (primary dedup by url_hash)
        """
        q = Query()
        exists = self._articles.contains(
            (q.url_hash == article.url_hash) & (q.agent_id == article.agent_id)
        )
        if exists:
            return False

        # Secondary near-duplicate check (SRC-012 architecture §3.3)
        self._check_near_duplicate(article)

        self._articles.insert(_article_to_doc(article))
        return True

    def _check_near_duplicate(self, candidate: ArticleRecord) -> None:
        """
        Scan stored headlines for this agent for similarity ≥ threshold.
        Logs a WARNING when found — does NOT block insertion.
        Traces: SRC-012 (secondary dedup signal)
        """
        q = Query()
        stored = self._articles.search(q.agent_id == candidate.agent_id)
        for doc in stored:
            sim = headline_similarity(candidate.headline, doc.get("headline", ""))
            if sim >= _HEADLINE_SIM_THRESHOLD:
                logger.warning(
                    "near_duplicate_detected",
                    extra={
                        "agent_id":        candidate.agent_id,
                        "new_url":         candidate.url,
                        "existing_url":    doc.get("url", ""),
                        "similarity":      round(sim, 3),
                        "new_headline":    candidate.headline,
                        "existing_headline": doc.get("headline", ""),
                    },
                )
                break  # one warning per insertion is enough

    def get_window(
        self,
        agent_id: str,
        window_start: datetime,
        window_end: datetime,
    ) -> list[ArticleRecord]:
        """
        Return all articles for ``agent_id`` within ``[window_start, window_end]``.
        Results are sorted by ``pub_date`` ascending.
        Traces: SRC-008–SRC-010 (lookback window queries)
        """
        q = Query()
        docs = self._articles.search(q.agent_id == agent_id)
        results: list[ArticleRecord] = []
        for doc in docs:
            pub = _parse_dt(doc["pub_date"])
            if window_start <= pub <= window_end:
                results.append(_doc_to_article(doc))

        results.sort(key=lambda a: a.pub_date)
        return results

    def get_window_by_cadence(
        self,
        agent_id: str,
        cadence: Cadence,
        reference: datetime | None = None,
    ) -> list[ArticleRecord]:
        """
        Convenience wrapper: compute the window for ``cadence`` then call
        :meth:`get_window`.
        Traces: SRC-009 (daily), SRC-028–SRC-032 (all cadence windows)
        """
        start, end = lookback_window(cadence, reference)
        return self.get_window(agent_id, start, end)

    def count_articles(self, agent_id: str) -> int:
        """
        Total article count for ``agent_id`` across all time.
        Traces: SRC-150 (quality monitoring)
        """
        q = Query()
        return len(self._articles.search(q.agent_id == agent_id))

    def count_window(
        self,
        agent_id: str,
        window_start: datetime,
        window_end: datetime,
    ) -> int:
        """
        Return the count of articles for ``agent_id`` with ``pub_date`` in
        ``[window_start, window_end]``.

        Iterates without constructing ``ArticleRecord`` instances — the
        Pipeline uses this for cheap "is the store sparse for this cadence?"
        checks before deciding to expand sourcing's window.
        """
        q = Query()
        docs = self._articles.search(q.agent_id == agent_id)
        return sum(
            1 for doc in docs
            if window_start <= _parse_dt(doc["pub_date"]) <= window_end
        )

    def get_stats(
        self,
        agent_id: str,
        window_start: datetime,
        window_end: datetime,
    ) -> StoreStats:
        """
        Return :class:`StoreStats` for articles in the window.
        Traces: SRC-150 (items_by_tier, items_by_source_class)
        """
        articles = self.get_window(agent_id, window_start, window_end)
        by_tier: dict[str, int] = {}
        by_class: dict[str, int] = {}
        for a in articles:
            by_tier[a.tier] = by_tier.get(a.tier, 0) + 1
            by_class[a.source_class] = by_class.get(a.source_class, 0) + 1
        return StoreStats(total=len(articles), by_tier=by_tier, by_source_class=by_class)

    def delete_older_than(self, agent_id: str, cutoff: datetime) -> int:
        """
        Delete all articles for ``agent_id`` with ``pub_date`` < ``cutoff``.
        Returns number of records deleted.
        Traces: store maintenance (bounded file size for annual window)
        """
        q = Query()
        docs = self._articles.search(q.agent_id == agent_id)
        ids_to_delete = [
            doc.doc_id  # type: ignore[attr-defined]
            for doc in docs
            if _parse_dt(doc["pub_date"]) < cutoff
        ]
        if ids_to_delete:
            self._articles.remove(doc_ids=ids_to_delete)
        return len(ids_to_delete)

    # ------------------------------------------------------------------
    # TweetSignal operations (SRC-047, SRC-067–SRC-069)
    # ------------------------------------------------------------------

    def insert_tweet_signal(self, signal: TweetSignal) -> bool:
        """
        Insert ``signal`` if ``(tweet_id, agent_id)`` is not present.
        Returns ``True`` if inserted (new), ``False`` if duplicate.
        Traces: SRC-067
        """
        q = Query()
        exists = self._tweets.contains(
            (q.tweet_id == signal.tweet_id) & (q.agent_id == signal.agent_id)
        )
        if not exists:
            self._tweets.insert(_tweet_to_doc(signal))
            return True
        return False

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
        q = Query()
        docs = self._tweets.search(q.agent_id == agent_id)
        results: list[TweetSignal] = []
        for doc in docs:
            created = _parse_dt(doc["created_at"])
            if window_start <= created <= window_end:
                results.append(_doc_to_tweet(doc))
        results.sort(key=lambda s: s.created_at)
        return results

    # ------------------------------------------------------------------
    # DigestRecord operations (SRC-129, SRC-145, SRC-150)
    # ------------------------------------------------------------------

    def upsert_digest(self, record: DigestRecord) -> None:
        """
        Insert or replace the :class:`DigestRecord` for
        ``(agent_id, cadence, run_date)``.  Re-runs overwrite cleanly (SRC-145).
        Traces: SRC-129 (prompt_version), SRC-145 (idempotent),
                SRC-150 (monitoring fields)
        """
        q = Query()
        doc = _digest_to_doc(record)
        existing = self._digests.search(
            (q.agent_id == record.agent_id)
            & (q.cadence == record.cadence)
            & (q.run_date == record.run_date.isoformat())
        )
        if existing:
            self._digests.update(
                doc,
                (q.agent_id == record.agent_id)
                & (q.cadence == record.cadence)
                & (q.run_date == record.run_date.isoformat()),
            )
        else:
            self._digests.insert(doc)

    def get_digest(
        self,
        agent_id: str,
        cadence: str,
        run_date: date | None = None,
    ) -> DigestRecord | None:
        """
        Retrieve a digest record.  If ``run_date`` is ``None``, returns the
        most recent one for the ``(agent_id, cadence)`` pair.

        ``run_date`` matches :attr:`DigestRecord.run_date` (a :class:`date`).
        A :class:`datetime` is also accepted and coerced via ``.date()``.

        Traces: SRC-145 (portal listing)
        """
        q = Query()
        if run_date is not None:
            run_date_key = (
                run_date.date() if isinstance(run_date, datetime) else run_date
            )
            docs = self._digests.search(
                (q.agent_id == agent_id)
                & (q.cadence == cadence)
                & (q.run_date == run_date_key.isoformat())
            )
        else:
            docs = self._digests.search(
                (q.agent_id == agent_id) & (q.cadence == cadence)
            )

        if not docs:
            return None

        # Sort by run_date descending and take the latest
        docs.sort(key=lambda d: d["run_date"], reverse=True)
        return _doc_to_digest(docs[0])

    def list_digests(
        self,
        agent_id: str,
        cadence: str | None = None,
        limit: int = 50,
    ) -> list[DigestRecord]:
        """
        Return up to ``limit`` :class:`DigestRecord` objects, most-recent first.
        Filtered by ``cadence`` when provided.
        Traces: SRC-133–SRC-134 (portal index listing)
        """
        q = Query()
        if cadence is not None:
            docs = self._digests.search(
                (q.agent_id == agent_id) & (q.cadence == cadence)
            )
        else:
            docs = self._digests.search(q.agent_id == agent_id)

        docs.sort(key=lambda d: d["run_date"], reverse=True)
        return [_doc_to_digest(d) for d in docs[:limit]]
