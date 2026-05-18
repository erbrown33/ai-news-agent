"""
tests/unit/test_twitter_integration.py — Comprehensive Twitter/X integration tests.

Covers the full requirements.md §3.3 Twitter/X integration contract:

  Authentication & initialisation (SRC-063, SRC-064, SRC-065):
    - tweepy.Client constructed with bearer token + wait_on_rate_limit=True
    - Empty/missing bearer token → client disabled, graceful degradation
    - tweepy not installed → client disabled, graceful degradation
    - DegradationReason constants distinguish root causes for operators

  Fetch pipeline per handle (SRC-067):
    - Resolve handle → user ID via get_user (strips leading @)
    - Page through get_users_tweets with start_time/end_time
    - Paginator collects all pages, empty pages skipped
    - Multiple handles each produce signals independently
    - Unresolvable handles skipped; other handles continue

  Substantive post filtering (SRC-068):
    - SKIP pure replies (@mention as first non-whitespace token)
    - SKIP bare retweets (text starts with "RT @")
    - KEEP any tweet with a URL regardless of length
    - KEEP original tweet ≥ 50 chars without URL
    - Boundary: exactly 50 chars → kept; 49 chars → filtered
    - Mixed pages: only substantive tweets are normalised

  URL hydration (SRC-069):
    - expanded_url returned from entities.urls
    - twitter.com / x.com self-referential links excluded
    - Missing entities → empty list (graceful)
    - Empty entities.urls → empty list
    - Duplicate expanded_url → deduplicated
    - Fallback to url field when expanded_url is None

  Signal normalisation (SRC-011, SRC-046):
    - TweetSignal fields populated correctly (tweet_id, handle, text, weight)
    - tweet_id stored as str even when tweepy returns int
    - handle stored without "@" prefix
    - created_at naive datetime → UTC tz attached
    - linked_urls populated from _hydrate_urls output
    - fetched_at is UTC-aware

  Handle weight ordering in prompt section (SRC-046, SRC-119):
    - Signals sorted by weight descending in _format_twitter_section
    - Higher-weight handle appears first in prompt output

  Graceful degradation (SRC-148):
    - tweepy not installed → ([], False)
    - Empty bearer token → ([], False)
    - TweepyException raised → ([], False)
    - tweepy.errors.TooManyRequests (HTTP 429) → ([], False) with rate-limit log
    - tweepy.errors.Unauthorized (HTTP 401) → ([], False) with auth-error log
    - RuntimeError (non-tweepy) → ([], False)
    - _resolve_user failure → outer handler → ([], False)
    - _fetch_tweets failure → outer handler → ([], False)
    - fetch_signals returns True on clean run even with 0 signals

  Prompt builder Twitter section (SRC-119, SRC-148):
    - Signals list → labeled Influencer Signal section with IMPORTANT note
    - Empty signals + API available → "no substantive posts" message
    - Empty signals + API unavailable → "API was unreachable" warning
    - Signals sorted by weight in rendered output
    - Linked URLs included for each signal (up to 3)
    - Signal text truncated to 280 chars in prompt

  Digest degradation note in rendered Markdown (SRC-148):
    - twitter_degradation_note set in CurationRunResult → blockquote in MD
    - No degradation note when Twitter signals present
    - Degradation note text contains SRC-148 reference

  TwitterFetcher thin wrapper (SRC-047, SRC-062):
    - Delegates to TwitterClient.fetch_signals
    - Passes config.twitter.handles to client
    - Bearer token forwarded from secrets

  SourcingAgent integration (SRC-008–SRC-013, SRC-148, SRC-150):
    - twitter_signal_available=False in result when Twitter down
    - Web articles still inserted when Twitter down (sourcing continues)
    - tweet_api_call_count=0 when Twitter unavailable
    - Tweet signals stored via insert_tweet_signal
    - Tweet-linked URLs fed into web article enrichment pipeline
    - Duplicate tweet_id signals not re-inserted

Traces: SRC-011, SRC-012, SRC-036–SRC-047, SRC-048, SRC-062–SRC-070,
        SRC-098 (mocked tweepy), SRC-119, SRC-148, SRC-150
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from ai_news_agent.config.models import TwitterHandleConfig
from ai_news_agent.curation.prompt_builder import _format_twitter_section
from ai_news_agent.twitter.client import (
    _MAX_RESULTS_PER_PAGE,
    _MIN_TWEET_CHARS,
    DegradationReason,
    TwitterClient,
)

# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------

_WINDOW_START = datetime(2026, 5, 9, 0, 0, tzinfo=UTC)
_WINDOW_END = datetime(2026, 5, 9, 23, 59, tzinfo=UTC)


_DEFAULT_HANDLES = [TwitterHandleConfig(handle="karpathy", weight=1.0)]


def _make_client(
    handles: list[TwitterHandleConfig] | None = None,
    bearer_token: str = "test-bearer",
) -> TwitterClient:
    """
    Build a TwitterClient with a mocked tweepy.Client.
    No real HTTP requests are made — tweepy is fully patched.

    Pass ``handles=[]`` explicitly for an empty-handle client.
    ``handles=None`` (default) uses [karpathy].
    """
    resolved_handles = _DEFAULT_HANDLES if handles is None else handles
    with patch("ai_news_agent.twitter.client.tweepy") as mock_tweepy:
        mock_tweepy.Client.return_value = MagicMock()
        client = TwitterClient(bearer_token=bearer_token, handles=resolved_handles)
    return client


def _make_tweet(
    text: str,
    tweet_id: str | int = "123",
    created_at: datetime | None = None,
    entities: Any = None,
) -> MagicMock:
    """Build a mock tweepy Tweet data object."""
    tweet = MagicMock()
    tweet.text = text
    tweet.id = tweet_id
    tweet.created_at = created_at or datetime(2026, 5, 9, 10, 0, tzinfo=UTC)
    tweet.entities = entities
    return tweet


def _make_entities(urls: list[dict]) -> MagicMock:
    """Build a mock entities object with a list of URL entry mocks."""
    entities = MagicMock()
    url_objects = []
    for u in urls:
        url_obj = MagicMock()
        url_obj.expanded_url = u.get("expanded_url", "")
        url_obj.url = u.get("url", "")
        url_objects.append(url_obj)
    entities.urls = url_objects
    return entities


def _make_page(tweets: list | None) -> MagicMock:
    """Build a mock Paginator page."""
    page = MagicMock()
    page.data = tweets
    return page


# ===========================================================================
# 1. Authentication & initialisation (SRC-063, SRC-064, SRC-065)
# ===========================================================================


class TestClientInitialisation:
    """
    Validate TwitterClient construction and bearer-token handling.
    Traces: SRC-063 (tweepy library), SRC-064 (bearer token env var),
            SRC-065 (API tier awareness)
    """

    def test_tweepy_client_constructed_with_bearer_token_and_rate_limit(self) -> None:
        """tweepy.Client(bearer_token=..., wait_on_rate_limit=True) called on init."""
        handles = [TwitterHandleConfig(handle="karpathy", weight=1.0)]
        with patch("ai_news_agent.twitter.client.tweepy") as mock_tweepy:
            mock_tweepy.Client.return_value = MagicMock()
            TwitterClient(bearer_token="my-token", handles=handles)
        mock_tweepy.Client.assert_called_once_with(
            bearer_token="my-token",
            wait_on_rate_limit=True,
        )

    def test_empty_bearer_token_disables_client(self) -> None:
        """Empty bearer token → _client is None (SRC-064 graceful degradation)."""
        handles = [TwitterHandleConfig(handle="karpathy", weight=1.0)]
        with patch("ai_news_agent.twitter.client.tweepy"):
            client = TwitterClient(bearer_token="", handles=handles)
        assert client._client is None

    def test_empty_bearer_token_sets_degradation_reason(self) -> None:
        """Empty bearer token sets degradation reason to BEARER_TOKEN_MISSING."""
        handles = [TwitterHandleConfig(handle="karpathy", weight=1.0)]
        with patch("ai_news_agent.twitter.client.tweepy"):
            client = TwitterClient(bearer_token="", handles=handles)
        assert client._degradation_reason == DegradationReason.BEARER_TOKEN_MISSING

    def test_empty_bearer_token_fetch_signals_returns_false(self) -> None:
        """fetch_signals returns ([], False) immediately when bearer token is empty."""
        handles = [TwitterHandleConfig(handle="karpathy", weight=1.0)]
        with patch("ai_news_agent.twitter.client.tweepy"):
            client = TwitterClient(bearer_token="", handles=handles)
        signals, available = client.fetch_signals(
            window_start=_WINDOW_START,
            window_end=_WINDOW_END,
            agent_id="test",
        )
        assert signals == []
        assert available is False

    def test_tweepy_not_available_disables_client(self) -> None:
        """If _TWEEPY_AVAILABLE is False, _client is None (SRC-148)."""
        handles = [TwitterHandleConfig(handle="karpathy", weight=1.0)]
        with (
            patch("ai_news_agent.twitter.client._TWEEPY_AVAILABLE", False),
            patch("ai_news_agent.twitter.client.tweepy", None),
        ):
            client = TwitterClient(bearer_token="token", handles=handles)
        assert client._client is None

    def test_tweepy_not_available_sets_degradation_reason(self) -> None:
        """Absent tweepy sets DegradationReason.TWEEPY_NOT_INSTALLED."""
        handles = [TwitterHandleConfig(handle="karpathy", weight=1.0)]
        with (
            patch("ai_news_agent.twitter.client._TWEEPY_AVAILABLE", False),
            patch("ai_news_agent.twitter.client.tweepy", None),
        ):
            client = TwitterClient(bearer_token="token", handles=handles)
        assert client._degradation_reason == DegradationReason.TWEEPY_NOT_INSTALLED

    def test_valid_token_sets_no_degradation_reason(self) -> None:
        """Valid bearer token → _degradation_reason is None (no pre-init degradation)."""
        client = _make_client()
        assert client._degradation_reason is None

    def test_handles_stored_for_iteration(self) -> None:
        """All configured handles are stored in _handles for per-handle fetching."""
        handles = [
            TwitterHandleConfig(handle="karpathy", weight=1.0),
            TwitterHandleConfig(handle="sama", weight=1.5),
            TwitterHandleConfig(handle="ylecun", weight=0.8),
        ]
        client = _make_client(handles)
        assert len(client._handles) == 3

    def test_degradation_reason_constants_are_distinct(self) -> None:
        """All DegradationReason values are non-empty strings and mutually distinct."""
        reasons = [
            DegradationReason.TWEEPY_NOT_INSTALLED,
            DegradationReason.BEARER_TOKEN_MISSING,
            DegradationReason.RATE_LIMIT,
            DegradationReason.UNAUTHORIZED,
            DegradationReason.TWEEPY_API_ERROR,
            DegradationReason.UNEXPECTED_ERROR,
        ]
        assert len(set(reasons)) == len(reasons), "DegradationReason values must be unique"
        assert all(isinstance(r, str) and r for r in reasons)


# ===========================================================================
# 2. Substantive post filtering (SRC-068)
# ===========================================================================


class TestSubstantiveFiltering:
    """
    Full coverage of the SRC-068 filter rules.
    Traces: SRC-068 (skip pure replies, bare RTs, short tweets without link)
    """

    def _client(self) -> TwitterClient:
        return _make_client()

    # -- Pure reply rules --

    def test_pure_reply_at_start_filtered(self) -> None:
        """@mention as first token → filtered (SRC-068)."""
        client = self._client()
        assert client._is_substantive(_make_tweet("@karpathy Great point!")) is False

    def test_reply_with_leading_whitespace_filtered(self) -> None:
        """Whitespace before @mention still filtered (SRC-068)."""
        client = self._client()
        assert client._is_substantive(_make_tweet("  @sama This is interesting.")) is False

    def test_reply_with_tab_prefix_filtered(self) -> None:
        """Tab character before @mention → filtered."""
        client = self._client()
        assert client._is_substantive(_make_tweet("\t@ylecun Right?")) is False

    def test_tweet_mentioning_handle_mid_sentence_is_kept(self) -> None:
        """@mention appearing mid-sentence is NOT a pure reply → kept (SRC-068)."""
        client = self._client()
        tweet = _make_tweet(
            "As @karpathy said, transformers are fundamentally about attention mechanisms."
        )
        assert client._is_substantive(tweet) is True

    # -- Bare retweet rules --

    def test_bare_rt_filtered(self) -> None:
        """'RT @' prefix → filtered (SRC-068)."""
        client = self._client()
        assert client._is_substantive(_make_tweet("RT @karpathy: AI paper link")) is False

    def test_rt_without_at_sign_is_kept(self) -> None:
        """'RT' without '@' is not a bare retweet → kept if long enough."""
        client = self._client()
        text = (
            "RTFM is good advice for AI engineers who want to understand transformer architecture."
        )
        assert client._is_substantive(_make_tweet(text)) is True

    # -- URL-containing tweets always kept --

    def test_short_tweet_with_url_kept(self) -> None:
        """Any URL in tweet → kept regardless of length (SRC-068)."""
        client = self._client()
        assert client._is_substantive(_make_tweet("AI https://openai.com/blog")) is True

    def test_two_word_tweet_with_url_kept(self) -> None:
        """Minimal content + URL → kept."""
        client = self._client()
        assert client._is_substantive(_make_tweet("Read: https://reuters.com/ai")) is True

    def test_http_url_kept(self) -> None:
        """http:// URLs (not just https://) trigger the URL rule."""
        client = self._client()
        assert client._is_substantive(_make_tweet("Link: http://techcrunch.com/article")) is True

    # -- Length threshold --

    def test_exactly_50_chars_kept(self) -> None:
        """Tweet of exactly _MIN_TWEET_CHARS characters → kept (boundary, SRC-068)."""
        client = self._client()
        assert client._is_substantive(_make_tweet("A" * _MIN_TWEET_CHARS)) is True

    def test_49_chars_filtered(self) -> None:
        """Tweet of _MIN_TWEET_CHARS - 1 characters → filtered (just below threshold)."""
        client = self._client()
        assert client._is_substantive(_make_tweet("A" * (_MIN_TWEET_CHARS - 1))) is False

    def test_long_substantive_tweet_kept(self) -> None:
        """Long original tweet without URL → kept."""
        client = self._client()
        text = (
            "The latest AI governance proposals from Brussels represent a significant "
            "shift in regulatory philosophy toward capability-based oversight."
        )
        assert len(text) >= _MIN_TWEET_CHARS
        assert client._is_substantive(_make_tweet(text)) is True

    def test_empty_text_filtered(self) -> None:
        """Empty tweet text → filtered (length < 50, no URL)."""
        client = self._client()
        assert client._is_substantive(_make_tweet("")) is False

    def test_whitespace_only_filtered(self) -> None:
        """Whitespace-only tweet → filtered."""
        client = self._client()
        assert client._is_substantive(_make_tweet("   \t\n  ")) is False

    def test_quote_tweet_with_original_content_kept(self) -> None:
        """
        Quote-tweet (own text first, then RT content) is NOT a bare retweet → kept.
        A quoted tweet leads with the author's own words; the 'RT @' rule does not apply
        if 'RT @' is not the very first token.
        """
        client = self._client()
        tweet = _make_tweet(
            "This matters because AI safety is now a boardroom issue. "
            "https://nytimes.com/ai-safety RT @darioa great piece"
        )
        assert client._is_substantive(tweet) is True


