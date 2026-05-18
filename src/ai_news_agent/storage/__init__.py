"""
storage — Document store abstraction, implementations, and data models.

Public surface for the rest of the pipeline:

    from ai_news_agent.storage import (
        AbstractArticleStore,   # interface only
        StoreFactory,           # create() or from_backend()
        TinyDBArticleStore,     # default concrete store
        SQLiteArticleStore,     # production alternative
        ArticleRecord,          # sourcing data model
        TweetSignal,            # Twitter signal data model
        DigestRecord,           # curated run persistence
        CuratedItem,            # curation output model
        CuratedItemRaw,         # LLM raw output schema (Pydantic)
        CurationResponse,       # top-level LLM output schema
        DigestMetadata,         # quality monitoring metadata
        StoreStats,             # aggregated window stats
        Cadence,                # cadence enum
        lookback_window,        # window helper for cadence queries
        normalize_url,          # URL canonicalisation
        url_hash,               # SHA-256 dedup key
        headline_similarity,    # Levenshtein similarity (0–1)
    )

Traces: SRC-008–SRC-013 (lookback windows, deduplication),
        SRC-028–SRC-032 (cadence windows),
        SRC-048–SRC-049 (curated item schema, URL enforcement),
        SRC-053 (document store — pluggable via factory),
        SRC-129 (prompt_version SHA-256),
        SRC-145 (idempotent digest records),
        SRC-150 (quality monitoring / StoreStats)
"""

from ai_news_agent.storage.base import AbstractArticleStore, StoreStats
from ai_news_agent.storage.factory import StoreFactory
from ai_news_agent.storage.models import (
    ArticleRecord,
    Cadence,
    CuratedItem,
    CuratedItemRaw,
    CurationResponse,
    DigestMetadata,
    DigestRecord,
    TweetSignal,
    headline_similarity,
    lookback_window,
    normalize_url,
    url_hash,
)
from ai_news_agent.storage.sqlite_store import SQLiteArticleStore
from ai_news_agent.storage.tinydb_store import TinyDBArticleStore

__all__ = [
    # Interface
    "AbstractArticleStore",
    "StoreStats",
    # Factory
    "StoreFactory",
    # Concrete implementations
    "TinyDBArticleStore",
    "SQLiteArticleStore",
    # Data models
    "ArticleRecord",
    "Cadence",
    "CuratedItem",
    "CuratedItemRaw",
    "CurationResponse",
    "DigestMetadata",
    "DigestRecord",
    "TweetSignal",
    # Helpers
    "headline_similarity",
    "lookback_window",
    "normalize_url",
    "url_hash",
]
