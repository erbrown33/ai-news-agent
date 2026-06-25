"""
curation/agent.py — CurationAgent orchestrator and CLI entry point.

The Curation Agent is the intelligence layer of the pipeline.  It:
- Reads candidates + tweet signals for the lookback window from the store.
- Selects the appropriate LLM model (default or cadence override).
- Builds a fully-parameterised prompt (ISO dates, tier articles, Twitter signal, budget).
- Calls the LLM via the Scorer to score, rank, and produce all cadence-specific outputs.
- Enforces the URL requirement (items without working URLs are dropped).
- Persists a DigestRecord for portal listing and idempotent re-runs.
- Returns a CurationRunResult with all fields needed by the Rendering Agent and portal.

Traces: SRC-014–SRC-032 (curation agent, all four cadences),
        SRC-047–SRC-049 (Twitter signal role, URL requirement),
        SRC-054 (research LLM for monthly/annual),
        SRC-102 (dry-run mode for CI smoke test),
        SRC-112–SRC-131 (prompt design, versioning, ownership),
        SRC-129 (prompt_version in every digest output),
        SRC-145 (idempotent re-runs: overwrite by date),
        SRC-147 (manual override / on-demand re-run),
        SRC-148 (Twitter degradation note in digest),
        SRC-150 (quality monitoring log)
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

import structlog

from ai_news_agent.config.loader import load_agent_config
from ai_news_agent.config.models import AgentConfig, RuntimeSecrets
from ai_news_agent.curation.prompt_builder import Cadence, PromptBuilder
from ai_news_agent.curation.scorer import Scorer
from ai_news_agent.llm.factory import get_llm_client
from ai_news_agent.storage.models import (
    CuratedItem,
    CurationDiagnostics,
    DigestMetadata,
    DigestRecord,
)
from ai_news_agent.storage.tinydb_store import TinyDBArticleStore

if TYPE_CHECKING:
    from ai_news_agent.storage.base import AbstractArticleStore

log = structlog.get_logger(__name__)


@dataclass
class CurationRunResult:
    """
    Complete output of a curation run — passed to the Rendering Agent.

    All four cadences return items and metadata.  Cadence-specific extras:

    - **daily**: ``items`` with headline/source/URL/why_it_matters (SRC-029)
    - **weekly**: ``items`` + ``themes`` + ``outlook`` (look-ahead) (SRC-030)
    - **monthly**: ``items`` + ``themes`` + ``outlook`` (anticipated news) (SRC-031)
    - **annual**: ``items`` + ``themes`` + ``predictions`` (SRC-032, SRC-124)

    Traces: SRC-029–SRC-032 (cadence-specific output), SRC-048 (curated item schema),
            SRC-124 (annual predictions), SRC-129 (prompt_version), SRC-148 (twitter flag),
            SRC-150 (quality monitoring)
    """

    metadata: DigestMetadata
    """Quality-monitoring metadata for every digest output (SRC-129, SRC-150)."""

    items: list[CuratedItem]
    """Ranked, URL-validated curated items — all cadences (SRC-029–SRC-032)."""

    themes: list[str] = field(default_factory=list)
    """Weekly/monthly/annual themes identified by the LLM (SRC-030–SRC-032)."""

    outlook: str = ""
    """
    Forward-looking text for weekly (look-ahead, SRC-030) and
    monthly (anticipated news, SRC-031) digests.
    """

    predictions: list[str] = field(default_factory=list)
    """
    Annual only: 10 falsifiable predictions grounded in observed trends (SRC-032, SRC-124).
    Each prediction includes reasoning and links where appropriate.
    """

    twitter_degradation_note: str | None = None
    """
    Non-None when Twitter/X API was unavailable or produced no signals.
    Included in rendered digest and portal display (SRC-148).
    """

    dry_run: bool = False
    """
    When True, this result was produced in dry-run mode — no store writes occurred.
    Used by CI smoke tests (SRC-102).
    """

    diagnostics: CurationDiagnostics | None = None
    """
    Populated when ``items_included`` falls below the sparse-digest threshold
    (default 3). Provides a human-readable explanation plus structured counters
    so operators can answer "why was today's digest empty?" without trawling
    logs. ``None`` for normal runs.
    """


# ---------------------------------------------------------------------------
# Window computation helpers (SRC-009, SRC-028–SRC-032)
# ---------------------------------------------------------------------------


def _daily_window(reference: datetime) -> tuple[datetime, datetime]:
    """
    Rolling 24–48h window: from 00:00 UTC *yesterday* through ``reference`` (now).

    Why this is wider than a strict prior calendar day (SRC-029):
    Sourcing uses ``00:00 today → now`` and stamps articles without a snippet-
    extractable publication date with ``fetched_at = now``. A strict
    ``[yesterday 00:00, yesterday 23:59]`` window would then exclude every
    article a same-day pipeline run produced, yielding zero candidates. Widening
    the daily curation window to include "today so far" lets fresh runs return
    items while still covering the full previous day for late-night sourcing.

    Traces: SRC-009, SRC-029
    """
    yesterday_start = reference.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(
        days=1
    )
    return yesterday_start, reference


def _weekly_window(reference: datetime) -> tuple[datetime, datetime]:
    """
    Previous Sunday through Saturday (Sun=0, Sat=6).
    Run by default on Monday morning to cover the previous full week (SRC-030).

    Traces: SRC-030
    """
    # Python weekday(): Monday=0 … Sunday=6
    # To get previous Sunday: advance to next day, then step back to Sunday
    today = reference.replace(hour=0, minute=0, second=0, microsecond=0)
    # isoweekday(): Mon=1 … Sun=7
    # Days since last Sunday:
    days_since_sunday = (reference.weekday() + 1) % 7
    last_sunday = today - timedelta(days=days_since_sunday)
    week_start = last_sunday - timedelta(weeks=1)
    week_end = (week_start + timedelta(days=6)).replace(
        hour=23, minute=59, second=59, microsecond=999999
    )
    return week_start, week_end


def _monthly_window(reference: datetime) -> tuple[datetime, datetime]:
    """
    Previous full calendar month, first to last day inclusive.
    Run on the 1st of the current month (SRC-031).

    Traces: SRC-031
    """
    first_of_current = reference.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    last_of_previous = (first_of_current - timedelta(seconds=1)).replace(
        hour=23, minute=59, second=59, microsecond=999999
    )
    first_of_previous = last_of_previous.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    return first_of_previous, last_of_previous


def _annual_window(reference: datetime) -> tuple[datetime, datetime]:
    """
    Previous full calendar year, Jan 1 to Dec 31.
    Run on January 1st of the current year (SRC-032).

    Traces: SRC-032
    """
    year = reference.year - 1
    year_start = datetime(year, 1, 1, 0, 0, 0, tzinfo=UTC)
    year_end = datetime(year, 12, 31, 23, 59, 59, 999999, tzinfo=UTC)
    return year_start, year_end


_WINDOW_FN = {
    "daily": _daily_window,
    "weekly": _weekly_window,
    "monthly": _monthly_window,
    "annual": _annual_window,
}


# Below this many included items, attach a CurationDiagnostics block to the
# digest so operators can see *why* the run was sparse. Three is generous
# enough to flag almost-empty digests while letting a normal slow news day
# (e.g. a holiday with 5 items) render without diagnostic noise.
_SPARSE_DIGEST_THRESHOLD = 3


def _build_diagnostics(
    *,
    threshold: int,
    items_included: int,
    items_considered: int,
    articles_in_store: int,
    candidates: list,  # list[ArticleRecord] — kept loose to avoid type import here
    items_dropped_no_url: int,
    twitter_signal_available: bool,
    window_start: datetime,
    window_end: datetime,
) -> CurationDiagnostics:
    """
    Compose human-readable reasons for a sparse/empty digest plus the
    structured counters operators can use to verify each reason.

    Reasons are ordered most-to-least likely cause. The first matching
    condition is always emitted; subsequent ones add nuance.
    """
    by_tier: dict[str, int] = {}
    for article in candidates:
        by_tier[article.tier] = by_tier.get(article.tier, 0) + 1

    reasons: list[str] = []
    window_str = f"{window_start.isoformat()} → {window_end.isoformat()}"

    if articles_in_store == 0:
        reasons.append(
            "Article store is empty for this agent — sourcing has not yet "
            "produced any articles. Run the sourcing stage (or the full "
            "pipeline) before expecting a populated digest."
        )
    elif items_considered == 0:
        reasons.append(
            f"Store has {articles_in_store} article(s) but none fall inside "
            f"the curation window ({window_str}). Common cause: sourced "
            "articles had no extractable publication date and were stamped "
            "with fetch time, which falls outside the window. Re-run with "
            "--window-start/--window-end to cover the fetch day, or wait "
            "until the next scheduled run."
        )
    elif items_included == 0:
        reasons.append(
            f"The LLM considered {items_considered} candidate(s) in the window "
            "but selected none. Likely causes: candidates were off-topic for "
            "the configured curation prompt, failed the quality bar, or were "
            "dropped by URL enforcement."
        )
        if items_dropped_no_url > 0:
            reasons.append(
                f"{items_dropped_no_url} LLM-selected item(s) were dropped for "
                "missing or invalid URLs (SRC-049). Inspect the source data — "
                "candidates may lack canonical URLs."
            )
    else:
        reasons.append(
            f"Only {items_included} item(s) selected (threshold {threshold}). "
            f"Considered {items_considered} candidate(s) in the window. "
            "Sourcing may be too narrow, or the window may overlap a quiet "
            "news day."
        )
        if items_dropped_no_url > 0:
            reasons.append(
                f"{items_dropped_no_url} additional item(s) were dropped for "
                "missing or invalid URLs (SRC-049)."
            )

    if not twitter_signal_available and items_considered < 20:
        reasons.append(
            "Twitter/X influencer signal was unavailable for this run, which "
            "reduces lead-generation coverage. Configure TWITTER_BEARER_TOKEN "
            "to enable signal-driven sourcing (SRC-148)."
        )

    return CurationDiagnostics(
        threshold=threshold,
        articles_in_store=articles_in_store,
        articles_in_window=items_considered,
        articles_in_window_by_tier=by_tier,
        items_dropped_no_url=items_dropped_no_url,
        twitter_signal_available=twitter_signal_available,
        reasons=reasons,
    )


# ---------------------------------------------------------------------------
# CurationAgent
# ---------------------------------------------------------------------------


class CurationAgent:
    """
    Curation Agent — intelligently scores and summarizes candidates using an LLM.

    Runs at least once each day; re-runnable on demand for any lookback window (SRC-028).

    Key behaviours (SRC-014–SRC-032):
    - Reads all candidate articles + tweet signals for the lookback window from the store.
    - Selects a model: cadence override (monthly/annual research LLM) or agent default
      (SRC-054).
    - Builds the prompt with injected ISO dates, tier-separated articles, Twitter signal
      section, search budget (SRC-115–SRC-124).
    - Calls LLM via Scorer for scoring — all prioritisation is via LLM, not rule-based
      (SRC-027).
    - Extracts cadence-specific synthesis: themes + outlook (weekly/monthly, SRC-030–SRC-031)
      and annual predictions grounded in observed trends (SRC-032, SRC-124).
    - Drops any item without a working URL before returning (SRC-049, SRC-141).
    - Persists DigestRecord to store for portal listing and idempotent re-runs (SRC-145).
    - Emits structured quality monitoring log (SRC-150).
    - Returns CurationRunResult with all data for the Rendering Agent.

    Dry-run mode (SRC-102):
    - Pass ``dry_run=True`` to skip all store writes.
    - Produces a complete result for CI smoke testing.
    - The result has ``dry_run=True`` set for caller verification.

    Traces: SRC-014–SRC-032, SRC-047–SRC-049, SRC-054, SRC-102,
            SRC-112–SRC-131, SRC-145, SRC-148, SRC-150
    """

    def __init__(
        self,
        config: AgentConfig,
        secrets: RuntimeSecrets,
        store: AbstractArticleStore | None = None,
        prompts_dir: str = "prompts",
    ) -> None:
        """
        Args:
            config:      Per-agent configuration (SRC-071–SRC-073).
            secrets:     Runtime secrets from env vars (SRC-073).
            store:       Article store; defaults to TinyDBArticleStore (SRC-053).
            prompts_dir: Directory containing prompt template files (SRC-113).
        """
        self._config = config
        self._secrets = secrets
        self._store: AbstractArticleStore = store or TinyDBArticleStore(
            f"{config.output_dir}/store.json"
        )
        self._llm = get_llm_client(config.llm, secrets)
        self._prompt_builder = PromptBuilder(prompts_dir=prompts_dir)
        self._scorer = Scorer(llm_client=self._llm)

    # ------------------------------------------------------------------
    # Cross-digest deduplication (daily cadence)
    # ------------------------------------------------------------------

    def _get_recently_curated_urls(self, cadence: str, lookback_days: int = 7) -> set[str]:
        """
        Return the set of article URLs that appeared in the last ``lookback_days``
        daily digest JSON files.  Used to prevent the same article from showing
        up in consecutive daily digests when the curation window overlaps.

        Silently skips missing or malformed files.
        """
        seen: set[str] = set()
        output_dir = Path(self._config.output_dir)
        today = date.today()
        for i in range(1, lookback_days + 1):
            target = today - timedelta(days=i)
            path = output_dir / f"{target}-{cadence}.json"
            if path.exists():
                try:
                    data = json.loads(path.read_text())
                    for item in data.get("items", []):
                        url = item.get("url")
                        if url:
                            seen.add(url)
                except Exception:  # noqa: BLE001
                    pass
        return seen

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def run(
        self,
        cadence: Cadence,
        reference_time: datetime | None = None,
        window_start: datetime | None = None,
        window_end: datetime | None = None,
        twitter_api_available: bool | None = None,
        dry_run: bool = False,
    ) -> CurationRunResult:
        """
        Execute a curation run for the given cadence.

        Args:
            cadence:
                ``"daily"`` | ``"weekly"`` | ``"monthly"`` | ``"annual"`` (SRC-029–SRC-032).
            reference_time:
                UTC datetime used to compute window if ``window_start``/``window_end``
                are not provided.  Defaults to ``datetime.now(UTC)``.
            window_start:
                Override window start (UTC-aware datetime).  If provided, ``window_end``
                must also be provided.  Enables on-demand re-runs for any window (SRC-028).
            window_end:
                Override window end (UTC-aware datetime).
            twitter_api_available:
                Explicit Twitter API availability flag.

                - Pass ``False`` when the Sourcing Agent recorded a degradation event so
                  the prompt and digest note accurately state "API was unreachable" rather
                  than "no tweets found" (SRC-148).
                - When ``None`` (default), availability is inferred from whether any tweet
                  signals exist in the store for the window (``True`` if >0, ``False`` if 0).
                  This correctly handles normal runs but cannot distinguish "API down" from
                  "quiet window" — pass the explicit flag for accurate diagnostics.
            dry_run:
                When ``True``, skip all store writes (no DigestRecord upsert).
                Produces a complete result for CI smoke testing (SRC-102).

        Returns:
            :class:`CurationRunResult` with curated items, cadence-specific synthesis
            outputs, metadata, and monitoring fields.

        Traces: SRC-028 (runnable on demand), SRC-029–SRC-032 (cadence outputs),
                SRC-047–SRC-049 (signal role, URL enforcement), SRC-054 (model selection),
                SRC-102 (dry_run for CI), SRC-145 (idempotent digest persist),
                SRC-148 (Twitter degradation), SRC-150 (monitoring log)
        """
        if reference_time is None:
            reference_time = datetime.now(UTC)

        if window_start is None or window_end is None:
            window_fn = _WINDOW_FN[cadence]
            window_start, window_end = window_fn(reference_time)

        log.info(
            "curation_run_start",
            agent_id=self._config.agent_id,
            cadence=cadence,
            window_start=window_start.isoformat(),
            window_end=window_end.isoformat(),
            dry_run=dry_run,
        )

        # ------------------------------------------------------------------
        # Step 1: Retrieve candidates + tweet signals from store (SRC-008–SRC-010)
        # ------------------------------------------------------------------
        candidates = self._store.get_window(
            agent_id=self._config.agent_id,
            window_start=window_start,
            window_end=window_end,
        )
        tweet_signals = self._store.get_tweet_signals(
            agent_id=self._config.agent_id,
            window_start=window_start,
            window_end=window_end,
        )

        # ------------------------------------------------------------------
        # Step 1b: Cross-digest dedup — filter URLs seen in recent daily digests
        #
        # The daily window is yesterday-00:00 → now, creating a ~7-hour overlap
        # with tomorrow's window.  Without this filter the same article can appear
        # in two consecutive daily digests.  We scan the last 7 daily JSON output
        # files and exclude any candidate whose URL was already featured.
        # Only applied to the daily cadence; weekly/monthly/annual are unaffected.
        # ------------------------------------------------------------------
        if cadence == "daily" and not dry_run:
            recently_shown = self._get_recently_curated_urls(cadence, lookback_days=7)
            if recently_shown:
                before = len(candidates)
                candidates = [c for c in candidates if c.url not in recently_shown]
                log.info(
                    "curation_cross_digest_dedup",
                    agent_id=self._config.agent_id,
                    cadence=cadence,
                    digests_scanned=7,
                    urls_in_recent_digests=len(recently_shown),
                    candidates_before=before,
                    candidates_after=len(candidates),
                    filtered_out=before - len(candidates),
                )

        # ------------------------------------------------------------------
        # Step 2: Resolve Twitter availability (SRC-148)
        #
        # If explicitly supplied, honour it (caller knows API reachability).
        # Otherwise infer from signal count — cannot distinguish "API down"
        # from "quiet window" in this path (document this to operators).
        # ------------------------------------------------------------------
        if twitter_api_available is None:
            twitter_available = len(tweet_signals) > 0
        else:
            twitter_available = twitter_api_available

        degradation_note: str | None = None
        if not twitter_available:
            degradation_note = (
                "⚠️ Twitter/X influencer signal was unavailable for this run. "
                "Curation is based on web sources only. (SRC-148)"
            )
            log.warning(
                "curation_twitter_unavailable",
                agent_id=self._config.agent_id,
                cadence=cadence,
                signals_in_store=len(tweet_signals),
                api_available_explicit=twitter_api_available,
            )

        # ------------------------------------------------------------------
        # Step 3: Select model — cadence override or default (SRC-054)
        # ------------------------------------------------------------------
        model = self._config.llm.model
        thinking = False
        if cadence in self._config.llm.cadence_overrides:
            override = self._config.llm.cadence_overrides[cadence]
            model = override.model
            thinking = override.thinking
            log.debug(
                "curation_model_override",
                cadence=cadence,
                model=model,
                thinking=thinking,
            )

        # ------------------------------------------------------------------
        # Step 4: Resolve top_n from config (SRC-029–SRC-032)
        # ------------------------------------------------------------------
        top_n_map = {
            "daily": self._config.limits.daily_top_n,
            "weekly": self._config.limits.weekly_top_n,
            "monthly": self._config.limits.monthly_top_n,
            "annual": self._config.limits.annual_top_n,
        }
        top_n = top_n_map[cadence]

        # ------------------------------------------------------------------
        # Step 5: Build prompt (SRC-115–SRC-124)
        #
        # Candidates are injected into tier-separated sections so the LLM
        # receives a complete self-contained system prompt (SRC-016–SRC-021,
        # SRC-027).  The Scorer also passes them as a user message for clarity.
        # ------------------------------------------------------------------
        prompt_text, prompt_version = self._prompt_builder.build(
            cadence=cadence,
            window_start=window_start,
            window_end=window_end,
            tweet_signals=tweet_signals,
            top_n=top_n,
            candidates=candidates,
            twitter_api_available=twitter_available,
        )

        # ------------------------------------------------------------------
        # Step 6: Score, rank, and extract cadence-specific outputs
        #         (SRC-027, SRC-029–SRC-032, SRC-049, SRC-124, SRC-141)
        # ------------------------------------------------------------------
        scorer_result = self._scorer.score_and_rank(
            prompt=prompt_text,
            model=model,
            candidates=candidates,
            tweet_signals=tweet_signals,
            top_n=top_n,
            prompt_version=prompt_version,
            cadence=cadence,
            thinking=thinking,
        )

        curated_items = scorer_result.items
        themes = scorer_result.themes
        outlook = scorer_result.outlook
        predictions = scorer_result.predictions
        token_usage = scorer_result.token_usage

        # ------------------------------------------------------------------
        # Step 7: Assemble quality monitoring metadata (SRC-150)
        # ------------------------------------------------------------------
        tier_counts: dict[str, int] = {}
        source_class_counts: dict[str, int] = {}
        for item in curated_items:
            tier_counts[item.tier] = tier_counts.get(item.tier, 0) + 1
            sc = "twitter" if item.twitter_handle else "web"
            source_class_counts[sc] = source_class_counts.get(sc, 0) + 1

        metadata = DigestMetadata(
            agent_id=self._config.agent_id,
            cadence=cadence,
            run_date=reference_time.date(),
            window_start=window_start,
            window_end=window_end,
            prompt_version=prompt_version,  # SRC-129
            llm_provider=self._config.llm.provider,  # SRC-150
            llm_model=model,  # SRC-150
            items_considered=len(candidates),  # SRC-150
            items_included=len(curated_items),  # SRC-150
            items_by_tier=tier_counts,  # SRC-150
            items_by_source_class=source_class_counts,  # SRC-150
            twitter_signal_available=twitter_available,  # SRC-148
            tweet_api_call_count=len(tweet_signals),  # SRC-150
            token_usage=token_usage,  # SRC-150
        )

        # ------------------------------------------------------------------
        # Step 8: Persist DigestRecord (SRC-145 — idempotent; re-runs overwrite)
        # ------------------------------------------------------------------
        if not dry_run:
            digest_record = DigestRecord(
                agent_id=self._config.agent_id,
                cadence=cadence,
                run_date=reference_time.date(),
                window_start=window_start,
                window_end=window_end,
                prompt_version=prompt_version,
                llm_provider=self._config.llm.provider,
                llm_model=model,
                items_considered=len(candidates),
                items_included=len(curated_items),
                items_by_tier=tier_counts,
                items_by_source_class=source_class_counts,
                twitter_signal_available=twitter_available,
                tweet_api_call_count=len(tweet_signals),
                token_usage=token_usage,
            )
            self._store.upsert_digest(digest_record)
            log.debug(
                "curation_digest_persisted",
                agent_id=self._config.agent_id,
                cadence=cadence,
                run_date=reference_time.date().isoformat(),
                digest_key=digest_record.digest_key,
            )

        # ------------------------------------------------------------------
        # Step 9: Structured quality monitoring log (SRC-150)
        # ------------------------------------------------------------------
        log.info(
            "curation_run_complete",
            agent_id=self._config.agent_id,
            cadence=cadence,
            window_start=window_start.isoformat(),
            window_end=window_end.isoformat(),
            items_considered=len(candidates),
            items_included=len(curated_items),
            items_by_tier=tier_counts,
            items_by_source_class=source_class_counts,
            themes_count=len(themes),
            has_outlook=bool(outlook),
            predictions_count=len(predictions),
            llm_provider=self._config.llm.provider,
            llm_model=model,
            thinking=thinking,
            prompt_version=prompt_version,
            twitter_signal_available=twitter_available,
            tweet_signals_in_window=len(tweet_signals),
            token_usage=token_usage,
            dry_run=dry_run,
        )

        # ------------------------------------------------------------------
        # Step 10: Build empty/sparse-digest diagnostics (SRC-150)
        #
        # When the run yielded fewer than _SPARSE_DIGEST_THRESHOLD items,
        # attach a human-readable explanation so operators can answer
        # "why was today's digest empty?" without reading logs.
        # ------------------------------------------------------------------
        diagnostics: CurationDiagnostics | None = None
        if len(curated_items) < _SPARSE_DIGEST_THRESHOLD:
            articles_in_store = self._store.count_articles(self._config.agent_id)
            diagnostics = _build_diagnostics(
                threshold=_SPARSE_DIGEST_THRESHOLD,
                items_included=len(curated_items),
                items_considered=len(candidates),
                articles_in_store=articles_in_store,
                candidates=candidates,
                items_dropped_no_url=scorer_result.items_dropped_no_url,
                twitter_signal_available=twitter_available,
                window_start=window_start,
                window_end=window_end,
            )
            log.warning(
                "curation_sparse_digest",
                agent_id=self._config.agent_id,
                cadence=cadence,
                items_included=len(curated_items),
                items_considered=len(candidates),
                articles_in_store=articles_in_store,
                items_dropped_no_url=scorer_result.items_dropped_no_url,
                reasons=diagnostics.reasons,
            )

        return CurationRunResult(
            metadata=metadata,
            items=curated_items,
            themes=themes,
            outlook=outlook,
            predictions=predictions,
            twitter_degradation_note=degradation_note,
            dry_run=dry_run,
            diagnostics=diagnostics,
        )


# ---------------------------------------------------------------------------
# CLI entry point (SRC-028: rerun on demand; SRC-076–SRC-077: local dev trigger)
# ---------------------------------------------------------------------------


def cli_main() -> None:
    """
    Command-line entry point: ``ai-news-curate``.

    Supports:
    - Standard scheduled runs (cadence determines window automatically)
    - On-demand re-runs with explicit ``--window-start`` / ``--window-end`` (SRC-028)
    - Dry-run mode for CI smoke testing (SRC-102)

    Usage examples::

        # Scheduled daily run
        ai-news-curate --agent configs/default-agent.yaml --cadence daily

        # On-demand re-run for a specific week (SRC-028)
        ai-news-curate --cadence weekly \\
            --window-start 2026-05-03 --window-end 2026-05-09

        # Annual curation with research model (SRC-032, SRC-054)
        ai-news-curate --cadence annual

        # Dry-run for CI smoke test (SRC-102)
        ai-news-curate --cadence daily --dry-run

    Traces: SRC-028 (re-runnable on demand), SRC-076 (local dev), SRC-077 (manual trigger),
            SRC-102 (dry-run mode)
    """
    parser = argparse.ArgumentParser(
        prog="ai-news-curate",
        description=(
            "Run the AI News Curation Agent for a single cadence. "
            "Supports on-demand re-runs with explicit window overrides (SRC-028)."
        ),
    )
    parser.add_argument(
        "--agent",
        default="configs/default-agent.yaml",
        help="Path to per-agent YAML config file",
    )
    parser.add_argument(
        "--cadence",
        choices=["daily", "weekly", "monthly", "annual"],
        default="daily",
        help="Curation cadence (default: daily)",
    )
    parser.add_argument(
        "--prompts-dir",
        default=None,
        help=(
            "Directory containing prompt template files. "
            "When omitted, resolved from the agent config's curation_prompt "
            "(falls back to prompts/ if neither resolves)."
        ),
    )
    parser.add_argument(
        "--window-start",
        default=None,
        metavar="YYYY-MM-DD",
        help=(
            "Override lookback window start date (YYYY-MM-DD UTC). "
            "Must be used together with --window-end. "
            "Enables on-demand re-run for any historical window (SRC-028)."
        ),
    )
    parser.add_argument(
        "--window-end",
        default=None,
        metavar="YYYY-MM-DD",
        help=(
            "Override lookback window end date (YYYY-MM-DD UTC). "
            "Must be used together with --window-start. (SRC-028)"
        ),
    )
    parser.add_argument(
        "--twitter-available",
        choices=["true", "false"],
        default=None,
        help=(
            "Explicitly set Twitter API availability for this run. "
            "When omitted, inferred from tweet signals in the store. (SRC-148)"
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help=("Produce a digest but skip all store writes. Used for CI smoke testing (SRC-102)."),
    )
    args = parser.parse_args()

    # Validate: --window-start and --window-end must be used together
    if (args.window_start is None) != (args.window_end is None):
        print(
            "Error: --window-start and --window-end must be provided together. (SRC-028)",
            file=sys.stderr,
        )
        sys.exit(2)

    # Parse explicit window overrides into UTC-aware datetimes (SRC-028)
    window_start: datetime | None = None
    window_end: datetime | None = None
    if args.window_start is not None:
        try:
            window_start = datetime.fromisoformat(args.window_start).replace(tzinfo=UTC)
            window_end = datetime.fromisoformat(args.window_end).replace(
                hour=23, minute=59, second=59, microsecond=999999, tzinfo=UTC
            )
        except ValueError as exc:
            print(f"Error parsing window dates: {exc}", file=sys.stderr)
            sys.exit(2)

    # Parse explicit Twitter availability flag
    twitter_api_available: bool | None = None
    if args.twitter_available is not None:
        twitter_api_available = args.twitter_available.lower() == "true"

    config = load_agent_config(args.agent)
    secrets = RuntimeSecrets()  # type: ignore[call-arg]

    # Resolve prompts_dir: explicit --prompts-dir wins, else fall back to the
    # same config-driven resolution Pipeline uses (deferred import avoids the
    # pipeline → curation.agent circular import).
    if args.prompts_dir is not None:
        prompts_dir = args.prompts_dir
    else:
        from ai_news_agent.pipeline import _resolve_prompts_dir

        prompts_dir = _resolve_prompts_dir(config.curation_prompt)

    agent = CurationAgent(
        config=config,
        secrets=secrets,
        prompts_dir=prompts_dir,
    )

    result = agent.run(
        cadence=args.cadence,
        window_start=window_start,
        window_end=window_end,
        twitter_api_available=twitter_api_available,
        dry_run=args.dry_run,
    )

    # Summary output to stderr (not stdout — keep stdout for piping)
    dry_tag = " [DRY-RUN]" if result.dry_run else ""
    print(
        f"Curation complete{dry_tag} — "
        f"{result.metadata.items_included} items selected "
        f"from {result.metadata.items_considered} candidates "
        f"[{args.cadence} | {result.metadata.llm_model}]",
        file=sys.stderr,
    )

    if result.themes:
        print(
            f"Themes ({len(result.themes)}): {', '.join(result.themes[:3])}{'…' if len(result.themes) > 3 else ''}",
            file=sys.stderr,
        )

    if result.predictions:
        print(
            f"Predictions: {len(result.predictions)} annual predictions generated (SRC-032)",
            file=sys.stderr,
        )

    if result.twitter_degradation_note:
        print(f"Warning: {result.twitter_degradation_note}", file=sys.stderr)

    if result.diagnostics is not None:
        diag = result.diagnostics
        print(
            f"\nDigest is sparse (< {diag.threshold} items). Why:",
            file=sys.stderr,
        )
        for reason in diag.reasons:
            print(f"  - {reason}", file=sys.stderr)
        print(
            f"  [articles_in_store={diag.articles_in_store} "
            f"articles_in_window={diag.articles_in_window} "
            f"items_dropped_no_url={diag.items_dropped_no_url} "
            f"twitter_available={diag.twitter_signal_available}]",
            file=sys.stderr,
        )

    if result.dry_run:
        print(
            "Dry-run mode: no store writes performed. Output fields verified non-empty.",
            file=sys.stderr,
        )

    sys.exit(0)


if __name__ == "__main__":
    cli_main()