# ===========================================================================
# 3. URL hydration (SRC-069)
# ===========================================================================


class TestUrlHydration:
    """
    Validate t.co → canonical URL expansion from tweet entities.
    Traces: SRC-069 (hydrate linked URLs)
    """

    def _client(self) -> TwitterClient:
        return _make_client()

    def test_expanded_url_returned(self) -> None:
        """entities.urls[].expanded_url values extracted (SRC-069)."""
        client = self._client()
        entities = _make_entities(
            [
                {"expanded_url": "https://reuters.com/ai-article", "url": "https://t.co/abc"},
            ]
        )
        tweet = _make_tweet("Check this https://t.co/abc", entities=entities)
        urls = client._hydrate_urls(tweet)
        assert "https://reuters.com/ai-article" in urls

    def test_multiple_urls_extracted(self) -> None:
        """Multiple expanded URLs all extracted."""
        client = self._client()
        entities = _make_entities(
            [
                {"expanded_url": "https://reuters.com/a"},
                {"expanded_url": "https://bloomberg.com/b"},
            ]
        )
        tweet = _make_tweet("text", entities=entities)
        urls = client._hydrate_urls(tweet)
        assert len(urls) == 2

    def test_twitter_self_links_excluded(self) -> None:
        """twitter.com and x.com links filtered out (media cards, profiles)."""
        client = self._client()
        entities = _make_entities(
            [
                {"expanded_url": "https://twitter.com/karpathy/status/123"},
                {"expanded_url": "https://x.com/sama/photo/1"},
                {"expanded_url": "https://reuters.com/article"},
            ]
        )
        tweet = _make_tweet("text", entities=entities)
        urls = client._hydrate_urls(tweet)
        assert urls == ["https://reuters.com/article"]

    def test_twitter_subdomain_excluded(self) -> None:
        """Subdomains of twitter.com also excluded."""
        client = self._client()
        entities = _make_entities(
            [
                {"expanded_url": "https://pic.twitter.com/abc123"},
                {"expanded_url": "https://techcrunch.com/article"},
            ]
        )
        tweet = _make_tweet("text", entities=entities)
        urls = client._hydrate_urls(tweet)
        assert "https://techcrunch.com/article" in urls
        for url in urls:
            assert "twitter.com" not in url

    def test_no_entities_returns_empty_list(self) -> None:
        """No entities block → empty list (graceful, SRC-069)."""
        client = self._client()
        tweet = _make_tweet("No entities here.", entities=None)
        assert client._hydrate_urls(tweet) == []

    def test_entities_with_empty_urls_list_returns_empty(self) -> None:
        """entities.urls = [] → empty result."""
        client = self._client()
        entities = MagicMock()
        entities.urls = []
        tweet = _make_tweet("text", entities=entities)
        assert client._hydrate_urls(tweet) == []

    def test_entities_urls_none_returns_empty(self) -> None:
        """entities.urls = None → empty result (defensive null-check)."""
        client = self._client()
        entities = MagicMock()
        entities.urls = None
        tweet = _make_tweet("text", entities=entities)
        assert client._hydrate_urls(tweet) == []

    def test_duplicate_expanded_urls_deduplicated(self) -> None:
        """Same URL appearing twice in entities → appears once (SRC-012)."""
        client = self._client()
        entities = _make_entities(
            [
                {"expanded_url": "https://reuters.com/article"},
                {"expanded_url": "https://reuters.com/article"},
            ]
        )
        tweet = _make_tweet("text", entities=entities)
        urls = client._hydrate_urls(tweet)
        assert len(urls) == 1
        assert urls[0] == "https://reuters.com/article"

    def test_fallback_to_url_field_when_expanded_url_none(self) -> None:
        """If expanded_url is None, url field used as fallback (SRC-069)."""
        client = self._client()
        entities = MagicMock()
        url_obj = MagicMock()
        url_obj.expanded_url = None
        url_obj.url = "https://wired.com/story/ai-regulation"
        entities.urls = [url_obj]
        tweet = _make_tweet("text", entities=entities)
        urls = client._hydrate_urls(tweet)
        assert "https://wired.com/story/ai-regulation" in urls

    def test_both_expanded_and_fallback_none_skipped(self) -> None:
        """URL entry with both expanded_url=None and url=None → skipped gracefully."""
        client = self._client()
        entities = MagicMock()
        url_obj = MagicMock()
        url_obj.expanded_url = None
        url_obj.url = None
        entities.urls = [url_obj]
        tweet = _make_tweet("text", entities=entities)
        urls = client._hydrate_urls(tweet)
        assert urls == []

    def test_case_insensitive_twitter_domain_exclusion(self) -> None:
        """Twitter.com (mixed-case) still excluded."""
        client = self._client()
        entities = _make_entities(
            [
                {"expanded_url": "HTTPS://TWITTER.COM/user/status/123"},
                {"expanded_url": "https://reuters.com/article"},
            ]
        )
        tweet = _make_tweet("text", entities=entities)
        urls = client._hydrate_urls(tweet)
        assert urls == ["https://reuters.com/article"]


