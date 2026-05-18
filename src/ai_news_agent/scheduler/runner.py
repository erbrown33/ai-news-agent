"""
scheduler/runner.py — APScheduler orchestration, retry policy, and manual override API.

Responsibilities:
- Load all enabled per-agent YAML configs from scheduler.yaml at startup (SRC-072).
- Register APScheduler cron jobs for every cadence and every enabled agent (SRC-052).
- Wrap each job in exponential-backoff retry logic (SRC-144).
- Expose ``trigger_now()`` for authenticated on-demand execution (SRC-147).
- Expose ``get_job_statuses()`` for operational observability (SRC-150).
- Provide ``cli_main()`` entry point for local-cron and manual CLI invocations (SRC-076).

Cron schedule (all UTC):
  sourcing   → 00:00 daily for all agents               (SRC-009, SRC-052)
  daily      → 00:05 daily                              (SRC-029)
  weekly     → 01:00 every Sunday (covers prior Sun-Sat)(SRC-030)
  monthly    → 02:00 the 1st of each month              (SRC-031)
  annual     → 03:00 every January 1st                  (SRC-032)

Retry policy (SRC-144): 3 retries, exponential backoff 30s → 60s → 120s.

Manual override (SRC-147): ``trigger_now()`` / POST /api/trigger endpoint.
Authentication: Bearer ``SCHEDULER_API_KEY`` env var (SRC-073).

Traces: SRC-009 (daily sourcing 00:00 UTC), SRC-028–SRC-032 (curation cadences),
        SRC-052 (scheduler triggers), SRC-072 (multi-agent; reads scheduler.yaml),
        SRC-073 (secrets from env vars only), SRC-144 (3 retries + exponential backoff),
        SRC-146 (non-2xx alerting via structured logs), SRC-147 (POST /api/trigger),
        SRC-148 (Twitter degradation propagated through pipeline), SRC-150 (monitoring log)
"""

from __future__ import annotations

import argparse
import sys
import time
from datetime import UTC
from typing import Any, Literal

import structlog
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from ai_news_agent.config.loader import (
    load_agent_config,
    load_scheduler_config,
)
from ai_news_agent.config.models import (
    AgentConfig,
    RuntimeSecrets,
    SchedulerConfig,
)
from ai_news_agent.pipeline import Pipeline
from ai_news_agent.sourcing.agent import SourcingAgent

log = structlog.get_logger(__name__)

Cadence = Literal["daily", "weekly", "monthly", "annual"]


# ---------------------------------------------------------------------------
# Individual job runners (called by cron callbacks and trigger_now)
# ---------------------------------------------------------------------------

def _run_sourcing_job(agent_config: AgentConfig, secrets: RuntimeSecrets) -> None:
    """
    Execute one sourcing run for a single agent configuration.

    Wrapped in retry logic by the scheduler (SRC-144).
    Graceful Twitter degradation propagated automatically by SourcingAgent (SRC-148).

    Traces: SRC-006–SRC-013, SRC-052, SRC-144, SRC-148
    """
    log.info("scheduler_sourcing_job_start", agent_id=agent_config.agent_id)
    try:
        agent = SourcingAgent(config=agent_config, secrets=secrets)
        result = agent.run()
        log.info(
            "scheduler_sourcing_job_complete",
            agent_id=agent_config.agent_id,
            articles_inserted=result.articles_inserted,
            twitter_available=result.twitter_signal_available,
        )
    except Exception as exc:  # noqa: BLE001
        log.error("scheduler_sourcing_job_failed", agent_id=agent_config.agent_id, error=str(exc))
        raise


