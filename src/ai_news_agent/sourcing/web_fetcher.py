"""
sourcing/web_fetcher.py — Web article fetch strategies for the Sourcing Agent.

Provides:
  - Tier-aware search query construction across all 5 tiers (SRC-016–SRC-021)
  - Domain-to-tier classification for search result URLs (SRC-016–SRC-021)
  - ``WebFetcher.fetch_all`` — main sourcing entry point (SRC-053, SRC-060)
  - ``WebFetcher.fetch_from_tweet_urls`` — lead-generation from tweet links
    (SRC-069, SRC-070)
  - Publication date extraction from search-result metadata (SRC-011)
  - Per-run deduplication by url_hash to avoid redundant insertions (SRC-012)
  - URL enforcement — articles without a URL are dropped at source (SRC-049)

Search strategy (SRC-053, SRC-060):
  The ``AbstractSearchTool`` interface is the ONLY search primitive used here.
  Concrete implementations (NativeOpenAI / Brave / Tavily) are injected at
  construction time via the factory — this module never imports a provider
  directly, keeping the pipeline provider-agnostic.

Tier 1a — custom user sources:
  Each custom domain generates a ``site:<domain> AI news`` query so the sourcing
  agent can pull content from user-specified blogs and press rooms (SRC-017).

Tiers 1b–4 — standard thematic queries (SRC-018–SRC-021):
  Pre-defined query templates are used for each tier and are appended with a
  date-range suffix (``since:YYYY-MM-DD until:YYYY-MM-DD``) so the search
  provider knows we want recent results matching the lookback window (SRC-116).

Pub-date extraction:
  Search result snippets often contain ISO-8601-like or natural-language dates.
  ``_extract_pub_date`` attempts to parse these; on failure it falls back to
  ``fetched_at`` so every record has a non-null ``pub_date`` (SRC-011).

Traces: SRC-011 (article storage schema), SRC-012 (url_hash dedup),
        SRC-016–SRC-021 (source tiers), SRC-049 (URL enforcement),
        SRC-053 (configurable fetch methods), SRC-060 (abstract tool use),
        SRC-069 (hydrate tweet URLs), SRC-070 (tweet lead-generation),
        SRC-116 (concrete ISO date range in queries)
"""

from __future__ import annotations

import re
import urllib.parse
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import structlog

from ai_news_agent.storage.models import ArticleRecord, normalize_url, url_hash

if TYPE_CHECKING:
    from ai_news_agent.config.models import AgentConfig
    from ai_news_agent.llm.base import AbstractLLMClient, SearchResult
    from ai_news_agent.llm.search_tools import AbstractSearchTool

log = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Default per-tier search query templates (SRC-016–SRC-021)
#
# These queries are designed to cast a wide net across each tier's topic space.
# They are appended with a date suffix at runtime so results stay within the
# lookback window.  Users can override these at the config level in future
# (currently the list is hard-coded as a sensible default — SRC-034).
# ---------------------------------------------------------------------------

_TIER_QUERIES: dict[str, list[str]] = {
    # Tier 1b — popular business press (SRC-018)
    "1b": [
        "AI artificial intelligence business strategy news",
        "AI enterprise adoption regulations business impact",
        "artificial intelligence mergers acquisitions market news",
    ],
    # Tier 2 — top AI / tech company blogs (SRC-019)
    "2": [
        "AI machine learning technology blog announcement",
        "OpenAI Anthropic Google DeepMind AI model release",
        "large language model AI product launch blog",
    ],
    # Tier 3 — tech business press (SRC-020)
    "3": [
        "AI technology industry news startup funding",
        "artificial intelligence policy legislation news analysis",
        "AI workforce impact jobs automation news",
    ],
    # Tier 4 — policy and research institutions (SRC-021)
    "4": [
        "AI policy regulation government research report",
        "AI governance safety ethics research paper policy",
        "AI societal impact study research institute",
    ],
}

# ---------------------------------------------------------------------------
# Publication date extraction helpers (SRC-011)
# ---------------------------------------------------------------------------