# ===========================================================================
# 4. Signal normalisation (SRC-011, SRC-046)
# ===========================================================================


class TestNormalisation:
    """
    Validate TweetSignal production from raw tweet + config.
    Traces: SRC-011 (storage fields), SRC-046 (handle weight)
    """

    def test_all_fields_populated(self) -> None:
        """All TweetSignal fields correctly populated from tweet + config."""
        from ai_news_agent.storage.models import TweetSignal

        client = _make_client()
        handle_cfg = TwitterHandleConfig(handle="drfeifei", weight=2.0)
        created = datetime(2026, 5, 9, 12, 30, tzinfo=UTC)
        entities = _make_entities([{"expanded_url": "https://hai.stanford.edu/news"}])
        tweet = _make_tweet(
            "AI safety research from Stanford HAI. https://t.co/test",
            tweet_id="9000000001",
            created_at=created,
            entities=entities,
        )

        signal = client._normalize(tweet, handle_cfg, "agent-x")

        assert isinstance(signal, TweetSignal)
        assert signal.tweet_id == "9000000001"
        assert signal.handle == "drfeifei"
        assert signal.weight == 2.0
        assert signal.agent_id == "agent-x"
        assert signal.created_at == created
        assert "https://hai.stanford.edu/news" in signal.linked_urls

    def test_tweet_id_always_string(self) -> None:
        """tweet_id is cast to str even when tweepy returns an integer."""
        client = _make_client()
        handle_cfg = TwitterHandleConfig(handle="karpathy", weight=1.0)
        tweet = _make_tweet("Substantive content here.", tweet_id=1234567890)
        signal = client._normalize(tweet, handle_cfg, "agent")
        assert isinstance(signal.tweet_id, str)
        assert signal.tweet_id == "1234567890"

    def test_handle_stored_without_at_prefix(self) -> None:
        """TweetSignal.handle never starts with '@' (SRC-036 pattern)."""
        client = _make_client()
        handle_cfg = TwitterHandleConfig(handle="sama", weight=1.0)
        tweet = _make_tweet("AI is transforming everything.", tweet_id="111")
        signal = client._normalize(tweet, handle_cfg, "agent")
        assert not signal.handle.startswith("@")
        assert signal.handle == "sama"

    def test_naive_created_at_gets_utc_tz(self) -> None:
        """Naive datetime from created_at → UTC timezone attached."""
        client = _make_client()
        handle_cfg = TwitterHandleConfig(handle="ylecun", weight=1.0)
        naive_dt = datetime(2026, 5, 9, 10, 0)  # no tzinfo
        tweet = _make_tweet("Interesting AI reasoning paper.", tweet_id="222", created_at=naive_dt)
        signal = client._normalize(tweet, handle_cfg, "agent")
        assert signal.created_at.tzinfo is not None

    def test_none_created_at_defaults_to_now_utc(self) -> None:
        """None created_at → datetime.now(UTC) is used."""
        client = _make_client()
        handle_cfg = TwitterHandleConfig(handle="sama", weight=1.0)
        tweet = _make_tweet("Important AI announcement.", tweet_id="333")
        tweet.created_at = None
        signal = client._normalize(tweet, handle_cfg, "agent")
        assert signal.created_at.tzinfo is not None

    def test_fetched_at_is_utc_aware(self) -> None:
        """fetched_at on TweetSignal is always UTC-aware."""
        client = _make_client()
        handle_cfg = TwitterHandleConfig(handle="karpathy", weight=1.0)
        tweet = _make_tweet("Something interesting.", tweet_id="444")
        signal = client._normalize(tweet, handle_cfg, "agent")
        assert signal.fetched_at.tzinfo is not None

    def test_weight_from_handle_config(self) -> None:
        """Weight on TweetSignal comes from TwitterHandleConfig.weight (SRC-046)."""
        client = _make_client()
        handle_cfg = TwitterHandleConfig(handle="fchollet", weight=0.5)
        tweet = _make_tweet("Deep learning research note.", tweet_id="555")
        signal = client._normalize(tweet, handle_cfg, "agent")
        assert signal.weight == 0.5

    def test_linked_urls_empty_when_no_entities(self) -> None:
        """Signal with no URL entities has empty linked_urls list."""
        client = _make_client()
        handle_cfg = TwitterHandleConfig(handle="karpathy", weight=1.0)
        tweet = _make_tweet(
            "A long and substantive thought about AI policy implications.", tweet_id="666"
        )
        tweet.entities = None
        signal = client._normalize(tweet, handle_cfg, "agent")
        assert signal.linked_urls == []


# ===========================================================================
# 5. User resolution (SRC-067)
# ===========================================================================


class TestUserResolution:
    """
    Validate handle → user ID resolution via get_user.
    Traces: SRC-067 (resolve each configured handle)
    """

    def test_strips_at_prefix_before_api_call(self) -> None:
        """@-prefixed handle → '@' stripped before calling get_user (SRC-067)."""
        client = _make_client()
        mock_user = MagicMock()
        mock_user.id = "99999"
        mock_resp = MagicMock()
        mock_resp.data = mock_user
        client._client.get_user = MagicMock(return_value=mock_resp)

        result = client._resolve_user("@karpathy")

        client._client.get_user.assert_called_once_with(username="karpathy")
        assert result is not None

    def test_no_at_prefix_handled(self) -> None:
        """Handle without '@' passes through unchanged."""
        client = _make_client()
        mock_user = MagicMock()
        mock_user.id = "88888"
        mock_resp = MagicMock()
        mock_resp.data = mock_user
        client._client.get_user = MagicMock(return_value=mock_resp)

        result = client._resolve_user("sama")
        client._client.get_user.assert_called_once_with(username="sama")
        assert result is not None

    def test_not_found_returns_none(self) -> None:
        """Response with data=None → None returned (user not found)."""
        client = _make_client()
        mock_resp = MagicMock()
        mock_resp.data = None
        client._client.get_user = MagicMock(return_value=mock_resp)
        assert client._resolve_user("nonexistent_handle") is None

    def test_none_response_returns_none(self) -> None:
        """get_user returns None → gracefully returns None."""
        client = _make_client()
        client._client.get_user = MagicMock(return_value=None)
        assert client._resolve_user("karpathy") is None

    def test_api_error_returns_none(self) -> None:
        """Exception from get_user → None (degraded gracefully, SRC-148)."""
        client = _make_client()
        client._client.get_user = MagicMock(side_effect=Exception("API error"))
        assert client._resolve_user("karpathy") is None