def _run_curation_job(
    agent_config: AgentConfig,
    secrets: RuntimeSecrets,
    cadence: Cadence,
) -> None:
    """
    Execute one curation + rendering run for a single agent configuration and cadence.

    Uses the unified :class:`~ai_news_agent.pipeline.Pipeline` with
    ``skip_sourcing=True`` so the curation job operates on candidates already
    deposited by the preceding sourcing job (SRC-009, SRC-029–SRC-032).

    Emits the full §8.2 ``pipeline_run_complete`` monitoring log via the
    Pipeline (SRC-150), which covers: items_considered/included, items_by_tier,
    items_by_source_class, token_usage, llm_provider/model/prompt_version,
    and twitter_api_call_count.

    Wrapped in retry logic by the scheduler (SRC-144).
    Renders all three formats (MD, HTML, JSON) after curation (SRC-004).

    Traces: SRC-004, SRC-014–SRC-032, SRC-052, SRC-129, SRC-144, SRC-145, SRC-150
    """
    log.info(
        "scheduler_curation_job_start",
        agent_id=agent_config.agent_id,
        cadence=cadence,
    )
    try:
        # Use the unified Pipeline — skip sourcing because the sourcing job
        # already ran at 00:00 UTC; this curation job reads from the store.
        pipeline = Pipeline(config=agent_config, secrets=secrets)
        result = pipeline.run(cadence=cadence, skip_sourcing=True)

        if not result.success:
            raise RuntimeError(
                f"Pipeline returned failure for agent={agent_config.agent_id!r} "
                f"cadence={cadence!r}: {result.errors}"
            )

        log.info(
            "scheduler_curation_job_complete",
            agent_id=agent_config.agent_id,
            cadence=cadence,
            # §8.2 monitoring fields (SRC-150)
            items_considered=result.items_considered,
            items_included=result.items_included,
            items_by_tier=result.items_by_tier,
            items_by_source_class=result.items_by_source_class,
            token_usage=result.token_usage,
            llm_provider=result.llm_provider,
            llm_model=result.llm_model,
            prompt_version=result.prompt_version,
            twitter_signal_available=result.twitter_signal_available,
            tweet_api_call_count=result.tweet_api_call_count,
            md_path=str(result.markdown_path) if result.markdown_path else None,
        )
    except Exception as exc:  # noqa: BLE001
        log.error(
            "scheduler_curation_job_failed",
            agent_id=agent_config.agent_id,
            cadence=cadence,
            error=str(exc),
        )
        raise


# ---------------------------------------------------------------------------
# Retry wrapper (SRC-144)
# ---------------------------------------------------------------------------

def _with_retry(fn: Any, max_retries: int, backoff_base: int) -> None:
    """
    Execute ``fn`` with exponential backoff retry.

    Retry schedule (SRC-144): ``backoff_base → backoff_base*2 → backoff_base*4``
    Default (from scheduler.yaml): 30s → 60s → 120s.

    Traces: SRC-144 (3 retries, exponential backoff: 30s → 60s → 120s)
    """
    last_exc: Exception | None = None
    for attempt in range(max_retries + 1):
        try:
            fn()
            return
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            if attempt < max_retries:
                sleep_secs = backoff_base * (2 ** attempt)
                log.warning(
                    "scheduler_retry",
                    attempt=attempt + 1,
                    max_retries=max_retries,
                    sleep_secs=sleep_secs,
                    error=str(exc),
                )
                time.sleep(sleep_secs)
            else:
                log.error(
                    "scheduler_exhausted_retries",
                    attempt=attempt + 1,
                    error=str(exc),
                )
    if last_exc:
        raise last_exc


# ---------------------------------------------------------------------------
# Cron expression parser
# ---------------------------------------------------------------------------

def _parse_cron(expr: str) -> dict[str, str]:
    """
    Parse a 5-field cron expression into APScheduler CronTrigger kwargs.

    Format: ``minute hour day_of_month month day_of_week``
    Example: ``"5 0 * * *"`` → ``{"minute": "5", "hour": "0", ...}``

    Traces: SRC-052 (cron triggers from scheduler.yaml)
    """
    parts = expr.strip().split()
    if len(parts) != 5:
        raise ValueError(
            f"Invalid cron expression {expr!r} — expected 5 fields "
            f"(minute hour day_of_month month day_of_week)"
        )
    return {
        "minute": parts[0],
        "hour": parts[1],
        "day": parts[2],
        "month": parts[3],
        "day_of_week": parts[4],
    }


# ---------------------------------------------------------------------------
# SchedulerRunner — main orchestration class
# ---------------------------------------------------------------------------