# Match ISO 8601-style dates (YYYY-MM-DD) or written dates (Month D, YYYY)
_ISO_DATE_RE = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")
_NATURAL_DATE_RE = re.compile(
    r"\b(January|February|March|April|May|June|July|August|September|October|November|December)"
    r"\s+(\d{1,2}),?\s+(\d{4})\b",
    re.IGNORECASE,
)
_MONTH_ABBR_MAP = {
    "january": 1,
    "february": 2,
    "march": 3,
    "april": 4,
    "may": 5,
    "june": 6,
    "july": 7,
    "august": 8,
    "september": 9,
    "october": 10,
    "november": 11,
    "december": 12,
}


def _extract_pub_date(snippet: str | None, fetched_at: datetime) -> datetime:
    """
    Attempt to parse a publication date from a search-result snippet.

    Strategy:
    1. Scan for an ISO-8601 date: ``YYYY-MM-DD``.
    2. Scan for a natural-language date: ``Month D, YYYY``.
    3. Fall back to ``fetched_at`` (ensures non-null pub_date — SRC-011).

    The returned datetime is UTC-aware.

    Args:
        snippet:    Raw snippet text from the search result.
        fetched_at: Sourcing-run timestamp as fallback.

    Returns:
        A UTC-aware :class:`datetime`.

    Traces: SRC-011 (pub_date field — must be present on every ArticleRecord)
    """
    if snippet:
        # Attempt 1: ISO date
        m = _ISO_DATE_RE.search(snippet)
        if m:
            try:
                year, month, day = int(m.group(1)), int(m.group(2)), int(m.group(3))
                return datetime(year, month, day, tzinfo=UTC)
            except ValueError:
                pass

        # Attempt 2: natural-language date
        m2 = _NATURAL_DATE_RE.search(snippet)
        if m2:
            try:
                month_name = m2.group(1).lower()
                month = _MONTH_ABBR_MAP[month_name]
                day = int(m2.group(2))
                year = int(m2.group(3))
                return datetime(year, month, day, tzinfo=UTC)
            except (ValueError, KeyError):
                pass

    # Fallback: use fetched_at (SRC-011 — pub_date must be present)
    return fetched_at


# ---------------------------------------------------------------------------
# Domain helpers
# ---------------------------------------------------------------------------


def _strip_www(s: str) -> str:
    """Remove a leading 'www.' prefix from a domain string."""
    return s[4:] if s.startswith("www.") else s


def _classify_tier(url: str, config: AgentConfig) -> str:
    """
    Classify a URL into its source tier by domain-matching against the agent's
    configured source lists.

    Tier priority (SRC-016–SRC-021):
    - Tier 1a: custom user-specified sources (highest priority — SRC-017)
    - Tier 1b: popular business press (SRC-018)
    - Tier 2:  top AI / tech blogs (SRC-019)
    - Tier 3:  tech business press (SRC-020)
    - Tier 4:  policy / research (SRC-021)
    - ``"unknown"``: no tier matched — article may still be stored and curated

    Domain matching is substring-based so that sub-domains (e.g. ``blog.openai.com``)
    match the configured domain (``openai.com``).  Both the URL netloc and the
    configured domain are normalised (lowercased, ``www.`` stripped) before
    comparison.

    Args:
        url:    Candidate article URL.
        config: Per-agent configuration carrying the source tier lists.

    Returns:
        A tier string: ``"1a"`` | ``"1b"`` | ``"2"`` | ``"3"`` | ``"4"`` | ``"unknown"``.

    Traces: SRC-016–SRC-021 (tier hierarchy)
    """
    try:
        domain = _strip_www(urllib.parse.urlparse(url).netloc.lower())
    except Exception:  # noqa: BLE001
        return "unknown"

    sources = config.sources

    # Tier 1a — highest priority (SRC-017)
    for custom_domain in sources.custom:
        if _strip_www(custom_domain.lower()) in domain:
            return "1a"

    # Tier 1b — popular business press (SRC-018)
    for tier1b_domain in sources.tier_1b:
        if _strip_www(tier1b_domain.lower()) in domain:
            return "1b"

    # Tier 2 — top AI / tech blogs (SRC-019)
    for tier2_domain in sources.tier_2:
        if _strip_www(tier2_domain.lower()) in domain:
            return "2"

    # Tier 3 — tech business press (SRC-020)
    for tier3_domain in sources.tier_3:
        if _strip_www(tier3_domain.lower()) in domain:
            return "3"

    # Tier 4 — policy / research (SRC-021)
    for tier4_domain in sources.tier_4:
        if _strip_www(tier4_domain.lower()) in domain:
            return "4"

    return "unknown"