# ===========================================================================
# 6. Paginated tweet fetching (SRC-067)
# ===========================================================================


class TestFetchTweets:
    """
    Validate paginated tweet fetch via tweepy.Paginator.
    Traces: SRC-067 (fetch all pages within lookback window)
    """

    def test_all_pages_collected_as_flat_list(self) -> None:
        """Paginator pages combined into a single flat list."""
        client = _make_client()

        tweet1 = _make_tweet("Page 1 tweet A.", tweet_id="1")
        tweet2 = _make_tweet("Page 1 tweet B. https://reuters.com", tweet_id="2")
        tweet3 = _make_tweet("Page 2 tweet A. https://bloomberg.com", tweet_id="3")

        pages = [_make_page([tweet1, tweet2]), _make_page([tweet3])]

        with patch("ai_news_agent.twitter.client.tweepy") as mock_tweepy:
            mock_tweepy.Paginator.return_value = pages
            tweets = client._fetch_tweets(
                user_id="12345",
                start=_WINDOW_START,
                end=_WINDOW_END,
            )

        assert len(tweets) == 3
        ids = [t.id for t in tweets]
        assert "1" in ids
        assert "2" in ids
        assert "3" in ids

    def test_paginator_called_with_correct_params(self) -> None:
        """Paginator called with correct fields, expansions, and time window."""
        client = _make_client()

        with patch("ai_news_agent.twitter.client.tweepy") as mock_tweepy:
            mock_tweepy.Paginator.return_value = []
            client._fetch_tweets(
                user_id="55555",
                start=_WINDOW_START,
                end=_WINDOW_END,
            )

        call_kwargs = mock_tweepy.Paginator.call_args.kwargs
        assert call_kwargs["id"] == "55555"
        assert call_kwargs["start_time"] == _WINDOW_START
        assert call_kwargs["end_time"] == _WINDOW_END
        assert "created_at" in call_kwargs["tweet_fields"]
        assert "entities" in call_kwargs["tweet_fields"]
        assert call_kwargs["max_results"] == _MAX_RESULTS_PER_PAGE

    def test_empty_pages_skipped_gracefully(self) -> None:
        """Pages with data=None are skipped; valid pages processed normally."""
        client = _make_client()
        tweet1 = _make_tweet("Substantive tweet. https://openai.com", tweet_id="10")
        pages = [_make_page(None), _make_page([tweet1]), _make_page(None)]

        with patch("ai_news_agent.twitter.client.tweepy") as mock_tweepy:
            mock_tweepy.Paginator.return_value = pages
            tweets = client._fetch_tweets("99", _WINDOW_START, _WINDOW_END)

        assert len(tweets) == 1
        assert tweets[0].id == "10"

    def test_exception_propagates_for_outer_degradation_handler(self) -> None:
        """Exception from Paginator is re-raised so outer handler classifies it (SRC-148)."""
        client = _make_client()

        with patch("ai_news_agent.twitter.client.tweepy") as mock_tweepy:
            mock_tweepy.Paginator.side_effect = RuntimeError("Connection timeout")
            with pytest.raises(RuntimeError, match="Connection timeout"):
                client._fetch_tweets("12345", _WINDOW_START, _WINDOW_END)

    def test_large_window_uses_paginator_not_single_call(self) -> None:
        """Paginator is always used — even a one-page result goes through Paginator."""
        client = _make_client()

        tweet = _make_tweet("Interesting AI news. https://techcrunch.com", tweet_id="1")

        with patch("ai_news_agent.twitter.client.tweepy") as mock_tweepy:
            mock_tweepy.Paginator.return_value = [_make_page([tweet])]
            tweets = client._fetch_tweets("12345", _WINDOW_START, _WINDOW_END)

        mock_tweepy.Paginator.assert_called_once()
        assert len(tweets) == 1


# ===========================================================================
# 7. Graceful degradation (SRC-148)
# ===========================================================================


class TestGracefulDegradation:
    """
    Validate all degradation paths — every error type returns ([], False).
    Traces: SRC-065 (rate-limit tier note), SRC-148 (graceful degradation)
    """

    def test_generic_exception_degrades(self) -> None:
        """Any exception in fetch_signals → ([], False) (SRC-148)."""
        client = _make_client()
        client._resolve_user = MagicMock(side_effect=Exception("Generic failure"))
        signals, available = client.fetch_signals(_WINDOW_START, _WINDOW_END, "test")
        assert signals == []
        assert available is False

    def test_runtime_error_degrades(self) -> None:
        """RuntimeError (non-tweepy) → ([], False) via UNEXPECTED_ERROR path."""
        client = _make_client()
        client._resolve_user = MagicMock(side_effect=RuntimeError("Timeout"))
        signals, available = client.fetch_signals(_WINDOW_START, _WINDOW_END, "test")
        assert signals == []
        assert available is False

    def test_value_error_degrades(self) -> None:
        """ValueError → ([], False), sourcing run continues unaffected (SRC-148)."""
        client = _make_client()
        client._resolve_user = MagicMock(side_effect=ValueError("Bad response"))
        signals, available = client.fetch_signals(_WINDOW_START, _WINDOW_END, "test")
        assert signals == []
        assert available is False

    def test_rate_limit_exception_via_handle_api_exception(self) -> None:
        """
        tweepy.errors.TooManyRequests → ([], False) with RATE_LIMIT degradation reason.
        The rate-limit path logs a specific message noting the API tier (SRC-065).
        We mock a TooManyRequests exception class on tweepy.errors.
        """
        client = _make_client()

        # Create a mock TooManyRequests exception that isinstance-checks pass.
        # Named with "Error" suffix to comply with N818 (exception naming convention).
        class FakeTooManyRequestsError(Exception):
            pass

        class FakeErrors:
            TooManyRequests = FakeTooManyRequestsError
            Unauthorized = None

        fake_tweepy = MagicMock()
        fake_tweepy.errors = FakeErrors()
        fake_tweepy.TweepyException = Exception  # base

        with (
            patch("ai_news_agent.twitter.client.tweepy", fake_tweepy),
            patch("ai_news_agent.twitter.client._TWEEPY_AVAILABLE", True),
        ):
            exc = FakeTooManyRequestsError("Rate limit exceeded")
            signals, available = client._handle_api_exception(exc, "test-agent")

        assert signals == []
        assert available is False

    def test_unauthorized_exception_via_handle_api_exception(self) -> None:
        """
        tweepy.errors.Unauthorized → ([], False).
        Logs an auth-error message pointing to bearer token rotation (SRC-064).
        """
        client = _make_client()

        class FakeUnauthorizedError(Exception):
            pass

        class FakeErrors:
            TooManyRequests = None
            Unauthorized = FakeUnauthorizedError

        fake_tweepy = MagicMock()
        fake_tweepy.errors = FakeErrors()
        fake_tweepy.TweepyException = Exception  # base

        with (
            patch("ai_news_agent.twitter.client.tweepy", fake_tweepy),
            patch("ai_news_agent.twitter.client._TWEEPY_AVAILABLE", True),
        ):
            exc = FakeUnauthorizedError("Invalid bearer token")
            signals, available = client._handle_api_exception(exc, "test-agent")

        assert signals == []
        assert available is False

    def test_tweepy_exception_via_handle_api_exception(self) -> None:
        """
        tweepy.TweepyException (non rate-limit, non-auth) → ([], False).
        Degradation reason: TWEEPY_API_ERROR.
        """
        client = _make_client()

        class FakeTweepyExceptionError(Exception):
            pass

        class FakeErrors:
            TooManyRequests = None
            Unauthorized = None

        fake_tweepy = MagicMock()
        fake_tweepy.errors = FakeErrors()
        fake_tweepy.TweepyException = FakeTweepyExceptionError

        with (
            patch("ai_news_agent.twitter.client.tweepy", fake_tweepy),
            patch("ai_news_agent.twitter.client._TWEEPY_AVAILABLE", True),
        ):
            exc = FakeTweepyExceptionError("API error")
            signals, available = client._handle_api_exception(exc, "test-agent")

        assert signals == []
        assert available is False

    def test_unresolvable_handle_skipped_not_propagated(self) -> None:
        """
        When _resolve_user returns None (not an exception), the handle is skipped
        but fetch_signals continues and returns ([], True) — API is still available.
        """
        client = _make_client()
        client._resolve_user = MagicMock(return_value=None)
        signals, available = client.fetch_signals(_WINDOW_START, _WINDOW_END, "test")
        assert available is True  # API worked; user just not found
        assert signals == []

    def test_multiple_handles_one_fails_resolution(self) -> None:
        """
        When the first handle fails to resolve but the second succeeds, signals
        from the second handle are returned (available=True).
        """
        handles = [
            TwitterHandleConfig(handle="ghost_user", weight=1.0),
            TwitterHandleConfig(handle="karpathy", weight=1.0),
        ]
        client = _make_client(handles)

        call_count = [0]

        def selective_resolve(handle: str):
            call_count[0] += 1
            if handle == "ghost_user":
                return None  # not found
            user = MagicMock()
            user.id = f"user_{handle}"
            return user

        client._resolve_user = selective_resolve
        client._fetch_tweets = MagicMock(
            return_value=[
                _make_tweet(
                    "AI research update for the week. https://arxiv.org/abs/test",
                    tweet_id="9001",
                )
            ]
        )

        signals, available = client.fetch_signals(_WINDOW_START, _WINDOW_END, "test")

        assert available is True
        assert len(signals) == 1
        assert signals[0].handle == "karpathy"

    def test_all_tweets_non_substantive_returns_empty_but_true(self) -> None:
        """
        If all tweets for a handle fail the substantive filter, signals = [] but
        twitter_available = True (API worked; tweets just weren't newsworthy).
        """
        client = _make_client()
        client._resolve_user = MagicMock(return_value=MagicMock(id="12345"))
        client._fetch_tweets = MagicMock(
            return_value=[
                _make_tweet("RT @someone: retweet"),  # bare RT
                _make_tweet("@reply yes exactly"),  # pure reply
                _make_tweet("lol"),  # too short
            ]
        )

        signals, available = client.fetch_signals(_WINDOW_START, _WINDOW_END, "test")

        assert available is True
        assert signals == []

    def test_fetch_signals_propagates_degraded_reason_on_client_none(self) -> None:
        """
        When _client is None (empty token), degradation_reason is included in
        the log output. We verify _degradation_reason is set correctly.
        """
        handles = [TwitterHandleConfig(handle="karpathy", weight=1.0)]
        with patch("ai_news_agent.twitter.client.tweepy"):
            client = TwitterClient(bearer_token="", handles=handles)

        assert client._degradation_reason == DegradationReason.BEARER_TOKEN_MISSING
        signals, available = client.fetch_signals(_WINDOW_START, _WINDOW_END, "test")
        assert signals == []
        assert available is False


