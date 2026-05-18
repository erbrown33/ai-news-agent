"""
storage/base.py — AbstractArticleStore interface.

This is the **only** surface the rest of the pipeline depends on for
persistence.  All sourcing, curation, and rendering code imports
``AbstractArticleStore`` and calls its methods — never a concrete class.

Concrete implementations (in the same package):
  - ``TinyDBArticleStore``  — default; zero infrastructure; file-backed JSON
  - ``SQLiteArticleStore``  — production alternative; indexed SQL; cloud-safe

Swap by changing the ``store_backend`` key in the agent YAML and registering
the new class in ``StoreFactory``.

Traces: SRC-008–SRC-013 (lookback windows, dedup, sourcing-only rule),
        SRC-028–SRC-032 (cadence windows), SRC-053 (document store abstraction),
        SRC-072 (agent_id scoping), SRC-145 (idempotent overwrite),
        SRC-150 (monitoring stats)
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from datetime import date, datetime

    from ai_news_agent.storage.models import (
        ArticleRecord,
        Cadence,
        DigestRecord,
        TweetSignal,
    )


class AbstractArticleStore(ABC):
    """
    Provider-agnostic document store interface shared by Sourcing and
    Curation agents.

    **Contract guarantees (SRC-008–SRC-012):**
    - ``insert_if_new`` is the sole write path for articles — idempotent;
      duplicate ``(url_hash, agent_id)`` pairs are silently skipped.
    - ``get_window`` filters by ``pub_date``; lookback spans are computed
      by the caller using ``lookback_window()`` from ``storage.models``.
    - ``insert_tweet_signal`` deduplicates by ``(tweet_id, agent_id)``.
    - ``get_tweet_signals`` filters by ``created_at`` within the window.
    - All stores are scoped to ``agent_id`` — multiple agents sharing one
      process never see each other's data (SRC-072).

    **Pluggable swap path (SRC-053):**
    Provide a concrete subclass and register it in ``StoreFactory``.  No
    sourcing, curation, or rendering code needs to change.

    **Resource lifecycle:**
    Stores support the context-manager protocol (``__enter__`` / ``__exit__``)
    and have an explicit ``close()`` method.  Always call ``close()`` or use
    the store inside a ``with`` block to flush writes and release file handles.
    """

    # ------------------------------------------------------------------
    # Context-manager protocol
    # ------------------------------------------------------------------

    def __enter__(self) -> AbstractArticleStore:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    @abstractmethod
    def close(self) -> None:
        """
        Flush pending writes and release any open file handles or connections.
        Called automatically when using the store as a context manager.
        """

    # ------------------------------------------------------------------
    # ArticleRecord operations (SRC-011–SRC-012)
    # ------------------------------------------------------------------

    @abstractmethod
    def insert_if_new(self, article: ArticleRecord) -> bool:
        """
        Insert ``article`` only if its ``(url_hash, agent_id)`` pair is not
        already present in the store.

        Returns ``True`` if inserted (new record), ``False`` if duplicate.

        Lookback filtering is handled at query time (``get_window``), *not*
        at insertion time.  Articles from any time period may be stored; the
        cadence window restricts what the Curation Agent reads, not what is
        written.

        Traces: SRC-010 (multiple runs — add new, never duplicate),
                SRC-012 (deduplication — same article stored once per agent)
        """

    @abstractmethod
    def get_window(
        self,
        agent_id: str,
        window_start: datetime,
        window_end: datetime,
    ) -> list[ArticleRecord]:
        """
        Return all :class:`ArticleRecord` objects for ``agent_id`` whose
        ``pub_date`` falls within ``[window_start, window_end]`` inclusive.

        Results are ordered by ``pub_date`` ascending (oldest first).

        Traces: SRC-008–SRC-010 (lookback window queries),
                SRC-029–SRC-032 (daily/weekly/monthly/annual windows),
                SRC-053 (document store)
        """

    @abstractmethod
    def get_window_by_cadence(
        self,
        agent_id: str,
        cadence: Cadence,
        reference: datetime | None = None,
    ) -> list[ArticleRecord]:
        """
        Convenience wrapper — computes the window from ``cadence`` + ``reference``
        using :func:`~ai_news_agent.storage.models.lookback_window`, then delegates
        to :meth:`get_window`.

        ``reference`` defaults to ``datetime.now(UTC)`` when ``None``.

        Traces: SRC-009 (daily), SRC-028–SRC-032 (all cadence windows)
        """

    @abstractmethod
    def count_articles(self, agent_id: str) -> int:
        """
        Return the total number of :class:`ArticleRecord` objects stored for
        ``agent_id`` across all time.

        Used by the Scheduler and quality-monitoring logging (SRC-150).
        """

    @abstractmethod
    def count_window(
        self,
        agent_id: str,
        window_start: datetime,
        window_end: datetime,
    ) -> int:
        """
        Return the count of :class:`ArticleRecord` objects for ``agent_id``
        whose ``pub_date`` falls within ``[window_start, window_end]``.

        Cheaper than :meth:`get_window` when only the count is needed
        (e.g. when the Pipeline decides whether to expand sourcing's window
        to a wider cadence range).
        """

    @abstractmethod
    def get_stats(
        self,
        agent_id: str,
        window_start: datetime,
        window_end: datetime,
    ) -> StoreStats:
        """
        Return aggregated stats for articles within the window.

        Populates:
        - ``total``          — total article count in window
        - ``by_tier``        — count per tier string
        - ``by_source_class``— count per source_class ("web", "twitter")

        Used by the Curation Agent to build :class:`DigestMetadata` (SRC-150).
        """

    @abstractmethod
    def delete_older_than(self, agent_id: str, cutoff: datetime) -> int:
        """
        Delete all :class:`ArticleRecord` objects for ``agent_id`` whose
        ``pub_date`` is strictly before ``cutoff``.

        Returns the number of records deleted.

        Call periodically to bound store file size (e.g. delete articles
        older than 400 days to preserve annual window coverage).
        Not called automatically — triggered by the Scheduler's maintenance job.
        """

    # ------------------------------------------------------------------
    # TweetSignal operations (SRC-047, SRC-067–SRC-069)
    # ------------------------------------------------------------------

    @abstractmethod
    def insert_tweet_signal(self, signal: TweetSignal) -> bool:
        """
        Insert ``signal`` if its ``(tweet_id, agent_id)`` pair is not already
        stored for this agent.  Returns ``True`` if inserted (new),
        ``False`` if duplicate.

        Traces: SRC-067 (fetch tweets within lookback window)
        """

    @abstractmethod
    def get_tweet_signals(
        self,
        agent_id: str,
        window_start: datetime,
        window_end: datetime,
    ) -> list[TweetSignal]:
        """
        Return all :class:`TweetSignal` objects for ``agent_id`` whose
        ``created_at`` falls within ``[window_start, window_end]`` inclusive.

        Traces: SRC-047 (Twitter signal in curation),
                SRC-070 (pass to LLM as influencer context)
        """

    # ------------------------------------------------------------------
    # DigestRecord operations (SRC-129, SRC-145, SRC-150)
    # ------------------------------------------------------------------

    @abstractmethod
    def upsert_digest(self, record: DigestRecord) -> None:
        """
        Insert or replace the :class:`DigestRecord` identified by
        ``(agent_id, cadence, run_date)``.

        Re-runs overwrite cleanly — idempotent by design (SRC-145).

        Traces: SRC-129 (prompt_version stored per run),
                SRC-145 (idempotency),
                SRC-150 (quality monitoring fields persisted)
        """

    @abstractmethod
    def get_digest(
        self,
        agent_id: str,
        cadence: str,
        run_date: date | None = None,
    ) -> DigestRecord | None:
        """
        Retrieve the most recent :class:`DigestRecord` for ``(agent_id, cadence)``
        or the specific record for ``run_date`` when provided.

        ``run_date`` matches the type of :attr:`DigestRecord.run_date` (a
        :class:`date`).  Implementations also accept a :class:`datetime` and
        coerce it via ``.date()`` for backwards compatibility.

        Returns ``None`` if no matching record exists.

        Traces: SRC-145 (portal listing of available digests)
        """

    @abstractmethod
    def list_digests(
        self,
        agent_id: str,
        cadence: str | None = None,
        limit: int = 50,
    ) -> list[DigestRecord]:
        """
        Return up to ``limit`` :class:`DigestRecord` objects for ``agent_id``,
        optionally filtered by ``cadence``, ordered by ``run_date`` descending.

        Used by the web portal to populate the index listing. (SRC-133–SRC-134)
        """


# ---------------------------------------------------------------------------
# StoreStats — aggregated window statistics (SRC-150)
# ---------------------------------------------------------------------------

class StoreStats:
    """
    Aggregated article statistics over a window — returned by
    :meth:`AbstractArticleStore.get_stats`.

    Traces: SRC-150 (items_by_tier, items_by_source_class monitoring fields)
    """

    __slots__ = ("total", "by_tier", "by_source_class")

    def __init__(
        self,
        total: int,
        by_tier: dict[str, int],
        by_source_class: dict[str, int],
    ) -> None:
        self.total = total
        self.by_tier = by_tier
        self.by_source_class = by_source_class

    def __repr__(self) -> str:
        return (
            f"StoreStats(total={self.total}, "
            f"by_tier={self.by_tier}, "
            f"by_source_class={self.by_source_class})"
        )
