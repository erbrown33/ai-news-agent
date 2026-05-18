"""
storage/models.py — Core data model dataclasses and Pydantic schemas.

Traces: SRC-008 (lookback windows), SRC-009 (daily 00:00–23:59 UTC),
        SRC-010 (multiple runs per day), SRC-011 (article storage fields),
        SRC-012 (url_hash dedup key), SRC-028–SRC-032 (cadence windows),
        SRC-048 (curated item schema), SRC-049 (URL required),
        SRC-129 (prompt_version SHA-256), SRC-150 (monitoring fields)
"""

from __future__ import annotations

import hashlib
import urllib.parse
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from enum import StrEnum
from typing import Any

from pydantic import BaseModel

# ---------------------------------------------------------------------------
# Cadence definitions — lookback windows (SRC-008–SRC-010, SRC-028–SRC-032)
# ---------------------------------------------------------------------------

class Cadence(StrEnum):
    """
    Supported digest cadences.
    Traces: SRC-008 (lookback windows), SRC-009 (daily), SRC-028–SRC-032
    """
    DAILY   = "daily"    # SRC-009, SRC-029
    WEEKLY  = "weekly"   # SRC-030
    MONTHLY = "monthly"  # SRC-031
    ANNUAL  = "annual"   # SRC-032


def lookback_window(cadence: Cadence, reference: datetime | None = None) -> tuple[datetime, datetime]:
    """
    Return the ``(window_start, window_end)`` for a cadence relative to
    ``reference`` (defaults to ``datetime.now(UTC)``).

    Rules (SRC-009, SRC-028–SRC-032):
    - daily   → previous calendar day 00:00–23:59 UTC
    - weekly  → previous Sunday-through-Saturday (Mon run covers Sun–Sat)
    - monthly → previous month first-through-last day
    - annual  → previous full calendar year (Jan 1 – Dec 31)

    The returned datetimes are always timezone-aware UTC.
    """
    ref = (reference or datetime.now(UTC)).replace(tzinfo=UTC)

    if cadence == Cadence.DAILY:
        # yesterday 00:00 → 23:59:59 UTC  (SRC-009)
        day_start = (ref - timedelta(days=1)).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        day_end = day_start.replace(hour=23, minute=59, second=59, microsecond=999999)
        return day_start, day_end

    if cadence == Cadence.WEEKLY:
        # Sunday-through-Saturday of the *previous* completed week (SRC-030).
        # Curation fires at 01:00 UTC on Sunday (curation_weekly: "0 1 * * 0").
        # The window covers the just-completed week: prior Sunday 00:00 → prior Saturday 23:59.
        #
        # isoweekday(): Monday=1, Tuesday=2, …, Saturday=6, Sunday=7
        #
        # Formula — days back to the most-recent completed Saturday:
        #   (isoweekday() % 7) + 1
        #   Mon(1%7=1)+1=2  → 2 days back = last Sat ✓
        #   Tue(2%7=2)+1=3  → 3 days back = last Sat ✓
        #   …
        #   Sat(6%7=6)+1=7  → 7 days back = last Sat ✓ (same Sat)
        #   Sun(7%7=0)+1=1  → 1 day back  = yesterday = last Sat ✓
        #
        # Without the modulo, Sun would give 7+1=8 which jumps to 2 weeks ago (SRC-030).
        today = ref.replace(hour=0, minute=0, second=0, microsecond=0)
        days_since_saturday = (today.isoweekday() % 7) + 1
        last_saturday = today - timedelta(days=days_since_saturday)   # most-recent Saturday
        last_sunday   = last_saturday - timedelta(days=6)             # Sunday before it
        return (
            last_sunday,
            last_saturday.replace(hour=23, minute=59, second=59, microsecond=999999),
        )

    if cadence == Cadence.MONTHLY:
        # previous month, first through last day (SRC-031)
        first_of_this_month = ref.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        last_of_prev_month  = first_of_this_month - timedelta(seconds=1)
        first_of_prev_month = last_of_prev_month.replace(
            day=1, hour=0, minute=0, second=0, microsecond=0
        )
        return (
            first_of_prev_month,
            last_of_prev_month.replace(hour=23, minute=59, second=59, microsecond=999999),
        )

    if cadence == Cadence.ANNUAL:
        # previous calendar year, Jan 1 through Dec 31 (SRC-032)
        year = ref.year - 1
        start = datetime(year, 1, 1, 0, 0, 0, tzinfo=UTC)
        end   = datetime(year, 12, 31, 23, 59, 59, 999999, tzinfo=UTC)
        return start, end

    raise ValueError(f"Unknown cadence: {cadence!r}")