# ===========================================================================
# 8. End-to-end fetch_signals (SRC-067, SRC-068, SRC-069)
# ===========================================================================


class TestFetchSignalsEndToEnd:
    """
    Full end-to-end tests for fetch_signals with mocked tweepy internals.
    Traces: SRC-067 (fetch), SRC-068 (filter), SRC-069 (hydrate), SRC-148 (degrade)
    """

    def test_substantive_tweets_returned_as_signals(self) -> None:
        """Substantive tweet → TweetSignal in result list."""
        from ai_news_agent.storage.models import TweetSignal

        client = _make_client()
        client._resolve_user = MagicMock(return_value=MagicMock(id="12345"))
        client._fetch_tweets = MagicMock(
            return_value=[
                _make_tweet(
                    "Fascinating research on enterprise AI adoption. https://arxiv.org/abs/2026.99",
                    tweet_id="99988",
                ),
            ]
        )

        signals, available = client.fetch_signals(_WINDOW_START, _WINDOW_END, "test-agent")

        assert available is True
        assert len(signals) == 1
        assert isinstance(signals[0], TweetSignal)
        assert signals[0].tweet_id == "99988"

    def test_mixed_page_only_substantive_kept(self) -> None:
        """
        A page with a mix of substantive and non-substantive tweets:
        only the substantive ones produce TweetSignal objects.
        """
        client = _make_client()
        client._resolve_user = MagicMock(return_value=MagicMock(id="12345"))
        client._fetch_tweets = MagicMock(
            return_value=[
                _make_tweet("RT @karpathy: old retweet"),  # bare RT → skip
                _make_tweet("@sama cool idea"),  # reply → skip
                _make_tweet(
                    "AI shifts enterprise landscape. https://t.co/x1", tweet_id="good1"
                ),  # has URL → keep
                _make_tweet("yo"),  # too short → skip
                _make_tweet(
                    "New paper on reinforcement learning from human feedback — "
                    "implications for safety are substantial.",
                    tweet_id="good2",
                ),  # long → keep
            ]
        )

        signals, available = client.fetch_signals(_WINDOW_START, _WINDOW_END, "test")

        assert available is True
        signal_ids = {s.tweet_id for s in signals}
        assert "good1" in signal_ids
        assert "good2" in signal_ids
        assert len(signals) == 2

    def test_multiple_handles_each_contribute_signals(self) -> None:
        """Signals from all handles combined in the returned list."""
        handles = [
            TwitterHandleConfig(handle="karpathy", weight=1.0),
            TwitterHandleConfig(handle="sama", weight=1.2),
            TwitterHandleConfig(handle="ylecun", weight=0.9),
        ]
        client = _make_client(handles)

        call_count = [0]

        def resolve_user(handle: str):
            user = MagicMock()
            user.id = f"uid_{handle}"
            return user

        def fetch_tweets(user_id, start, end):
            call_count[0] += 1
            return [
                _make_tweet(
                    f"AI insight from handle {user_id}. https://openai.com/research",
                    tweet_id=f"tweet_{call_count[0]}",
                )
            ]

        client._resolve_user = resolve_user
        client._fetch_tweets = fetch_tweets

        signals, available = client.fetch_signals(_WINDOW_START, _WINDOW_END, "test")

        assert available is True
        assert len(signals) == 3  # one from each handle

    def test_signals_contain_hydrated_linked_urls(self) -> None:
        """linked_urls on each TweetSignal come from _hydrate_urls (SRC-069)."""
        client = _make_client()
        client._resolve_user = MagicMock(return_value=MagicMock(id="12345"))

        entities = _make_entities(
            [
                {"expanded_url": "https://reuters.com/ai-story"},
                {"expanded_url": "https://bloomberg.com/tech"},
            ]
        )
        tweet = _make_tweet(
            "Check out these AI articles. https://t.co/abc",
            tweet_id="5001",
            entities=entities,
        )
        client._fetch_tweets = MagicMock(return_value=[tweet])

        signals, _ = client.fetch_signals(_WINDOW_START, _WINDOW_END, "test")

        assert len(signals) == 1
        assert "https://reuters.com/ai-story" in signals[0].linked_urls
        assert "https://bloomberg.com/tech" in signals[0].linked_urls

    def test_no_handles_configured_returns_empty_and_true(self) -> None:
        """Empty handles list → no API calls, ([], True) — API is technically available."""
        client = _make_client(handles=[])
        signals, available = client.fetch_signals(_WINDOW_START, _WINDOW_END, "test")
        assert signals == []
        assert available is True


# ===========================================================================
# 9. Handle weight ordering in prompt section (SRC-046, SRC-119)
# ===========================================================================


