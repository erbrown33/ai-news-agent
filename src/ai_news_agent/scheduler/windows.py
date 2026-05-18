"""
scheduler/windows.py — Cadence window boundary helpers and trigger-time verification.

The scheduler fires at specific UTC times so that the curation window covers the
correct prior period.  This module documents and verifies those relationships so
that both the APScheduler cron expressions AND the lookback_window() function in
storage.models stay in sync.

Cadence → trigger time → lookback window:

  daily   → 00:05 UTC every day          → yesterday 00:00–23:59 UTC          (SRC-009, SRC-029)
  weekly  → 01:00 UTC every Sunday       → prior Sun 00:00 – Sat 23:59 UTC    (SRC-030)
  monthly → 02:00 UTC the 1st of month   → prior month first–last day          (SRC-031)
  annual  → 03:00 UTC every January 1st  → prior calendar year Jan 1–Dec 31   (SRC-032)

NOTE: The weekly window runs on **Sunday** (day_of_week=0 in APScheduler / cron).
The spec says "run by default on Sunday every week" (SRC-030) covering "Sunday
through Saturday" of the *prior* week.  When storage.models.lookback_window()
is called at 01:00 UTC on a Sunday, it returns the **previous** Sun–Sat range
because isoweekday() for Sunday = 7, and days_since_saturday = 7+1 = 8, so the
window is unambiguously the completed week ending on the most-recent Saturday.

Traces: SRC-009 (daily sourcing 00:00 UTC), SRC-028 (curation at start of next
        window), SRC-029 (daily curation), SRC-030 (weekly on Sunday),
        SRC-031 (monthly on 1st), SRC-032 (annual on Jan 1st),
        SRC-052 (scheduler triggers)
"""

from __future__ import annotations

from datetime import UTC, datetime

from ai_news_agent.storage.models import Cadence, lookback_window

# ---------------------------------------------------------------------------
# Public constants — used by runner.py and tests
# ---------------------------------------------------------------------------

# Maps each cadence to a human-readable description of its trigger schedule.
CADENCE_TRIGGER_DESCRIPTIONS: dict[str, str] = {
    "daily": "00:05 UTC every day — curates yesterday's articles (SRC-029)",
    "weekly": "01:00 UTC every Sunday — curates prior Sun–Sat (SRC-030)",
    "monthly": "02:00 UTC the 1st of each month — curates prior month (SRC-031)",
    "annual": "03:00 UTC every January 1st — curates prior year (SRC-032)",
}

# Default APScheduler cron expressions per cadence (mirror of scheduler.yaml).
CADENCE_DEFAULT_CRONS: dict[str, str] = {
    "sourcing_daily": "0 0 * * *",  # 00:00 UTC daily  (SRC-009)
    "curation_daily": "5 0 * * *",  # 00:05 UTC daily  (SRC-029)
    "curation_weekly": "0 1 * * 0",  # 01:00 UTC Sunday (SRC-030)
    "curation_monthly": "0 2 1 * *",  # 02:00 UTC 1st    (SRC-031)
    "curation_annual": "0 3 1 1 *",  # 03:00 UTC Jan 1  (SRC-032)
}


# ---------------------------------------------------------------------------
# Window-boundary verification
# ---------------------------------------------------------------------------


def verify_window_for_trigger(cadence: str, trigger_utc: datetime) -> tuple[datetime, datetime]:
    """
    Return the lookback window that would be computed when curation fires at
    ``trigger_utc``.  Primarily used in tests to assert correct window boundaries.

    Args:
        cadence:     One of "daily" | "weekly" | "monthly" | "annual".
        trigger_utc: The UTC datetime at which the curation job fires.

    Returns:
        ``(window_start, window_end)`` — the lookback window for that trigger time.

    Traces: SRC-028 (curation at start of next window), SRC-029–SRC-032
    """
    ref = trigger_utc.replace(tzinfo=UTC) if trigger_utc.tzinfo is None else trigger_utc
    return lookback_window(Cadence(cadence), reference=ref)


def window_summary(cadence: str, trigger_utc: datetime | None = None) -> str:
    """
    Human-readable summary of the window that will be curated for a given
    cadence at the given trigger time (defaults to now).

    Useful for structured log entries and health-check responses.

    Traces: SRC-028, SRC-150 (quality monitoring log)
    """
    ref = trigger_utc or datetime.now(UTC)
    start, end = verify_window_for_trigger(cadence, ref)
    return (
        f"{cadence}: {start.date().isoformat()} → {end.date().isoformat()}"
        f" (triggered at {ref.strftime('%H:%M UTC')})"
    )
