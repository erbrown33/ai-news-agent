"""
scheduler/serverless.py — Serverless and cloud-scheduler trigger entry points.

This module provides three handler patterns so that the same container image
can be triggered by ANY cloud scheduler without code changes (SRC-085):

1. **HTTP handler** (``http_handler``) — suitable for:
   - GCP Cloud Run jobs triggered by Cloud Scheduler (HTTP POST)
   - AWS App Runner tasks
   - Azure Container Apps timer-trigger jobs
   Cloud schedulers POST to the container's HTTP endpoint; the handler decodes
   the payload and dispatches sourcing + curation jobs.

2. **AWS Lambda handler** (``lambda_handler``) — wraps ``http_handler`` in the
   Lambda invocation protocol.  Note: Lambda has a 15-minute hard timeout which
   is fine for all cadences except potentially annual synthesis.  Use App Runner
   or Fargate for annual runs to avoid this constraint (SRC-090).

3. **CLI one-shot handler** (``cli_oneshot``) — invoked by a local cron job or
   GitHub Actions schedule trigger.  Runs all enabled agents for the given job
   type and cadence, then exits cleanly.  This is the "Phase 1" local-dev
   trigger described in SRC-076–SRC-077.

Authentication (SRC-147):
All HTTP entry points enforce ``SCHEDULER_API_KEY`` Bearer-token auth via the
:func:`~ai_news_agent.scheduler.auth.validate_api_key` function.  The Lambda
and CLI handlers also accept the key via the ``SCHEDULER_API_KEY`` env var.

Environment variables consumed (SRC-073, SRC-105–SRC-111):
- ``SCHEDULER_CONFIG``   — path to scheduler.yaml (default: configs/scheduler.yaml)
- ``SCHEDULER_API_KEY``  — bearer token for HTTP triggers (optional; dev mode if absent)
- ``AGENT_ID``           — if set, restrict to a single agent (useful for per-agent jobs)
- ``JOB_TYPE``           — "sourcing" | "curation" (required for one-shot triggers)
- ``CADENCE``            — "daily" | "weekly" | "monthly" | "annual" (for curation)

Traces: SRC-052 (scheduler), SRC-072 (multi-agent), SRC-075–SRC-079 (deployment),
        SRC-080–SRC-086 (serverless containers), SRC-089 (GCP/AWS/Azure),
        SRC-090 (Lambda timeout warning), SRC-144 (retry), SRC-146 (non-2xx),
        SRC-147 (authenticated trigger)
"""

from __future__ import annotations

import json
import os
import sys
from typing import Any

import structlog

from ai_news_agent.config.loader import load_scheduler_config
from ai_news_agent.scheduler.auth import validate_api_key
from ai_news_agent.scheduler.runner import (
    SchedulerRunner,
    _run_curation_job,
    _run_sourcing_job,
    _with_retry,
)

log = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_DEFAULT_CONFIG_PATH = "configs/scheduler.yaml"

_VALID_JOB_TYPES = frozenset({"sourcing", "curation"})
_VALID_CADENCES = frozenset({"daily", "weekly", "monthly", "annual"})


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _load_runner(config_path: str | None = None) -> SchedulerRunner:
    """
    Load scheduler config and agent configs; return a ready-to-use runner.

    Does NOT start the background scheduler — this is for one-shot invocations.

    Traces: SRC-052, SRC-072
    """
    path = config_path or os.environ.get("SCHEDULER_CONFIG", _DEFAULT_CONFIG_PATH)
    sched_cfg = load_scheduler_config(path)
    runner = SchedulerRunner(
        scheduler_config=sched_cfg,
        scheduler_config_path=path,
    )
    runner.load_agent_configs()
    return runner