# ---------------------------------------------------------------------------
# URL normalisation and hashing (SRC-012)
# ---------------------------------------------------------------------------

_TRACKING_PARAMS = frozenset(
    {
        "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
        "fbclid", "ref", "source", "campaign", "medium", "content",
        "gclid", "msclkid", "mc_cid", "mc_eid",
    }
)


def normalize_url(raw_url: str) -> str:
    """
    Canonical URL normalisation for deduplication.

    Steps (SRC-012):
    1. Parse with ``urllib.parse.urlparse()``.
    2. Lowercase scheme + netloc.
    3. Strip tracking query parameters (utm_*, fbclid, etc.).
    4. Strip trailing slash from path.
    5. Rebuild canonical URL string.

    Returns the normalised URL string.
    """
    parsed = urllib.parse.urlparse(raw_url)
    scheme = parsed.scheme.lower()
    netloc = parsed.netloc.lower()
    path = parsed.path.rstrip("/")

    # Strip tracking params
    qparams = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
    filtered = {k: v for k, v in qparams.items() if k.lower() not in _TRACKING_PARAMS}
    query = urllib.parse.urlencode(filtered, doseq=True)

    canonical = urllib.parse.urlunparse((scheme, netloc, path, "", query, ""))
    return canonical


def url_hash(canonical_url: str) -> str:
    """
    SHA-256 hex digest of a canonical URL — used as the primary dedup key.
    Traces: SRC-012 (deduplication)
    """
    return hashlib.sha256(canonical_url.encode("utf-8")).hexdigest()


def headline_similarity(a: str, b: str) -> float:
    """
    Compute normalised similarity between two headline strings using
    ``difflib.SequenceMatcher``.

    Returns a float in [0.0, 1.0].  Values ≥ 0.85 indicate the headlines
    are likely the same article from a different URL (AMP, redirect, etc.)
    and should be flagged as near-duplicates.

    Traces: SRC-012 (secondary dedup signal — architecture §3.3)
    """
    import difflib
    return difflib.SequenceMatcher(None, a.lower(), b.lower()).ratio()


# ---------------------------------------------------------------------------
# ArticleRecord — sourcing layer output (SRC-011)
# ---------------------------------------------------------------------------

@dataclass
class ArticleRecord:
    """
    A single candidate article as stored by the Sourcing Agent.

    ``url_hash`` (SHA-256 of the canonical URL) is the **primary dedup key**
    within an agent's store; the ``(url_hash, agent_id)`` pair must be unique.
    (SRC-012)

    Traces: SRC-011 (storage fields), SRC-012 (url_hash dedup key),
            SRC-048 (twitter fields), SRC-049 (url required — enforced upstream),
            SRC-072 (agent_id scoping)
    """

    # Primary identifier — dedup key (SRC-012)
    url_hash: str
    url: str          # canonical URL; required (SRC-049 enforced at curation + rendering)
    headline: str
    abstract: str | None
    source_name: str
    pub_date: datetime
    fetched_at: datetime   # when the sourcing agent retrieved it
    tier: str              # "1a" | "1b" | "2" | "3" | "4"
    source_class: str      # "web" | "twitter"  (SRC-150 quality monitoring)
    agent_id: str          # scopes record to this agent config (SRC-072)

    # Twitter provenance (present only when source_class == "twitter") (SRC-048)
    twitter_handle: str | None = None
    tweet_url: str | None = None


