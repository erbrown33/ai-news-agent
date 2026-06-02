"""
llm/search_tools.py — AbstractSearchTool and concrete provider adapters.

Three implementations:
  NativeOpenAISearchTool — OpenAI Responses API web_search tool (fallback for provider=openai)
  BraveSearchTool        — Brave Search API via httpx (key from WEB_SEARCH_API_KEY)
  TavilySearchTool       — Tavily Search API via tavily-python (key from WEB_SEARCH_API_KEY)

All implementations return the identical ``list[SearchResult]`` shape so the pipeline
above this layer is completely provider-agnostic. (SRC-060)

Traces: SRC-060 (abstract tool use; Brave/Tavily search),
        SRC-069 (hydrate linked URLs from tweets),
        SRC-109 (WEB_SEARCH_API_KEY),
        SRC-056 (provider-agnostic — no provider types leak to pipeline)
"""

from __future__ import annotations

import json
import re
import time
import urllib.parse
from abc import ABC, abstractmethod
from typing import Any

import httpx
import structlog

from ai_news_agent.llm.base import SearchResult

log = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

_MAX_SNIPPET_LEN = 500  # truncate snippets to keep token usage sane


def _extract_domain(url: str) -> str:
    """Return the registered domain (netloc minus www.) from a URL."""
    try:
        netloc = urllib.parse.urlparse(url).netloc.lower()
        return netloc[4:] if netloc.startswith("www.") else netloc
    except Exception:  # noqa: BLE001
        return ""


# ---------------------------------------------------------------------------
# Abstract base
# ---------------------------------------------------------------------------


class AbstractSearchTool(ABC):
    """
    Provider-agnostic search tool interface.

    Concrete implementations:
      NativeOpenAISearchTool — OpenAI Responses API web_search (fallback for provider=openai)
      BraveSearchTool        — Brave Search API (key from WEB_SEARCH_API_KEY)
      TavilySearchTool       — Tavily Search API (key from WEB_SEARCH_API_KEY)

    The pipeline depends **only** on this interface so search providers can be
    swapped transparently. (SRC-060)

    Traces: SRC-060 (abstract tool use — swap search provider without pipeline changes)
    """

    @abstractmethod
    def search(self, query: str, n: int = 10) -> list[SearchResult]:
        """
        Execute a web search and return normalised results.

        Args:
            query: Plain text search query.
            n:     Desired number of results (treated as a hint; actual count may vary).

        Returns:
            List of :class:`SearchResult` — same shape for every implementation.

        Traces: SRC-060
        """

    @abstractmethod
    def hydrate_url(self, url: str) -> str | None:
        """
        Fetch and return the primary text content of ``url``.

        Used to hydrate tweet-linked URLs so the sourcing agent can build an
        :class:`ArticleRecord` abstract even when no search result is available.
        Returns ``None`` on any fetch failure (network error, 4xx/5xx, empty body).

        Traces: SRC-069 (hydrate linked URLs from tweets)
        """


# ---------------------------------------------------------------------------
# NativeOpenAISearchTool — OpenAI Responses API web_search tool
# ---------------------------------------------------------------------------