def _dispatch(
    runner: SchedulerRunner,
    job_type: str,
    cadence: str | None,
    agent_id: str | None,
) -> dict[str, Any]:
    """
    Dispatch one or all agents for the given job_type + cadence.

    Returns a structured result dict for HTTP responses and Lambda return values.

    Traces: SRC-052 (trigger), SRC-072 (multi-agent), SRC-144 (retry)
    """
    if job_type not in _VALID_JOB_TYPES:
        return {
            "status": "error",
            "message": f"Invalid job_type: {job_type!r}. Expected one of {sorted(_VALID_JOB_TYPES)}.",
            "code": 400,
        }

    if job_type == "curation" and (cadence is None or cadence not in _VALID_CADENCES):
        return {
            "status": "error",
            "message": (
                f"Invalid cadence: {cadence!r}. "
                f"Expected one of {sorted(_VALID_CADENCES)} when job_type='curation'."
            ),
            "code": 400,
        }

    # Target agents: single agent_id or all loaded agents
    if agent_id:
        if agent_id not in runner._agent_configs:
            return {
                "status": "error",
                "message": f"Unknown agent_id: {agent_id!r}.",
                "code": 404,
            }
        target_ids = [agent_id]
    else:
        target_ids = list(runner._agent_configs.keys())

    if not target_ids:
        return {
            "status": "error",
            "message": "No enabled agents loaded.",
            "code": 503,
        }

    results: list[dict[str, Any]] = []
    max_retries = runner._sched_cfg.scheduler.max_retries
    backoff_base = runner._sched_cfg.scheduler.retry_backoff_base_seconds
    secrets = runner._get_secrets()

    for aid in target_ids:
        cfg = runner._agent_configs[aid]
        try:
            if job_type == "sourcing":
                _with_retry(
                    lambda c=cfg, s=secrets: _run_sourcing_job(c, s),
                    max_retries,
                    backoff_base,
                )
            else:  # curation — cadence already validated above
                _with_retry(
                    lambda c=cfg, s=secrets, ca=cadence: _run_curation_job(c, s, ca),  # type: ignore[arg-type]
                    max_retries,
                    backoff_base,
                )
            results.append({"agent_id": aid, "status": "ok"})
            log.info(
                "serverless_dispatch_ok",
                agent_id=aid,
                job_type=job_type,
                cadence=cadence,
            )
        except Exception as exc:  # noqa: BLE001
            results.append({"agent_id": aid, "status": "error", "error": str(exc)})
            log.error(
                "serverless_dispatch_error",
                agent_id=aid,
                job_type=job_type,
                cadence=cadence,
                error=str(exc),
            )

    any_ok = any(r["status"] == "ok" for r in results)
    any_error = any(r["status"] == "error" for r in results)

    overall = "ok" if any_ok and not any_error else ("partial" if any_ok else "error")
    http_code = 200 if overall != "error" else 500

    return {
        "status": overall,
        "job_type": job_type,
        "cadence": cadence,
        "agents": results,
        "code": http_code,
    }


# ---------------------------------------------------------------------------
# HTTP handler — Cloud Run / App Runner / Azure Container Apps
# ---------------------------------------------------------------------------


def http_handler(
    payload: dict[str, Any],
    auth_header: str | None = None,
    config_path: str | None = None,
) -> dict[str, Any]:
    """
    Handle an HTTP-triggered serverless job invocation.

    Suitable for GCP Cloud Scheduler → Cloud Run, AWS EventBridge → App Runner,
    and Azure Logic Apps → Container Apps patterns (SRC-089).

    Args:
        payload:     Parsed JSON body.  Expected keys:
                     - ``job_type``:  "sourcing" | "curation"
                     - ``cadence``:   "daily" | "weekly" | "monthly" | "annual"
                                      (required when job_type == "curation")
                     - ``agent_id``:  Optional.  If omitted, all enabled agents run.
        auth_header: The raw ``Authorization`` HTTP header value (e.g. "Bearer <key>").
        config_path: Override scheduler config path (defaults to ``SCHEDULER_CONFIG``
                     env var or ``configs/scheduler.yaml``).

    Returns:
        A response dict with keys: ``status``, ``code``, and ``agents``.
        ``code`` is the HTTP status code to return to the cloud scheduler.
        Non-2xx codes trigger cloud-native failure alerting (SRC-146).

    Traces: SRC-085 (same image in all envs), SRC-089 (multi-cloud),
            SRC-146 (non-2xx alerting), SRC-147 (authenticated trigger)
    """
    # --- Auth (SRC-147) ---
    provided_key: str | None = None
    if auth_header and auth_header.startswith("Bearer "):
        provided_key = auth_header[len("Bearer ") :]

    if not validate_api_key(provided_key):
        log.warning("serverless_http_auth_failed")
        return {
            "status": "error",
            "message": "Unauthorized — invalid or missing API key.",
            "code": 401,
        }

    job_type = payload.get("job_type", os.environ.get("JOB_TYPE", ""))
    cadence = payload.get("cadence", os.environ.get("CADENCE"))
    agent_id = payload.get("agent_id", os.environ.get("AGENT_ID"))

    log.info(
        "serverless_http_trigger",
        job_type=job_type,
        cadence=cadence,
        agent_id=agent_id,
    )

    try:
        runner = _load_runner(config_path)
    except Exception as exc:  # noqa: BLE001
        log.error("serverless_runner_load_failed", error=str(exc))
        return {
            "status": "error",
            "message": f"Scheduler config load failed: {exc}",
            "code": 500,
        }

    return _dispatch(runner, job_type, cadence, agent_id)


# ---------------------------------------------------------------------------
# AWS Lambda handler (SRC-090)
# ---------------------------------------------------------------------------


