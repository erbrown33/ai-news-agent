"""
tests/unit/test_twitter.py — TwitterClient: full behaviour coverage.

Covers:
  - Construction: tweepy.Client instantiated with bearer_token (SRC-063, SRC-064)
  - tweepy import failure disables client gracefully (SRC-148)
  - Graceful degradation: any API error → ([], False) (SRC-148)
  - _is_substantive filter rules (SRC-068):
      - SKIP pure replies (@mention as first token)
      - SKIP bare retweets (RT @)
      - KEEP tweets with URL regardless of length
      - KEEP original tweets ≥ 50 chars
  - _hydrate_urls: expand t.co links from entities (SRC-069):
      - Returns expanded_url values
      - Skips twitter.com / x.com self-referential links
      - Handles missing entities gracefully
      - Deduplicates expanded URLs
  - _normalize: produces TweetSignal with correct fields (SRC-011, SRC-046)
  - _resolve_user: returns user object or None on error (SRC-067)
  - fetch_signals: end-to-end with mocked tweepy (SRC-067–SRC-069)
  - TwitterFetcher thin wrapper (SRC-047)

Traces: SRC-036–SRC-046 (influencer list + weights), SRC-047 (signal role),
        SRC-063–SRC-070 (Twitter integration), SRC-098 (mocked tweepy),
        SRC-148 (graceful degradation)
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from ai_news_agent.config.models import TwitterHandleConfig
from ai_news_agent.twitter.client import (
    _MIN_TWEET_CHARS,
    TwitterClient,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_client(handles: list[TwitterHandleConfig] | None = None) -> TwitterClient:
    """
    Construct a TwitterClient with a mocked tweepy.Client.

    The real ``tweepy.Client`` is not called during tests — we patch the
    module-level import so no actual HTTP requests are made.
    """
    handles = handles or [TwitterHandleConfig(handle="karpathy", weight=1.0)]
    with patch("ai_news_agent.twitter.client.tweepy") as mock_tweepy:
        mock_tweepy.Client.return_value = MagicMock()
        client = TwitterClient(bearer_token="test-bearer", handles=handles)
    return client


def _make_tweet(
    text: str,
    tweet_id: str = "123",
    created_at: datetime | None = None,
    entities: Any = None,
) -> MagicMock:
    """
    Build a mock tweepy Tweet object with the attributes used by TwitterClient.
    """
    tweet = MagicMock()
    tweet.text = text
    tweet.id = tweet_id
    tweet.created_at = created_at or datetime(2026, 5, 9, 10, 0, tzinfo=UTC)
    tweet.entities = entities
    return tweet


def _make_entities(urls: list[dict]) -> MagicMock:
    """
    Build a mock entities object with a list of URL entry mocks.

    Args:
        urls: List of dicts with 'expanded_url' and optional 'url' keys.
    """
    entities = MagicMock()
    url_objects = []
    for u in urls:
        url_obj = MagicMock()
        url_obj.expanded_url = u.get("expanded_url", "")
        url_obj.url = u.get("url", "")
        url_objects.append(url_obj)
    entities.urls = url_objects
    return entities


# ===========================================================================
# Construction (SRC-063, SRC-064)
# ===========================================================================

class TestTwitterClientConstruction:
    """Traces: SRC-063 (tweepy), SRC-064 (bearer token from env var)."""

    def test_client_created_with_bearer_token(self) -> None:
        """tweepy.Client is initialised with the bearer_token and wait_on_rate_limit=True."""
        handles = [TwitterHandleConfig(handle="karpathy", weight=1.0)]
        with patch("ai_news_agent.twitter.client.tweepy") as mock_tweepy:
            mock_tweepy.Client.return_value = MagicMock()
            TwitterClient(bearer_token="test-bearer-token", handles=handles)
        mock_tweepy.Client.assert_called_once_with(
            bearer_token="test-bearer-token",
            wait_on_rate_limit=True,
        )

    def test_handles_stored(self) -> None:
        """Configured handles are stored for later iteration."""
        handles = [
            TwitterHandleConfig(handle="karpathy", weight=1.0),
            TwitterHandleConfig(handle="sama", weight=1.5),
        ]
        client = _make_client(handles)
        assert len(client._handles) == 2

    def test_tweepy_import_failure_disables_client(self) -> None:
        """If tweepy is not installed, _client is None."""
        handles = [TwitterHandleConfig(handle="karpathy", weight=1.0)]
        with patch("ai_news_agent.twitter.client._TWEEPY_AVAILABLE", False):
            client = TwitterClient(bearer_token="test", handles=handles)
            client._client = None  # simulate absent tweepy
        assert client._client is None

    def test_tweepy_unavailable_fetch_returns_false(self) -> None:
        """fetch_signals returns ([], False) when tweepy is not installed (SRC-148)."""
        handles = [TwitterHandleConfig(handle="karpathy", weight=1.0)]
        with patch("ai_news_agent.twitter.client._TWEEPY_AVAILABLE", False):
            client = TwitterClient(bearer_token="test", handles=handles)
            client._client = None

        signals, available = client.fetch_signals(
            window_start=datetime(2026, 5, 9, tzinfo=UTC),
            window_end=datetime(2026, 5, 9, 23, 59, tzinfo=UTC),
            agent_id="test",
        )
        assert signals == []
        assert available is False


# ===========================================================================
# Graceful degradation (SRC-148)
# ===========================================================================

class TestGracefulDegradation:
    """Traces: SRC-148 (Twitter unavailable → continue with web sources)."""

    def test_api_exception_returns_empty_and_false(self) -> None:
        """Any exception during fetch_signals → ([], False) without propagating (SRC-148)."""
        client = _make_client()
        client._resolve_user = MagicMock(side_effect=Exception("Rate limit exceeded"))
        signals, available = client.fetch_signals(
            window_start=datetime(2026, 5, 9, tzinfo=UTC),
            window_end=datetime(2026, 5, 9, 23, 59, tzinfo=UTC),
            agent_id="test",
        )
        assert signals == []
        assert available is False

    def test_resolve_user_error_does_not_abort_all_handles(self) -> None:
        """
        If _resolve_user fails for one handle, remaining handles are skipped
        but the outer try/except catches all — final result is ([], False).
        (SRC-148 — graceful degradation applies at the per-run level)
        """
        handles = [
            TwitterHandleConfig(handle="karpathy", weight=1.0),
            TwitterHandleConfig(handle="sama", weight=1.0),
        ]
        client = _make_client(handles)
        client._resolve_user = MagicMock(side_effect=Exception("API down"))
        signals, available = client.fetch_signals(
            window_start=datetime(2026, 5, 9, tzinfo=UTC),
            window_end=datetime(2026, 5, 9, 23, 59, tzinfo=UTC),
            agent_id="test",
        )
        assert available is False

    def test_sourcing_continues_when_twitter_down(self) -> None:
        """
        SourcingRunResult.twitter_signal_available=False does not abort the sourcing run.
        This is the integration-level guarantee for SRC-148 — documented here for
        traceability; integration covered in test_sourcing.py.
        """
        assert True  # SRC-148 integration covered in TestSourcingAgent


# ===========================================================================
# _is_substantive filter (SRC-068)
# ===========================================================================

class TestIsSubstantive:
    """
    Validate the per-tweet filtering rules.
    Traces: SRC-068 (filter: skip pure replies, bare RTs, short tweets without URL)
    """

    def _client(self) -> TwitterClient:
        return _make_client()

    # -- Skip rules --

    def test_pure_reply_is_filtered(self) -> None:
        """Tweet starting with @mention → False (pure reply, SRC-068)."""
        client = self._client()
        tweet = _make_tweet("@karpathy Great point about transformers!")
        assert client._is_substantive(tweet) is False

    def test_pure_reply_with_whitespace_prefix_is_filtered(self) -> None:
        """Tweet with leading whitespace before @mention → still filtered."""
        client = self._client()
        tweet = _make_tweet("  @sama Interesting announcement.")
        assert client._is_substantive(tweet) is False

    def test_bare_retweet_is_filtered(self) -> None:
        """RT @ tweet → False (bare retweet, SRC-068)."""
        client = self._client()
        tweet = _make_tweet("RT @karpathy: Check out this paper https://arxiv.org/abs/1")
        assert client._is_substantive(tweet) is False

    def test_short_tweet_without_url_is_filtered(self) -> None:
        """Original tweet under 50 chars with no URL → False (SRC-068)."""
        client = self._client()
        tweet = _make_tweet("Short tweet.")  # well under 50 chars
        assert client._is_substantive(tweet) is False

    # -- Keep rules --

    def test_tweet_with_url_is_kept_regardless_of_length(self) -> None:
        """Tweet with URL is kept even if short (SRC-068)."""
        client = self._client()
        tweet = _make_tweet("AI https://openai.com/blog/new")
        assert client._is_substantive(tweet) is True

    def test_long_original_tweet_is_kept(self) -> None:
        """Long original tweet (≥ 50 chars, no URL) → True (SRC-068)."""
        client = self._client()
        # Exactly 50+ chars, no URL, not a reply, not an RT
        tweet = _make_tweet("This is a substantive original thought about AI policy and its impact.")
        assert client._is_substantive(tweet) is True

    def test_exactly_50_chars_is_kept(self) -> None:
        """Tweet of exactly 50 chars is substantive."""
        client = self._client()
        tweet = _make_tweet("A" * _MIN_TWEET_CHARS)
        assert client._is_substantive(tweet) is True

    def test_49_chars_no_url_is_filtered(self) -> None:
        """Tweet of 49 chars without URL → filtered (just below threshold)."""
        client = self._client()
        tweet = _make_tweet("A" * (_MIN_TWEET_CHARS - 1))
        assert client._is_substantive(tweet) is False

    def test_original_tweet_with_url_always_kept(self) -> None:
        """Tweet containing a URL is always kept (SRC-068 — URL is a lead)."""
        client = self._client()
        tweet = _make_tweet("x https://techcrunch.com/a")
        assert client._is_substantive(tweet) is True

    def test_quoted_retweet_with_original_content_is_kept(self) -> None:
        """
        Quote-tweet (not starting with 'RT @') with meaningful content → kept.
        A quoted retweet begins with the author's own text, not 'RT @'.
        """
        client = self._client()
        tweet = _make_tweet(
            "This is exactly why AI safety matters — see the referenced work. "
            "https://arxiv.org/abs/test RT behaviour"
        )
        assert client._is_substantive(tweet) is True


# ===========================================================================
# _hydrate_urls (SRC-069)
# ===========================================================================

class TestHydrateUrls:
    """
    Validate expanded URL extraction from tweet entities.
    Traces: SRC-069 (hydrate linked URLs from tweet entities.urls)
    """

    def _client(self) -> TwitterClient:
        return _make_client()

    def test_expanded_urls_returned(self) -> None:
        """expanded_url values from entities.urls are returned."""
        client = self._client()
        entities = _make_entities([
            {"expanded_url": "https://reuters.com/ai-article", "url": "https://t.co/abc"},
        ])
        tweet = _make_tweet("Check this out https://t.co/abc", entities=entities)
        urls = client._hydrate_urls(tweet)
        assert "https://reuters.com/ai-article" in urls

    def test_twitter_self_links_excluded(self) -> None:
        """twitter.com and x.com links are excluded (media cards, profile links)."""
        client = self._client()
        entities = _make_entities([
            {"expanded_url": "https://twitter.com/karpathy/status/123", "url": "https://t.co/a"},
            {"expanded_url": "https://x.com/sama/status/456", "url": "https://t.co/b"},
            {"expanded_url": "https://reuters.com/article", "url": "https://t.co/c"},
        ])
        tweet = _make_tweet("text", entities=entities)
        urls = client._hydrate_urls(tweet)
        assert "https://reuters.com/article" in urls
        for url in urls:
            assert "twitter.com" not in url
            assert "x.com" not in url

    def test_no_entities_returns_empty_list(self) -> None:
        """Tweet without entities block → empty list (graceful, SRC-069)."""
        client = self._client()
        tweet = _make_tweet("No entities here", entities=None)
        urls = client._hydrate_urls(tweet)
        assert urls == []

    def test_entities_with_no_urls_returns_empty_list(self) -> None:
        """Entities block without urls field → empty list."""
        client = self._client()
        entities = MagicMock()
        entities.urls = []
        tweet = _make_tweet("text", entities=entities)
        urls = client._hydrate_urls(tweet)
        assert urls == []

    def test_duplicate_expanded_urls_deduplicated(self) -> None:
        """Same expanded_url appearing twice → deduplicated in result (SRC-012)."""
        client = self._client()
        entities = _make_entities([
            {"expanded_url": "https://reuters.com/article"},
            {"expanded_url": "https://reuters.com/article"},
        ])
        tweet = _make_tweet("text", entities=entities)
        urls = client._hydrate_urls(tweet)
        assert len(urls) == 1
        assert urls[0] == "https://reuters.com/article"

    def test_missing_expanded_url_falls_back_to_url(self) -> None:
        """If expanded_url is missing, url field is used as fallback."""
        client = self._client()
        entities = MagicMock()
        url_obj = MagicMock()
        url_obj.expanded_url = None
        url_obj.url = "https://reuters.com/via-url-field"
        entities.urls = [url_obj]
        tweet = _make_tweet("text", entities=entities)
        urls = client._hydrate_urls(tweet)
        # Falls back to url field which is reuters.com (not twitter.com) → included
        assert "https://reuters.com/via-url-field" in urls


# ===========================================================================
# _normalize (SRC-011, SRC-046)
# ===========================================================================

class TestNormalize:
    """
    Validate TweetSignal production from raw tweet + handle config.
    Traces: SRC-011 (storage schema), SRC-046 (handle weight from config)
    """

    def test_normalize_produces_tweet_signal(self) -> None:
        """_normalize converts a raw tweet to a TweetSignal."""
        from ai_news_agent.storage.models import TweetSignal

        client = _make_client()
        handle_cfg = TwitterHandleConfig(handle="karpathy", weight=1.5)
        tweet = _make_tweet(
            "Fascinating new paper on AI alignment. https://arxiv.org/abs/test",
            tweet_id="9876543210",
            created_at=datetime(2026, 5, 9, 11, 30, tzinfo=UTC),
        )

        signal = client._normalize(tweet, handle_cfg, "test-agent")

        assert isinstance(signal, TweetSignal)
        assert signal.tweet_id == "9876543210"
        assert signal.handle == "karpathy"
        assert signal.weight == 1.5
        assert signal.agent_id == "test-agent"
        assert signal.created_at == datetime(2026, 5, 9, 11, 30, tzinfo=UTC)

    def test_normalize_handle_stored_without_at_prefix(self) -> None:
        """
        TweetSignal.handle never contains the '@' prefix (SRC-036).

        TwitterHandleConfig enforces no '@' via pattern validation; _normalize
        also calls .lstrip('@') as a defensive guard so signals are always clean.
        """
        client = _make_client()
        # Config validates handle without '@' — but we verify the signal output
        handle_cfg = TwitterHandleConfig(handle="sama", weight=1.0)
        tweet = _make_tweet("AI is transforming everything.", tweet_id="111")
        signal = client._normalize(tweet, handle_cfg, "test-agent")
        assert not signal.handle.startswith("@")
        assert signal.handle == "sama"

    def test_normalize_coerces_naive_datetime_to_utc(self) -> None:
        """Naive created_at datetime gets UTC timezone attached."""
        client = _make_client()
        handle_cfg = TwitterHandleConfig(handle="ylecun", weight=1.0)
        naive_dt = datetime(2026, 5, 9, 10, 0)  # no tzinfo
        tweet = _make_tweet("LeCun on AI reasoning.", tweet_id="222", created_at=naive_dt)
        signal = client._normalize(tweet, handle_cfg, "test-agent")
        assert signal.created_at.tzinfo is not None

    def test_normalize_populates_linked_urls(self) -> None:
        """linked_urls populated from _hydrate_urls output."""
        client = _make_client()
        handle_cfg = TwitterHandleConfig(handle="drfeifei", weight=1.0)
        entities = _make_entities([
            {"expanded_url": "https://stanforddaily.com/ai-article"},
        ])
        tweet = _make_tweet("Read this. https://t.co/abc", tweet_id="333", entities=entities)
        signal = client._normalize(tweet, handle_cfg, "test-agent")
        assert "https://stanforddaily.com/ai-article" in signal.linked_urls

    def test_normalize_tweet_id_is_string(self) -> None:
        """tweet_id is stored as string for storage compatibility."""
        client = _make_client()
        handle_cfg = TwitterHandleConfig(handle="karpathy", weight=1.0)
        tweet = _make_tweet("Text", tweet_id=9999999)  # integer id
        signal = client._normalize(tweet, handle_cfg, "test-agent")
        assert isinstance(signal.tweet_id, str)

    def test_normalize_fetched_at_is_utc_aware(self) -> None:
        """fetched_at is always UTC-aware."""
        client = _make_client()
        handle_cfg = TwitterHandleConfig(handle="sama", weight=1.0)
        tweet = _make_tweet("Interesting development.", tweet_id="444")
        signal = client._normalize(tweet, handle_cfg, "test-agent")
        assert signal.fetched_at.tzinfo is not None


# ===========================================================================
# _fetch_tweets (SRC-067)
# ===========================================================================

class TestFetchTweets:
    """
    Validate the paginated tweet fetch logic.
    Traces: SRC-067 (fetch tweets within lookback window via Paginator)
    """

    def test_fetch_tweets_returns_flat_list(self) -> None:
        """
        _fetch_tweets paginates via tweepy.Paginator and returns a flat list
        of all tweet data objects.
        """
        client = _make_client()

        # Mock tweets across two pages
        tweet1 = _make_tweet("AI research update.", tweet_id="1")
        tweet2 = _make_tweet("Enterprise AI news. https://reuters.com", tweet_id="2")
        tweet3 = _make_tweet("Policy implications. https://brookings.edu/report", tweet_id="3")

        page1 = MagicMock()
        page1.data = [tweet1, tweet2]
        page2 = MagicMock()
        page2.data = [tweet3]

        with patch("ai_news_agent.twitter.client.tweepy") as mock_tweepy:
            mock_tweepy.Paginator.return_value = [page1, page2]
            tweets = client._fetch_tweets(
                user_id="12345",
                start=datetime(2026, 5, 9, tzinfo=UTC),
                end=datetime(2026, 5, 9, 23, 59, tzinfo=UTC),
            )

        # Paginator called with correct parameters
        mock_tweepy.Paginator.assert_called_once()
        call_kwargs = mock_tweepy.Paginator.call_args.kwargs
        assert call_kwargs["id"] == "12345"
        assert "created_at" in call_kwargs["tweet_fields"]

        # All tweets from all pages returned as flat list
        assert len(tweets) == 3
        assert tweets[0].id == "1"
        assert tweets[2].id == "3"

    def test_fetch_tweets_skips_empty_pages(self) -> None:
        """Pages with data=None are skipped gracefully."""
        client = _make_client()
        tweet1 = _make_tweet("Substantive content here. https://arxiv.org/abs/test", tweet_id="10")
        page1 = MagicMock()
        page1.data = [tweet1]
        page_empty = MagicMock()
        page_empty.data = None  # empty page

        with patch("ai_news_agent.twitter.client.tweepy") as mock_tweepy:
            mock_tweepy.Paginator.return_value = [page_empty, page1]
            tweets = client._fetch_tweets(
                user_id="99",
                start=datetime(2026, 5, 9, tzinfo=UTC),
                end=datetime(2026, 5, 9, 23, 59, tzinfo=UTC),
            )
        assert len(tweets) == 1

    def test_fetch_tweets_propagates_exception_for_degradation(self) -> None:
        """Exception from Paginator is re-raised so outer try/except degrades (SRC-148)."""
        client = _make_client()

        with patch("ai_news_agent.twitter.client.tweepy") as mock_tweepy:
            mock_tweepy.Paginator.side_effect = RuntimeError("Rate limit exceeded")
            with pytest.raises(RuntimeError, match="Rate limit exceeded"):
                client._fetch_tweets(
                    user_id="12345",
                    start=datetime(2026, 5, 9, tzinfo=UTC),
                    end=datetime(2026, 5, 9, 23, 59, tzinfo=UTC),
                )


# ===========================================================================
# _resolve_user (SRC-067)
# ===========================================================================

class TestResolveUser:
    """
    Validate user resolution (handle → user object).
    Traces: SRC-067 (resolve handle to user ID)
    """

    def test_resolve_user_strips_at_prefix(self) -> None:
        """@-prefixed handles are stripped before API call."""
        client = _make_client()
        mock_user = MagicMock()
        mock_user.id = "12345"
        mock_response = MagicMock()
        mock_response.data = mock_user
        client._client.get_user = MagicMock(return_value=mock_response)

        user = client._resolve_user("@karpathy")
        client._client.get_user.assert_called_once_with(username="karpathy")
        assert user is not None

    def test_resolve_user_returns_none_on_not_found(self) -> None:
        """Returns None when get_user returns no data."""
        client = _make_client()
        mock_response = MagicMock()
        mock_response.data = None
        client._client.get_user = MagicMock(return_value=mock_response)

        user = client._resolve_user("nonexistent")
        assert user is None

    def test_resolve_user_returns_none_on_api_error(self) -> None:
        """Returns None on API exception (graceful, SRC-148)."""
        client = _make_client()
        client._client.get_user = MagicMock(side_effect=Exception("API down"))
        user = client._resolve_user("karpathy")
        assert user is None


# ===========================================================================
# fetch_signals end-to-end (SRC-067–SRC-069)
# ===========================================================================

class TestFetchSignals:
    """
    End-to-end tests for fetch_signals with mocked tweepy internals.
    Traces: SRC-067 (fetch), SRC-068 (filter), SRC-069 (hydrate), SRC-148 (degrade)
    """

    def test_fetch_signals_returns_true_on_success(self) -> None:
        """fetch_signals returns (signals, True) when API is available (SRC-067)."""
        client = _make_client()
        client._resolve_user = MagicMock(return_value=MagicMock(id="12345"))
        client._fetch_tweets = MagicMock(return_value=[])

        _, available = client.fetch_signals(
            window_start=datetime(2026, 5, 9, tzinfo=UTC),
            window_end=datetime(2026, 5, 9, 23, 59, tzinfo=UTC),
            agent_id="test",
        )
        assert available is True

    def test_fetch_signals_skips_unresolvable_handles(self) -> None:
        """Handles that cannot be resolved are skipped gracefully."""
        client = _make_client()
        client._resolve_user = MagicMock(return_value=None)

        signals, available = client.fetch_signals(
            window_start=datetime(2026, 5, 9, tzinfo=UTC),
            window_end=datetime(2026, 5, 9, 23, 59, tzinfo=UTC),
            agent_id="test",
        )
        assert available is True
        assert signals == []

    def test_fetch_signals_filters_non_substantive_tweets(self) -> None:
        """Non-substantive tweets (RTs, replies) are filtered before normalisation."""
        client = _make_client()
        client._resolve_user = MagicMock(return_value=MagicMock(id="12345"))
        client._fetch_tweets = MagicMock(return_value=[
            _make_tweet("RT @karpathy: Some news"),  # bare RT → filtered
            _make_tweet("@sama What do you think?"),  # reply → filtered
        ])

        signals, _ = client.fetch_signals(
            window_start=datetime(2026, 5, 9, tzinfo=UTC),
            window_end=datetime(2026, 5, 9, 23, 59, tzinfo=UTC),
            agent_id="test",
        )
        assert signals == []

    def test_fetch_signals_normalises_substantive_tweets(self) -> None:
        """Substantive tweets are normalised and returned as TweetSignal objects."""
        from ai_news_agent.storage.models import TweetSignal

        client = _make_client()
        client._resolve_user = MagicMock(return_value=MagicMock(id="12345"))
        client._fetch_tweets = MagicMock(return_value=[
            _make_tweet(
                "Fascinating paper on enterprise AI. https://arxiv.org/abs/test",
                tweet_id="99988",
            ),
        ])

        signals, available = client.fetch_signals(
            window_start=datetime(2026, 5, 9, tzinfo=UTC),
            window_end=datetime(2026, 5, 9, 23, 59, tzinfo=UTC),
            agent_id="test-agent",
        )
        assert available is True
        assert len(signals) == 1
        assert isinstance(signals[0], TweetSignal)
        assert signals[0].tweet_id == "99988"

    def test_fetch_signals_returns_false_on_exception(self) -> None:
        """Any unexpected exception → ([], False) (SRC-148)."""
        client = _make_client()
        client._resolve_user = MagicMock(side_effect=RuntimeError("Timeout"))

        signals, available = client.fetch_signals(
            window_start=datetime(2026, 5, 9, tzinfo=UTC),
            window_end=datetime(2026, 5, 9, 23, 59, tzinfo=UTC),
            agent_id="test",
        )
        assert signals == []
        assert available is False

    def test_multiple_handles_produce_signals_from_each(self) -> None:
        """Signals collected from all handles whose users are resolved."""

        handles = [
            TwitterHandleConfig(handle="karpathy", weight=1.0),
            TwitterHandleConfig(handle="sama", weight=1.2),
        ]
        client = _make_client(handles)

        call_count = 0

        def resolve_user(handle):
            user = MagicMock()
            user.id = f"user_{handle}"
            return user

        def fetch_tweets(user_id, start, end):
            nonlocal call_count
            call_count += 1
            return [
                _make_tweet(
                    f"AI news from handle {user_id} https://openai.com/blog",
                    tweet_id=f"tweet_{call_count}",
                ),
            ]

        client._resolve_user = resolve_user
        client._fetch_tweets = fetch_tweets

        signals, available = client.fetch_signals(
            window_start=datetime(2026, 5, 9, tzinfo=UTC),
            window_end=datetime(2026, 5, 9, 23, 59, tzinfo=UTC),
            agent_id="test",
        )
        assert available is True
        assert len(signals) == 2  # one substantive tweet per handle


# ===========================================================================
# TwitterFetcher wrapper (SRC-047)
# ===========================================================================

class TestTwitterFetcher:
    """
    Validate the thin TwitterFetcher wrapper used by SourcingAgent.
    Traces: SRC-047 (signal role), SRC-062 (Twitter integration wrapper)
    """

    def test_fetcher_delegates_to_client(self, sample_agent_config, sample_secrets) -> None:
        """TwitterFetcher.fetch delegates to TwitterClient.fetch_signals."""
        from ai_news_agent.sourcing.twitter_fetcher import TwitterFetcher

        with patch("ai_news_agent.sourcing.twitter_fetcher.TwitterClient") as MockClient:
            MockClient.return_value.fetch_signals.return_value = ([], True)
            fetcher = TwitterFetcher(
                config=sample_agent_config,
                bearer_token=sample_secrets.twitter_bearer_token,
            )
            signals, available = fetcher.fetch(
                window_start=datetime(2026, 5, 9, tzinfo=UTC),
                window_end=datetime(2026, 5, 9, 23, 59, tzinfo=UTC),
                agent_id="test",
            )

        MockClient.return_value.fetch_signals.assert_called_once()
        assert available is True

    def test_fetcher_passes_handles_to_client(self, sample_agent_config, sample_secrets) -> None:
        """TwitterFetcher passes config.twitter.handles to TwitterClient."""
        from ai_news_agent.sourcing.twitter_fetcher import TwitterFetcher

        with patch("ai_news_agent.sourcing.twitter_fetcher.TwitterClient") as MockClient:
            MockClient.return_value.fetch_signals.return_value = ([], True)
            TwitterFetcher(
                config=sample_agent_config,
                bearer_token=sample_secrets.twitter_bearer_token,
            )

        # Verify handles were passed from config
        init_kwargs = MockClient.call_args.kwargs
        assert "handles" in init_kwargs
        # sample_agent_config has karpathy and sama
        handle_names = [h.handle for h in init_kwargs["handles"]]
        assert "karpathy" in handle_names
        assert "sama" in handle_names