class SchedulerRunner:
    """
    APScheduler-based multi-agent scheduler.

    Reads ``configs/scheduler.yaml`` at startup to discover all enabled agent
    configurations and registers all sourcing + curation jobs with cron triggers.

    Job schedule (SRC-028–SRC-032, SRC-052):
    - Sourcing:          00:00 UTC daily for all enabled agents          (SRC-009)
    - Curation daily:   00:05 UTC daily                                  (SRC-029)
    - Curation weekly:  01:00 UTC every Sunday (covers prior Sun–Sat)    (SRC-030)
    - Curation monthly: 02:00 UTC the 1st of each month                  (SRC-031)
    - Curation annual:  03:00 UTC January 1st                            (SRC-032)

    Each agent configuration is a separate, independently schedulable unit (SRC-072).
    Retry policy: 3 retries with exponential backoff 30s → 60s → 120s (SRC-144).
    Manual override: ``trigger_now()`` / ``POST /api/trigger`` (SRC-147).
    Job status: ``get_job_statuses()`` for operational observability (SRC-150).

    Traces: SRC-009, SRC-028–SRC-032, SRC-052, SRC-072, SRC-144, SRC-147,
            SRC-148, SRC-150
    """

    def __init__(
        self,
        scheduler_config: SchedulerConfig,
        scheduler_config_path: str = "configs/scheduler.yaml",
        secrets: RuntimeSecrets | None = None,
    ) -> None:
        self._sched_cfg = scheduler_config
        self._sched_cfg_path = scheduler_config_path
        # Accept pre-built secrets (e.g. in tests) or lazy-load from env vars at start().
        self._secrets: RuntimeSecrets | None = secrets
        self._scheduler = BackgroundScheduler(timezone="UTC")
        self._agent_configs: dict[str, AgentConfig] = {}

    # ------------------------------------------------------------------
    # Agent discovery (SRC-072)
    # ------------------------------------------------------------------

    def load_agent_configs(self, base_dir: str | None = None) -> None:
        """
        Load all enabled per-agent YAML configs from the scheduler registry.

        Iterates the registry directly and calls :func:`load_agent_config` per entry
        so that a single bad config does not abort loading of the remaining agents
        (SRC-072).

        Args:
            base_dir: Optional base directory to resolve relative config paths.
                      Defaults to the directory containing the scheduler config.

        Traces: SRC-072 (multi-agent discovery from scheduler.yaml)
        """
        from pathlib import Path

        from ai_news_agent.config.loader import ConfigError

        cwd = Path(base_dir) if base_dir else Path(self._sched_cfg_path).parent
        loaded: dict[str, AgentConfig] = {}

        for registration in self._sched_cfg.agents:
            if not registration.enabled:
                log.info(
                    "config_agent_skipped_disabled",
                    agent_id=registration.id,
                    config=registration.config,
                )
                continue

            config_path = Path(registration.config)
            if not config_path.is_absolute():
                config_path = cwd / config_path

            try:
                agent_cfg = load_agent_config(config_path)

                # Cross-check: agent_id in file should match registry id
                if agent_cfg.agent_id != registration.id:
                    log.warning(
                        "config_agent_id_mismatch",
                        registry_id=registration.id,
                        file_agent_id=agent_cfg.agent_id,
                        config=str(config_path),
                        hint=(
                            "The 'agent_id' field in the YAML file does not match the "
                            "'id' field in scheduler.yaml. Using the file's agent_id."
                        ),
                    )

                loaded[agent_cfg.agent_id] = agent_cfg
                log.info(
                    "config_agent_registered",
                    agent_id=agent_cfg.agent_id,
                    config=str(config_path),
                    description=registration.description,
                )

            except ConfigError as exc:
                log.error(
                    "config_agent_load_failed",
                    agent_id=registration.id,
                    config=str(config_path),
                    error=str(exc),
                )
                # Do not re-raise — other agents continue loading (SRC-072)

        self._agent_configs.update(loaded)
        log.info(
            "scheduler_agents_loaded",
            count=len(loaded),
            agent_ids=list(loaded.keys()),
        )

    # ------------------------------------------------------------------
    # Secrets (SRC-073)
    # ------------------------------------------------------------------

    def _get_secrets(self) -> RuntimeSecrets:
        """
        Return runtime secrets, loading from env vars if not already available.

        Lazy-loads on first call so that :class:`SchedulerRunner` can be
        constructed in tests without requiring real environment variables.

        Traces: SRC-073 (secrets from env vars only)
        """
        if self._secrets is None:
            self._secrets = RuntimeSecrets()  # type: ignore[call-arg]
        return self._secrets

    # ------------------------------------------------------------------
    # Job registration (SRC-052, SRC-144)
    # ------------------------------------------------------------------

    def register_jobs(self) -> None:
        """
        Register all sourcing and curation cron jobs for each enabled agent.

        Job IDs are namespaced by agent_id to prevent conflicts.
        Retry logic wraps each job via :func:`_with_retry`.

        Traces: SRC-009 (sourcing daily), SRC-028–SRC-032 (curation cadences),
                SRC-072 (one job set per agent), SRC-144 (retry policy)
        """
        triggers = self._sched_cfg.triggers
        max_retries = self._sched_cfg.scheduler.max_retries
        backoff_base = self._sched_cfg.scheduler.retry_backoff_base_seconds

        for agent_id, agent_cfg in self._agent_configs.items():
            secrets = self._get_secrets()
            cfg = agent_cfg  # capture loop variable

            # Sourcing: daily at 00:00 UTC (SRC-009, SRC-052)
            sourcing_cron = _parse_cron(triggers.sourcing_daily)
            self._scheduler.add_job(
                func=lambda c=cfg, s=secrets: _with_retry(
                    lambda: _run_sourcing_job(c, s), max_retries, backoff_base
                ),
                trigger=CronTrigger(**sourcing_cron, timezone="UTC"),
                id=f"{agent_id}_sourcing_daily",
                name=f"Sourcing [{agent_id}] daily",
                replace_existing=True,
            )

            # Curation daily: 00:05 UTC (SRC-029)
            daily_cron = _parse_cron(triggers.curation_daily)
            self._scheduler.add_job(
                func=lambda c=cfg, s=secrets: _with_retry(
                    lambda: _run_curation_job(c, s, "daily"), max_retries, backoff_base
                ),
                trigger=CronTrigger(**daily_cron, timezone="UTC"),
                id=f"{agent_id}_curation_daily",
                name=f"Curation [{agent_id}] daily",
                replace_existing=True,
            )

            # Curation weekly: 01:00 UTC Sunday (SRC-030)
            # Runs on Sunday; covers the prior Sun–Sat week via lookback_window()
            weekly_cron = _parse_cron(triggers.curation_weekly)
            self._scheduler.add_job(
                func=lambda c=cfg, s=secrets: _with_retry(
                    lambda: _run_curation_job(c, s, "weekly"), max_retries, backoff_base
                ),
                trigger=CronTrigger(**weekly_cron, timezone="UTC"),
                id=f"{agent_id}_curation_weekly",
                name=f"Curation [{agent_id}] weekly",
                replace_existing=True,
            )

            # Curation monthly: 02:00 UTC 1st of month (SRC-031)
            # Runs on the 1st; lookback_window() returns the prior complete month
            monthly_cron = _parse_cron(triggers.curation_monthly)
            self._scheduler.add_job(
                func=lambda c=cfg, s=secrets: _with_retry(
                    lambda: _run_curation_job(c, s, "monthly"), max_retries, backoff_base
                ),
                trigger=CronTrigger(**monthly_cron, timezone="UTC"),
                id=f"{agent_id}_curation_monthly",
                name=f"Curation [{agent_id}] monthly",
                replace_existing=True,
            )

            # Curation annual: 03:00 UTC January 1st (SRC-032)
            # Runs on Jan 1; lookback_window() returns the prior complete year
            annual_cron = _parse_cron(triggers.curation_annual)
            self._scheduler.add_job(
                func=lambda c=cfg, s=secrets: _with_retry(
                    lambda: _run_curation_job(c, s, "annual"), max_retries, backoff_base
                ),
                trigger=CronTrigger(**annual_cron, timezone="UTC"),
                id=f"{agent_id}_curation_annual",
                name=f"Curation [{agent_id}] annual",
                replace_existing=True,
            )

            log.info(
                "scheduler_jobs_registered",
                agent_id=agent_id,
                job_count=5,
            )

    # ------------------------------------------------------------------
    # Job status (SRC-150)
    # ------------------------------------------------------------------

    def get_job_statuses(self) -> list[dict[str, Any]]:
        """
        Return status information for all registered scheduler jobs.

        Used by the ``GET /api/jobs`` endpoint and operational dashboards.
        Each entry contains: job_id, name, next_run_time (ISO 8601 UTC), pending.

        ``next_run_utc`` is None for pending jobs (scheduler not yet started)
        because APScheduler only computes ``next_run_time`` after the scheduler
        has been started and job stores initialised.

        Traces: SRC-150 (quality monitoring — operational observability)
        """
        jobs = []
        for job in self._scheduler.get_jobs():
            # ``next_run_time`` is a slot that only exists once the scheduler has
            # been started; pending jobs expose the attribute but it is None (or
            # absent via __slots__) until then.  Use getattr with a None sentinel
            # so this works whether the scheduler is started or not (SRC-150).
            next_run = getattr(job, "next_run_time", None)
            jobs.append({
                "job_id":      job.id,
                "name":        job.name,
                "next_run_utc": (
                    next_run.astimezone(UTC).isoformat() if next_run else None
                ),
                "pending":     job.pending,
            })
        # Sort by next_run_utc ascending (None last)
        return sorted(
            jobs,
            key=lambda j: (j["next_run_utc"] is None, j["next_run_utc"]),
        )

    # ------------------------------------------------------------------
    # Manual override — trigger_now (SRC-147)
    # ------------------------------------------------------------------

    def trigger_now(
        self,
        agent_id: str,
        job_type: Literal["sourcing", "curation"],
        cadence: Cadence | None = None,
    ) -> None:
        """
        Manual override: trigger a job on demand without waiting for the cron schedule.
        Used for backfills and misfire recovery (SRC-147).

        Validation order:
        1. ``agent_id`` must be a loaded agent.
        2. For curation, ``cadence`` must be provided.
        3. Secrets are loaded (fail fast if env vars missing).
        4. Job executes synchronously with the configured retry policy.

        Args:
            agent_id:  ID of the agent configuration to run.
            job_type:  "sourcing" | "curation"
            cadence:   Required if job_type == "curation".

        Raises:
            ValueError: On unknown agent_id, bad job_type, or missing cadence.

        Traces: SRC-028 (re-runnable on demand), SRC-144 (retry), SRC-147 (manual override)
        """
        if agent_id not in self._agent_configs:
            raise ValueError(f"Unknown agent_id: {agent_id!r}")

        # Validate cadence BEFORE loading secrets so the ValueError is raised
        # even when secrets are missing from the environment (SRC-147).
        if job_type == "curation" and cadence is None:
            raise ValueError("cadence is required for curation jobs")

        cfg = self._agent_configs[agent_id]
        max_retries = self._sched_cfg.scheduler.max_retries
        backoff_base = self._sched_cfg.scheduler.retry_backoff_base_seconds

        log.info(
            "scheduler_manual_trigger",
            agent_id=agent_id,
            job_type=job_type,
            cadence=cadence,
        )

        secrets = self._get_secrets()
        if job_type == "sourcing":
            _with_retry(
                lambda: _run_sourcing_job(cfg, secrets),
                max_retries,
                backoff_base,
            )
        elif job_type == "curation":
            if cadence is None:  # pragma: no cover — already checked above
                raise ValueError("cadence is required for curation jobs")
            _with_retry(
                lambda: _run_curation_job(cfg, secrets, cadence),
                max_retries,
                backoff_base,
            )
        else:
            raise ValueError(
                f"Unknown job_type: {job_type!r}. Expected 'sourcing' or 'curation'."
            )

    # ------------------------------------------------------------------
    # Lifecycle: start / shutdown
    # ------------------------------------------------------------------

    def start(self) -> None:
        """
        Start the APScheduler background scheduler.
        Loads agent configs and registers all jobs, then blocks until interrupted.

        Secrets are loaded from env vars at this point (SRC-073).

        Traces: SRC-052, SRC-072, SRC-144
        """
        # Eagerly resolve secrets so startup fails fast with a clear error
        # if required env vars are missing, before any jobs are registered.
        self._get_secrets()
        self.load_agent_configs()
        self.register_jobs()
        self._scheduler.start()

        log.info(
            "scheduler_started",
            agents=list(self._agent_configs.keys()),
            job_count=len(self._scheduler.get_jobs()),
        )

        try:
            # Block forever — jobs run in background threads
            while True:
                time.sleep(60)
        except (KeyboardInterrupt, SystemExit):
            log.info("scheduler_shutdown")
            self._scheduler.shutdown()

    def shutdown(self) -> None:
        """
        Gracefully shut down the background scheduler.

        Idempotent — safe to call even when the scheduler was never started.
        APScheduler raises ``SchedulerNotRunningError`` in that case; we absorb
        it so callers do not need to guard against it (SRC-052).
        """
        import contextlib
        with contextlib.suppress(Exception):  # SchedulerNotRunningError or similar
            self._scheduler.shutdown(wait=False)

    # ------------------------------------------------------------------
    # Properties (used by portal and tests)
    # ------------------------------------------------------------------

    @property
    def agent_ids(self) -> list[str]:
        """List of all currently loaded agent IDs."""
        return list(self._agent_configs.keys())

    @property
    def is_running(self) -> bool:
        """True if the background scheduler is running."""
        return self._scheduler.running