# ---------------------------------------------------------------------------
# TweetSignal — Twitter/X sourcing output (SRC-047, SRC-067–SRC-069)
# ---------------------------------------------------------------------------

@dataclass
class TweetSignal:
    """
    A filtered, hydrated tweet from a tracked influencer.
    Role: signal and lead-generation only — NOT primary news (SRC-047).
    Traces: SRC-047 (signal role), SRC-067–SRC-069 (fetch/filter/hydrate)
    """

    tweet_id: str
    handle: str
    text: str
    created_at: datetime
    linked_urls: list[str]    # hydrated expanded URLs from t.co (SRC-069)
    agent_id: str
    fetched_at: datetime
    weight: float = 1.0       # handle weight from config (SRC-046)


# ---------------------------------------------------------------------------
# DigestRecord — persisted curation run output (SRC-129, SRC-145, SRC-150)
# ---------------------------------------------------------------------------

@dataclass
class DigestRecord:
    """
    Persisted record of a completed curation run, stored in the document store
    for portal retrieval and run-history queries.

    Storing completed digests in the same store allows the portal to list
    available digests without scanning the filesystem, and enables re-runs
    to overwrite cleanly (SRC-145 — idempotency via run_date + cadence key).

    Traces: SRC-129 (prompt_version SHA-256), SRC-145 (idempotent filenames),
            SRC-150 (quality monitoring fields), SRC-072 (agent_id scoping)
    """

    # Compound key — (agent_id, cadence, run_date) is unique per run (SRC-145)
    agent_id: str
    cadence: str             # "daily" | "weekly" | "monthly" | "annual"
    run_date: date
    window_start: datetime
    window_end: datetime

    # Prompt provenance (SRC-129)
    prompt_version: str      # "sha256:<64-char hex>"

    # LLM provenance (SRC-150)
    llm_provider: str
    llm_model: str

    # Quality monitoring (SRC-150)
    items_considered: int
    items_included: int
    items_by_tier: dict[str, int]         # {"1a": n, "1b": n, ...}
    items_by_source_class: dict[str, int] # {"web": n, "twitter": n}
    twitter_signal_available: bool        # SRC-148
    tweet_api_call_count: int             # SRC-150 — 0 if degraded
    token_usage: int                      # SRC-150 — total tokens consumed

    # Output file paths (relative to output_dir) — populated by Rendering Agent
    md_path: str | None = None
    html_path: str | None = None
    json_path: str | None = None

    @property
    def digest_key(self) -> str:
        """Unique key string for this digest — used as the store dedup key."""
        return f"{self.agent_id}:{self.cadence}:{self.run_date.isoformat()}"


# ---------------------------------------------------------------------------
# CuratedItem — curation → rendering → portal (SRC-048)
# ---------------------------------------------------------------------------

@dataclass
class CuratedItem:
    """
    A single curated news item, output of the Curation Agent.
    Traces: SRC-048 (curated item schema), SRC-049 (URL required — empty = dropped),
            SRC-129 (prompt_version)
    """

    headline: str
    source_name: str
    url: str              # REQUIRED — empty/None = item dropped (SRC-049, SRC-141)
    pub_date: date
    why_it_matters: str   # 2–3 sentences (SRC-048, SRC-122)
    impact_tags: list[str]     # "business_impact" | "workforce_impact" | "policy_impact"
    tier: str                  # "1a" | "1b" | "2" | "3" | "4"
    cross_refs: list[str]      # related item URLs (SRC-048)
    twitter_handle: str | None  # SRC-048 — null if web-sourced
    tweet_url: str | None       # SRC-048 — null if web-sourced
    prompt_version: str         # "sha256:<64-char hex>" (SRC-129)


# ---------------------------------------------------------------------------
# DigestMetadata — quality monitoring metadata attached to every output (SRC-150)
# ---------------------------------------------------------------------------

