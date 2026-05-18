"""
pipeline.py — End-to-end pipeline wiring: sourcing → curation → rendering.

This module is the authoritative integration point that runs the full pipeline
for a single agent configuration and cadence in one call.  It is used by:
  - The ``ai-news-run`` CLI entry point (Phase-1 local dev trigger, SRC-076)
  - ``SchedulerRunner`` cron jobs (via internal _run_pipeline_job)
  - Integration smoke tests (SRC-102 dry-run mode)
  - Serverless one-shot triggers (SRC-080–SRC-086)

Pipeline stages
───────────────
1. **Sourcing** (SRC-006–SRC-013, SRC-033–SRC-053):
   Fetches candidate articles from web sources and Twitter/X signals,
   deduplicates, and persists them to the store.  Twitter degradation is
   handled gracefully — pipeline continues with web sources (SRC-148).

2. **Curation** (SRC-014–SRC-032, SRC-047–SRC-049, SRC-054):
   Reads candidates from the store, builds a parameterised prompt (ISO dates,
   tier-separated articles, Twitter signal section, search budget), calls the
   LLM via Scorer, enforces URL requirement, and persists a DigestRecord.

3. **Rendering** (SRC-004, SRC-135–SRC-141):
   Writes Markdown, HTML, and JSON to the configured output directory with
   date-stamped filenames.  Updates the DigestRecord with rendered file paths
   so the portal serves correct download links.

Run metadata (§8.2, SRC-150)
─────────────────────────────
``PipelineRunResult`` and the structured ``pipeline_run_complete`` log event
capture all §8.2 monitoring fields for every run:

  - items_considered / items_included       (SRC-150)
  - items_by_tier                           (SRC-150)
  - items_by_source_class                   (SRC-150)
  - token_usage                             (SRC-150)
  - llm_provider + llm_model + prompt_version (SRC-150, SRC-129)
  - twitter_api_call_count                  (SRC-150)
  - twitter_signal_available                (SRC-148)
  - sourcing counters (articles_fetched/inserted/duplicate)

Dry-run mode (SRC-102)
───────────────────────
When ``dry_run=True``:
  - A temporary scratch directory (``--scratch-dir`` or auto-generated ``tmp_path``)
    receives all three rendered formats.
  - No writes occur to the agent's persistent store.
  - The returned ``PipelineRunResult.dry_run`` flag is ``True``.
  - CI smoke test can verify non-empty output + required fields without
    touching production storage or making real LLM/Twitter calls.

Traces: SRC-004 (MD/HTML/JSON outputs), SRC-006–SRC-013 (sourcing),
        SRC-014–SRC-032 (curation cadences), SRC-047–SRC-049 (URL enforcement,
        Twitter signal role), SRC-052 (scheduler integration),
        SRC-054 (research LLM for monthly/annual),
        SRC-072 (per-agent config), SRC-073 (secrets from env vars),
        SRC-076–SRC-077 (local dev / manual trigger), SRC-080–SRC-086
        (serverless container deployment), SRC-102 (dry-run smoke test),
        SRC-129 (prompt_version SHA-256), SRC-135–SRC-141 (rendered export),
        SRC-144 (retry handled by caller / scheduler), SRC-145 (idempotent
        date-stamped filenames), SRC-147 (on-demand re-run),
        SRC-148 (Twitter degradation), SRC-150 (quality monitoring)
"""

from __future__ import annotations

import argparse
import contextlib
import sys
import tempfile
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

import structlog

from ai_news_agent.config.loader import load_agent_config
from ai_news_agent.config.models import AgentConfig, RuntimeSecrets
from ai_news_agent.curation.agent import (
    _WINDOW_FN,
    CurationAgent,
    CurationRunResult,
)
from ai_news_agent.curation.prompt_builder import Cadence
from ai_news_agent.rendering.agent import RenderingAgent, RenderingResult
from ai_news_agent.sourcing.agent import SourcingAgent, SourcingRunResult
from ai_news_agent.storage.tinydb_store import TinyDBArticleStore

if TYPE_CHECKING:
    from ai_news_agent.storage.base import AbstractArticleStore
    from ai_news_agent.storage.models import CurationDiagnostics

log = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Prompt-directory resolution (SRC-113)
# ---------------------------------------------------------------------------

#: Default prompts directory when neither config nor caller specifies one.
_DEFAULT_PROMPTS_DIR = "prompts"


def _resolve_prompts_dir(curation_prompt: str | None) -> str:
    """
    Resolve the prompt-template directory from ``config.curation_prompt``.

    Behavior (matches the schema description in :class:`AgentConfig`):

    - A directory path is used as the prompts dir.
    - A file path uses its parent directory (so the cadence selector picks
      ``daily.md`` / ``weekly.md`` / etc. from the same folder).
    - Anything else falls back to the default ``"prompts"`` directory.

    Traces: SRC-113 (per-agent prompt directory; cadence file selection at runtime)
    """
    if not curation_prompt:
        return _DEFAULT_PROMPTS_DIR
    path = Path(curation_prompt)
    if path.is_dir():
        return str(path)
    if path.suffix:  # file-like path (e.g. prompts/daily.md)
        parent = path.parent
        return str(parent) if str(parent) not in ("", ".") else _DEFAULT_PROMPTS_DIR
    return _DEFAULT_PROMPTS_DIR