# ---------------------------------------------------------------------------
# SearchResult → ArticleRecord conversion (SRC-011, SRC-012, SRC-049)
# ---------------------------------------------------------------------------


def _search_result_to_record(
    result: SearchResult,
    tier: str,
    agent_id: str,
    fetched_at: datetime,
    pub_date_fallback: datetime | None = None,
) -> ArticleRecord | None:
    """
    Convert a :class:`SearchResult` to an :class:`ArticleRecord`.

    Returns ``None`` if the result has no URL (SRC-049 — URL enforcement).

    URL normalisation strips tracking parameters before hashing so that
    ``https://reuters.com/article?utm_source=rss`` and
    ``https://reuters.com/article`` hash to the same dedup key (SRC-012).

    Publication date is extracted from the snippet when possible; otherwise
    it falls back to ``fetched_at`` (SRC-011 — pub_date must be present).

    Args:
        result:     Normalised search result from the search tool.
        tier:       Source tier label derived from the query context or URL.
        agent_id:   Scoped agent identifier (SRC-072).
        fetched_at: Sourcing-run timestamp.

    Returns:
        A populated :class:`ArticleRecord`, or ``None`` if URL is missing.

    Traces: SRC-011 (storage schema), SRC-012 (url_hash dedup key),
            SRC-049 (URL required — None → dropped at caller)
    """
    if not result.url:
        log.debug("web_fetcher_skip_no_url", title=(result.title or "")[:60])
        return None

    canonical = normalize_url(result.url)
    if not canonical:
        return None

    record_hash = url_hash(canonical)

    # Best-effort pub_date from snippet (SRC-011).
    # When the caller provides ``pub_date_fallback`` (e.g. for a backfill run
    # whose window ends well before "now"), prefer that over ``fetched_at`` so
    # articles whose snippet has no parseable date still land *inside* the
    # curation window. Without this, a backfill weekly run that uses OpenAI
    # native search — where snippets rarely carry parseable dates — would
    # stamp every article with today's timestamp and the curation step would
    # silently drop them all.
    fallback = pub_date_fallback if pub_date_fallback is not None else fetched_at
    pub_date = _extract_pub_date(result.snippet, fallback)

    return ArticleRecord(
        url_hash=record_hash,
        url=canonical,
        headline=result.title or canonical,
        abstract=result.snippet or None,
        source_name=result.source or _strip_www(urllib.parse.urlparse(canonical).netloc),
        pub_date=pub_date,
        fetched_at=fetched_at,
        tier=tier,
        source_class="web",
        agent_id=agent_id,
        twitter_handle=None,
        tweet_url=None,
    )


# ---------------------------------------------------------------------------
# WebFetcher
# ---------------------------------------------------------------------------