@dataclass
class DigestMetadata:
    """
    Quality monitoring metadata attached to every digest output.
    Traces: SRC-129 (prompt_version), SRC-148 (twitter_signal_available),
            SRC-150 (all monitoring fields)
    """

    agent_id: str
    cadence: str         # "daily" | "weekly" | "monthly" | "annual"
    run_date: date
    window_start: datetime
    window_end: datetime
    prompt_version: str        # SRC-129 — "sha256:<hex>"
    llm_provider: str          # SRC-150
    llm_model: str             # SRC-150
    items_considered: int      # SRC-150 — total candidates from store
    items_included: int        # SRC-150 — after LLM selection + URL drop
    items_by_tier: dict[str, int]         # SRC-150 — {"1a": n, "1b": n, ...}
    items_by_source_class: dict[str, int] # SRC-150 — {"web": n, "twitter": n}
    twitter_signal_available: bool        # SRC-148
    tweet_api_call_count: int             # SRC-150 — 0 if degraded
    token_usage: int                      # SRC-150 — total tokens consumed


# ---------------------------------------------------------------------------
# CurationDiagnostics — explanation surfaced when a digest has few/no items
# ---------------------------------------------------------------------------

@dataclass
class CurationDiagnostics:
    """
    Explanation attached to a digest when the curated item count falls below
    a configured threshold (default 3).

    The goal is to make "why was today's digest empty?" answerable from the
    digest itself — without diving into logs. ``reasons`` are short, human-
    readable strings ordered most-to-least likely cause; the structured
    counters below let an operator verify each reason quickly.

    Field semantics:

    * ``threshold``: the items_included floor that triggered this block.
    * ``articles_in_store``: total articles for the agent across all time.
      A zero here means sourcing has never produced anything.
    * ``articles_in_window``: candidates whose ``pub_date`` fell inside the
      curation window (equals ``items_considered``).
    * ``articles_in_window_by_tier``: tier breakdown of in-window candidates.
    * ``items_dropped_no_url``: items the LLM returned that were dropped for
      missing/invalid URLs (SRC-049 / SRC-141).
    * ``twitter_signal_available``: mirrors the digest's Twitter flag.
    * ``reasons``: ordered list of human-readable explanations.

    Traces: SRC-150 (quality monitoring — diagnostic field for empty/sparse runs)
    """

    threshold: int
    articles_in_store: int
    articles_in_window: int
    articles_in_window_by_tier: dict[str, int]
    items_dropped_no_url: int
    twitter_signal_available: bool
    reasons: list[str]


# ---------------------------------------------------------------------------
# CuratedItemRaw — Pydantic schema matching what the LLM is asked to produce
# ---------------------------------------------------------------------------

class CuratedItemRaw(BaseModel):
    """
    The per-item schema that the LLM is instructed to produce inside the
    ```json block.  Parsed by parse_structured() → converted to CuratedItem.
    Traces: SRC-048 (curated item fields), SRC-120 (output format constraint)
    """

    headline: str
    source_name: str
    url: str = ""
    pub_date: str = ""            # "YYYY-MM-DD"
    why_it_matters: str = ""
    impact_tags: list[str] = field(default_factory=list)
    tier: str = ""
    cross_refs: list[str] = field(default_factory=list)
    twitter_handle: str | None = None
    tweet_url: str | None = None

    # Pydantic-compatible defaults
    model_config = {"extra": "ignore"}

    def __init__(self, **data: Any) -> None:
        # Provide list defaults in pydantic-compatible way
        if "impact_tags" not in data:
            data["impact_tags"] = []
        if "cross_refs" not in data:
            data["cross_refs"] = []
        super().__init__(**data)


# ---------------------------------------------------------------------------
# CurationResponse — top-level LLM output schema (SRC-061, SRC-120)
# ---------------------------------------------------------------------------

class CurationResponse(BaseModel):
    """
    Top-level schema the LLM is instructed to produce in the ```json block.
    Traces: SRC-061 (output parsing from plain text), SRC-120 (output format),
            SRC-124 (predictions — annual only)
    """

    items: list[CuratedItemRaw] = []
    themes: list[str] = []
    outlook: str = ""
    predictions: list[str] = []   # annual only (SRC-124)

    model_config = {"extra": "ignore"}