# ---------------------------------------------------------------------------
# PipelineRunResult — aggregated §8.2 quality-monitoring output (SRC-150)
# ---------------------------------------------------------------------------


@dataclass
class PipelineRunResult:
    """
    Aggregated output of a full sourcing → curation → rendering pipeline run.

    Carries all §8.2 quality-monitoring fields required for every run (SRC-150)
    plus the three rendered file paths produced by the Rendering Agent (SRC-004).

    Attributes
    ----------
    agent_id:
        Identifier of the agent configuration used for this run (SRC-072).
    cadence:
        One of ``"daily"`` | ``"weekly"`` | ``"monthly"`` | ``"annual"``.
    run_at:
        UTC timestamp of when the pipeline was invoked.

    Sourcing counters (SRC-150):
    - articles_fetched:   Total raw candidates examined by the Sourcing Agent.
    - articles_inserted:  New (non-duplicate) articles added to the store.
    - articles_duplicate: Duplicate candidates silently skipped.
    - tweets_fetched:     Raw tweet signals collected from all configured handles.
    - tweets_inserted:    New (non-duplicate) tweet signals stored.

    Twitter API (SRC-148, SRC-150):
    - twitter_signal_available: False ⟹ web-only degraded mode.
    - tweet_api_call_count:     0 when degraded.

    Curation counters (SRC-150):
    - items_considered:     All candidates passed to the LLM for scoring.
    - items_included:       Selected items after LLM scoring + URL enforcement.
    - items_by_tier:        ``{"1a": n, "1b": n, ...}`` distribution.
    - items_by_source_class: ``{"web": n, "twitter": n}`` distribution.
    - token_usage:          Total LLM tokens consumed (prompt + completion).

    LLM provenance (SRC-129, SRC-150):
    - llm_provider: e.g. ``"openai"``.
    - llm_model:    e.g. ``"gpt-4o"`` or cadence override model.
    - prompt_version: SHA-256 of the prompt template used (``"sha256:<hex>"``).

    Rendering paths (SRC-004, SRC-145):
    - markdown_path, html_path, json_path: Written output files.
      ``None`` if the pipeline failed before reaching the rendering stage.

    Control flags:
    - dry_run: ``True`` ⟹ all outputs written to a scratch directory; no
      production store writes occurred (SRC-102).
    - success: ``True`` ⟹ all three stages completed without exception.
    - errors:  List of non-fatal error messages from any stage.

    Traces: SRC-004, SRC-129, SRC-145, SRC-148, SRC-150
    """

    # Identity
    agent_id: str
    cadence: str
    run_at: datetime

    # Sourcing counters (SRC-150)
    articles_fetched: int = 0
    articles_inserted: int = 0
    articles_duplicate: int = 0
    tweets_fetched: int = 0
    tweets_inserted: int = 0

    # Twitter availability (SRC-148, SRC-150)
    twitter_signal_available: bool = True
    tweet_api_call_count: int = 0

    # Curation counters (SRC-150)
    items_considered: int = 0
    items_included: int = 0
    items_by_tier: dict[str, int] = field(default_factory=dict)
    items_by_source_class: dict[str, int] = field(default_factory=dict)
    token_usage: int = 0

    # LLM provenance (SRC-129, SRC-150)
    llm_provider: str = ""
    llm_model: str = ""
    prompt_version: str = ""

    # Rendering paths (SRC-004, SRC-145) — None until rendering completes
    markdown_path: Path | None = None
    html_path: Path | None = None
    json_path: Path | None = None

    # Pipeline control
    dry_run: bool = False
    success: bool = False
    errors: list[str] = field(default_factory=list)

    # Sparse-digest diagnostics — populated when items_included is below the
    # curation threshold (default 3). None for normal runs.
    diagnostics: CurationDiagnostics | None = None

    def _populate_from_sourcing(self, sourcing: SourcingRunResult) -> None:
        """Merge SourcingRunResult fields into this PipelineRunResult."""
        self.articles_fetched = sourcing.articles_fetched
        self.articles_inserted = sourcing.articles_inserted
        self.articles_duplicate = sourcing.articles_duplicate
        self.tweets_fetched = sourcing.tweets_fetched
        self.tweets_inserted = sourcing.tweets_inserted
        self.twitter_signal_available = sourcing.twitter_signal_available
        self.tweet_api_call_count = sourcing.tweet_api_call_count

    def _populate_from_curation(self, curation: CurationRunResult) -> None:
        """Merge CurationRunResult metadata into this PipelineRunResult."""
        meta = curation.metadata
        self.items_considered = meta.items_considered
        self.items_included = meta.items_included
        self.items_by_tier = dict(meta.items_by_tier)
        self.items_by_source_class = dict(meta.items_by_source_class)
        self.token_usage = meta.token_usage
        self.llm_provider = meta.llm_provider
        self.llm_model = meta.llm_model
        self.prompt_version = meta.prompt_version
        # Twitter availability may be refined by curation (inferred from signals)
        self.twitter_signal_available = meta.twitter_signal_available
        self.tweet_api_call_count = meta.tweet_api_call_count
        self.diagnostics = curation.diagnostics

    def _populate_from_rendering(self, rendering: RenderingResult) -> None:
        """Merge RenderingResult paths into this PipelineRunResult."""
        self.markdown_path = rendering.markdown_path
        self.html_path = rendering.html_path
        self.json_path = rendering.json_path


