"""
storage/factory.py — StoreFactory: create the correct AbstractArticleStore
from agent configuration without the caller knowing the concrete class.

Default store is **TinyDB** — zero infrastructure, file-backed JSON.
Activate **SQLite** by setting ``store_backend: sqlite`` in the agent YAML.
New store backends register here without changing sourcing, curation, or
rendering code.

Usage (throughout pipeline):
    from ai_news_agent.storage.factory import StoreFactory

    store = StoreFactory.create(agent_cfg, output_base="outputs")
    # or directly:
    store = StoreFactory.from_backend("sqlite", db_path="outputs/agent/store.db")

Auto-migration: when ``store_backend: sqlite`` is set for the first time on an
agent that previously used TinyDB, ``StoreFactory.create()`` detects the
existing ``store.json`` alongside the new ``store.db`` and imports all records
automatically (articles, tweets, digests) before the pipeline runs.

Traces: SRC-053 (pluggable document store), SRC-072 (per-agent scoped store),
        SRC-076 (local dev: TinyDB), SRC-085 (container: SQLite on mounted volume)
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING

from ai_news_agent.storage.base import AbstractArticleStore
from ai_news_agent.storage.sqlite_store import SQLiteArticleStore
from ai_news_agent.storage.tinydb_store import (
    TinyDBArticleStore,
    _doc_to_article,
    _doc_to_digest,
    _doc_to_tweet,
)

if TYPE_CHECKING:
    from ai_news_agent.config.models import AgentConfig

logger = logging.getLogger(__name__)

# Supported backend identifiers and their factory callables.
# Add new entries here to register a new concrete store.
_BACKENDS: dict[str, type[AbstractArticleStore]] = {
    "tinydb": TinyDBArticleStore,
    "sqlite": SQLiteArticleStore,
}

_DEFAULT_BACKEND = "tinydb"

# File extension per backend
_EXT: dict[str, str] = {
    "tinydb": "store.json",
    "sqlite": "store.db",
}


def _migrate_tinydb_to_sqlite(
    tinydb_path: Path,
    dest: AbstractArticleStore,
) -> dict[str, int]:
    """
    One-shot import of all records from a TinyDB JSON store into ``dest``.

    Reads the raw TinyDB tables directly (no TinyDBArticleStore wrapper) and
    inserts via the AbstractArticleStore interface so dedup logic is preserved.
    Records that fail to deserialise are skipped with a WARNING.

    Returns counts: ``{"articles": n, "tweets": n, "digests": n}``.
    """
    from tinydb import TinyDB  # local import — tinydb is an optional dep for sqlite-only users

    counts: dict[str, int] = {"articles": 0, "tweets": 0, "digests": 0}
    src = TinyDB(str(tinydb_path))
    try:
        for doc in src.table("articles").all():
            try:
                if dest.insert_if_new(_doc_to_article(doc)):
                    counts["articles"] += 1
            except Exception as exc:
                logger.warning(
                    "tinydb_migration_article_skip",
                    extra={"doc_keys": list(doc.keys()), "error": str(exc)},
                )

        for doc in src.table("tweets").all():
            try:
                if dest.insert_tweet_signal(_doc_to_tweet(doc)):
                    counts["tweets"] += 1
            except Exception as exc:
                logger.warning(
                    "tinydb_migration_tweet_skip",
                    extra={"doc_keys": list(doc.keys()), "error": str(exc)},
                )

        for doc in src.table("digests").all():
            try:
                dest.upsert_digest(_doc_to_digest(doc))
                counts["digests"] += 1
            except Exception as exc:
                logger.warning(
                    "tinydb_migration_digest_skip",
                    extra={"doc_keys": list(doc.keys()), "error": str(exc)},
                )
    finally:
        src.close()

    return counts


class StoreFactory:
    """
    Factory for :class:`~ai_news_agent.storage.base.AbstractArticleStore`.

    No sourcing, curation, or rendering code should instantiate a concrete
    store class directly — always go through this factory so the backend
    can be swapped by changing configuration.

    Traces: SRC-053 (pluggable store), SRC-072 (agent_id scoping)
    """

    @staticmethod
    def create(
        agent_cfg: AgentConfig,
        output_base: str | Path = "outputs",
    ) -> AbstractArticleStore:
        """
        Create the store for ``agent_cfg`` under ``output_base/{agent_id}/``.

        The backend key is read from ``agent_cfg.store_backend``; it defaults
        to ``"tinydb"`` for existing agent configs (SRC-053 backward-compat).
        When switching to ``"sqlite"``, existing TinyDB data is auto-migrated
        on first use if ``store.json`` is present in the output directory.

        Args:
            agent_cfg:   Validated AgentConfig for this agent instance.
            output_base: Root output directory; resolved to an absolute path.

        Returns:
            A ready-to-use ``AbstractArticleStore`` with the output directory
            pre-created.

        Traces: SRC-053 (pluggable), SRC-072 (one store per agent_id)
        """
        backend = getattr(agent_cfg, "store_backend", _DEFAULT_BACKEND) or _DEFAULT_BACKEND
        backend = backend.lower().strip()

        output_dir = Path(output_base) / agent_cfg.agent_id
        output_dir.mkdir(parents=True, exist_ok=True)

        filename = _EXT.get(backend, "store.json")
        db_path = output_dir / filename

        logger.debug(
            "store_factory_create",
            extra={
                "agent_id": agent_cfg.agent_id,
                "backend": backend,
                "db_path": str(db_path),
            },
        )
        store = StoreFactory.from_backend(backend, db_path)

        # Auto-migrate from TinyDB when switching to SQLite for the first time.
        # Condition: store.json exists alongside the new store.db AND the SQLite
        # store is empty for this agent (meaning no prior migration has run).
        if backend == "sqlite":
            tinydb_path = output_dir / "store.json"
            if tinydb_path.exists() and store.count_articles(agent_cfg.agent_id) == 0:
                logger.info(
                    "tinydb_migration_start",
                    extra={"agent_id": agent_cfg.agent_id, "source": str(tinydb_path)},
                )
                counts = _migrate_tinydb_to_sqlite(tinydb_path, store)
                logger.info(
                    "tinydb_migration_complete",
                    extra={"agent_id": agent_cfg.agent_id, **counts},
                )

        return store

    @staticmethod
    def from_backend(
        backend: str,
        db_path: str | Path,
    ) -> AbstractArticleStore:
        """
        Directly create a store by backend name and path.

        Useful in tests, CLI tools, and migration scripts where a full
        ``AgentConfig`` is not available.

        Args:
            backend: ``"tinydb"`` | ``"sqlite"`` (or any registered key)
            db_path: Full filesystem path to the store file.

        Raises:
            ValueError: If ``backend`` is not in the registered backends.

        Traces: SRC-053 (pluggable store)
        """
        backend = backend.lower().strip()
        if backend not in _BACKENDS:
            registered = ", ".join(sorted(_BACKENDS))
            raise ValueError(
                f"Unknown store backend {backend!r}. Registered backends: {registered}"
            )
        store_cls = _BACKENDS[backend]
        return store_cls(db_path)  # type: ignore[call-arg]

    @staticmethod
    def register(name: str, store_cls: type[AbstractArticleStore]) -> None:
        """
        Register a new concrete :class:`AbstractArticleStore` subclass under
        ``name`` so it becomes selectable via ``store_backend: <name>`` in
        agent YAML configs.

        This is the extension point for cloud document stores (DynamoDB,
        Firestore, Cosmos DB) without modifying this module.

        Example::

            from ai_news_agent.storage.factory import StoreFactory
            from my_package.dynamo_store import DynamoArticleStore

            StoreFactory.register("dynamodb", DynamoArticleStore)

        Traces: SRC-053 (pluggable swap path for cloud document stores),
                SRC-088–SRC-089 (cloud-equivalent storage tiers)
        """
        if not issubclass(store_cls, AbstractArticleStore):
            raise TypeError(f"{store_cls.__name__} must be a subclass of AbstractArticleStore")
        _BACKENDS[name.lower().strip()] = store_cls

    @staticmethod
    def available_backends() -> list[str]:
        """Return sorted list of registered backend names."""
        return sorted(_BACKENDS)
