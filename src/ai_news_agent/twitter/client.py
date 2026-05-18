"""
twitter/client.py — tweepy-based Twitter/X client with graceful degradation.

Implements the full fetch-filter-hydrate pipeline for influencer signals:
  1. Resolve each configured handle to a Twitter user ID  (SRC-067)
  2. Page through ``get_users_tweets`` within the lookback window  (SRC-067)
  3. Filter: skip pure replies and bare retweets; keep substantive posts  (SRC-068)
  4. Hydrate: expand t.co short links to canonical URLs  (SRC-069)
  5. Normalise to :class:`TweetSignal` with handle weight from config  (SRC-046)
  6. Graceful degradation on any API error → web-only mode  (SRC-148)

Twitter content is **signal and lead-generation only** — not primary news.
(SRC-047, SRC-070)

Error handling (SRC-065, SRC-148):
  - ``tweepy.errors.TooManyRequests`` (HTTP 429): rate-limit hit — log a specific
    warning noting the API tier constraint (SRC-065) and degrade gracefully.
  - ``tweepy.errors.Unauthorized`` (HTTP 401): invalid/expired bearer token —
    log a clear auth-error message to aid debugging (SRC-064).
  - ``tweepy.TweepyException``: any other tweepy API error — log and degrade.
  - All other exceptions: caught as a final safety net so the sourcing run is
    never blocked by an unexpected Twitter failure (SRC-148).

Traces: SRC-036–SRC-046 (configurable influencer list and weights),
        SRC-047 (signal role — not primary news),
        SRC-063 (tweepy library),
        SRC-064 (bearer token auth from env var — never hardcoded),
        SRC-065 (API tier: Basic minimum; rate-limit awareness),
        SRC-066–SRC-069 (fetch, filter, hydrate pipeline),
        SRC-070 (influencer signal labelled section for LLM),
        SRC-148 (graceful degradation — digest still produced from web only)
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import structlog

try:
    import tweepy  # type: ignore[import]
    _TWEEPY_AVAILABLE = True
except ImportError:
    tweepy = None  # type: ignore[assignment]
    _TWEEPY_AVAILABLE = False

if TYPE_CHECKING:
    from ai_news_agent.config.models import TwitterHandleConfig
    from ai_news_agent.storage.models import TweetSignal

log = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Tweet filtering constants (SRC-068)
# ---------------------------------------------------------------------------

# Minimum character count for original tweets (no URL) to be considered substantive
_MIN_TWEET_CHARS: int = 50

# Fields to request from the Twitter v2 API
_TWEET_FIELDS: list[str] = ["created_at", "entities", "text", "referenced_tweets"]
_EXPANSIONS: list[str] = ["author_id"]

# Maximum results per page (Twitter v2 cap is 100)
_MAX_RESULTS_PER_PAGE: int = 100

# Regex: detect a bare URL anywhere in tweet text
_URL_RE = re.compile(r"https?://\S+")

# Regex: detect pure reply — text starts with "@handle" (no preceding non-whitespace)
_REPLY_RE = re.compile(r"^\s*@\S+")


# ---------------------------------------------------------------------------
# Degradation reason constants — surfaced in logs and digest notes (SRC-148)
# ---------------------------------------------------------------------------

class DegradationReason:
    """
    Named constants for Twitter API degradation reasons.

    Used in structured log events and, when necessary, in the digest note so
    operators can distinguish a rate-limit problem (fix: upgrade API tier) from
    an auth problem (fix: rotate bearer token) or a transient error.

    Traces: SRC-065 (tier awareness), SRC-148 (graceful degradation)
    """
    TWEEPY_NOT_INSTALLED  = "tweepy_not_installed"
    BEARER_TOKEN_MISSING  = "bearer_token_missing"
    RATE_LIMIT            = "rate_limit_429"          # SRC-065 — API tier issue
    UNAUTHORIZED          = "unauthorized_401"         # SRC-064 — invalid bearer token
    TWEEPY_API_ERROR      = "tweepy_api_error"         # other tweepy exception
    UNEXPECTED_ERROR      = "unexpected_error"


class TwitterClient:
    """
    tweepy-based Twitter/X v2 client for the Sourcing Agent.

    Role in the pipeline (SRC-047, SRC-070):
    - Twitter content is **signal and lead-generation only** — not primary news.
    - A tweet alone rarely warrants inclusion; it surfaces topics worth investigating
      via web search.
    - Exception: when the tweet itself IS the news (e.g. an executive announcement
      on X before press coverage exists) — in that case the tweet URL is stored as a
      primary source article with ``source_class="twitter"``.

    Graceful degradation (SRC-148):
    - On ``tweepy.errors.TooManyRequests`` (HTTP 429): logs a rate-limit warning
      with the API tier note (SRC-065) and returns ``([], False, DegradationReason.RATE_LIMIT)``.
    - On ``tweepy.errors.Unauthorized`` (HTTP 401): logs an auth-error warning
      and returns ``([], False, DegradationReason.UNAUTHORIZED)``.
    - On any other ``tweepy.TweepyException``: logs a general API error warning
      and returns ``([], False, DegradationReason.TWEEPY_API_ERROR)``.
    - On any unexpected exception: caught as a final safety net, returns
      ``([], False, DegradationReason.UNEXPECTED_ERROR)``.
    - In all degraded cases: the Sourcing Agent continues with web-only sources and the
      digest is annotated that influencer signal was unavailable for this run.
    - Twitter is signal, not a hard dependency.

    API tier (SRC-065):
    - Minimum: Twitter API Basic tier.
    - The Free tier's rate limits and 7-day search depth are insufficient for
      reliable lookback windows spanning the full daily/weekly cadences.
    - When a TooManyRequests error is returned, the degradation reason is logged
      so operators know whether to upgrade the API tier.

    Fetch pipeline:
    1. ``_resolve_user`` — user lookup by handle via ``get_user``
    2. ``_fetch_tweets`` — paginated timeline via ``get_users_tweets``
    3. ``_is_substantive`` — filter skip rules (SRC-068)
    4. ``_hydrate_urls`` — expand t.co short links (SRC-069)
    5. ``_normalize`` — produce :class:`TweetSignal` with weight (SRC-046)

    Traces: SRC-036–SRC-046 (influencer list + weights),
            SRC-047 (signal role), SRC-063–SRC-070, SRC-148
    """

    def __init__(
        self,
        bearer_token: str,
        handles: list[TwitterHandleConfig],
    ) -> None:
        """
        Initialise the client with a bearer token and influencer handle list.

        Args:
            bearer_token: ``TWITTER_BEARER_TOKEN`` env var value (SRC-064).
                          NEVER pass a hardcoded secret here — always read from env.
                          An empty or missing token sets ``_client=None`` and triggers
                          graceful degradation immediately (SRC-148).
            handles:      Configured influencer handles with signal weights
                          (SRC-036–SRC-046).  Handles can be added, removed, or
                          re-weighted without code changes.
        """
        if not _TWEEPY_AVAILABLE:
            log.warning(
                "tweepy_not_installed",
                degradation_reason=DegradationReason.TWEEPY_NOT_INSTALLED,
                msg=(
                    "Twitter integration disabled — install tweepy to enable. "
                    "Digests will be produced from web sources only (SRC-148)."
                ),
            )
            self._client: Any = None
            self._degradation_reason: str | None = DegradationReason.TWEEPY_NOT_INSTALLED
        elif not bearer_token:
            log.warning(
                "twitter_bearer_token_missing",
                degradation_reason=DegradationReason.BEARER_TOKEN_MISSING,
                msg=(
                    "TWITTER_BEARER_TOKEN is empty — Twitter integration disabled. "
                    "Set the env var to re-enable (SRC-064, SRC-148)."
                ),
            )
            self._client = None
            self._degradation_reason = DegradationReason.BEARER_TOKEN_MISSING
        else:
            self._client = tweepy.Client(
                bearer_token=bearer_token,
                wait_on_rate_limit=True,  # honour rate-limit headers automatically
            )
            self._degradation_reason = None
        self._handles = handles

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def fetch_signals(
        self,
        window_start: datetime,
        window_end: datetime,
        agent_id: str,
    ) -> tuple[list[TweetSignal], bool]:
        """
        Fetch and filter tweets from all configured influencer handles within
        the lookback window.

        Pipeline per handle:
        1. Resolve handle → user ID (``_resolve_user``).
        2. Page through tweets in ``[window_start, window_end]`` (``_fetch_tweets``).
        3. Filter to substantive posts (``_is_substantive``).
        4. Hydrate t.co links to canonical URLs (``_hydrate_urls``).
        5. Normalise to :class:`TweetSignal` (``_normalize``).

        Error handling (SRC-065, SRC-148):
        - ``tweepy.errors.TooManyRequests`` (HTTP 429): rate-limit hit — logs the
          API tier note (SRC-065) and returns ``([], False)``.
        - ``tweepy.errors.Unauthorized`` (HTTP 401): invalid/expired bearer token —
          logs an auth-error message and returns ``([], False)``.
        - ``tweepy.TweepyException``: any other tweepy API error — logs and degrades.
        - All other exceptions: caught as final safety net (SRC-148).

        Returns:
            ``(signals, twitter_available)`` tuple:
            - ``signals``: list of hydrated :class:`TweetSignal` objects.
            - ``twitter_available``: ``False`` if the Twitter API is unavailable
              — triggers web-only mode and a degradation note in the digest (SRC-148).

        Traces: SRC-067 (fetch from lookback window), SRC-068 (filter),
                SRC-069 (hydrate URLs), SRC-148 (graceful degradation)
        """
        if self._client is None:
            log.warning(
                "twitter_client_unavailable",
                degradation_reason=self._degradation_reason or DegradationReason.TWEEPY_NOT_INSTALLED,
                agent_id=agent_id,
                msg="Twitter API disabled — digests produced from web sources only (SRC-148).",
            )
            return [], False

        try:
            signals: list[TweetSignal] = []
            for handle_cfg in self._handles:
                user = self._resolve_user(handle_cfg.handle)
                if user is None:
                    log.debug(
                        "twitter_user_not_found",
                        handle=handle_cfg.handle,
                    )
                    continue

                tweets = self._fetch_tweets(user.id, window_start, window_end)
                log.debug(
                    "twitter_handle_tweets_fetched",
                    handle=handle_cfg.handle,
                    raw_count=len(tweets),
                )

                for tweet in tweets:
                    if self._is_substantive(tweet):
                        sig = self._normalize(tweet, handle_cfg, agent_id)
                        signals.append(sig)

            log.info(
                "twitter_fetch_complete",
                agent_id=agent_id,
                handles=len(self._handles),
                signals=len(signals),
            )
            return signals, True

        except Exception as exc:  # noqa: BLE001
            return self._handle_api_exception(exc, agent_id)

    def _handle_api_exception(
        self,
        exc: Exception,
        agent_id: str,
    ) -> tuple[list[TweetSignal], bool]:
        """
        Classify and log a Twitter API exception, then return the degraded tuple.

        Checks (in priority order):
        1. ``tweepy.errors.TooManyRequests`` — HTTP 429, rate-limit hit (SRC-065).
        2. ``tweepy.errors.Unauthorized``    — HTTP 401, invalid bearer token (SRC-064).
        3. ``tweepy.TweepyException``        — any other tweepy error.
        4. Fallback ``Exception``            — unexpected non-tweepy error.

        All paths return ``([], False)`` so the Sourcing Agent continues with
        web-only sources (SRC-148).

        Traces: SRC-064 (bearer token), SRC-065 (API tier), SRC-148 (degradation)
        """
        # Guard: if tweepy is not installed, all branches below are unreachable
        # (AttributeError on tweepy.errors.*) — fall through to generic handler.
        if _TWEEPY_AVAILABLE and tweepy is not None:
            # Rate-limit — HTTP 429 (SRC-065)
            too_many = getattr(tweepy.errors, "TooManyRequests", None)
            if too_many is not None and isinstance(exc, too_many):
                log.warning(
                    "twitter_rate_limit_exceeded",
                    degradation_reason=DegradationReason.RATE_LIMIT,
                    error=str(exc),
                    agent_id=agent_id,
                    msg=(
                        "Twitter API rate limit exceeded (HTTP 429). "
                        "Consider upgrading to a higher API tier (SRC-065). "
                        "Continuing with web sources only (SRC-148)."
                    ),
                )
                return [], False

            # Unauthorized — HTTP 401 (SRC-064)
            unauthorized = getattr(tweepy.errors, "Unauthorized", None)
            if unauthorized is not None and isinstance(exc, unauthorized):
                log.warning(
                    "twitter_unauthorized",
                    degradation_reason=DegradationReason.UNAUTHORIZED,
                    error=str(exc),
                    agent_id=agent_id,
                    msg=(
                        "Twitter API returned HTTP 401 Unauthorized. "
                        "Verify TWITTER_BEARER_TOKEN is valid and not expired (SRC-064). "
                        "Continuing with web sources only (SRC-148)."
                    ),
                )
                return [], False

            # Any other tweepy exception
            tweepy_exc = getattr(tweepy, "TweepyException", None)
            if tweepy_exc is not None and isinstance(exc, tweepy_exc):
                log.warning(
                    "twitter_api_error",
                    degradation_reason=DegradationReason.TWEEPY_API_ERROR,
                    error=str(exc),
                    error_type=type(exc).__name__,
                    agent_id=agent_id,
                    msg="tweepy API error — continuing with web sources only (SRC-148).",
                )
                return [], False

        # Fallback: unexpected non-tweepy exception
        log.warning(
            "twitter_unexpected_error",
            degradation_reason=DegradationReason.UNEXPECTED_ERROR,
            error=str(exc),
            error_type=type(exc).__name__,
            agent_id=agent_id,
            msg="Unexpected error in Twitter fetch — continuing with web sources only (SRC-148).",
        )
        return [], False

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _resolve_user(self, handle: str) -> Any | None:
        """
        Look up a Twitter/X user by handle using the v2 API.

        Calls ``tweepy.Client.get_user(username=handle)`` and returns the
        user object (which has an ``.id`` attribute for subsequent timeline
        calls), or ``None`` if the user is not found or the call fails.

        Handles are stripped of leading ``@`` before the API call because
        Twitter v2 ``username`` field does not include the ``@`` prefix.

        Args:
            handle: Twitter handle, with or without the leading ``@``.

        Returns:
            A tweepy ``User`` object with ``.id`` and ``.username``,
            or ``None`` on not-found or error.

        Traces: SRC-067 (per-handle user resolution)
        """
        # Strip leading @ if present (config stores handles without @, but be safe)
        clean_handle = handle.lstrip("@")
        try:
            response = self._client.get_user(username=clean_handle)
            if response and response.data:
                return response.data
            log.debug("twitter_user_not_found", handle=clean_handle)
            return None
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "twitter_resolve_user_error",
                handle=clean_handle,
                error=str(exc),
            )
            return None

    def _fetch_tweets(
        self,
        user_id: str,
        start: datetime,
        end: datetime,
    ) -> list[Any]:
        """
        Fetch tweets from a single user within ``[start, end]`` via the Twitter
        v2 ``get_users_tweets`` endpoint, handling pagination automatically.

        Request parameters (SRC-067):
        - ``tweet_fields``: ``['created_at', 'entities', 'text', 'referenced_tweets']``
        - ``max_results``: 100 per page (Twitter v2 maximum).
        - ``start_time`` / ``end_time``: UTC-aware datetime objects; the API
          accepts ISO 8601 format and tweepy converts them transparently.

        Pagination: ``tweepy.Paginator`` is used to iterate all result pages
        within the window so that no tweets are missed even across rate-limit
        boundaries (``wait_on_rate_limit=True`` in ``__init__``).

        Args:
            user_id: Twitter numeric user ID (string).
            start:   Lookback window start (UTC-aware datetime).
            end:     Lookback window end (UTC-aware datetime).

        Returns:
            Flat list of raw tweepy ``Tweet`` data objects.

        Traces: SRC-067 (lookback window fetch — all pages within window)
        """
        all_tweets: list[Any] = []
        try:
            paginator = tweepy.Paginator(
                self._client.get_users_tweets,
                id=user_id,
                start_time=start,
                end_time=end,
                tweet_fields=_TWEET_FIELDS,
                expansions=_EXPANSIONS,
                max_results=_MAX_RESULTS_PER_PAGE,
            )
            for page in paginator:
                if page.data:
                    all_tweets.extend(page.data)
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "twitter_fetch_tweets_error",
                user_id=user_id,
                error=str(exc),
            )
            # Propagate so the caller's outer try/except catches it → degraded mode
            raise

        return all_tweets

    def _is_substantive(self, tweet: Any) -> bool:
        """
        Determine whether a tweet is substantive enough to be stored as signal.

        Filter rules (SRC-068):
        - **SKIP** pure replies: the text begins with ``@handle`` with no
          meaningful content before the mention.  These are conversational
          and rarely signal newsworthy topics.
        - **SKIP** bare retweets: the text starts with ``RT @``.  These add no
          original commentary or leads.
        - **KEEP** any tweet that contains a URL, regardless of length.
          A link is an explicit lead for web fetching (SRC-069, SRC-070).
        - **KEEP** original tweets whose text length (after stripping the
          Twitter card suffix ``https://t.co/…``) is ≥ 50 characters.  Short
          tweets without links rarely surface actionable topics.

        The 50-character threshold is a deliberate heuristic; it excludes
        one-word reactions and emoji-only posts while preserving substantive
        commentary that happens to be concise.

        Args:
            tweet: A tweepy ``Tweet`` data object (has ``.text`` attribute).

        Returns:
            ``True`` if the tweet should be stored as a :class:`TweetSignal`.

        Traces: SRC-068 (filter rules)
        """
        text: str = getattr(tweet, "text", "") or ""

        # Rule 1: skip bare retweets (SRC-068)
        if text.startswith("RT @"):
            return False

        # Rule 2: skip pure replies — text that begins with @mention (SRC-068)
        # A "pure reply" starts with @handle immediately; a quote-tweet or thread
        # reply that leads with original text is substantive.
        if _REPLY_RE.match(text):
            return False

        # Rule 3: keep if there is a URL (includes Twitter card t.co links)
        if _URL_RE.search(text):
            return True

        # Rule 4: keep original tweets that are long enough without a URL
        return len(text.strip()) >= _MIN_TWEET_CHARS

    def _hydrate_urls(self, tweet: Any) -> list[str]:
        """
        Expand t.co short links embedded in a tweet to their canonical destination URLs.

        Twitter wraps all URLs in ``https://t.co/…`` short links. The v2 API
        returns these pre-expanded in ``tweet.entities.urls[].expanded_url``
        when ``tweet_fields=['entities']`` is requested.

        If the entities block is absent (e.g. the tweet has no links or the
        field was not requested), an empty list is returned gracefully.

        Filtering: Twitter card meta-links ending in ``/photo/``, ``/video/``,
        or pointing to ``twitter.com``/``x.com`` themselves are excluded —
        these are media attachments, not external article URLs.

        Args:
            tweet: A tweepy ``Tweet`` data object.

        Returns:
            List of expanded, de-duplicated canonical URLs from ``entities.urls``.

        Traces: SRC-069 (hydrate linked URLs — expanded_url from entities)
        """
        urls: list[str] = []
        entities = getattr(tweet, "entities", None)
        if entities is None:
            return urls

        url_entries = getattr(entities, "urls", None) or []
        seen: set[str] = set()

        for entry in url_entries:
            # ``expanded_url`` is the canonical destination; fall back to ``url``
            expanded = getattr(entry, "expanded_url", None) or getattr(entry, "url", None)
            if not expanded:
                continue

            # Skip Twitter/X self-referential links (media cards, profile links, etc.)
            lower = expanded.lower()
            if "twitter.com" in lower or "x.com" in lower:
                continue

            if expanded not in seen:
                seen.add(expanded)
                urls.append(expanded)

        return urls

    def _normalize(
        self,
        tweet: Any,
        handle_cfg: TwitterHandleConfig,
        agent_id: str,
    ) -> TweetSignal:
        """
        Convert a raw tweepy ``Tweet`` data object to a :class:`TweetSignal`.

        Field mapping:
        - ``tweet_id``     ← ``tweet.id`` (cast to ``str`` for storage compatibility)
        - ``handle``       ← ``handle_cfg.handle`` (the configured @handle without ``@``)
        - ``text``         ← ``tweet.text``
        - ``created_at``   ← ``tweet.created_at`` (UTC-aware datetime from v2 API)
        - ``linked_urls``  ← ``_hydrate_urls(tweet)`` — expanded canonical URLs
        - ``weight``       ← ``handle_cfg.weight`` (SRC-046 — configurable signal weight)
        - ``agent_id``     ← passed in from the calling context (SRC-072)
        - ``fetched_at``   ← ``datetime.now(UTC)``

        ``created_at`` coercion: The Twitter v2 API returns an aware UTC
        datetime for ``created_at``.  If for any reason the value is timezone-
        naive, UTC is attached so all downstream comparisons remain safe.

        Args:
            tweet:      Raw tweepy ``Tweet`` object.
            handle_cfg: Matching :class:`TwitterHandleConfig` with weight.
            agent_id:   Scoped agent identifier for multi-agent isolation.

        Returns:
            A fully populated :class:`TweetSignal`.

        Traces: SRC-011 (storage fields), SRC-046 (handle weight),
                SRC-069 (linked_urls from entities), SRC-072 (agent_id scoping)
        """
        # Import here so the TYPE_CHECKING guard still works in tests
        from ai_news_agent.storage.models import TweetSignal  # noqa: PLC0415

        tweet_id: str = str(getattr(tweet, "id", ""))
        text: str = getattr(tweet, "text", "") or ""

        # Normalise created_at to UTC-aware datetime
        raw_dt: datetime | None = getattr(tweet, "created_at", None)
        if raw_dt is None:
            created_at = datetime.now(UTC)
        elif raw_dt.tzinfo is None:
            created_at = raw_dt.replace(tzinfo=UTC)
        else:
            created_at = raw_dt

        linked_urls = self._hydrate_urls(tweet)

        return TweetSignal(
            tweet_id=tweet_id,
            handle=handle_cfg.handle.lstrip("@"),
            text=text,
            created_at=created_at,
            linked_urls=linked_urls,
            agent_id=agent_id,
            fetched_at=datetime.now(UTC),
            weight=handle_cfg.weight,
        )