class TestPromptSignalSection:
    """
    Validate _format_twitter_section output content and ordering.
    Traces: SRC-046 (weight ordering), SRC-047 (signal role),
            SRC-070 (labeled section), SRC-119 (separate influencer section),
            SRC-148 (API unavailability messaging)
    """

    def _make_signal(self, handle: str, weight: float, text: str = "AI news insight.") -> Any:
        from ai_news_agent.storage.models import TweetSignal

        return TweetSignal(
            tweet_id=f"tweet_{handle}",
            handle=handle,
            text=text,
            created_at=datetime(2026, 5, 9, 10, 0, tzinfo=UTC),
            linked_urls=[],
            agent_id="test-agent",
            fetched_at=datetime(2026, 5, 9, 10, 5, tzinfo=UTC),
            weight=weight,
        )

    def test_section_contains_important_note(self) -> None:
        """Produced section includes the lead-generation-only IMPORTANT note (SRC-119)."""
        signals = [self._make_signal("karpathy", 1.0)]
        section = _format_twitter_section(signals)
        assert "IMPORTANT" in section
        assert "lead-generation" in section

    def test_section_header_present(self) -> None:
        """Section starts with '## Influencer Signal' heading (SRC-070, SRC-119)."""
        signals = [self._make_signal("karpathy", 1.0)]
        section = _format_twitter_section(signals)
        assert "## Influencer Signal" in section

    def test_handles_sorted_by_weight_descending(self) -> None:
        """Higher-weight handles appear before lower-weight ones (SRC-046)."""
        signals = [
            self._make_signal("low_weight", weight=0.5),
            self._make_signal("high_weight", weight=2.0),
            self._make_signal("mid_weight", weight=1.0),
        ]
        section = _format_twitter_section(signals)
        pos_high = section.index("high_weight")
        pos_mid = section.index("mid_weight")
        pos_low = section.index("low_weight")
        assert pos_high < pos_mid < pos_low, (
            "Handles should appear in descending weight order: high → mid → low"
        )

    def test_handle_displayed_with_at_prefix(self) -> None:
        """Handles shown as '@handle' in the section."""
        signals = [self._make_signal("karpathy", 1.0)]
        section = _format_twitter_section(signals)
        assert "@karpathy" in section

    def test_weight_shown_for_each_handle(self) -> None:
        """Weight value displayed next to each handle (SRC-046)."""
        signals = [self._make_signal("sama", 1.5)]
        section = _format_twitter_section(signals)
        assert "1.5" in section

    def test_tweet_text_included_in_section(self) -> None:
        """Tweet text (up to 280 chars) appears in the section."""
        text = "AI governance framework announced by EU Commission affecting all frontier models."
        signals = [self._make_signal("euregulator", 1.0, text=text)]
        section = _format_twitter_section(signals)
        assert text in section

    def test_tweet_text_truncated_to_280_chars(self) -> None:
        """Tweet text longer than 280 chars is truncated in the section."""
        long_text = "A" * 400
        signals = [self._make_signal("verbose_handle", 1.0, text=long_text)]
        section = _format_twitter_section(signals)
        # The section should NOT contain 400 A's
        assert "A" * 400 not in section
        # But should contain 280 A's (or fewer — up to 280 guaranteed)
        assert "A" * 280 in section

    def test_linked_urls_shown_in_section(self) -> None:
        """linked_urls listed in the section (up to 3)."""
        from ai_news_agent.storage.models import TweetSignal

        signal = TweetSignal(
            tweet_id="abc",
            handle="karpathy",
            text="Useful resource.",
            created_at=datetime(2026, 5, 9, 10, 0, tzinfo=UTC),
            linked_urls=[
                "https://reuters.com/a1",
                "https://bloomberg.com/a2",
                "https://techcrunch.com/a3",
            ],
            agent_id="test-agent",
            fetched_at=datetime(2026, 5, 9, 10, 5, tzinfo=UTC),
            weight=1.0,
        )
        section = _format_twitter_section([signal])
        assert "https://reuters.com/a1" in section
        assert "https://bloomberg.com/a2" in section
        assert "https://techcrunch.com/a3" in section

    def test_more_than_3_linked_urls_capped_at_3(self) -> None:
        """Only the first 3 linked_urls are shown per signal."""
        from ai_news_agent.storage.models import TweetSignal

        signal = TweetSignal(
            tweet_id="def",
            handle="sama",
            text="Multiple links.",
            created_at=datetime(2026, 5, 9, 10, 0, tzinfo=UTC),
            linked_urls=["https://a.com", "https://b.com", "https://c.com", "https://d.com"],
            agent_id="test-agent",
            fetched_at=datetime(2026, 5, 9, 10, 5, tzinfo=UTC),
            weight=1.0,
        )
        section = _format_twitter_section([signal])
        assert "https://d.com" not in section  # 4th URL not shown

    def test_empty_signals_api_available_quiet_window_message(self) -> None:
        """No signals + API available → quiet-window message (not API-error)."""
        section = _format_twitter_section([], twitter_api_available=True)
        assert "no substantive" in section.lower() or "no twitter" in section.lower()
        assert "unavailable" not in section.lower() or "not" in section.lower()

    def test_empty_signals_api_unavailable_error_message(self) -> None:
        """No signals + API unavailable → API-error warning message (SRC-148)."""
        section = _format_twitter_section([], twitter_api_available=False)
        # Should indicate the API was specifically unreachable
        lower = section.lower()
        assert "unavailable" in lower or "api" in lower

    def test_api_unavailable_message_includes_web_sources_note(self) -> None:
        """Degradation message instructs model to use web sources only (SRC-148)."""
        section = _format_twitter_section([], twitter_api_available=False)
        assert "web sources" in section.lower()

    def test_signals_present_regardless_of_api_flag(self) -> None:
        """If signals are present, they always render (api_available flag irrelevant)."""
        signals = [self._make_signal("karpathy", 1.0, "Test tweet. https://openai.com")]
        section_true = _format_twitter_section(signals, twitter_api_available=True)
        section_false = _format_twitter_section(signals, twitter_api_available=False)
        assert "@karpathy" in section_true
        assert "@karpathy" in section_false


# ===========================================================================
# 10. Digest degradation note in rendered Markdown (SRC-148)
# ===========================================================================


class TestDigestDegradationNoteRendering:
    """
    Verify that the Twitter degradation note flows from CurationRunResult
    into the rendered Markdown output.
    Traces: SRC-148 (degradation note in digest output)
    """

    def _make_curation_result(
        self,
        degradation_note: str | None,
    ):
        """Build a minimal CurationRunResult with or without a degradation note."""
        from datetime import date

        from ai_news_agent.curation.agent import CurationRunResult
        from ai_news_agent.storage.models import DigestMetadata

        meta = DigestMetadata(
            agent_id="test-agent",
            cadence="daily",
            run_date=date(2026, 5, 10),
            window_start=datetime(2026, 5, 9, 0, 0, tzinfo=UTC),
            window_end=datetime(2026, 5, 9, 23, 59, tzinfo=UTC),
            prompt_version="sha256:abc123",
            llm_provider="openai",
            llm_model="gpt-4o",
            items_considered=10,
            items_included=3,
            items_by_tier={"1b": 2, "2": 1},
            items_by_source_class={"web": 3},
            twitter_signal_available=(degradation_note is None),
            tweet_api_call_count=2 if degradation_note is None else 0,
            token_usage=1000,
        )
        return CurationRunResult(
            metadata=meta,
            items=[],
            twitter_degradation_note=degradation_note,
        )

    def test_degradation_note_rendered_as_blockquote(self) -> None:
        """
        When twitter_degradation_note is set, the Markdown renderer includes
        a blockquote warning (SRC-148).
        """
        from ai_news_agent.rendering.markdown_renderer import MarkdownRenderer

        result = self._make_curation_result(
            "⚠️ Twitter/X influencer signal was unavailable for this run. "
            "Curation is based on web sources only. (SRC-148)"
        )
        renderer = MarkdownRenderer()
        output = renderer.render(result)

        # Markdown blockquote prefix
        assert "> ⚠️" in output
        assert "SRC-148" in output

    def test_no_degradation_note_no_blockquote(self) -> None:
        """When twitter_degradation_note is None, no degradation blockquote rendered."""
        from ai_news_agent.rendering.markdown_renderer import MarkdownRenderer

        result = self._make_curation_result(degradation_note=None)
        renderer = MarkdownRenderer()
        output = renderer.render(result)

        # Should NOT contain the degradation blockquote
        assert "> ⚠️ Twitter" not in output

    def test_twitter_signal_status_in_footer(self) -> None:
        """
        Metadata footer shows '⚠️ unavailable' when twitter_signal_available=False
        and '✅ available' when True.
        """
        from ai_news_agent.rendering.markdown_renderer import MarkdownRenderer

        # Degraded
        degraded_result = self._make_curation_result("Test degradation note")
        degraded_output = MarkdownRenderer().render(degraded_result)
        assert "⚠️ unavailable" in degraded_output

        # Available
        ok_result = self._make_curation_result(degradation_note=None)
        ok_output = MarkdownRenderer().render(ok_result)
        assert "✅ available" in ok_output

    def test_degradation_note_content_references_web_sources(self) -> None:
        """Degradation note directs readers/LLM to web sources (SRC-148)."""
        note = (
            "⚠️ Twitter/X influencer signal was unavailable for this run. "
            "Curation is based on web sources only. (SRC-148)"
        )
        result = self._make_curation_result(note)
        from ai_news_agent.rendering.markdown_renderer import MarkdownRenderer

        output = MarkdownRenderer().render(result)
        assert "web sources" in output.lower()


# ===========================================================================
# 11. PromptBuilder integration — twitter_api_available threading (SRC-148)
# ===========================================================================