def lambda_handler(event: dict[str, Any], context: object) -> dict[str, Any]:
    """
    AWS Lambda invocation handler.

    Decodes the Lambda event payload and calls :func:`http_handler`.

    NOTE (SRC-090): Lambda has a 15-minute hard timeout.  This is sufficient
    for daily/weekly/monthly curation but may be tight for annual synthesis
    with extended-thinking models.  For annual runs, prefer AWS App Runner or
    Fargate which have no enforced timeout.

    Event format (scheduled via EventBridge):
    ```json
    {
      "job_type": "sourcing",
      "agent_id": "default",
      "cadence":  null
    }
    ```

    The Lambda function URL or API Gateway may pass ``SCHEDULER_API_KEY`` via
    the event's ``headers`` dict (key: ``"Authorization"``).

    Returns a dict compatible with API Gateway proxy integration:
    ``{"statusCode": <int>, "body": <JSON string>}``

    Traces: SRC-089 (AWS), SRC-090 (Lambda timeout note), SRC-147 (auth)
    """
    headers: dict[str, str] = event.get("headers") or {}
    auth_header = headers.get("Authorization") or headers.get("authorization")

    # EventBridge scheduled events don't include headers — allow via key in body
    if auth_header is None:
        body_key = event.get("scheduler_api_key") or event.get("SCHEDULER_API_KEY")
        if body_key:
            auth_header = f"Bearer {body_key}"

    result = http_handler(payload=event, auth_header=auth_header)

    return {
        "statusCode": result.get("code", 500),
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps({k: v for k, v in result.items() if k != "code"}),
    }


# ---------------------------------------------------------------------------
# CLI one-shot entry point — local cron / GitHub Actions (SRC-076–SRC-077)
# ---------------------------------------------------------------------------


def cli_oneshot(
    job_type: str,
    cadence: str | None = None,
    agent_id: str | None = None,
    config_path: str | None = None,
) -> int:
    """
    Run all enabled agents (or a specific one) for the given job type and exit.

    This is the entry point for local cron jobs and GitHub Actions scheduled
    workflows (SRC-076–SRC-077, SRC-094).  It exits with:
    - 0  on full success
    - 1  on partial success (some agents failed, some succeeded)
    - 2  on configuration/auth error
    - 3  on complete failure (all agents errored)

    Traces: SRC-076 (local dev), SRC-077 (local cron), SRC-094 (GitHub Actions),
            SRC-144 (retry inside _dispatch), SRC-147 (auth via env var)
    """
    log.info(
        "serverless_cli_oneshot_start",
        job_type=job_type,
        cadence=cadence,
        agent_id=agent_id,
    )

    try:
        runner = _load_runner(config_path)
    except Exception as exc:  # noqa: BLE001
        log.error("serverless_cli_load_failed", error=str(exc))
        return 2

    result = _dispatch(runner, job_type, cadence, agent_id)

    if result["status"] == "ok":
        log.info("serverless_cli_oneshot_complete", agents=result.get("agents"))
        return 0
    if result["status"] == "partial":
        log.warning("serverless_cli_oneshot_partial", agents=result.get("agents"))
        return 1
    if result.get("code", 500) in (400, 404):
        log.error("serverless_cli_oneshot_config_error", **result)
        return 2

    log.error("serverless_cli_oneshot_failed", agents=result.get("agents"))
    return 3


# ---------------------------------------------------------------------------
# CLI entry point: ai-news-oneshot
# ---------------------------------------------------------------------------


def cli_main() -> None:
    """
    ``ai-news-oneshot`` — one-shot trigger entry point for cron / CI.

    Usage::

        ai-news-oneshot --job sourcing
        ai-news-oneshot --job curation --cadence daily
        ai-news-oneshot --job curation --cadence weekly --agent default
        ai-news-oneshot --job curation --cadence annual --config configs/scheduler.yaml

    Exit codes:
      0  All agents succeeded
      1  Partial success (some agents failed)
      2  Config / argument error
      3  All agents failed

    Traces: SRC-076 (local dev one-shot), SRC-077 (cron), SRC-094 (GitHub Actions)
    """
    import argparse

    parser = argparse.ArgumentParser(
        prog="ai-news-oneshot",
        description=(
            "One-shot trigger: run sourcing or curation for all enabled agents "
            "and exit.  Suitable for local cron and GitHub Actions schedules."
        ),
    )
    parser.add_argument(
        "--job",
        required=True,
        choices=["sourcing", "curation"],
        help="Job type to run.",
    )
    parser.add_argument(
        "--cadence",
        choices=["daily", "weekly", "monthly", "annual"],
        help="Curation cadence (required when --job=curation).",
    )
    parser.add_argument(
        "--agent",
        metavar="AGENT_ID",
        help="Run only this agent (default: all enabled agents).",
    )
    parser.add_argument(
        "--config",
        default=None,
        help="Path to scheduler.yaml (default: configs/scheduler.yaml).",
    )
    args = parser.parse_args()

    if args.job == "curation" and not args.cadence:
        parser.error("--cadence is required when --job=curation")

    exit_code = cli_oneshot(
        job_type=args.job,
        cadence=args.cadence,
        agent_id=args.agent,
        config_path=args.config,
    )
    sys.exit(exit_code)


if __name__ == "__main__":
    cli_main()