# ---------------------------------------------------------------------------
# Pipeline runner
# ---------------------------------------------------------------------------


class Pipeline:
    """
    Wires sourcing → curation → rendering into a single callable unit.

    Each pipeline run is stateless and idempotent: re-running with the same
    (agent_id, cadence, date) produces the same digest files (SRC-145).

    Sourcing always precedes curation within a pipeline run so that the
    curation step sees the freshest available candidates (SRC-008–SRC-013).
    This mirrors the scheduler's ``sourcing_daily → curation_daily`` ordering.

    Stages
    ──────
    1. Sourcing  — fetches + deduplicates candidates (SRC-006–SRC-013)
    2. Curation  — LLM scoring + prompt versioning   (SRC-014–SRC-032)
    3. Rendering — writes MD/HTML/JSON to disk       (SRC-004, SRC-135–SRC-141)
    4. Logging   — emits ``pipeline_run_complete``   (SRC-150)

    Dry-run mode (SRC-102)
    ──────────────────────
    Pass ``dry_run=True`` to write rendered files to *scratch_dir* (a temporary
    directory if not supplied) without touching the production store.  Ideal for
    CI smoke tests and prompt regression checks.

    Traces: SRC-004, SRC-006–SRC-032, SRC-047–SRC-049, SRC-052–SRC-054,
            SRC-072–SRC-073, SRC-076–SRC-077, SRC-102, SRC-129, SRC-135–SRC-141,
            SRC-145, SRC-147–SRC-148, SRC-150
    """

    def __init__(
        self,
        config: AgentConfig,
        secrets: RuntimeSecrets,
        store: AbstractArticleStore | None = None,
        prompts_dir: str | None = None,
    ) -> None:
        """
        Initialise the pipeline for a single agent configuration.

        Args:
            config:      Per-agent YAML config (SRC-071–SRC-073).
            secrets:     Runtime secrets from environment variables (SRC-073).
            store:       Article store; defaults to TinyDB at
                         ``{config.output_dir}/store.json`` (SRC-053).
                         Inject a mock or in-memory store for tests.
            prompts_dir: Explicit prompt-template directory override. When
                         ``None`` (default), the directory is resolved from
                         ``config.curation_prompt``: a directory path is used
                         as-is, a file path uses its parent directory. Falls
                         back to ``"prompts"`` if neither resolves (SRC-113).

        Traces: SRC-053 (TinyDB default), SRC-071–SRC-073 (config + secrets),
                SRC-113 (per-agent prompt directory)
        """
        self._config = config
        self._secrets = secrets
        self._prompts_dir = prompts_dir or _resolve_prompts_dir(config.curation_prompt)
        self._store: AbstractArticleStore = store or TinyDBArticleStore(
            f"{config.output_dir}/store.json"
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def run(
        self,
        cadence: Cadence,
        *,
        window_start: datetime | None = None,
        window_end: datetime | None = None,
        twitter_api_available: bool | None = None,
        dry_run: bool = False,
        scratch_dir: str | Path | None = None,
        skip_sourcing: bool = False,
    ) -> PipelineRunResult:
        """
        Execute the full sourcing → curation → rendering pipeline.

        Args:
            cadence:
                ``"daily"`` | ``"weekly"`` | ``"monthly"`` | ``"annual"``.
                Determines the lookback window and LLM model selection (SRC-029–SRC-032).
            window_start:
                Override lookback window start (UTC-aware datetime).  When supplied,
                ``window_end`` must also be supplied; enables on-demand re-runs for
                any historical window (SRC-028, SRC-147).
            window_end:
                Override lookback window end (UTC-aware datetime).
            twitter_api_available:
                Explicit Twitter API availability flag.  When ``None``, availability
                is inferred from tweet signals in the store (SRC-148).
            dry_run:
                When ``True``, skip all production store writes and render to
                *scratch_dir* instead.  Returns a complete result for CI smoke
                testing (SRC-102).
            scratch_dir:
                Output directory for dry-run renders.  If ``None`` and ``dry_run``
                is ``True``, a temporary directory is created automatically and
                preserved for this call only.
            skip_sourcing:
                When ``True``, skip the sourcing stage and use only candidates
                already in the store (useful for re-curating without re-sourcing).

        Returns:
            :class:`PipelineRunResult` with all §8.2 monitoring fields and
            rendered file paths.

        Traces: SRC-004, SRC-008–SRC-032, SRC-047–SRC-049, SRC-102, SRC-129,
                SRC-135–SRC-141, SRC-145, SRC-147–SRC-148, SRC-150
        """
        run_at = datetime.now(UTC)
        result = PipelineRunResult(
            agent_id=self._config.agent_id,
            cadence=cadence,
            run_at=run_at,
            dry_run=dry_run,
        )

        log.info(
            "pipeline_run_start",
            agent_id=self._config.agent_id,
            cadence=cadence,
            dry_run=dry_run,
            skip_sourcing=skip_sourcing,
            window_start=window_start.isoformat() if window_start else None,
            window_end=window_end.isoformat() if window_end else None,
        )

        # Determine effective scratch directory for dry-run renders (SRC-102)
        _tmpdir_ctx: tempfile.TemporaryDirectory | None = None  # type: ignore[type-arg]
        if dry_run:
            if scratch_dir is None:
                _tmpdir_ctx = tempfile.TemporaryDirectory(prefix="ai-news-dry-run-")
                effective_output_dir = Path(_tmpdir_ctx.name)
            else:
                effective_output_dir = Path(scratch_dir)
                effective_output_dir.mkdir(parents=True, exist_ok=True)
        else:
            effective_output_dir = Path(self._config.output_dir)

        # Decide the sourcing window. When the caller passed explicit overrides,
        # honour them. Otherwise, for weekly/monthly/annual cadences, peek at
        # the store: if it doesn't already have enough material to populate a
        # cadence-appropriate digest, widen sourcing to the full cadence window
        # so a fresh-install backfill actually returns articles. If the store
        # is already populated, fall through to sourcing's "today only" default
        # — no point burning search quota on candidates we already have.
        sourcing_window_start = window_start
        sourcing_window_end = window_end

        if (
            not skip_sourcing
            and window_start is None
            and window_end is None
            and cadence != "daily"
        ):
            cadence_start, cadence_end = _WINDOW_FN[cadence](run_at)
            top_n_map = {
                "weekly":  self._config.limits.weekly_top_n,
                "monthly": self._config.limits.monthly_top_n,
                "annual":  self._config.limits.annual_top_n,
            }
            backfill_threshold = max(top_n_map.get(cadence, 5), 5)
            existing = self._store.count_window(
                agent_id=self._config.agent_id,
                window_start=cadence_start,
                window_end=cadence_end,
            )
            if existing < backfill_threshold:
                sourcing_window_start = cadence_start
                sourcing_window_end = cadence_end
                log.info(
                    "pipeline_sourcing_window_expanded_for_backfill",
                    agent_id=self._config.agent_id,
                    cadence=cadence,
                    existing_in_window=existing,
                    threshold=backfill_threshold,
                    window_start=cadence_start.isoformat(),
                    window_end=cadence_end.isoformat(),
                )
            else:
                log.debug(
                    "pipeline_sourcing_window_default",
                    agent_id=self._config.agent_id,
                    cadence=cadence,
                    existing_in_window=existing,
                    threshold=backfill_threshold,
                )

        try:
            # ==============================================================
            # Stage 1: Sourcing (SRC-006–SRC-013, SRC-033–SRC-053, SRC-148)
            # ==============================================================
            sourcing_result = self._run_sourcing(
                window_start=sourcing_window_start,
                window_end=sourcing_window_end,
                dry_run=dry_run,
                skip_sourcing=skip_sourcing,
            )
            if sourcing_result is not None:
                result._populate_from_sourcing(sourcing_result)

                # Propagate Twitter availability to curation if caller did not
                # supply an explicit override (SRC-148)
                if twitter_api_available is None and not sourcing_result.twitter_signal_available:
                    twitter_api_available = False

            # ==============================================================
            # Stage 2: Curation (SRC-014–SRC-032, SRC-047–SRC-049, SRC-054)
            # ==============================================================
            curation_result = self._run_curation(
                cadence=cadence,
                window_start=window_start,
                window_end=window_end,
                twitter_api_available=twitter_api_available,
                dry_run=dry_run,
            )
            result._populate_from_curation(curation_result)

            # ==============================================================
            # Stage 3: Rendering (SRC-004, SRC-135–SRC-141, SRC-145)
            # ==============================================================
            rendering_result = self._run_rendering(
                curation_result=curation_result,
                output_dir=effective_output_dir,
                dry_run=dry_run,
            )
            result._populate_from_rendering(rendering_result)

            result.success = True

        except Exception as exc:  # noqa: BLE001
            error_msg = f"{type(exc).__name__}: {exc}"
            result.errors.append(error_msg)
            log.error(
                "pipeline_run_failed",
                agent_id=self._config.agent_id,
                cadence=cadence,
                error=error_msg,
            )
        finally:
            if _tmpdir_ctx is not None:
                # Preserve temp dir for dry-run callers to inspect; cleanup on
                # context exit (Python GC when result goes out of scope).
                # For explicit scratch_dir we do nothing — caller owns it.
                with contextlib.suppress(Exception):
                    _tmpdir_ctx.cleanup()

        # ==================================================================
        # Stage 4: §8.2 quality monitoring log (SRC-150)
        # ==================================================================
        self._emit_monitoring_log(result)
        return result

    # ------------------------------------------------------------------
    # Private stage runners
    # ------------------------------------------------------------------

    def _run_sourcing(
        self,
        window_start: datetime | None,
        window_end: datetime | None,
        dry_run: bool,
        skip_sourcing: bool,
    ) -> SourcingRunResult | None:
        """
        Stage 1: run sourcing agent; returns None when skipped.

        In dry-run mode the SourcingAgent writes to an ephemeral in-memory
        store that is discarded at the end of this call, so the production
        store is never touched.  The curation stage then reads from the
        production store (which may or may not have existing articles).

        Traces: SRC-006–SRC-013, SRC-033–SRC-053, SRC-148
        """
        if skip_sourcing:
            log.info(
                "pipeline_sourcing_skipped",
                agent_id=self._config.agent_id,
                reason="skip_sourcing=True",
            )
            return None

        if dry_run:
            # Dry-run: use the shared store but skip persisting (SRC-102).
            # SourcingAgent.run() always uses self._store; we pass a fresh
            # in-memory TinyDB so no production writes happen.
            import os as _os
            import tempfile as _tmp

            with _tmp.TemporaryDirectory(prefix="ai-news-src-dry-") as tmpdir:
                dry_store = TinyDBArticleStore(_os.path.join(tmpdir, "dry-src.json"))
                agent = SourcingAgent(
                    config=self._config,
                    secrets=self._secrets,
                    store=dry_store,
                )
                sourcing_result = agent.run(
                    window_start=window_start,
                    window_end=window_end,
                )
                dry_store.close()
        else:
            agent = SourcingAgent(
                config=self._config,
                secrets=self._secrets,
                store=self._store,
            )
            sourcing_result = agent.run(
                window_start=window_start,
                window_end=window_end,
            )

        log.info(
            "pipeline_sourcing_done",
            agent_id=self._config.agent_id,
            articles_fetched=sourcing_result.articles_fetched,
            articles_inserted=sourcing_result.articles_inserted,
            articles_duplicate=sourcing_result.articles_duplicate,
            tweets_fetched=sourcing_result.tweets_fetched,
            twitter_available=sourcing_result.twitter_signal_available,
        )
        return sourcing_result

    def _run_curation(
        self,
        cadence: Cadence,
        window_start: datetime | None,
        window_end: datetime | None,
        twitter_api_available: bool | None,
        dry_run: bool,
    ) -> CurationRunResult:
        """
        Stage 2: run curation agent.

        In dry-run mode ``CurationAgent.run(dry_run=True)`` is called so no
        DigestRecord is written to the production store (SRC-102).

        Traces: SRC-014–SRC-032, SRC-047–SRC-049, SRC-054, SRC-102
        """
        agent = CurationAgent(
            config=self._config,
            secrets=self._secrets,
            store=self._store,
            prompts_dir=self._prompts_dir,
        )
        curation_result = agent.run(
            cadence=cadence,
            window_start=window_start,
            window_end=window_end,
            twitter_api_available=twitter_api_available,
            dry_run=dry_run,
        )

        log.info(
            "pipeline_curation_done",
            agent_id=self._config.agent_id,
            cadence=cadence,
            items_considered=curation_result.metadata.items_considered,
            items_included=curation_result.metadata.items_included,
            themes_count=len(curation_result.themes),
            predictions_count=len(curation_result.predictions),
            prompt_version=curation_result.metadata.prompt_version,
            llm_model=curation_result.metadata.llm_model,
            token_usage=curation_result.metadata.token_usage,
        )
        return curation_result

    def _run_rendering(
        self,
        curation_result: CurationRunResult,
        output_dir: Path,
        dry_run: bool,
    ) -> RenderingResult:
        """
        Stage 3: render MD/HTML/JSON and (in production mode) update DigestRecord.

        In dry-run mode the Rendering Agent writes to the scratch directory and
        does NOT attempt to update the DigestRecord in the store (SRC-102).

        Traces: SRC-004, SRC-135–SRC-141, SRC-145
        """
        rendering_agent = RenderingAgent(output_dir=output_dir)

        if dry_run:
            # Write to scratch directory — no store update (SRC-102)
            rendering_result = rendering_agent.render(curation_result)
        else:
            # Write to production directory + update DigestRecord paths (SRC-145)
            rendering_result = rendering_agent.render_and_update_store(
                curation_result, self._store
            )

        log.info(
            "pipeline_rendering_done",
            agent_id=self._config.agent_id,
            items_rendered=rendering_result.items_rendered,
            items_dropped_no_url=rendering_result.items_dropped_no_url,
            markdown_path=str(rendering_result.markdown_path),
            html_path=str(rendering_result.html_path),
            json_path=str(rendering_result.json_path),
        )
        return rendering_result

    def _emit_monitoring_log(self, result: PipelineRunResult) -> None:
        """
        Emit the ``pipeline_run_complete`` structured log event covering
        all §8.2 quality-monitoring fields (SRC-150).

        Fields logged (§8.2):
        - items_considered / items_included
        - items_by_tier
        - items_by_source_class
        - token_usage
        - llm_provider / llm_model / prompt_version
        - twitter_api_call_count
        - twitter_signal_available

        Additional pipeline-level fields for operational health:
        - articles_fetched / articles_inserted / articles_duplicate
        - tweets_fetched / tweets_inserted
        - markdown_path / html_path / json_path

        Traces: SRC-150 (§8.2 quality monitoring log)
        """
        log.info(
            "pipeline_run_complete",
            # Identity
            agent_id=result.agent_id,
            cadence=result.cadence,
            run_at=result.run_at.isoformat(),
            success=result.success,
            dry_run=result.dry_run,
            # Sourcing counters (SRC-150)
            articles_fetched=result.articles_fetched,
            articles_inserted=result.articles_inserted,
            articles_duplicate=result.articles_duplicate,
            tweets_fetched=result.tweets_fetched,
            tweets_inserted=result.tweets_inserted,
            # Twitter API (SRC-148, SRC-150)
            twitter_signal_available=result.twitter_signal_available,
            tweet_api_call_count=result.tweet_api_call_count,
            # §8.2 curation / monitoring fields (SRC-150)
            items_considered=result.items_considered,
            items_included=result.items_included,
            items_by_tier=result.items_by_tier,
            items_by_source_class=result.items_by_source_class,
            token_usage=result.token_usage,
            # §8.2 LLM provenance (SRC-129, SRC-150)
            llm_provider=result.llm_provider,
            llm_model=result.llm_model,
            prompt_version=result.prompt_version,
            # Rendering outputs (SRC-004, SRC-145)
            markdown_path=str(result.markdown_path) if result.markdown_path else None,
            html_path=str(result.html_path) if result.html_path else None,
            json_path=str(result.json_path) if result.json_path else None,
            # Errors (non-fatal)
            errors=result.errors,
        )


# ---------------------------------------------------------------------------
# Convenience factory (used by SchedulerRunner and serverless handlers)
# ---------------------------------------------------------------------------


def build_pipeline(
    config: AgentConfig,
    secrets: RuntimeSecrets,
    prompts_dir: str | None = None,
    store: AbstractArticleStore | None = None,
) -> Pipeline:
    """
    Construct a :class:`Pipeline` for the given agent configuration.

    Convenience wrapper used by the scheduler and serverless handlers to
    avoid repeating construction logic.

    Args:
        config:      Per-agent configuration (SRC-071–SRC-073).
        secrets:     Runtime secrets from env vars (SRC-073).
        prompts_dir: Explicit prompt-templates directory override. When ``None``,
                     resolved from ``config.curation_prompt`` (SRC-113).
        store:       Optional pre-built store (for tests / dry-run).

    Returns:
        Ready-to-call :class:`Pipeline` instance.

    Traces: SRC-052 (scheduler), SRC-072 (per-agent), SRC-073 (secrets), SRC-113
    """
    return Pipeline(
        config=config,
        secrets=secrets,
        store=store,
        prompts_dir=prompts_dir,
    )


# ---------------------------------------------------------------------------
# CLI entry point: ``ai-news-run``  (SRC-076–SRC-077, SRC-102)
# ---------------------------------------------------------------------------


def cli_main() -> None:  # noqa: C901 — deliberately a unified CLI with full option set
    """
    Command-line entry point: ``ai-news-run``.

    Runs the full sourcing → curation → rendering pipeline for a single agent
    configuration and cadence.  Supports dry-run mode, window overrides, and
    skipping the sourcing stage.

    Usage examples::

        # Standard daily run (default agent, default configs)
        ai-news-run --cadence daily

        # Weekly run with explicit agent config
        ai-news-run --agent configs/default-agent.yaml --cadence weekly

        # Dry-run: produces digest to a temp scratch dir — no prod store writes
        ai-news-run --cadence daily --dry-run

        # Dry-run to a specific scratch directory (SRC-102)
        ai-news-run --cadence daily --dry-run --scratch-dir /tmp/ai-news-scratch

        # On-demand re-run for an explicit historical window (SRC-028, SRC-147)
        ai-news-run --cadence weekly \\
            --window-start 2026-05-03 --window-end 2026-05-09

        # Skip sourcing: re-curate from existing store candidates
        ai-news-run --cadence daily --skip-sourcing

        # Annual curation with research model (no sourcing re-run needed)
        ai-news-run --cadence annual --skip-sourcing

    Flags::

        --agent PATH            Per-agent YAML config (default: configs/default-agent.yaml)
        --cadence CADENCE       daily | weekly | monthly | annual (required)
        --prompts-dir DIR       Prompt templates directory (default: prompts/)
        --window-start DATE     Override window start YYYY-MM-DD (with --window-end)
        --window-end DATE       Override window end   YYYY-MM-DD (with --window-start)
        --twitter-available     Explicit Twitter availability: true | false
        --dry-run               Write to scratch dir only; no production store writes
        --scratch-dir DIR       Output directory for dry-run (default: auto tempdir)
        --skip-sourcing         Skip sourcing stage; curate from existing store candidates

    Exit codes::

        0  Pipeline completed successfully
        1  Pipeline failed (see stderr / structured logs for details)
        2  Configuration / argument error

    Traces: SRC-028 (re-runnable on demand), SRC-076 (local dev Phase 1),
            SRC-077 (manual trigger for backfills), SRC-102 (dry-run smoke test),
            SRC-147 (on-demand re-run with window override)
    """
    parser = argparse.ArgumentParser(
        prog="ai-news-run",
        description=(
            "Run the full AI News pipeline: sourcing → curation → rendering.\n"
            "Produces Markdown, HTML, and JSON digests in the configured output directory.\n"
            "\nUse --dry-run to render to a scratch directory without production writes."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    # Agent / config
    parser.add_argument(
        "--agent",
        default="configs/default-agent.yaml",
        metavar="PATH",
        help="Path to per-agent YAML config file (default: configs/default-agent.yaml)",
    )
    parser.add_argument(
        "--cadence",
        required=True,
        choices=["daily", "weekly", "monthly", "annual"],
        help="Curation cadence — determines lookback window and LLM model.",
    )
    parser.add_argument(
        "--prompts-dir",
        default="prompts",
        metavar="DIR",
        help="Directory containing prompt template files (default: prompts/)",
    )

    # Window override (SRC-028, SRC-147)
    parser.add_argument(
        "--window-start",
        default=None,
        metavar="YYYY-MM-DD",
        help=(
            "Override lookback window start date (UTC).  "
            "Must be used with --window-end.  Enables on-demand re-run (SRC-028)."
        ),
    )
    parser.add_argument(
        "--window-end",
        default=None,
        metavar="YYYY-MM-DD",
        help=(
            "Override lookback window end date (UTC).  "
            "Must be used with --window-start. (SRC-028)"
        ),
    )

    # Twitter availability override (SRC-148)
    parser.add_argument(
        "--twitter-available",
        choices=["true", "false"],
        default=None,
        help=(
            "Explicitly set Twitter API availability.  "
            "When omitted, inferred from sourcing results. (SRC-148)"
        ),
    )

    # Dry-run mode (SRC-102)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "Render to --scratch-dir (or a temp directory) without writing to the "
            "production store.  Used for CI smoke tests and prompt iteration. (SRC-102)"
        ),
    )
    parser.add_argument(
        "--scratch-dir",
        default=None,
        metavar="DIR",
        help=(
            "Output directory for dry-run renders.  "
            "A temporary directory is created automatically when omitted. (SRC-102)"
        ),
    )

    # Pipeline control
    parser.add_argument(
        "--skip-sourcing",
        action="store_true",
        help=(
            "Skip the sourcing stage and curate from candidates already in the store.  "
            "Useful for re-curation without re-fetching."
        ),
    )

    args = parser.parse_args()

    # ---- Validate: --window-start and --window-end must be used together ------
    if (args.window_start is None) != (args.window_end is None):
        print(
            "Error: --window-start and --window-end must be provided together. (SRC-028)",
            file=sys.stderr,
        )
        sys.exit(2)

    # ---- Parse window overrides (SRC-028) ------------------------------------
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

    # ---- Parse Twitter availability override (SRC-148) -----------------------
    twitter_api_available: bool | None = None
    if args.twitter_available is not None:
        twitter_api_available = args.twitter_available.lower() == "true"

    # ---- Load config + secrets -----------------------------------------------
    try:
        config = load_agent_config(args.agent)
    except Exception as exc:  # noqa: BLE001
        print(f"Error: failed to load agent config {args.agent!r}: {exc}", file=sys.stderr)
        sys.exit(2)

    try:
        secrets = RuntimeSecrets()  # type: ignore[call-arg]
    except Exception as exc:  # noqa: BLE001
        print(f"Error: failed to load runtime secrets: {exc}", file=sys.stderr)
        sys.exit(2)

    # ---- Resolve scratch directory for dry-run --------------------------------
    scratch_dir: str | None = args.scratch_dir
    if args.dry_run and scratch_dir is None:
        # Auto-create a named temp dir that persists for this invocation
        _td = tempfile.mkdtemp(prefix="ai-news-dry-run-")
        scratch_dir = _td
        print(f"Dry-run scratch directory: {scratch_dir}", file=sys.stderr)

    # ---- Announce run --------------------------------------------------------
    dry_tag = " [DRY-RUN]" if args.dry_run else ""
    print(
        f"ai-news-run{dry_tag}: agent={config.agent_id!r} cadence={args.cadence!r}",
        file=sys.stderr,
    )
    if window_start:
        print(
            f"  Window override: {window_start.date()} → {window_end.date()}",  # type: ignore[union-attr]
            file=sys.stderr,
        )

    # ---- Execute pipeline ----------------------------------------------------
    pipeline = Pipeline(
        config=config,
        secrets=secrets,
        prompts_dir=args.prompts_dir,
    )

    result = pipeline.run(
        cadence=args.cadence,
        window_start=window_start,
        window_end=window_end,
        twitter_api_available=twitter_api_available,
        dry_run=args.dry_run,
        scratch_dir=scratch_dir,
        skip_sourcing=args.skip_sourcing,
    )

    # ---- Print §8.2 monitoring summary to stderr (SRC-150) -------------------
    _print_run_summary(result)

    if not result.success:
        print(
            f"\nPipeline FAILED — errors: {result.errors}",
            file=sys.stderr,
        )
        sys.exit(1)

    sys.exit(0)


def _print_run_summary(result: PipelineRunResult) -> None:
    """
    Print the §8.2 quality-monitoring summary to stderr.

    Covers all fields required by §8.2:
    - items considered / included
    - items by tier
    - items by source class
    - token usage
    - LLM provider + model + prompt version
    - Twitter API call counts

    Traces: SRC-150 (§8.2 quality monitoring)
    """
    status = "SUCCESS" if result.success else "FAILED"
    dry_tag = " [DRY-RUN]" if result.dry_run else ""

    print(
        f"\n{'=' * 60}\n"
        f"Pipeline run {status}{dry_tag}\n"
        f"  Agent:              {result.agent_id}\n"
        f"  Cadence:            {result.cadence}\n"
        f"  Run at (UTC):       {result.run_at.isoformat()}\n"
        f"\n── Sourcing ──────────────────────────────────────────────\n"
        f"  Articles fetched:   {result.articles_fetched}\n"
        f"  Articles inserted:  {result.articles_inserted}\n"
        f"  Duplicates skipped: {result.articles_duplicate}\n"
        f"  Tweets fetched:     {result.tweets_fetched}\n"
        f"  Tweets inserted:    {result.tweets_inserted}\n"
        f"  Twitter available:  {result.twitter_signal_available}\n"
        f"  Twitter API calls:  {result.tweet_api_call_count}\n"
        f"\n── Curation (§8.2) ────────────────────────────────────────\n"
        f"  Items considered:   {result.items_considered}\n"
        f"  Items included:     {result.items_included}\n"
        f"  By tier:            {result.items_by_tier}\n"
        f"  By source class:    {result.items_by_source_class}\n"
        f"  Token usage:        {result.token_usage:,}\n"
        f"  LLM provider:       {result.llm_provider}\n"
        f"  LLM model:          {result.llm_model}\n"
        f"  Prompt version:     {result.prompt_version}\n"
        f"\n── Rendering ─────────────────────────────────────────────",
        file=sys.stderr,
    )
    if result.markdown_path:
        print(f"  Markdown:           {result.markdown_path}", file=sys.stderr)
        print(f"  HTML:               {result.html_path}", file=sys.stderr)
        print(f"  JSON:               {result.json_path}", file=sys.stderr)
    else:
        print("  No rendered files (pipeline did not reach rendering stage)", file=sys.stderr)

    if result.diagnostics is not None:
        diag = result.diagnostics
        print(
            "\n── Sparse-digest diagnostics ─────────────────────────────",
            file=sys.stderr,
        )
        print(
            f"  Items included ({result.items_included}) is below threshold "
            f"({diag.threshold}). Reasons:",
            file=sys.stderr,
        )
        for reason in diag.reasons:
            print(f"   • {reason}", file=sys.stderr)
        print(
            f"  [articles_in_store={diag.articles_in_store} "
            f"articles_in_window={diag.articles_in_window} "
            f"items_dropped_no_url={diag.items_dropped_no_url} "
            f"twitter_available={diag.twitter_signal_available}]",
            file=sys.stderr,
        )

    if result.errors:
        print("\n── Errors ────────────────────────────────────────────────", file=sys.stderr)
        for err in result.errors:
            print(f"  ⚠  {err}", file=sys.stderr)

    print("=" * 60, file=sys.stderr)


if __name__ == "__main__":
    cli_main()