class TestPromptBuilderTwitterIntegration:
    """
    Verify that PromptBuilder.build correctly passes twitter_api_available
    through to _format_twitter_section and the resulting prompt text.
    Traces: SRC-119 (Twitter signal section), SRC-148 (API availability)
    """

    def test_prompt_contains_api_unavailable_warning_when_flag_false(self, prompts_dir) -> None:
        """
        When twitter_api_available=False and no signals, the built prompt
        contains the API-unavailability warning text (SRC-148).
        """
        from ai_news_agent.curation.prompt_builder import PromptBuilder

        builder = PromptBuilder(prompts_dir=prompts_dir)
        prompt, version = builder.build(
            cadence="daily",
            window_start=_WINDOW_START,
            window_end=_WINDOW_END,
            tweet_signals=[],
            top_n=5,
            twitter_api_available=False,
        )
        # Should contain API-failure warning
        assert "unavailable" in prompt.lower() or "api" in prompt.lower()
        assert "web sources" in prompt.lower()

    def test_prompt_contains_quiet_window_message_when_api_available(self, prompts_dir) -> None:
        """
        When twitter_api_available=True and no signals, the built prompt
        contains the quiet-window message (not the API-error message).
        """
        from ai_news_agent.curation.prompt_builder import PromptBuilder

        builder = PromptBuilder(prompts_dir=prompts_dir)
        prompt, _ = builder.build(
            cadence="daily",
            window_start=_WINDOW_START,
            window_end=_WINDOW_END,
            tweet_signals=[],
            top_n=5,
            twitter_api_available=True,
        )
        # Should contain "no substantive" or "no twitter" (quiet window)
        lower = prompt.lower()
        assert "no substantive" in lower or "no twitter" in lower

    def test_prompt_contains_signal_section_with_handles_when_signals_present(
        self, prompts_dir
    ) -> None:
        """
        When signals are present, the built prompt contains handle names
        and the IMPORTANT lead-generation note (SRC-119).
        """
        from ai_news_agent.curation.prompt_builder import PromptBuilder
        from ai_news_agent.storage.models import TweetSignal

        signals = [
            TweetSignal(
                tweet_id="s1",
                handle="karpathy",
                text="AI safety milestone this week. https://openai.com/safety",
                created_at=_WINDOW_START,
                linked_urls=["https://openai.com/safety"],
                agent_id="test-agent",
                fetched_at=_WINDOW_START,
                weight=1.5,
            )
        ]

        builder = PromptBuilder(prompts_dir=prompts_dir)
        prompt, _ = builder.build(
            cadence="daily",
            window_start=_WINDOW_START,
            window_end=_WINDOW_END,
            tweet_signals=signals,
            top_n=5,
            twitter_api_available=True,
        )
        assert "@karpathy" in prompt
        assert "IMPORTANT" in prompt
        assert "lead-generation" in prompt

    def test_prompt_version_is_sha256_string(self, prompts_dir) -> None:
        """prompt_version is a 'sha256:<hex>' string (SRC-129)."""
        from ai_news_agent.curation.prompt_builder import PromptBuilder

        builder = PromptBuilder(prompts_dir=prompts_dir)
        _, version = builder.build(
            cadence="daily",
            window_start=_WINDOW_START,
            window_end=_WINDOW_END,
            tweet_signals=[],
            top_n=5,
        )
        assert version.startswith("sha256:")
        assert len(version) == len("sha256:") + 64  # SHA-256 = 64 hex chars


# ===========================================================================
# 12. CurationAgent twitter_api_available threading (SRC-148)
# ===========================================================================


class TestCurationAgentTwitterAvailability:
    """
    Validate that CurationAgent.run correctly threads twitter_api_available
    into the prompt and result metadata.
    Traces: SRC-148 (degradation flag passed end-to-end)
    """

    def _make_agent(self, config, secrets, store, prompts_dir, dummy_llm):
        from ai_news_agent.curation.agent import CurationAgent

        with (
            patch("ai_news_agent.curation.agent.get_llm_client", return_value=dummy_llm),
        ):
            agent = CurationAgent(
                config=config,
                secrets=secrets,
                store=store,
                prompts_dir=str(prompts_dir),
            )
            agent._llm = dummy_llm
            # Patch scorer to avoid LLM calls; return ScorerResult (not bare list)
            from ai_news_agent.curation.scorer import ScorerResult

            agent._scorer.score_and_rank = MagicMock(
                return_value=ScorerResult(
                    items=[], themes=[], outlook="", predictions=[], token_usage=0
                )
            )
        return agent

    def test_explicit_false_sets_degradation_note(
        self,
        sample_agent_config,
        sample_secrets,
        tiny_db_store,
        prompts_dir,
        dummy_llm,
    ) -> None:
        """
        twitter_api_available=False explicitly → CurationRunResult has
        a non-None twitter_degradation_note (SRC-148).
        """
        agent = self._make_agent(
            sample_agent_config, sample_secrets, tiny_db_store, prompts_dir, dummy_llm
        )
        result = agent.run(
            cadence="daily",
            window_start=_WINDOW_START,
            window_end=_WINDOW_END,
            twitter_api_available=False,
        )
        assert result.twitter_degradation_note is not None
        assert (
            "SRC-148" in result.twitter_degradation_note
            or "unavailable" in result.twitter_degradation_note.lower()
        )

    def test_explicit_true_no_degradation_note_when_signals_present(
        self,
        sample_agent_config,
        sample_secrets,
        tiny_db_store,
        prompts_dir,
        dummy_llm,
        sample_tweet_signal,
    ) -> None:
        """
        twitter_api_available=True with signals in store → no degradation note.
        """
        # Pre-populate store with a signal in the window
        sample_tweet_signal_in_window = sample_tweet_signal.__class__(
            tweet_id=sample_tweet_signal.tweet_id,
            handle=sample_tweet_signal.handle,
            text=sample_tweet_signal.text,
            created_at=_WINDOW_START,  # in window
            linked_urls=sample_tweet_signal.linked_urls,
            agent_id=sample_tweet_signal.agent_id,
            fetched_at=_WINDOW_START,
            weight=sample_tweet_signal.weight,
        )
        tiny_db_store.insert_tweet_signal(sample_tweet_signal_in_window)

        agent = self._make_agent(
            sample_agent_config, sample_secrets, tiny_db_store, prompts_dir, dummy_llm
        )
        result = agent.run(
            cadence="daily",
            window_start=_WINDOW_START,
            window_end=_WINDOW_END,
            twitter_api_available=True,
        )
        assert result.twitter_degradation_note is None

    def test_metadata_twitter_available_flag_reflects_state(
        self,
        sample_agent_config,
        sample_secrets,
        tiny_db_store,
        prompts_dir,
        dummy_llm,
    ) -> None:
        """
        CurationRunResult.metadata.twitter_signal_available reflects
        the resolved twitter_api_available flag (SRC-148, SRC-150).
        """
        agent = self._make_agent(
            sample_agent_config, sample_secrets, tiny_db_store, prompts_dir, dummy_llm
        )
        result = agent.run(
            cadence="daily",
            window_start=_WINDOW_START,
            window_end=_WINDOW_END,
            twitter_api_available=False,
        )
        assert result.metadata.twitter_signal_available is False


# ===========================================================================
# 13. TwitterFetcher thin wrapper (SRC-047, SRC-062)
# ===========================================================================


class TestTwitterFetcherWrapper:
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
                window_start=_WINDOW_START,
                window_end=_WINDOW_END,
                agent_id="test",
            )

        MockClient.return_value.fetch_signals.assert_called_once()
        assert available is True

    def test_fetcher_passes_handles_from_config(self, sample_agent_config, sample_secrets) -> None:
        """TwitterFetcher passes config.twitter.handles to TwitterClient."""
        from ai_news_agent.sourcing.twitter_fetcher import TwitterFetcher

        with patch("ai_news_agent.sourcing.twitter_fetcher.TwitterClient") as MockClient:
            MockClient.return_value.fetch_signals.return_value = ([], True)
            TwitterFetcher(
                config=sample_agent_config,
                bearer_token=sample_secrets.twitter_bearer_token,
            )

        init_kwargs = MockClient.call_args.kwargs
        assert "handles" in init_kwargs
        handle_names = [h.handle for h in init_kwargs["handles"]]
        assert "karpathy" in handle_names
        assert "sama" in handle_names

    def test_fetcher_passes_bearer_token(self, sample_agent_config, sample_secrets) -> None:
        """Bearer token forwarded from secrets to TwitterClient (SRC-064)."""
        from ai_news_agent.sourcing.twitter_fetcher import TwitterFetcher

        with patch("ai_news_agent.sourcing.twitter_fetcher.TwitterClient") as MockClient:
            MockClient.return_value.fetch_signals.return_value = ([], True)
            TwitterFetcher(
                config=sample_agent_config,
                bearer_token="specific-token-value",
            )

        init_kwargs = MockClient.call_args.kwargs
        assert init_kwargs["bearer_token"] == "specific-token-value"

    def test_fetcher_propagates_twitter_unavailable(
        self, sample_agent_config, sample_secrets
    ) -> None:
        """([], False) from client is propagated unchanged (SRC-148)."""
        from ai_news_agent.sourcing.twitter_fetcher import TwitterFetcher

        with patch("ai_news_agent.sourcing.twitter_fetcher.TwitterClient") as MockClient:
            MockClient.return_value.fetch_signals.return_value = ([], False)
            fetcher = TwitterFetcher(
                config=sample_agent_config,
                bearer_token=sample_secrets.twitter_bearer_token,
            )
            signals, available = fetcher.fetch(
                window_start=_WINDOW_START,
                window_end=_WINDOW_END,
                agent_id="test",
            )

        assert signals == []
        assert available is False


# ===========================================================================
# 14. SourcingAgent integration (SRC-008–SRC-013, SRC-148, SRC-150)
# ===========================================================================