class WebFetcher:
    """
    Fetches web articles from all configured source tiers using the injected
    search tool (NativeOpenAI / Brave / Tavily). When ``search_tool=None``,
    ``fetch_all`` returns ``[]`` and ``fetch_from_tweet_urls`` creates stub
    records without hydration — Twitter URLs are still preserved.

    Configurable search strategies (SRC-053, SRC-060):
    - Delegates all search calls to the injected :class:`AbstractSearchTool`
      — never imports a concrete search provider.
    - Queries are constructed per-tier with a date-range suffix derived from
      the lookback window (SRC-116).
    - Results are normalised to :class:`ArticleRecord` instances.
    - Within a single ``fetch_all`` call, duplicate URL hashes are suppressed
      so a URL appearing in multiple tier queries is stored only once (SRC-012).

    Two main entry points:
    - ``fetch_all``            — primary sourcing run across all tiers.
    - ``fetch_from_tweet_urls`` — secondary enrichment from tweet-linked URLs
                                  (SRC-069, SRC-070).

    Traces: SRC-011 (storage schema), SRC-012 (dedup), SRC-016–SRC-021 (tiers),
            SRC-049 (URL enforcement), SRC-053 (configurable fetch),
            SRC-060 (abstract tool use), SRC-069–SRC-070 (tweet URL enrichment),
            SRC-116 (concrete date range in queries)
    """

    def __init__(
        self,
        config: AgentConfig,
        llm_client: AbstractLLMClient,
        search_tool: AbstractSearchTool | None = None,
    ) -> None:
        """
        Args:
            config:      Per-agent configuration (provides source tier lists, SRC-016–SRC-021).
            llm_client:  Provider-agnostic LLM client (reserved for future prompt-based
                         sourcing expansion; not used for search queries today).
            search_tool: Injected search tool — NativeOpenAI / Brave / Tavily (SRC-060).
                         ``None`` disables web fetching; ``fetch_all`` returns ``[]``
                         and ``fetch_from_tweet_urls`` skips hydration.
        """
        self._config = config
        self._llm = llm_client
        self._search_tool = search_tool

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def fetch_all(
        self,
        window_start: datetime,
        window_end: datetime,
    ) -> list[ArticleRecord]:
        """
        Run searches across all configured source tiers and return candidate articles.

        Steps:
        1. Build (tier, query) pairs for all tiers, appending a date-range suffix
           derived from ``window_start``/``window_end`` (SRC-116).
        2. For each query, call the search tool with up to 15 results (SRC-121).
        3. Convert each result to an :class:`ArticleRecord` via
           ``_search_result_to_record``.
        4. Classify the URL into the correct tier by domain matching (SRC-016–SRC-021).
        5. Skip records with no URL (SRC-049) or duplicate url_hash within this
           run (SRC-012).

        Articles without URLs are silently skipped (SRC-049).
        Tier classification is refined from the actual URL domain after retrieval
        so that results from wrong-tier queries are correctly labelled (SRC-016–SRC-021).

        Args:
            window_start: Start of the lookback window (UTC-aware datetime).
            window_end:   End of the lookback window (UTC-aware datetime).

        Returns:
            Deduplicated list of :class:`ArticleRecord` objects.

        Traces: SRC-008–SRC-011, SRC-012, SRC-016–SRC-021, SRC-049,
                SRC-053, SRC-060, SRC-116
        """
        if self._search_tool is None:
            log.debug("web_fetcher_skipped", reason="no search tool configured")
            return []

        fetched_at = datetime.now(UTC)
        articles: list[ArticleRecord] = []
        seen_hashes: set[str] = set()  # intra-run dedup (SRC-012)

        # When sourcing a window that ends > 24h ago (i.e. a backfill), use the
        # window midpoint as the pub_date fallback for results whose snippet
        # carries no parseable date. This keeps backfilled articles inside the
        # curation window — otherwise they'd be stamped with ``now`` and the
        # downstream cadence filter would drop them. ``None`` means "no
        # backfill — use ``fetched_at`` as before."
        pub_date_fallback: datetime | None = None
        if window_end < fetched_at - timedelta(days=1):
            pub_date_fallback = window_start + (window_end - window_start) / 2
            log.info(
                "web_fetcher_backfill_pub_date_fallback",
                agent_id=self._config.agent_id,
                window_start=window_start.isoformat(),
                window_end=window_end.isoformat(),
                fallback=pub_date_fallback.isoformat(),
            )

        # Build date-scoped query suffix (SRC-116 — concrete ISO dates, not relative phrases)
        date_suffix = (
            f"since:{window_start.strftime('%Y-%m-%d')} until:{window_end.strftime('%Y-%m-%d')}"
        )

        all_queries = self._build_tier_queries(date_suffix)

        for tier, query in all_queries:
            log.debug("web_fetcher_search", tier=tier, query=query[:120])
            try:
                results = self._search_tool.search(query=query, n=15)
            except Exception as exc:  # noqa: BLE001
                log.warning(
                    "web_fetcher_search_error",
                    tier=tier,
                    query=query[:80],
                    error=str(exc),
                )
                continue

            for result in results:
                record = _search_result_to_record(
                    result=result,
                    tier=tier,  # initial tier from the query context
                    agent_id=self._config.agent_id,
                    fetched_at=fetched_at,
                    pub_date_fallback=pub_date_fallback,
                )
                if record is None:
                    # URL missing — dropped (SRC-049)
                    continue

                if record.url_hash in seen_hashes:
                    # Intra-run duplicate (SRC-012)
                    log.debug(
                        "web_fetcher_intra_run_dedup",
                        url=record.url[:80],
                    )
                    continue

                # Refine tier classification from the actual result URL (SRC-016–SRC-021)
                classified_tier = _classify_tier(record.url, self._config)
                if classified_tier != "unknown":
                    # Create a new record with the classified tier rather than
                    # the query-context tier (which may differ for edge cases)
                    record = ArticleRecord(
                        url_hash=record.url_hash,
                        url=record.url,
                        headline=record.headline,
                        abstract=record.abstract,
                        source_name=record.source_name,
                        pub_date=record.pub_date,
                        fetched_at=record.fetched_at,
                        tier=classified_tier,
                        source_class=record.source_class,
                        agent_id=record.agent_id,
                        twitter_handle=record.twitter_handle,
                        tweet_url=record.tweet_url,
                    )

                seen_hashes.add(record.url_hash)
                articles.append(record)

        log.info(
            "web_fetcher_complete",
            agent_id=self._config.agent_id,
            total_articles=len(articles),
            window_start=window_start.isoformat(),
            window_end=window_end.isoformat(),
        )
        return articles

    def fetch_from_tweet_urls(
        self,
        urls: list[str],
        agent_id: str,
    ) -> list[ArticleRecord]:
        """
        Fetch primary reporting articles from URLs extracted from tweet signals.

        Implements the tweet → web article lead-generation flow (SRC-069–SRC-070):
        1. For each URL, call ``search_tool.hydrate_url`` to retrieve the page
           content (SRC-069 — hydrate linked URLs from tweets).
        2. Build an :class:`ArticleRecord` from the URL and the fetched content
           — the headline is a best-effort extraction from the content; the
           curation LLM refines it later.
        3. Tier is classified from the URL domain (SRC-016–SRC-021); unknown
           domains default to Tier 3 (tech press — safest assumption).
        4. ``source_class`` is set to ``"web"`` because we are fetching the
           primary article, not storing the tweet itself.

        This method is intentionally separate from ``fetch_all`` so it can be
        called selectively when tweet signals are available without re-running
        the full tier search.

        Args:
            urls:     Canonical URLs extracted from tweet ``entities.urls``
                      (after t.co expansion by ``TwitterClient._hydrate_urls``).
            agent_id: Agent scope for the produced records (SRC-072).

        Returns:
            List of :class:`ArticleRecord` objects (may be shorter than ``urls``
            if some hydrations fail or return empty content).

        Traces: SRC-069 (hydrate linked URLs), SRC-070 (web search for primary reporting)
        """
        fetched_at = datetime.now(UTC)
        articles: list[ArticleRecord] = []
        seen_hashes: set[str] = set()

        for url in urls:
            if not url:
                continue

            canonical = normalize_url(url)
            if not canonical:
                continue

            record_hash = url_hash(canonical)
            if record_hash in seen_hashes:
                continue

            if self._search_tool is not None:
                try:
                    content = self._search_tool.hydrate_url(canonical)
                except Exception as exc:  # noqa: BLE001
                    log.debug("tweet_url_hydrate_error", url=canonical[:80], error=str(exc))
                    content = None
            else:
                content = None

            if not content:
                log.debug("tweet_url_hydrate_empty", url=canonical[:80])
                # Still create a stub record — the URL itself is the value
                content = None

            # Extract a best-effort headline from the HTML title tag or first line
            headline = _extract_headline(content, canonical)

            tier = _classify_tier(canonical, self._config)
            if tier == "unknown":
                tier = "3"  # default to tier 3 (tech press) for unclassified tweet links

            record = ArticleRecord(
                url_hash=record_hash,
                url=canonical,
                headline=headline,
                abstract=content[:500] if content else None,
                source_name=_strip_www(urllib.parse.urlparse(canonical).netloc),
                pub_date=fetched_at,
                fetched_at=fetched_at,
                tier=tier,
                source_class="web",
                agent_id=agent_id,
                twitter_handle=None,
                tweet_url=None,
            )

            seen_hashes.add(record_hash)
            articles.append(record)

        log.info(
            "web_fetcher_tweet_urls_complete",
            agent_id=agent_id,
            urls_processed=len(urls),
            articles_produced=len(articles),
        )
        return articles

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _build_tier_queries(self, date_suffix: str) -> list[tuple[str, str]]:
        """
        Build ``(tier_label, query_string)`` pairs for all enabled source tiers.

        Tier 1a custom sources get targeted ``site:<domain>`` queries to pull
        content specifically from the user's priority sources (SRC-017).

        Standard tiers (1b–4) use pre-defined thematic query templates from
        ``_TIER_QUERIES``, each appended with the date-range suffix (SRC-018–SRC-021).

        Args:
            date_suffix: ISO date range suffix, e.g.
                         ``"since:2026-05-09 until:2026-05-10"``.

        Returns:
            List of ``(tier_label, full_query)`` tuples.  The order preserves
            tier priority so Tier 1a queries are submitted first.

        Traces: SRC-016–SRC-021 (tier queries), SRC-116 (date range in queries)
        """
        queries: list[tuple[str, str]] = []

        # Tier 1a — targeted queries for each custom domain (SRC-017)
        for domain in self._config.sources.custom:
            queries.append(
                (
                    "1a",
                    f"site:{domain} AI artificial intelligence news {date_suffix}",
                )
            )

        # Tiers 1b–4 — standard thematic queries (SRC-018–SRC-021)
        for tier_key, tier_query_list in _TIER_QUERIES.items():
            for q in tier_query_list:
                queries.append((tier_key, f"{q} {date_suffix}"))

        return queries


# ---------------------------------------------------------------------------
# Headline extraction helper
# ---------------------------------------------------------------------------

# HTML <title> tag
_TITLE_TAG_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)
# Markdown-style first heading
_MD_HEADING_RE = re.compile(r"^#+ +(.+)$", re.MULTILINE)


def _extract_headline(content: str | None, fallback: str) -> str:
    """
    Best-effort headline extraction from fetched page content.

    Strategy:
    1. Try ``<title>...</title>`` tag in HTML.
    2. Try first Markdown heading (``# ...``).
    3. Fall back to the URL itself (always a non-empty string — SRC-049).

    Args:
        content:  Raw page content returned by ``hydrate_url``, or ``None``.
        fallback: URL to use when no headline can be extracted.

    Returns:
        A non-empty headline string (≤ 200 chars for storage sanity).
    """
    if content:
        # HTML title tag
        m = _TITLE_TAG_RE.search(content)
        if m:
            title = re.sub(r"\s+", " ", m.group(1)).strip()
            if title:
                return title[:200]

        # Markdown heading
        m2 = _MD_HEADING_RE.search(content)
        if m2:
            heading = m2.group(1).strip()
            if heading:
                return heading[:200]

    return fallback[:200]