class NativeOpenAISearchTool(AbstractSearchTool):
    """
    Uses OpenAI's built-in web_search tool via the Responses API.

    Used as a fallback when ``llm.provider = "openai"`` and no
    ``WEB_SEARCH_API_KEY`` is set — no external API key required beyond the
    existing OpenAI key. Brave/Tavily take priority when explicitly configured.

    Traces: SRC-060 (native tool — OpenAI fallback), SRC-057 (OpenAI default)
    """

    _JSON_RE = re.compile(r"\[\s*\{.*\}\s*\]", re.DOTALL)
    _MD_LINK_RE = re.compile(r"\[([^\]]+)\]\((https?://[^)\s]+)\)")
    _BARE_URL_RE = re.compile(r"https?://[^\s)>\]]+")
    _TITLE_PREFIX_RE = re.compile(
        r"^\s*(?:\d+[.)]\s*)?(?:[-*•]\s*)?(?:\*+\s*)?"
        r"(?:source|title|article|read|reference|via)\s*[:\-—]\s*",
        re.IGNORECASE,
    )

    def __init__(self, client: Any) -> None:
        self._client = client
        self._http = httpx.Client(
            timeout=15.0,
            follow_redirects=True,
            headers={"User-Agent": "ai-news-agent/1.0 (research bot)"},
        )

    def search(self, query: str, n: int = 10) -> list[SearchResult]:
        """Call the OpenAI Responses API with the ``web_search_preview`` tool."""
        log.debug("native_openai_search", query=query[:80], n=n)
        try:
            response = self._client.responses.create(
                model="gpt-4o-mini",
                tools=[{"type": "web_search_preview"}],
                input=f"Search for: {query}\nReturn the top {n} results with title, URL, and snippet.",
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("native_openai_search_error", error=str(exc))
            return []

        results: list[SearchResult] = []
        for item in response.output or []:
            if hasattr(item, "content"):
                for block in item.content or []:
                    results.extend(self._parse_search_block(getattr(block, "text", "")))

        log.debug("native_openai_search_results", count=len(results))
        return results[:n]

    def _parse_search_block(self, text: str) -> list[SearchResult]:
        if not text:
            return []
        match = self._JSON_RE.search(text)
        if match:
            try:
                items = json.loads(match.group(0))
                results = [
                    SearchResult(
                        url=item.get("url", ""),
                        title=item.get("title", "")[:200],
                        snippet=item.get("snippet", "")[:_MAX_SNIPPET_LEN],
                        source=_extract_domain(item.get("url", "")),
                    )
                    for item in items
                    if isinstance(item, dict) and item.get("url")
                ]
                if results:
                    return results
            except (json.JSONDecodeError, KeyError, TypeError):
                pass
        return self._fallback_parse_markdown(text)

    def _fallback_parse_markdown(self, text: str) -> list[SearchResult]:
        lines = text.splitlines()
        results: list[SearchResult] = []
        seen_urls: set[str] = set()
        last_prose = ""

        for idx, raw_line in enumerate(lines):
            line = raw_line.rstrip()
            stripped = line.strip()
            if not stripped:
                continue

            md_links = list(self._MD_LINK_RE.finditer(line))
            if md_links:
                for m in md_links:
                    link_text = m.group(1).strip()
                    url = m.group(2).rstrip(".,)")
                    if not url or url in seen_urls:
                        continue
                    seen_urls.add(url)
                    title = self._clean_title(link_text) or _extract_domain(url) or url
                    snippet = self._collect_snippet(lines, idx, exclude_text=link_text, title=title)
                    if not snippet and last_prose:
                        snippet = self._clean_snippet(last_prose, title=title)
                    results.append(
                        SearchResult(
                            url=url,
                            title=title[:200],
                            snippet=snippet[:_MAX_SNIPPET_LEN],
                            source=_extract_domain(url),
                        )
                    )
                continue

            url_match = self._BARE_URL_RE.search(line)
            if url_match:
                url = url_match.group(0).rstrip(".,)")
                if not url or url in seen_urls:
                    continue
                seen_urls.add(url)
                before = line[: url_match.start()]
                title = self._clean_title(before) or _extract_domain(url) or url
                snippet = self._collect_snippet(lines, idx, exclude_text=url, title=title)
                if not snippet and last_prose:
                    snippet = self._clean_snippet(last_prose, title=title)
                results.append(
                    SearchResult(
                        url=url,
                        title=title[:200],
                        snippet=snippet[:_MAX_SNIPPET_LEN],
                        source=_extract_domain(url),
                    )
                )
                continue

            last_prose = stripped

        return results

    @classmethod
    def _clean_title(cls, raw: str) -> str:
        if not raw:
            return ""
        cleaned = cls._TITLE_PREFIX_RE.sub("", raw)
        cleaned = cleaned.strip().strip("*_`#[]()<> \t-•")
        return re.sub(r"\s+", " ", cleaned).strip()

    @classmethod
    def _clean_snippet(cls, raw: str, title: str = "") -> str:
        if not raw:
            return ""
        snippet = cls._MD_LINK_RE.sub(r"\1", raw)
        snippet = re.sub(r"\s+", " ", snippet).strip().strip("*_`#[]()<> \t-•")
        return "" if (title and snippet.lower() == title.lower()) else snippet

    @classmethod
    def _collect_snippet(cls, lines: list[str], idx: int, exclude_text: str, title: str) -> str:
        remainder = lines[idx].replace(exclude_text, " ").strip()
        snippet = cls._clean_snippet(remainder, title=title)
        if snippet:
            return snippet
        for follower in lines[idx + 1 : idx + 4]:
            f = follower.strip()
            if not f:
                continue
            if cls._MD_LINK_RE.search(f) or cls._BARE_URL_RE.search(f):
                break
            cleaned = cls._clean_snippet(f, title=title)
            if cleaned:
                return cleaned
            break
        return ""

    def hydrate_url(self, url: str) -> str | None:
        """Fetch page content via plain HTTP GET. Returns first 2000 chars or None."""
        if not url:
            return None
        try:
            resp = self._http.get(url)
            resp.raise_for_status()
            text = re.sub(r"\s+", " ", resp.text).strip()
            return text[:2000] if text else None
        except Exception as exc:  # noqa: BLE001
            log.debug("native_openai_hydrate_error", url=url[:80], error=str(exc))
            return None

    def close(self) -> None:
        self._http.close()


# ---------------------------------------------------------------------------
# BraveSearchTool — Brave Search API
# ---------------------------------------------------------------------------


class BraveSearchTool(AbstractSearchTool):
    """
    Brave Search API adapter via ``httpx``.

    API key sourced from ``WEB_SEARCH_API_KEY`` environment variable.
    Endpoint: ``https://api.search.brave.com/res/v1/web/search``

    Rate limiting: enforces a minimum interval between successive API calls
    (default 1.1 s — safe for Brave's free tier of 1 req/s) and retries on
    429 with exponential back-off so burst query sequences don't lose results.

    Traces: SRC-060 (Brave fallback), SRC-109 (WEB_SEARCH_API_KEY)
    """

    _BASE_URL = "https://api.search.brave.com/res/v1/web/search"
    _FRESHNESS_URL = "https://api.search.brave.com/res/v1/news/search"

    # Minimum seconds between API calls (free tier: 1 req/s; add 0.1 s headroom)
    _MIN_INTERVAL: float = 1.1
    _MAX_RETRIES: int = 3

    def __init__(self, api_key: str) -> None:
        self._api_key = api_key
        self._last_call_time: float = 0.0
        self._http = httpx.Client(
            timeout=15.0,
            follow_redirects=True,
            headers={
                "Accept": "application/json",
                "Accept-Encoding": "gzip",
                "X-Subscription-Token": api_key,
                "User-Agent": "ai-news-agent/1.0",
            },
        )

    def _throttle(self) -> None:
        """Sleep until the minimum inter-request interval has elapsed."""
        elapsed = time.monotonic() - self._last_call_time
        if elapsed < self._MIN_INTERVAL:
            time.sleep(self._MIN_INTERVAL - elapsed)
        self._last_call_time = time.monotonic()

    def search(self, query: str, n: int = 10) -> list[SearchResult]:
        """
        Call the Brave Web Search API and return normalised results.

        Uses the news-optimised endpoint when the query contains date filters
        (``since:`` / ``until:``), falling back to the general web endpoint.
        Retries up to ``_MAX_RETRIES`` times on 429 with exponential back-off.

        Traces: SRC-060 (Brave Search API fallback)
        """
        log.debug("brave_search", query=query[:80], n=n)

        # Choose endpoint: news endpoint has better recency for news queries
        endpoint = (
            self._FRESHNESS_URL
            if ("since:" in query or "until:" in query or "news" in query.lower())
            else self._BASE_URL
        )

        params = {
            "q": query,
            "count": min(n, 20),  # Brave max is 20 per call
            "text_decorations": "0",
            "search_lang": "en",
            "country": "us",
            "safesearch": "moderate",
        }

        delay = self._MIN_INTERVAL
        for attempt in range(self._MAX_RETRIES + 1):
            self._throttle()
            try:
                resp = self._http.get(endpoint, params=params)
                if resp.status_code == 429:
                    if attempt < self._MAX_RETRIES:
                        log.warning(
                            "brave_search_rate_limit",
                            attempt=attempt + 1,
                            retry_after=delay,
                        )
                        time.sleep(delay)
                        delay *= 2
                        continue
                    log.warning("brave_search_error", error=f"429 after {self._MAX_RETRIES} retries")
                    return []
                resp.raise_for_status()
                data = resp.json()
            except Exception as exc:  # noqa: BLE001
                log.warning("brave_search_error", error=str(exc))
                return []
            break

        return self._parse_response(data, n)

    def _parse_response(self, data: dict[str, Any], n: int) -> list[SearchResult]:
        """
        Parse a Brave API JSON response into ``SearchResult`` objects.

        The Brave API returns results under ``web.results`` (web endpoint) or
        ``results`` (news endpoint). Both shapes are handled.
        """
        results: list[SearchResult] = []

        # Web endpoint
        web_results = data.get("web", {}).get("results", [])
        # News endpoint
        news_results = data.get("results", [])

        for item in web_results + news_results:
            url = item.get("url") or item.get("link", "")
            if not url:
                continue
            title = item.get("title", "")[:200]
            snippet = (item.get("description") or item.get("extra_snippets", [""])[0] or "")[
                :_MAX_SNIPPET_LEN
            ]
            results.append(
                SearchResult(
                    url=url,
                    title=title,
                    snippet=snippet,
                    source=_extract_domain(url),
                )
            )
            if len(results) >= n:
                break

        log.debug("brave_search_results", count=len(results))
        return results

    def hydrate_url(self, url: str) -> str | None:
        """
        Fetch page content via plain HTTP GET using httpx (same client).

        Returns the first 2 000 characters of plain text, or ``None`` on failure.
        Traces: SRC-069 (hydrate linked tweet URLs)
        """
        if not url:
            return None
        try:
            resp = self._http.get(url)
            resp.raise_for_status()
            text = re.sub(r"\s+", " ", resp.text).strip()
            return text[:2000] if text else None
        except Exception as exc:  # noqa: BLE001
            log.debug("brave_hydrate_error", url=url[:80], error=str(exc))
            return None

    def close(self) -> None:
        """Close the underlying httpx client."""
        self._http.close()


# ---------------------------------------------------------------------------
# TavilySearchTool — Tavily Search API
# ---------------------------------------------------------------------------


class TavilySearchTool(AbstractSearchTool):
    """
    Tavily Search API adapter via ``tavily-python``.

    API key sourced from ``WEB_SEARCH_API_KEY`` environment variable.
    Tavily is optimised for LLM-oriented search with answer synthesis support.

    Install: ``pip install 'ai-news-agent[tavily]'``

    Traces: SRC-060 (Tavily fallback), SRC-109 (WEB_SEARCH_API_KEY)
    """

    def __init__(self, api_key: str) -> None:
        self._api_key = api_key
        # Deferred import — tavily-python is an optional dependency
        try:
            from tavily import TavilyClient  # type: ignore[import-untyped]

            self._client: Any = TavilyClient(api_key=api_key)
        except ImportError as exc:
            raise ImportError(
                "tavily-python is required for TavilySearchTool. "
                "Install with: pip install 'ai-news-agent[tavily]'"
            ) from exc

        # httpx client for hydrate_url (Tavily does not expose a raw fetch primitive)
        self._http = httpx.Client(
            timeout=15.0,
            follow_redirects=True,
            headers={"User-Agent": "ai-news-agent/1.0"},
        )

    def search(self, query: str, n: int = 10) -> list[SearchResult]:
        """
        Call the Tavily Search API and return normalised results.

        Uses ``search_depth="advanced"`` for deep-search cadences (decided by the
        caller via ``budget_hint``); otherwise ``"basic"``.

        Traces: SRC-060 (Tavily Search API fallback)
        """
        log.debug("tavily_search", query=query[:80], n=n)
        try:
            response = self._client.search(
                query=query,
                max_results=n,
                search_depth="basic",
                include_answer=False,
                include_raw_content=False,
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("tavily_search_error", error=str(exc))
            return []

        results: list[SearchResult] = []
        for item in response.get("results", []):
            url = item.get("url", "")
            if not url:
                continue
            results.append(
                SearchResult(
                    url=url,
                    title=item.get("title", "")[:200],
                    snippet=item.get("content", "")[:_MAX_SNIPPET_LEN],
                    source=_extract_domain(url),
                )
            )

        log.debug("tavily_search_results", count=len(results))
        return results

    def hydrate_url(self, url: str) -> str | None:
        """
        Fetch and return page text via Tavily's extract endpoint (or plain httpx
        GET if extract is not available on the current plan).

        Returns the first 2 000 characters, or ``None`` on failure.
        Traces: SRC-069
        """
        if not url:
            return None

        # Try Tavily extract first (richer content, better at JS-heavy pages)
        try:
            result = self._client.extract(urls=[url])
            for item in result.get("results", []):
                content = item.get("raw_content", "")
                if content:
                    return content[:2000]
        except Exception:  # noqa: BLE001
            pass

        # Fallback: plain HTTP GET
        try:
            resp = self._http.get(url)
            resp.raise_for_status()
            text = re.sub(r"\s+", " ", resp.text).strip()
            return text[:2000] if text else None
        except Exception as exc:  # noqa: BLE001
            log.debug("tavily_hydrate_error", url=url[:80], error=str(exc))
            return None

    def close(self) -> None:
        """Close the underlying httpx client."""
        self._http.close()
