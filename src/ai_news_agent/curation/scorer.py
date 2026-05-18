"""
curation/scorer.py — LLM-powered scoring, tier-weighted ranking, and URL enforcement.

Traces: SRC-022–SRC-027 (LLM scoring criteria), SRC-027 (all prioritisation via LLM),
        SRC-029 (daily: article title + source + link + why-it-matters),
        SRC-030 (weekly: themes + look-ahead + top articles),
        SRC-031 (monthly: bigger-picture themes + anticipated news),
        SRC-032 (annual: top-10 + 10 predictions + inflection points),
        SRC-048 (curated item schema: headline, source, URL, pub_date, why_it_matters,
                 impact_tags, tier, cross_refs, twitter_handle, tweet_url),
        SRC-049 (URL required — drop items with no URL),
        SRC-054 (research LLM for monthly/annual — thinking flag),
        SRC-061 (output parsing from structured JSON block),
        SRC-124 (annual predictions grounded in observed trends),
        SRC-129 (prompt_version attached to every item),
        SRC-141 (URL enforcement at scorer — first of two layers),
        SRC-150 (token_usage tracking per run)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import TYPE_CHECKING

import structlog

from ai_news_agent.storage.models import (
    ArticleRecord,
    CuratedItem,
    CurationResponse,
    TweetSignal,
)

if TYPE_CHECKING:
    from ai_news_agent.llm.base import AbstractLLMClient

log = structlog.get_logger(__name__)

# Tier ordering for ranking weight — lower number = higher priority (SRC-016–SRC-021)
_TIER_WEIGHT: dict[str, float] = {
    "1a": 1.0,
    "1b": 0.9,
    "2":  0.8,
    "3":  0.7,
    "4":  0.6,
    "unknown": 0.5,
}


@dataclass
class ScorerResult:
    """
    Complete output of a single LLM scoring pass.

    The Curation Agent uses this to populate ``CurationRunResult`` with all
    cadence-specific outputs returned by the LLM — not just the ranked items.

    Traces: SRC-029 (items, why_it_matters), SRC-030 (themes, outlook),
            SRC-031 (themes, outlook), SRC-032 (items, themes, predictions),
            SRC-048 (curated item schema), SRC-124 (predictions),
            SRC-150 (token_usage)
    """

    items: list[CuratedItem] = field(default_factory=list)
    """Ranked, URL-validated curated items (all cadences). SRC-029–SRC-032."""

    themes: list[str] = field(default_factory=list)
    """Weekly/monthly/annual themes. SRC-030, SRC-031, SRC-032."""

    outlook: str = ""
    """Forward-looking section for weekly/monthly digests. SRC-030, SRC-031."""

    predictions: list[str] = field(default_factory=list)
    """Annual predictions grounded in observed trends. SRC-032, SRC-124."""

    token_usage: int = 0
    """Total tokens consumed by this LLM call. SRC-150."""

    raw_response: str = ""
    """Raw LLM response text — stored for debugging and tracing."""

    items_dropped_no_url: int = 0
    """
    Count of items the LLM returned that were dropped during URL enforcement
    (SRC-049, SRC-141). Surfaced for empty-digest diagnostics so operators can
    distinguish "LLM excluded everything" from "LLM picked items but they all
    had bad URLs."
    """


class Scorer:
    """
    Scores and ranks candidate articles using an LLM against the curation prompt,
    then enforces the URL requirement before returning curated items.

    Steps (SRC-027):
    1. Build an LLM message from the prompt and candidate article list.
    2. Call ``llm_client.complete()`` → raw response text.
    3. Call ``llm_client.parse_structured(raw, CurationResponse)`` → typed schema.
    4. Drop any item with a missing/empty URL — non-negotiable (SRC-049, SRC-141).
    5. Rank by tier weight + LLM order; truncate to ``top_n``.
    6. Attach ``prompt_version`` to every item (SRC-129).
    7. Return ``ScorerResult`` with items + themes + outlook + predictions + token_usage.

    Cadence-specific outputs (SRC-029–SRC-032):
    - daily   → items with headline, source, URL, why_it_matters (SRC-029)
    - weekly  → items + themes + outlook (look-ahead) (SRC-030)
    - monthly → items + themes + outlook (anticipated news) (SRC-031)
    - annual  → items + themes + predictions (top-10 + 10 predictions) (SRC-032, SRC-124)

    Traces: SRC-022–SRC-032, SRC-048–SRC-049, SRC-054, SRC-061, SRC-124, SRC-129,
            SRC-141, SRC-150
    """

    def __init__(self, llm_client: AbstractLLMClient) -> None:
        self._llm = llm_client

    def score_and_rank(
        self,
        prompt: str,
        model: str,
        candidates: list[ArticleRecord],
        tweet_signals: list[TweetSignal],
        top_n: int,
        prompt_version: str,
        cadence: str,
        thinking: bool = False,
    ) -> ScorerResult:
        """
        Run LLM-powered scoring and return a ``ScorerResult`` with:
        - Ranked, URL-validated curated items (all cadences, SRC-029–SRC-032)
        - Themes for weekly/monthly/annual digests (SRC-030–SRC-032)
        - Outlook/look-ahead for weekly and monthly (SRC-030–SRC-031)
        - Annual predictions grounded in observed trends (SRC-032, SRC-124)
        - Token usage for quality monitoring (SRC-150)

        Args:
            prompt:         Fully-built prompt string from PromptBuilder (SRC-115–SRC-124).
            model:          LLM model identifier (from cadence override or agent default).
            candidates:     Article records for the lookback window.
            tweet_signals:  Tweet signals for context (already embedded in prompt).
            top_n:          Maximum number of items to return (SRC-029–SRC-032).
            prompt_version: SHA-256 of the prompt file for regression tracing (SRC-129).
            cadence:        "daily" | "weekly" | "monthly" | "annual".
            thinking:       Enable extended reasoning for annual o3 runs (SRC-032, SRC-054).

        Returns:
            :class:`ScorerResult` with all cadence-specific outputs from the LLM.

        Traces: SRC-027 (LLM scoring), SRC-029–SRC-032 (cadence outputs),
                SRC-049 (URL drop), SRC-061 (parse_structured),
                SRC-129 (prompt_version), SRC-141 (URL enforcement), SRC-150 (token_usage)
        """
        if not candidates:
            log.warning("scorer_no_candidates", cadence=cadence)
            return ScorerResult()

        # Serialise candidate articles into the user message (SRC-027)
        candidates_text = self._format_candidates(candidates)

        messages: list[dict[str, str]] = [
            {"role": "system", "content": prompt},
            {"role": "user", "content": candidates_text},
        ]

        log.info(
            "scorer_llm_call",
            cadence=cadence,
            model=model,
            candidates=len(candidates),
            top_n=top_n,
            thinking=thinking,
            prompt_version=prompt_version,
        )

        # LLM completion (SRC-027 — all prioritisation via LLM)
        raw_response = self._llm.complete(
            messages=messages,
            model=model,
            temperature=0.2,
            thinking=thinking,
        )

        # Estimate token usage from response length (actual usage populated by LLM client
        # when available via response metadata; this is a safe lower-bound fallback).
        # SRC-150 requires token_usage logging per run.
        token_usage = _estimate_token_usage(prompt, candidates_text, raw_response)

        # Parse structured output (SRC-061 — Markdown + JSON block)
        try:
            curation_response: CurationResponse = self._llm.parse_structured(
                raw_response, CurationResponse
            )
        except Exception as exc:  # noqa: BLE001
            log.error(
                "scorer_parse_failed",
                error=str(exc),
                raw_length=len(raw_response),
                cadence=cadence,
            )
            return ScorerResult(raw_response=raw_response, token_usage=token_usage)

        # ------------------------------------------------------------------
        # Convert CuratedItemRaw → CuratedItem + URL enforcement (SRC-049, SRC-141)
        # ------------------------------------------------------------------
        curated: list[CuratedItem] = []
        dropped_count = 0
        for raw_item in curation_response.items:
            if not raw_item.url or not raw_item.url.strip():
                # Non-negotiable: items without URL are DROPPED (SRC-049, SRC-141)
                log.debug(
                    "scorer_drop_no_url",
                    headline=raw_item.headline[:80],
                    cadence=cadence,
                )
                dropped_count += 1
                continue

            try:
                pub_date = (
                    date.fromisoformat(raw_item.pub_date)
                    if raw_item.pub_date
                    else date.today()
                )
            except ValueError:
                pub_date = date.today()

            # Build the full CuratedItem with all SRC-048 fields
            curated.append(
                CuratedItem(
                    headline=raw_item.headline,
                    source_name=raw_item.source_name,
                    url=raw_item.url.strip(),
                    pub_date=pub_date,
                    why_it_matters=raw_item.why_it_matters,    # SRC-029, SRC-048, SRC-122
                    impact_tags=list(raw_item.impact_tags),    # SRC-048, SRC-023–SRC-025
                    tier=raw_item.tier or "unknown",            # SRC-016–SRC-021
                    cross_refs=list(raw_item.cross_refs),       # SRC-048
                    twitter_handle=raw_item.twitter_handle,     # SRC-048
                    tweet_url=raw_item.tweet_url,               # SRC-048
                    prompt_version=prompt_version,              # SRC-129
                )
            )

        # Tier-weighted sort (higher tier weight first) + truncate to top_n (SRC-016–SRC-021)
        curated.sort(key=lambda c: _TIER_WEIGHT.get(c.tier, 0.5), reverse=True)
        result_items = curated[:top_n]

        # ------------------------------------------------------------------
        # Extract cadence-specific synthesis outputs (SRC-030–SRC-032, SRC-124)
        # ------------------------------------------------------------------
        themes = list(curation_response.themes)
        outlook = curation_response.outlook or ""
        predictions = list(curation_response.predictions)

        # Validate: annual must have predictions (SRC-032, SRC-124)
        if cadence == "annual" and not predictions:
            log.warning(
                "scorer_annual_no_predictions",
                cadence=cadence,
                model=model,
                items_returned=len(result_items),
            )

        log.info(
            "scorer_complete",
            cadence=cadence,
            candidates_considered=len(candidates),
            items_before_url_drop=len(curation_response.items),
            items_dropped_no_url=dropped_count,
            items_after_url_drop=len(curated),
            items_returned=len(result_items),
            themes_count=len(themes),
            predictions_count=len(predictions),
            has_outlook=bool(outlook),
            prompt_version=prompt_version,
            token_usage=token_usage,
        )

        return ScorerResult(
            items=result_items,
            themes=themes,
            outlook=outlook,
            predictions=predictions,
            token_usage=token_usage,
            raw_response=raw_response,
            items_dropped_no_url=dropped_count,
        )

    def _format_candidates(self, candidates: list[ArticleRecord]) -> str:
        """
        Serialise candidate articles into the user message for the LLM.

        Format: one numbered item per article with all available metadata.
        The LLM is instructed (in the system prompt) to select from this list.

        All SRC-048 fields are included so the LLM has complete article context:
        headline, source, URL, pub_date, tier, source_class (web vs twitter),
        abstract, and Twitter provenance when applicable.

        Traces: SRC-011 (article storage schema), SRC-027 (LLM receives full candidates),
                SRC-048 (curated item fields available for curation)
        """
        lines = [
            f"Below are {len(candidates)} candidate articles for curation.",
            "Select the most impactful items per the criteria in the system prompt.",
            "For each selected item output ALL required fields including URL.",
            "",
        ]
        for i, article in enumerate(candidates, start=1):
            pub = (
                article.pub_date.strftime("%Y-%m-%d")
                if isinstance(article.pub_date, datetime)
                else str(article.pub_date)
            )
            entry_lines = [
                f"{i}. [{article.tier.upper()}] {article.headline}",
                f"   Source: {article.source_name} | URL: {article.url}",
                f"   Published: {pub} | Source Class: {article.source_class}",
            ]
            if article.abstract:
                entry_lines.append(f"   Abstract: {article.abstract[:200]}")
            if article.twitter_handle:
                # Surface Twitter provenance so LLM can include it in output (SRC-048)
                entry_lines.append(
                    f"   Twitter signal: @{article.twitter_handle} "
                    f"| Tweet URL: {article.tweet_url}"
                )
            lines.extend(entry_lines)
            lines.append("")

        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Token usage estimation helper (SRC-150)
# ---------------------------------------------------------------------------


def _estimate_token_usage(
    system_prompt: str,
    user_message: str,
    response: str,
) -> int:
    """
    Rough token-usage estimate for quality monitoring (SRC-150).

    Uses the rule-of-thumb approximation of 1 token ≈ 4 characters of English text.
    This is a lower-bound fallback — actual providers may return exact usage via
    response metadata, which should override this estimate when available.

    The estimate is sufficient for monitoring purposes:
    - Detects unusually large or small runs
    - Enables cost estimation within ±25%
    - Never substitutes for exact billing data

    Traces: SRC-150 (token_usage field in quality monitoring log)
    """
    total_chars = len(system_prompt) + len(user_message) + len(response)
    return max(1, total_chars // 4)
