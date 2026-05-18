"""
sourcing/twitter_fetcher.py — Thin wrapper around TwitterClient for the Sourcing Agent.
Traces: SRC-047 (signal role — not primary news), SRC-062–SRC-070 (Twitter integration),
        SRC-148 (graceful degradation on API failure)
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import structlog

from ai_news_agent.twitter.client import TwitterClient

if TYPE_CHECKING:
    from datetime import datetime

    from ai_news_agent.config.models import AgentConfig
    from ai_news_agent.storage.models import TweetSignal

log = structlog.get_logger(__name__)


class TwitterFetcher:
    """
    Thin wrapper that instantiates a :class:`TwitterClient` from the agent config
    and returns ``(signals, twitter_available)`` pairs for the Sourcing Agent.

    Twitter content is **signal and lead-generation only** — not primary news (SRC-047).
    Graceful degradation: returns ``([], False)`` if Twitter API is unavailable (SRC-148).

    Traces: SRC-047, SRC-062–SRC-070, SRC-148
    """

    def __init__(
        self,
        config: AgentConfig,
        bearer_token: str,
    ) -> None:
        """
        Args:
            config:       Per-agent configuration (provides twitter.handles, SRC-036–SRC-046).
            bearer_token: ``TWITTER_BEARER_TOKEN`` env var (SRC-064).
        """
        self._client = TwitterClient(
            bearer_token=bearer_token,
            handles=config.twitter.handles,
        )
        self._agent_id = config.agent_id

    def fetch(
        self,
        window_start: datetime,
        window_end: datetime,
        agent_id: str,
    ) -> tuple[list[TweetSignal], bool]:
        """
        Fetch tweet signals for all configured influencer handles within the window.

        Returns:
            ``(signals, twitter_available)``
            - ``signals``: list of substantive, hydrated :class:`TweetSignal` objects.
            - ``twitter_available``: False if the Twitter API is unavailable (SRC-148).

        Steps:
        1. Delegate to :meth:`TwitterClient.fetch_signals`.
        2. Log outcome for quality monitoring (SRC-150).

        Traces: SRC-062–SRC-070, SRC-148, SRC-150
        """
        signals, twitter_available = self._client.fetch_signals(
            window_start=window_start,
            window_end=window_end,
            agent_id=agent_id,
        )

        log.info(
            "twitter_fetcher_result",
            agent_id=agent_id,
            signals_count=len(signals),
            twitter_available=twitter_available,
        )

        return signals, twitter_available