class TestSourcingAgentTwitterIntegration:
    """
    Integration-level tests for SourcingAgent with mocked sub-components.
    Focuses on Twitter-specific behaviour.
    Traces: SRC-008–SRC-013, SRC-047, SRC-069–SRC-070, SRC-148, SRC-150
    """

    @staticmethod
    def _make_agent(
        config,
        secrets,
        store,
        web_articles=None,
        tweet_signals=None,
        twitter_available: bool = True,
        tweet_url_articles=None,
    ):
        from ai_news_agent.sourcing.agent import SourcingAgent

        with (
            patch("ai_news_agent.sourcing.agent.get_llm_client"),
            patch("ai_news_agent.sourcing.agent.get_search_tool"),
            patch("ai_news_agent.sourcing.agent.WebFetcher") as MockWeb,
            patch("ai_news_agent.sourcing.agent.TwitterFetcher") as MockTwitter,
        ):
            MockWeb.return_value.fetch_all.return_value = web_articles or []
            MockWeb.return_value.fetch_from_tweet_urls.return_value = tweet_url_articles or []
            MockTwitter.return_value.fetch.return_value = (
                tweet_signals or [],
                twitter_available,
            )
            agent = SourcingAgent(config=config, secrets=secrets, store=store)
            agent._web_fetcher = MockWeb.return_value
            agent._twitter_fetcher = MockTwitter.return_value
        return agent

    def test_twitter_unavailable_sets_flag_false(
        self, sample_agent_config, sample_secrets, tiny_db_store
    ) -> None:
        """SourcingRunResult.twitter_signal_available=False when API is down (SRC-148)."""
        agent = self._make_agent(
            config=sample_agent_config,
            secrets=sample_secrets,
            store=tiny_db_store,
            twitter_available=False,
        )
        result = agent.run(window_start=_WINDOW_START, window_end=_WINDOW_END)
        assert result.twitter_signal_available is False

    def test_web_sourcing_continues_when_twitter_down(
        self, sample_agent_config, sample_secrets, tiny_db_store, sample_article
    ) -> None:
        """Web articles still inserted even when Twitter is unavailable (SRC-148)."""
        agent = self._make_agent(
            config=sample_agent_config,
            secrets=sample_secrets,
            store=tiny_db_store,
            web_articles=[sample_article],
            twitter_available=False,
        )
        result = agent.run(window_start=_WINDOW_START, window_end=_WINDOW_END)
        assert result.articles_inserted == 1
        assert result.twitter_signal_available is False

    def test_tweet_api_call_count_zero_when_unavailable(
        self, sample_agent_config, sample_secrets, tiny_db_store
    ) -> None:
        """tweet_api_call_count is 0 when API is unavailable (SRC-150)."""
        agent = self._make_agent(
            config=sample_agent_config,
            secrets=sample_secrets,
            store=tiny_db_store,
            twitter_available=False,
        )
        result = agent.run(window_start=_WINDOW_START, window_end=_WINDOW_END)
        assert result.tweet_api_call_count == 0

    def test_tweet_api_call_count_nonzero_when_available(
        self, sample_agent_config, sample_secrets, tiny_db_store
    ) -> None:
        """tweet_api_call_count > 0 when API is available and handles configured (SRC-150)."""
        agent = self._make_agent(
            config=sample_agent_config,
            secrets=sample_secrets,
            store=tiny_db_store,
            twitter_available=True,
        )
        result = agent.run(window_start=_WINDOW_START, window_end=_WINDOW_END)
        # Count = number of configured handles (2 in sample_agent_config)
        assert result.tweet_api_call_count == len(sample_agent_config.twitter.handles)

    def test_tweet_signals_stored(
        self, sample_agent_config, sample_secrets, tiny_db_store, sample_tweet_signal
    ) -> None:
        """Tweet signals are persisted to the store (SRC-067)."""
        agent = self._make_agent(
            config=sample_agent_config,
            secrets=sample_secrets,
            store=tiny_db_store,
            tweet_signals=[sample_tweet_signal],
            twitter_available=True,
        )
        result = agent.run(window_start=_WINDOW_START, window_end=_WINDOW_END)
        assert result.tweets_inserted == 1

    def test_duplicate_tweet_signal_not_reinserted(
        self, sample_agent_config, sample_secrets, tiny_db_store, sample_tweet_signal
    ) -> None:
        """Duplicate tweet_id + agent_id pair not re-inserted (SRC-012 for tweets)."""
        tiny_db_store.insert_tweet_signal(sample_tweet_signal)  # pre-insert

        agent = self._make_agent(
            config=sample_agent_config,
            secrets=sample_secrets,
            store=tiny_db_store,
            tweet_signals=[sample_tweet_signal],
            twitter_available=True,
        )
        result = agent.run(window_start=_WINDOW_START, window_end=_WINDOW_END)
        assert result.tweets_inserted == 0

    def test_tweet_linked_urls_fed_to_web_fetcher(
        self, sample_agent_config, sample_secrets, tiny_db_store, sample_tweet_signal
    ) -> None:
        """
        Linked URLs from tweet signals are passed to fetch_from_tweet_urls
        for primary-article hydration (SRC-069–SRC-070).
        """
        from ai_news_agent.sourcing.agent import SourcingAgent

        with (
            patch("ai_news_agent.sourcing.agent.get_llm_client"),
            patch("ai_news_agent.sourcing.agent.get_search_tool"),
            patch("ai_news_agent.sourcing.agent.WebFetcher") as MockWeb,
            patch("ai_news_agent.sourcing.agent.TwitterFetcher") as MockTwitter,
        ):
            MockWeb.return_value.fetch_all.return_value = []
            MockWeb.return_value.fetch_from_tweet_urls.return_value = []
            MockTwitter.return_value.fetch.return_value = (
                [sample_tweet_signal],
                True,
            )
            agent = SourcingAgent(
                config=sample_agent_config,
                secrets=sample_secrets,
                store=tiny_db_store,
            )
            agent._web_fetcher = MockWeb.return_value
            agent._twitter_fetcher = MockTwitter.return_value
            agent.run(window_start=_WINDOW_START, window_end=_WINDOW_END)

        # fetch_from_tweet_urls should have been called with the signal's linked URLs
        fetch_url_call_args = MockWeb.return_value.fetch_from_tweet_urls.call_args
        assert fetch_url_call_args is not None
        called_urls = fetch_url_call_args.kwargs.get("urls") or fetch_url_call_args.args[0]
        for url in sample_tweet_signal.linked_urls:
            assert url in called_urls

    def test_twitter_disabled_config_skips_fetcher(
        self, sample_agent_config, sample_secrets, tiny_db_store
    ) -> None:
        """When twitter.enabled=False, TwitterFetcher.fetch is never called."""
        from ai_news_agent.sourcing.agent import SourcingAgent

        config = sample_agent_config.model_copy(
            update={"twitter": sample_agent_config.twitter.model_copy(update={"enabled": False})}
        )

        with (
            patch("ai_news_agent.sourcing.agent.get_llm_client"),
            patch("ai_news_agent.sourcing.agent.get_search_tool"),
            patch("ai_news_agent.sourcing.agent.WebFetcher") as MockWeb,
            patch("ai_news_agent.sourcing.agent.TwitterFetcher") as MockTwitter,
        ):
            MockWeb.return_value.fetch_all.return_value = []
            MockWeb.return_value.fetch_from_tweet_urls.return_value = []

            agent = SourcingAgent(config=config, secrets=sample_secrets, store=tiny_db_store)
            agent._web_fetcher = MockWeb.return_value
            agent._twitter_fetcher = MockTwitter.return_value
            result = agent.run(window_start=_WINDOW_START, window_end=_WINDOW_END)

        MockTwitter.return_value.fetch.assert_not_called()
        assert result.twitter_signal_available is False

    def test_tweet_url_articles_combined_with_web(
        self,
        sample_agent_config,
        sample_secrets,
        tiny_db_store,
        sample_article,
        sample_tweet_signal,
    ) -> None:
        """
        Articles from tweet-URL hydration are combined with web articles
        for the total articles_fetched count (SRC-069–SRC-070).
        """
        from ai_news_agent.storage.models import ArticleRecord, normalize_url
        from ai_news_agent.storage.models import url_hash as uh

        tweet_url = "https://techcrunch.com/tweet-sourced-story"
        canon = normalize_url(tweet_url)
        tweet_article = ArticleRecord(
            url_hash=uh(canon),
            url=canon,
            headline="Tweet-Sourced Story",
            abstract="Found via tweet link.",
            source_name="techcrunch.com",
            pub_date=_WINDOW_START,
            fetched_at=_WINDOW_START,
            tier="3",
            source_class="web",
            agent_id="test-agent",
        )

        agent = self._make_agent(
            config=sample_agent_config,
            secrets=sample_secrets,
            store=tiny_db_store,
            web_articles=[sample_article],
            tweet_signals=[sample_tweet_signal],
            twitter_available=True,
            tweet_url_articles=[tweet_article],
        )
        result = agent.run(window_start=_WINDOW_START, window_end=_WINDOW_END)

        assert result.articles_fetched == 2  # web + tweet-url
        assert result.articles_inserted == 2
