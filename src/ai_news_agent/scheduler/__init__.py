"""
scheduler — APScheduler-based multi-agent job orchestration.

Submodules:
  runner      — SchedulerRunner: cron jobs, retry, manual override (SRC-052, SRC-072, SRC-144, SRC-147)
  auth        — Bearer-token API key auth for POST /api/trigger (SRC-073, SRC-147)
  serverless  — Cloud Run / Lambda / CLI one-shot handlers (SRC-076–SRC-077, SRC-085, SRC-089)
  windows     — Cadence window-boundary helpers and cron verification (SRC-028–SRC-032)

Traces: SRC-052 (scheduler), SRC-072 (multi-agent discovery), SRC-073 (env-var secrets),
        SRC-144 (retry + backoff), SRC-147 (manual override endpoint),
        SRC-148 (Twitter degradation propagated)
"""

from ai_news_agent.scheduler.runner import SchedulerRunner

__all__ = ["SchedulerRunner"]