# ---------------------------------------------------------------------------
# CLI entry point (SRC-076–SRC-077: local dev trigger)
# ---------------------------------------------------------------------------

def cli_main() -> None:
    """
    Command-line entry point: ``ai-news-schedule``.

    Usage::

        ai-news-schedule                                    # Start background scheduler
        ai-news-schedule --trigger-agent default --job sourcing
        ai-news-schedule --trigger-agent default --job curation --cadence daily
        ai-news-schedule --trigger-agent default --job curation --cadence weekly

    Traces: SRC-052, SRC-076 (local dev), SRC-077 (local cron), SRC-147 (manual trigger)
    """
    parser = argparse.ArgumentParser(
        prog="ai-news-schedule",
        description="Start the AI News Scheduler or trigger a job manually.",
    )
    parser.add_argument(
        "--config",
        default="configs/scheduler.yaml",
        help="Path to scheduler.yaml (default: configs/scheduler.yaml)",
    )
    parser.add_argument(
        "--trigger-agent",
        metavar="AGENT_ID",
        help="Manually trigger a job for the given agent (skips cron schedule)",
    )
    parser.add_argument(
        "--job",
        choices=["sourcing", "curation"],
        help="Job type for manual trigger (required with --trigger-agent)",
    )
    parser.add_argument(
        "--cadence",
        choices=["daily", "weekly", "monthly", "annual"],
        help="Cadence for manual curation trigger",
    )
    parser.add_argument(
        "--list-jobs",
        action="store_true",
        help="List all registered jobs and their next run times, then exit.",
    )
    args = parser.parse_args()

    sched_cfg = load_scheduler_config(args.config)
    runner = SchedulerRunner(scheduler_config=sched_cfg, scheduler_config_path=args.config)
    runner.load_agent_configs()

    if args.list_jobs:
        # Register jobs so APScheduler can compute next_run_time, then list and exit.
        runner.register_jobs()
        runner._scheduler.start()
        statuses = runner.get_job_statuses()
        runner.shutdown()
        print(f"{'JOB ID':<40} {'NEXT RUN (UTC)':<30} NAME")
        print("-" * 90)
        for s in statuses:
            next_run = s["next_run_utc"] or "—"
            print(f"{s['job_id']:<40} {next_run:<30} {s['name']}")
        sys.exit(0)

    if args.trigger_agent:
        # Manual override (SRC-147)
        if not args.job:
            parser.error("--job is required with --trigger-agent")
        runner.trigger_now(
            agent_id=args.trigger_agent,
            job_type=args.job,
            cadence=args.cadence,
        )
        sys.exit(0)

    # Normal mode: start background scheduler
    runner.start()


if __name__ == "__main__":
    cli_main()
