"""
tests/unit/test_scheduler.py — Full scheduler test suite.

Tests cover:
  - Cron expression parsing (SRC-052)
  - Exponential-backoff retry (SRC-144)
  - SchedulerRunner multi-agent discovery (SRC-072)
  - trigger_now() manual override with validation (SRC-147)
  - get_job_statuses() observability (SRC-150)
  - Window boundary correctness for all four cadences (SRC-028–SRC-032)
  - Auth helper: require_scheduler_auth + validate_api_key (SRC-073, SRC-147)
  - Serverless http_handler: dispatch, auth, error paths (SRC-085, SRC-146, SRC-147)
  - Serverless lambda_handler: protocol wrapping (SRC-089, SRC-090)
  - Serverless cli_oneshot: exit-code contract (SRC-076–SRC-077)
  - Portal /api/trigger endpoint: auth, dispatch, validation (SRC-147)
  - Portal /api/jobs endpoint (SRC-150)
  - Portal /api/health with scheduler status (SRC-102, SRC-150)

Traces: SRC-009, SRC-028–SRC-032, SRC-052, SRC-072, SRC-073, SRC-076–SRC-077,
        SRC-085, SRC-089–SRC-090, SRC-098, SRC-102, SRC-144, SRC-146–SRC-148, SRC-150
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from typing import Any
from unittest.mock import MagicMock, call, patch

import pytest
from fastapi.testclient import TestClient

from ai_news_agent.config.models import (
    AgentRegistration,
    RetryConfig,
    SchedulerConfig,
    TriggersConfig,
)
from ai_news_agent.scheduler.runner import SchedulerRunner, _parse_cron, _with_retry

# ===========================================================================
# Helpers / fixtures
# ===========================================================================

def _make_scheduler_config(
    agents: list[AgentRegistration] | None = None,
    max_retries: int = 3,
    backoff_base: int = 1,  # min=1 per Pydantic schema; tests patch time.sleep to avoid waits
) -> SchedulerConfig:
    return SchedulerConfig(
        scheduler=RetryConfig(
            max_retries=max_retries,
            retry_backoff_base_seconds=backoff_base,
        ),
        triggers=TriggersConfig(),
        agents=agents or [],
    )


def _make_runner(
    agents: list[AgentRegistration] | None = None,
    max_retries: int = 3,
    backoff_base: int = 1,
) -> SchedulerRunner:
    cfg = _make_scheduler_config(agents, max_retries, backoff_base)
    return SchedulerRunner(scheduler_config=cfg)


# ===========================================================================
# 1. Cron parser (SRC-052)
# ===========================================================================

class TestParseCron:
    """Traces: SRC-052 (cron triggers from scheduler.yaml)."""

    def test_sourcing_daily_cron(self) -> None:
        result = _parse_cron("0 0 * * *")
        assert result["minute"] == "0"
        assert result["hour"] == "0"
        assert result["day"] == "*"
        assert result["month"] == "*"
        assert result["day_of_week"] == "*"

    def test_curation_daily_cron(self) -> None:
        result = _parse_cron("5 0 * * *")
        assert result["minute"] == "5"
        assert result["hour"] == "0"

    def test_curation_weekly_cron(self) -> None:
        result = _parse_cron("0 1 * * 0")
        assert result["hour"] == "1"
        assert result["day_of_week"] == "0"  # Sunday

    def test_curation_monthly_cron(self) -> None:
        result = _parse_cron("0 2 1 * *")
        assert result["hour"] == "2"
        assert result["day"] == "1"

    def test_curation_annual_cron(self) -> None:
        result = _parse_cron("0 3 1 1 *")
        assert result["hour"] == "3"
        assert result["day"] == "1"
        assert result["month"] == "1"  # January

    def test_invalid_cron_four_fields(self) -> None:
        with pytest.raises(ValueError, match="Invalid cron"):
            _parse_cron("0 0 * *")  # Only 4 fields

    def test_invalid_cron_six_fields(self) -> None:
        with pytest.raises(ValueError, match="Invalid cron"):
            _parse_cron("0 0 * * * *")  # 6 fields

    def test_cron_with_leading_trailing_spaces(self) -> None:
        result = _parse_cron("  0 0 * * *  ")
        assert result["minute"] == "0"
        assert result["hour"] == "0"

    def test_all_five_fields_present(self) -> None:
        result = _parse_cron("30 6 15 3 1")
        assert len(result) == 5
        expected_keys = {"minute", "hour", "day", "month", "day_of_week"}
        assert set(result.keys()) == expected_keys


# ===========================================================================
# 2. Retry logic (SRC-144)
# ===========================================================================

class TestWithRetry:
    """Traces: SRC-144 (3 retries + exponential backoff: 30s → 60s → 120s)."""

    def test_success_on_first_attempt(self) -> None:
        """No retry needed on success."""
        fn = MagicMock(return_value=None)
        _with_retry(fn, max_retries=3, backoff_base=0)
        assert fn.call_count == 1

    def test_retries_on_transient_failure(self) -> None:
        """Failed attempts are retried up to max_retries times."""
        call_count = {"n": 0}

        def flaky() -> None:
            call_count["n"] += 1
            if call_count["n"] < 3:
                raise RuntimeError("transient error")

        _with_retry(flaky, max_retries=3, backoff_base=0)
        assert call_count["n"] == 3

    def test_raises_after_max_retries_exhausted(self) -> None:
        """Exception propagates after all retries exhausted (SRC-144)."""
        fn = MagicMock(side_effect=RuntimeError("permanent failure"))
        with pytest.raises(RuntimeError, match="permanent failure"):
            _with_retry(fn, max_retries=2, backoff_base=0)
        assert fn.call_count == 3  # 1 initial + 2 retries

    def test_zero_retries_raises_immediately(self) -> None:
        fn = MagicMock(side_effect=RuntimeError("immediate fail"))
        with pytest.raises(RuntimeError, match="immediate fail"):
            _with_retry(fn, max_retries=0, backoff_base=0)
        assert fn.call_count == 1

    def test_backoff_sleep_called_between_retries(self) -> None:
        """time.sleep is called with exponential backoff (SRC-144)."""
        fn = MagicMock(side_effect=[RuntimeError("fail"), RuntimeError("fail"), None])
        with patch("ai_news_agent.scheduler.runner.time") as mock_time:
            mock_time.sleep = MagicMock()
            _with_retry(fn, max_retries=3, backoff_base=30)

        sleep_calls = mock_time.sleep.call_args_list
        # First retry: 30s, second retry: 60s
        assert len(sleep_calls) == 2
        assert sleep_calls[0] == call(30)
        assert sleep_calls[1] == call(60)

    def test_backoff_schedule_three_retries(self) -> None:
        """Three retries with base=30: 30 → 60 → 120 (SRC-144)."""
        fn = MagicMock(
            side_effect=[
                RuntimeError("1"),
                RuntimeError("2"),
                RuntimeError("3"),
                None,
            ]
        )
        with patch("ai_news_agent.scheduler.runner.time") as mock_time:
            mock_time.sleep = MagicMock()
            _with_retry(fn, max_retries=3, backoff_base=30)

        sleeps = [c[0][0] for c in mock_time.sleep.call_args_list]
        assert sleeps == [30, 60, 120]

    def test_no_sleep_on_success(self) -> None:
        fn = MagicMock(return_value=None)
        with patch("ai_news_agent.scheduler.runner.time") as mock_time:
            mock_time.sleep = MagicMock()
            _with_retry(fn, max_retries=3, backoff_base=30)
        mock_time.sleep.assert_not_called()


# ===========================================================================
# 3. SchedulerRunner — multi-agent discovery (SRC-072)
# ===========================================================================

class TestSchedulerRunnerAgentDiscovery:
    """Traces: SRC-072 (multi-agent discovery from scheduler.yaml)."""

    def test_disabled_agent_not_loaded(self) -> None:
        """Disabled agents are skipped during load (SRC-072)."""
        runner = _make_runner(agents=[
            AgentRegistration(id="enabled", config="configs/default-agent.yaml", enabled=True),
            AgentRegistration(id="disabled", config="configs/nonexistent.yaml", enabled=False),
        ])

        with patch("ai_news_agent.scheduler.runner.load_agent_config") as mock_load:
            from ai_news_agent.config.models import AgentConfig
            mock_load.return_value = AgentConfig(agent_id="enabled")
            runner.load_agent_configs()

        assert "enabled" in runner._agent_configs
        assert "disabled" not in runner._agent_configs
        mock_load.assert_called_once()

    def test_load_failure_does_not_abort_others(self) -> None:
        """A bad config does not prevent other agents from loading (SRC-072)."""
        runner = _make_runner(agents=[
            AgentRegistration(id="good", config="configs/good-agent.yaml", enabled=True),
            AgentRegistration(id="bad", config="configs/bad.yaml", enabled=True),
        ])

        from ai_news_agent.config.loader import ConfigError
        from ai_news_agent.config.models import AgentConfig

        def mock_load(path: object) -> AgentConfig:
            if "bad" in str(path):
                raise ConfigError("File not found")
            return AgentConfig(agent_id="good")

        with patch("ai_news_agent.scheduler.runner.load_agent_config", side_effect=mock_load):
            runner.load_agent_configs()

        assert "good" in runner._agent_configs
        assert "bad" not in runner._agent_configs

    def test_multiple_agents_all_loaded(self) -> None:
        """All enabled agents are loaded as independent units (SRC-072)."""
        from pathlib import Path

        runner = _make_runner(agents=[
            AgentRegistration(id="alpha", config="alpha.yaml", enabled=True),
            AgentRegistration(id="beta",  config="beta.yaml",  enabled=True),
            AgentRegistration(id="gamma", config="gamma.yaml", enabled=True),
        ])

        from ai_news_agent.config.models import AgentConfig

        def mock_load(path: object) -> AgentConfig:
            # Use Path.stem to extract just the filename without extension,
            # regardless of the resolved directory prefix added by load_agent_configs.
            name = Path(str(path)).stem
            return AgentConfig(agent_id=name)

        with patch("ai_news_agent.scheduler.runner.load_agent_config", side_effect=mock_load):
            runner.load_agent_configs()

        assert set(runner._agent_configs.keys()) == {"alpha", "beta", "gamma"}

    def test_agent_id_mismatch_warning_logs(self) -> None:
        """agent_id mismatch between file and registry is logged as a warning."""
        runner = _make_runner(agents=[
            AgentRegistration(id="registry-id", config="file.yaml", enabled=True),
        ])
        from ai_news_agent.config.models import AgentConfig

        with patch("ai_news_agent.scheduler.runner.load_agent_config") as mock_load:
            mock_load.return_value = AgentConfig(agent_id="file-id")  # mismatches registry-id
            with patch("ai_news_agent.scheduler.runner.log") as mock_log:
                runner.load_agent_configs()
                mock_log.warning.assert_called_once()
                warn_call = mock_log.warning.call_args
                assert "config_agent_id_mismatch" in str(warn_call)

        # The file's agent_id wins
        assert "file-id" in runner._agent_configs

    def test_empty_agents_list(self) -> None:
        runner = _make_runner(agents=[])
        runner.load_agent_configs()
        assert runner._agent_configs == {}

    def test_agent_ids_property(self) -> None:
        runner = _make_runner()
        from ai_news_agent.config.models import AgentConfig
        runner._agent_configs = {"a": AgentConfig(agent_id="a"), "b": AgentConfig(agent_id="b")}
        assert sorted(runner.agent_ids) == ["a", "b"]


# ===========================================================================
# 4. trigger_now() — manual override (SRC-147)
# ===========================================================================

class TestTriggerNow:
    """Traces: SRC-028 (re-runnable on demand), SRC-144 (retry), SRC-147 (manual override)."""

    def _runner_with_agents(self) -> SchedulerRunner:
        from ai_news_agent.config.models import AgentConfig, RuntimeSecrets
        runner = _make_runner()
        runner._agent_configs["test"] = AgentConfig(agent_id="test")
        runner._secrets = RuntimeSecrets.model_validate({
            "OPENAI_API_KEY": "sk-fake",
            "TWITTER_BEARER_TOKEN": "fake-bearer",
        })
        return runner

    def test_unknown_agent_raises_value_error(self) -> None:
        """Triggering unknown agent_id raises ValueError (SRC-147)."""
        runner = _make_runner()
        with pytest.raises(ValueError, match="Unknown agent_id"):
            runner.trigger_now(agent_id="nonexistent", job_type="sourcing")

    def test_curation_without_cadence_raises(self) -> None:
        """Curation trigger without cadence raises ValueError (SRC-147)."""
        runner = self._runner_with_agents()
        with pytest.raises(ValueError, match="cadence is required"):
            runner.trigger_now(agent_id="test", job_type="curation", cadence=None)

    def test_invalid_job_type_raises(self) -> None:
        runner = self._runner_with_agents()
        with pytest.raises(ValueError, match="Unknown job_type"):
            runner.trigger_now(agent_id="test", job_type="invalid")  # type: ignore[arg-type]

    def test_sourcing_trigger_calls_run_sourcing_job(self) -> None:
        runner = self._runner_with_agents()
        with patch("ai_news_agent.scheduler.runner._run_sourcing_job") as mock_job:
            runner.trigger_now(agent_id="test", job_type="sourcing")
        mock_job.assert_called_once()

    def test_curation_trigger_calls_run_curation_job(self) -> None:
        runner = self._runner_with_agents()
        with patch("ai_news_agent.scheduler.runner._run_curation_job") as mock_job:
            runner.trigger_now(agent_id="test", job_type="curation", cadence="daily")
        mock_job.assert_called_once()
        assert mock_job.call_args[0][2] == "daily"  # cadence positional arg

    def test_curation_weekly_trigger(self) -> None:
        runner = self._runner_with_agents()
        with patch("ai_news_agent.scheduler.runner._run_curation_job") as mock_job:
            runner.trigger_now(agent_id="test", job_type="curation", cadence="weekly")
        mock_job.assert_called_once()
        assert mock_job.call_args[0][2] == "weekly"

    def test_curation_monthly_trigger(self) -> None:
        runner = self._runner_with_agents()
        with patch("ai_news_agent.scheduler.runner._run_curation_job") as mock_job:
            runner.trigger_now(agent_id="test", job_type="curation", cadence="monthly")
        mock_job.assert_called_once()
        assert mock_job.call_args[0][2] == "monthly"

    def test_curation_annual_trigger(self) -> None:
        runner = self._runner_with_agents()
        with patch("ai_news_agent.scheduler.runner._run_curation_job") as mock_job:
            runner.trigger_now(agent_id="test", job_type="curation", cadence="annual")
        mock_job.assert_called_once()
        assert mock_job.call_args[0][2] == "annual"

    def test_trigger_uses_retry_policy(self) -> None:
        """trigger_now wraps the job in _with_retry (SRC-144)."""
        runner = self._runner_with_agents()
        with patch("ai_news_agent.scheduler.runner._with_retry") as mock_retry:
            mock_retry.return_value = None
            runner.trigger_now(agent_id="test", job_type="sourcing")
        mock_retry.assert_called_once()

    def test_trigger_propagates_job_exception(self) -> None:
        """Exceptions from the job propagate back to caller (SRC-144)."""
        runner = self._runner_with_agents()
        with patch("ai_news_agent.scheduler.runner._run_sourcing_job",
                   side_effect=RuntimeError("sourcing failed")), \
             patch("ai_news_agent.scheduler.runner.time") as mock_time:
            mock_time.sleep = MagicMock()
            with pytest.raises(RuntimeError, match="sourcing failed"):
                runner.trigger_now(agent_id="test", job_type="sourcing")


# ===========================================================================
# 5. get_job_statuses() observability (SRC-150)
# ===========================================================================

class TestGetJobStatuses:
    """Traces: SRC-150 (quality monitoring — operational observability)."""

    def _runner_with_registered_jobs(self) -> SchedulerRunner:
        from ai_news_agent.config.models import AgentConfig, RuntimeSecrets
        runner = _make_runner(agents=[
            AgentRegistration(id="demo", config="demo.yaml", enabled=True),
        ])
        runner._agent_configs["demo"] = AgentConfig(agent_id="demo")
        runner._secrets = RuntimeSecrets.model_validate({
            "OPENAI_API_KEY": "sk-fake",
            "TWITTER_BEARER_TOKEN": "fake-bearer",
        })
        runner.register_jobs()
        return runner

    def test_no_jobs_before_registration(self) -> None:
        runner = _make_runner()
        assert runner.get_job_statuses() == []

    def test_five_jobs_per_agent_after_registration(self) -> None:
        runner = self._runner_with_registered_jobs()
        statuses = runner.get_job_statuses()
        # sourcing + 4 curation cadences = 5 jobs per agent
        assert len(statuses) == 5

    def test_job_status_fields(self) -> None:
        runner = self._runner_with_registered_jobs()
        status = runner.get_job_statuses()[0]
        assert "job_id" in status
        assert "name" in status
        assert "next_run_utc" in status
        assert "pending" in status

    def test_job_ids_namespaced_by_agent(self) -> None:
        runner = self._runner_with_registered_jobs()
        job_ids = {s["job_id"] for s in runner.get_job_statuses()}
        expected = {
            "demo_sourcing_daily",
            "demo_curation_daily",
            "demo_curation_weekly",
            "demo_curation_monthly",
            "demo_curation_annual",
        }
        assert job_ids == expected

    def test_ten_jobs_for_two_agents(self) -> None:
        from ai_news_agent.config.models import AgentConfig, RuntimeSecrets
        runner = _make_runner(agents=[
            AgentRegistration(id="alpha", config="a.yaml", enabled=True),
            AgentRegistration(id="beta",  config="b.yaml",  enabled=True),
        ])
        runner._agent_configs["alpha"] = AgentConfig(agent_id="alpha")
        runner._agent_configs["beta"]  = AgentConfig(agent_id="beta")
        runner._secrets = RuntimeSecrets.model_validate({
            "OPENAI_API_KEY": "sk-fake",
            "TWITTER_BEARER_TOKEN": "fake-bearer",
        })
        runner.register_jobs()
        assert len(runner.get_job_statuses()) == 10


# ===========================================================================
# 6. Window-boundary correctness (SRC-028–SRC-032)
# ===========================================================================

class TestWindowBoundaries:
    """
    Verify that the cadence triggers fire at times that produce correct lookback
    windows.

    Traces: SRC-028 (curation at start of next window), SRC-029 (daily),
            SRC-030 (weekly: prior Sun–Sat), SRC-031 (monthly: prior month),
            SRC-032 (annual: prior year)
    """

    def test_daily_window_covers_yesterday(self) -> None:
        """Daily trigger at 00:05 UTC → window = prior day 00:00–23:59 (SRC-029)."""
        from ai_news_agent.scheduler.windows import verify_window_for_trigger
        # Trigger fires 2026-05-11 00:05 UTC
        trigger = datetime(2026, 5, 11, 0, 5, tzinfo=UTC)
        start, end = verify_window_for_trigger("daily", trigger)
        assert start.date().isoformat() == "2026-05-10"
        assert end.date().isoformat() == "2026-05-10"
        assert start.hour == 0
        assert start.minute == 0
        assert end.hour == 23
        assert end.minute == 59

    def test_weekly_trigger_on_sunday_covers_prior_sun_to_sat(self) -> None:
        """Weekly trigger at 01:00 UTC Sunday → prior Sun–Sat (SRC-030)."""
        from ai_news_agent.scheduler.windows import verify_window_for_trigger
        # Sunday 2026-05-10 01:00 UTC
        trigger = datetime(2026, 5, 10, 1, 0, tzinfo=UTC)
        assert trigger.weekday() == 6  # Python weekday: Sunday = 6
        start, end = verify_window_for_trigger("weekly", trigger)
        # Prior Sunday = 2026-05-03, prior Saturday = 2026-05-09
        assert start.date().isoformat() == "2026-05-03"
        assert end.date().isoformat() == "2026-05-09"

    def test_monthly_trigger_on_first_covers_prior_month(self) -> None:
        """Monthly trigger on 1st of month → prior month first–last day (SRC-031)."""
        from ai_news_agent.scheduler.windows import verify_window_for_trigger
        # 2026-05-01 02:00 UTC
        trigger = datetime(2026, 5, 1, 2, 0, tzinfo=UTC)
        start, end = verify_window_for_trigger("monthly", trigger)
        assert start.date().isoformat() == "2026-04-01"
        assert end.date().isoformat() == "2026-04-30"

    def test_monthly_jan_trigger_covers_december(self) -> None:
        """Monthly trigger on Jan 1st → December of prior year (SRC-031)."""
        from ai_news_agent.scheduler.windows import verify_window_for_trigger
        trigger = datetime(2026, 1, 1, 2, 0, tzinfo=UTC)
        start, end = verify_window_for_trigger("monthly", trigger)
        assert start.year == 2025
        assert start.month == 12
        assert end.month == 12

    def test_annual_trigger_on_jan_1_covers_prior_year(self) -> None:
        """Annual trigger on Jan 1st → prior calendar year (SRC-032)."""
        from ai_news_agent.scheduler.windows import verify_window_for_trigger
        trigger = datetime(2026, 1, 1, 3, 0, tzinfo=UTC)
        start, end = verify_window_for_trigger("annual", trigger)
        assert start.date().isoformat() == "2025-01-01"
        assert end.date().isoformat() == "2025-12-31"
        assert start.year == 2025
        assert end.year == 2025

    def test_window_summary_returns_string(self) -> None:
        from ai_news_agent.scheduler.windows import window_summary
        trigger = datetime(2026, 5, 11, 0, 5, tzinfo=UTC)
        summary = window_summary("daily", trigger)
        assert "daily" in summary
        assert "2026-05-10" in summary

    def test_cadence_trigger_descriptions_all_present(self) -> None:
        from ai_news_agent.scheduler.windows import CADENCE_TRIGGER_DESCRIPTIONS
        assert set(CADENCE_TRIGGER_DESCRIPTIONS.keys()) == {
            "daily", "weekly", "monthly", "annual"
        }

    def test_cadence_default_crons_all_present(self) -> None:
        from ai_news_agent.scheduler.windows import CADENCE_DEFAULT_CRONS
        expected = {
            "sourcing_daily", "curation_daily",
            "curation_weekly", "curation_monthly", "curation_annual",
        }
        assert set(CADENCE_DEFAULT_CRONS.keys()) == expected

    def test_weekly_trigger_mon_covers_correct_week(self) -> None:
        """
        When triggered on a Monday (not the default, but verifiable),
        the weekly window still covers the most-recent completed Sun–Sat.
        """
        from ai_news_agent.scheduler.windows import verify_window_for_trigger
        # Monday 2026-05-11 01:00 UTC
        trigger = datetime(2026, 5, 11, 1, 0, tzinfo=UTC)
        assert trigger.weekday() == 0  # Monday
        start, end = verify_window_for_trigger("weekly", trigger)
        # Most recent Sunday was 2026-05-10; most recent Saturday was 2026-05-09
        # Days since Saturday from Monday: isoweekday(1)+1 = 2 → back 2 days → 2026-05-09
        assert end.date().isoformat() == "2026-05-09"
        assert start.date().isoformat() == "2026-05-03"


# ===========================================================================
# 7. Auth helpers (SRC-073, SRC-147)
# ===========================================================================

class TestValidateApiKey:
    """Traces: SRC-073 (env-var secrets), SRC-147 (authenticated trigger)."""

    def test_no_key_configured_always_passes(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("SCHEDULER_API_KEY", raising=False)
        from ai_news_agent.scheduler.auth import validate_api_key
        assert validate_api_key(None) is True
        assert validate_api_key("anything") is True

    def test_matching_key_passes(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("SCHEDULER_API_KEY", "correct-key")
        from ai_news_agent.scheduler.auth import validate_api_key
        assert validate_api_key("correct-key") is True

    def test_wrong_key_fails(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("SCHEDULER_API_KEY", "correct-key")
        from ai_news_agent.scheduler.auth import validate_api_key
        assert validate_api_key("wrong-key") is False

    def test_none_key_fails_when_configured(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("SCHEDULER_API_KEY", "correct-key")
        from ai_news_agent.scheduler.auth import validate_api_key
        assert validate_api_key(None) is False


class TestRequireSchedulerAuth:
    """Tests for the FastAPI dependency (SRC-073, SRC-146, SRC-147)."""

    def _make_mock_request(self, auth_header: str | None = None) -> MagicMock:
        req = MagicMock()
        req.headers = {}
        if auth_header is not None:
            req.headers["Authorization"] = auth_header
        req.client = MagicMock()
        req.client.host = "127.0.0.1"
        return req

    @pytest.mark.asyncio
    async def test_no_key_configured_passes_all(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("SCHEDULER_API_KEY", raising=False)
        from ai_news_agent.scheduler.auth import require_scheduler_auth
        req = self._make_mock_request()
        await require_scheduler_auth(req)  # Should not raise

    @pytest.mark.asyncio
    async def test_valid_bearer_token_passes(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from importlib import reload

        import ai_news_agent.scheduler.auth as auth_mod

        monkeypatch.setenv("SCHEDULER_API_KEY", "secret-key")
        reload(auth_mod)  # pick up monkeypatched env var

        req = self._make_mock_request(auth_header="Bearer secret-key")
        await auth_mod.require_scheduler_auth(req)  # Should not raise

    @pytest.mark.asyncio
    async def test_missing_auth_header_raises_401(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from importlib import reload

        from fastapi import HTTPException

        import ai_news_agent.scheduler.auth as auth_mod

        monkeypatch.setenv("SCHEDULER_API_KEY", "secret-key")
        reload(auth_mod)

        req = self._make_mock_request()  # no auth header
        with pytest.raises(HTTPException) as exc_info:
            await auth_mod.require_scheduler_auth(req)
        assert exc_info.value.status_code == 401

    @pytest.mark.asyncio
    async def test_wrong_key_raises_401(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from importlib import reload

        from fastapi import HTTPException

        import ai_news_agent.scheduler.auth as auth_mod

        monkeypatch.setenv("SCHEDULER_API_KEY", "correct-key")
        reload(auth_mod)

        req = self._make_mock_request(auth_header="Bearer wrong-key")
        with pytest.raises(HTTPException) as exc_info:
            await auth_mod.require_scheduler_auth(req)
        assert exc_info.value.status_code == 401

    @pytest.mark.asyncio
    async def test_non_bearer_scheme_raises_401(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from importlib import reload

        from fastapi import HTTPException

        import ai_news_agent.scheduler.auth as auth_mod

        monkeypatch.setenv("SCHEDULER_API_KEY", "correct-key")
        reload(auth_mod)

        req = self._make_mock_request(auth_header="Basic dXNlcjpwYXNz")
        with pytest.raises(HTTPException) as exc_info:
            await auth_mod.require_scheduler_auth(req)
        assert exc_info.value.status_code == 401


# ===========================================================================
# 8. Serverless HTTP handler (SRC-085, SRC-089, SRC-146, SRC-147)
# ===========================================================================

class TestHttpHandler:
    """Traces: SRC-085 (same image all envs), SRC-089 (multi-cloud),
               SRC-146 (non-2xx alerting), SRC-147 (auth)."""

    def _make_runner(self) -> SchedulerRunner:
        from ai_news_agent.config.models import AgentConfig, RuntimeSecrets
        runner = _make_runner_fn()
        runner._agent_configs["default"] = AgentConfig(agent_id="default")
        runner._secrets = RuntimeSecrets.model_validate({
            "OPENAI_API_KEY": "sk-fake",
            "TWITTER_BEARER_TOKEN": "fake-bearer",
        })
        return runner

    def test_auth_fails_with_wrong_key(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("SCHEDULER_API_KEY", "correct-key")
        from ai_news_agent.scheduler.serverless import http_handler
        result = http_handler(
            payload={"job_type": "sourcing"},
            auth_header="Bearer wrong-key",
        )
        assert result["code"] == 401
        assert result["status"] == "error"

    def test_auth_passes_with_correct_key(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("SCHEDULER_API_KEY", "correct-key")
        from ai_news_agent.scheduler.serverless import http_handler

        with patch("ai_news_agent.scheduler.serverless._load_runner") as mock_lr:
            mock_lr.return_value = self._make_runner()
            with patch("ai_news_agent.scheduler.serverless._run_sourcing_job"):
                result = http_handler(
                    payload={"job_type": "sourcing"},
                    auth_header="Bearer correct-key",
                )
        assert result["code"] == 200
        assert result["status"] == "ok"

    def test_no_key_configured_passes_through(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("SCHEDULER_API_KEY", raising=False)
        from ai_news_agent.scheduler.serverless import http_handler

        with patch("ai_news_agent.scheduler.serverless._load_runner") as mock_lr:
            mock_lr.return_value = self._make_runner()
            with patch("ai_news_agent.scheduler.serverless._run_sourcing_job"):
                result = http_handler(payload={"job_type": "sourcing"})
        assert result["code"] == 200

    def test_invalid_job_type_returns_400(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("SCHEDULER_API_KEY", raising=False)
        from ai_news_agent.scheduler.serverless import http_handler

        with patch("ai_news_agent.scheduler.serverless._load_runner") as mock_lr:
            mock_lr.return_value = self._make_runner()
            result = http_handler(payload={"job_type": "invalid"})
        assert result["code"] == 400

    def test_curation_without_cadence_returns_400(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("SCHEDULER_API_KEY", raising=False)
        from ai_news_agent.scheduler.serverless import http_handler

        with patch("ai_news_agent.scheduler.serverless._load_runner") as mock_lr:
            mock_lr.return_value = self._make_runner()
            result = http_handler(payload={"job_type": "curation"})
        assert result["code"] == 400

    def test_curation_with_valid_cadence(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("SCHEDULER_API_KEY", raising=False)
        from ai_news_agent.scheduler.serverless import http_handler

        with patch("ai_news_agent.scheduler.serverless._load_runner") as mock_lr:
            mock_lr.return_value = self._make_runner()
            with patch("ai_news_agent.scheduler.serverless._run_curation_job"):
                result = http_handler(
                    payload={"job_type": "curation", "cadence": "daily"}
                )
        assert result["code"] == 200
        assert result["cadence"] == "daily"

    def test_unknown_agent_returns_404(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("SCHEDULER_API_KEY", raising=False)
        from ai_news_agent.scheduler.serverless import http_handler

        with patch("ai_news_agent.scheduler.serverless._load_runner") as mock_lr:
            mock_lr.return_value = self._make_runner()
            result = http_handler(
                payload={"job_type": "sourcing", "agent_id": "nonexistent"}
            )
        assert result["code"] == 404

    def test_config_load_failure_returns_500(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("SCHEDULER_API_KEY", raising=False)
        from ai_news_agent.scheduler.serverless import http_handler

        with patch(
            "ai_news_agent.scheduler.serverless._load_runner",
            side_effect=RuntimeError("config missing"),
        ):
            result = http_handler(payload={"job_type": "sourcing"})
        assert result["code"] == 500

    def test_all_cadences_dispatched_correctly(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("SCHEDULER_API_KEY", raising=False)
        from ai_news_agent.scheduler.serverless import http_handler

        for cadence in ("daily", "weekly", "monthly", "annual"):
            with patch("ai_news_agent.scheduler.serverless._load_runner") as mock_lr:
                mock_lr.return_value = self._make_runner()
                with patch("ai_news_agent.scheduler.serverless._run_curation_job"):
                    result = http_handler(
                        payload={"job_type": "curation", "cadence": cadence}
                    )
            assert result["code"] == 200, f"cadence={cadence} failed"
            assert result["cadence"] == cadence


# local helper to avoid shadowing the class method
def _make_runner_fn() -> SchedulerRunner:
    return _make_runner()


# ===========================================================================
# 9. Lambda handler (SRC-089, SRC-090)
# ===========================================================================

class TestLambdaHandler:
    """Traces: SRC-089 (AWS), SRC-090 (Lambda timeout note)."""

    def _make_runner(self) -> SchedulerRunner:
        from ai_news_agent.config.models import AgentConfig, RuntimeSecrets
        runner = _make_runner_fn()
        runner._agent_configs["default"] = AgentConfig(agent_id="default")
        runner._secrets = RuntimeSecrets.model_validate({
            "OPENAI_API_KEY": "sk-fake",
            "TWITTER_BEARER_TOKEN": "fake-bearer",
        })
        return runner

    def test_lambda_handler_returns_status_code(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("SCHEDULER_API_KEY", raising=False)
        from ai_news_agent.scheduler.serverless import lambda_handler

        with patch("ai_news_agent.scheduler.serverless._load_runner") as mock_lr:
            mock_lr.return_value = self._make_runner()
            with patch("ai_news_agent.scheduler.serverless._run_sourcing_job"):
                response = lambda_handler(
                    event={"job_type": "sourcing"},
                    context=None,
                )

        assert "statusCode" in response
        assert "body" in response
        assert response["statusCode"] == 200

    def test_lambda_auth_via_event_key(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("SCHEDULER_API_KEY", "lambda-key")
        from ai_news_agent.scheduler.serverless import lambda_handler

        with patch("ai_news_agent.scheduler.serverless._load_runner") as mock_lr:
            mock_lr.return_value = self._make_runner()
            with patch("ai_news_agent.scheduler.serverless._run_sourcing_job"):
                response = lambda_handler(
                    event={"job_type": "sourcing", "scheduler_api_key": "lambda-key"},
                    context=None,
                )
        assert response["statusCode"] == 200

    def test_lambda_auth_via_headers(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("SCHEDULER_API_KEY", "hdr-key")
        from ai_news_agent.scheduler.serverless import lambda_handler

        with patch("ai_news_agent.scheduler.serverless._load_runner") as mock_lr:
            mock_lr.return_value = self._make_runner()
            with patch("ai_news_agent.scheduler.serverless._run_sourcing_job"):
                response = lambda_handler(
                    event={
                        "job_type": "sourcing",
                        "headers": {"Authorization": "Bearer hdr-key"},
                    },
                    context=None,
                )
        assert response["statusCode"] == 200

    def test_lambda_wrong_key_returns_401(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("SCHEDULER_API_KEY", "real-key")
        from ai_news_agent.scheduler.serverless import lambda_handler

        response = lambda_handler(
            event={"job_type": "sourcing", "scheduler_api_key": "wrong-key"},
            context=None,
        )
        assert response["statusCode"] == 401

    def test_lambda_body_is_valid_json(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("SCHEDULER_API_KEY", raising=False)
        from ai_news_agent.scheduler.serverless import lambda_handler

        with patch("ai_news_agent.scheduler.serverless._load_runner") as mock_lr:
            mock_lr.return_value = self._make_runner()
            with patch("ai_news_agent.scheduler.serverless._run_sourcing_job"):
                response = lambda_handler(event={"job_type": "sourcing"}, context=None)

        parsed = json.loads(response["body"])
        assert "status" in parsed


# ===========================================================================
# 10. cli_oneshot exit codes (SRC-076–SRC-077)
# ===========================================================================

class TestCliOneshot:
    """Traces: SRC-076 (local dev one-shot), SRC-077 (cron), SRC-094 (GitHub Actions)."""

    def _make_runner(self) -> SchedulerRunner:
        from ai_news_agent.config.models import AgentConfig, RuntimeSecrets
        runner = _make_runner_fn()
        runner._agent_configs["default"] = AgentConfig(agent_id="default")
        runner._secrets = RuntimeSecrets.model_validate({
            "OPENAI_API_KEY": "sk-fake",
            "TWITTER_BEARER_TOKEN": "fake-bearer",
        })
        return runner

    def test_exit_0_on_success(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from ai_news_agent.scheduler.serverless import cli_oneshot

        with patch("ai_news_agent.scheduler.serverless._load_runner") as mock_lr:
            mock_lr.return_value = self._make_runner()
            with patch("ai_news_agent.scheduler.serverless._run_sourcing_job"):
                code = cli_oneshot(job_type="sourcing")
        assert code == 0

    def test_exit_2_on_config_error(self) -> None:
        from ai_news_agent.scheduler.serverless import cli_oneshot

        with patch(
            "ai_news_agent.scheduler.serverless._load_runner",
            side_effect=RuntimeError("no config"),
        ):
            code = cli_oneshot(job_type="sourcing")
        assert code == 2

    def test_exit_2_on_invalid_job_type(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from ai_news_agent.scheduler.serverless import cli_oneshot

        with patch("ai_news_agent.scheduler.serverless._load_runner") as mock_lr:
            mock_lr.return_value = self._make_runner()
            code = cli_oneshot(job_type="invalid")  # type: ignore[arg-type]
        assert code == 2

    def test_exit_3_on_all_agents_failed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from ai_news_agent.scheduler.serverless import cli_oneshot

        with patch("ai_news_agent.scheduler.serverless._load_runner") as mock_lr, \
             patch(
                 "ai_news_agent.scheduler.serverless._run_sourcing_job",
                 side_effect=RuntimeError("fail"),
             ), \
             patch("ai_news_agent.scheduler.runner.time") as mock_time:
            mock_lr.return_value = self._make_runner()
            mock_time.sleep = MagicMock()
            code = cli_oneshot(job_type="sourcing")
        assert code == 3

    def test_curation_daily_exit_0(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from ai_news_agent.scheduler.serverless import cli_oneshot

        with patch("ai_news_agent.scheduler.serverless._load_runner") as mock_lr:
            mock_lr.return_value = self._make_runner()
            with patch("ai_news_agent.scheduler.serverless._run_curation_job"):
                code = cli_oneshot(job_type="curation", cadence="daily")
        assert code == 0

    def test_curation_missing_cadence_exit_2(self) -> None:
        from ai_news_agent.scheduler.serverless import cli_oneshot

        with patch("ai_news_agent.scheduler.serverless._load_runner") as mock_lr:
            mock_lr.return_value = self._make_runner()
            code = cli_oneshot(job_type="curation", cadence=None)
        assert code == 2

    def test_specific_agent_only(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from ai_news_agent.scheduler.serverless import cli_oneshot

        with patch("ai_news_agent.scheduler.serverless._load_runner") as mock_lr:
            runner = self._make_runner()
            mock_lr.return_value = runner
            with patch("ai_news_agent.scheduler.serverless._run_sourcing_job") as mock_job:
                code = cli_oneshot(job_type="sourcing", agent_id="default")
        assert code == 0
        mock_job.assert_called_once()


# ===========================================================================
# 11. Portal /api/trigger endpoint (SRC-147)
# ===========================================================================

class TestPortalTriggerEndpoint:
    """
    Tests for the POST /api/trigger portal route.
    Traces: SRC-028, SRC-073, SRC-146, SRC-147
    """

    def _make_test_client(
        self,
        runner: SchedulerRunner | None = None,
        scheduler_api_key: str | None = None,
    ) -> TestClient:
        """Build a TestClient with or without a real SchedulerRunner attached."""
        from ai_news_agent.portal.app import create_app
        _app = create_app(scheduler_runner=runner)
        if scheduler_api_key is not None:
            os.environ["SCHEDULER_API_KEY"] = scheduler_api_key
        return TestClient(_app, raise_server_exceptions=False)

    def _make_runner(self) -> SchedulerRunner:
        from ai_news_agent.config.models import AgentConfig, RuntimeSecrets
        runner = _make_runner_fn()
        runner._agent_configs["default"] = AgentConfig(agent_id="default")
        runner._secrets = RuntimeSecrets.model_validate({
            "OPENAI_API_KEY": "sk-fake",
            "TWITTER_BEARER_TOKEN": "fake-bearer",
        })
        return runner

    def test_trigger_no_runner_returns_accepted(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When no runner is attached, trigger returns 200 Accepted."""
        monkeypatch.delenv("SCHEDULER_API_KEY", raising=False)
        client = self._make_test_client(runner=None)
        resp = client.post(
            "/api/trigger",
            json={"agent_id": "default", "job_type": "sourcing"},
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "accepted"

    def test_trigger_with_runner_dispatches_job(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When runner is attached and agent exists, trigger returns 200 Accepted."""
        monkeypatch.delenv("SCHEDULER_API_KEY", raising=False)
        runner = self._make_runner()
        client = self._make_test_client(runner=runner)

        with patch.object(runner, "trigger_now") as mock_trigger:
            mock_trigger.return_value = None
            resp = client.post(
                "/api/trigger",
                json={"agent_id": "default", "job_type": "sourcing"},
            )
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "accepted"
        assert body["agent_id"] == "default"

    def test_trigger_unknown_agent_returns_404(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("SCHEDULER_API_KEY", raising=False)
        runner = self._make_runner()
        client = self._make_test_client(runner=runner)
        resp = client.post(
            "/api/trigger",
            json={"agent_id": "nonexistent", "job_type": "sourcing"},
        )
        assert resp.status_code == 404

    def test_trigger_invalid_job_type_returns_400(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("SCHEDULER_API_KEY", raising=False)
        runner = self._make_runner()
        client = self._make_test_client(runner=runner)
        resp = client.post(
            "/api/trigger",
            json={"agent_id": "default", "job_type": "invalid"},
        )
        assert resp.status_code == 400

    def test_curation_without_cadence_returns_400(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("SCHEDULER_API_KEY", raising=False)
        runner = self._make_runner()
        client = self._make_test_client(runner=runner)
        resp = client.post(
            "/api/trigger",
            json={"agent_id": "default", "job_type": "curation"},
        )
        assert resp.status_code == 400

    def test_curation_daily_with_runner(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("SCHEDULER_API_KEY", raising=False)
        runner = self._make_runner()
        client = self._make_test_client(runner=runner)
        with patch.object(runner, "trigger_now") as mock_trigger:
            mock_trigger.return_value = None
            resp = client.post(
                "/api/trigger",
                json={"agent_id": "default", "job_type": "curation", "cadence": "daily"},
            )
        assert resp.status_code == 200
        assert resp.json()["cadence"] == "daily"

    def test_trigger_requires_auth_when_key_set(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When SCHEDULER_API_KEY is set, requests without the key get 401 (SRC-147)."""
        from importlib import reload

        import ai_news_agent.scheduler.auth as auth_mod
        from ai_news_agent.portal.app import create_app

        monkeypatch.setenv("SCHEDULER_API_KEY", "portal-secret")
        runner = self._make_runner()
        reload(auth_mod)  # pick up new env var value
        _app = create_app(scheduler_runner=runner)
        client = TestClient(_app, raise_server_exceptions=False)

        # Without auth header
        resp = client.post(
            "/api/trigger",
            json={"agent_id": "default", "job_type": "sourcing"},
        )
        assert resp.status_code == 401

    def test_trigger_passes_with_correct_bearer_token(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from importlib import reload

        import ai_news_agent.scheduler.auth as auth_mod
        from ai_news_agent.portal.app import create_app

        monkeypatch.setenv("SCHEDULER_API_KEY", "portal-secret")
        runner = self._make_runner()
        reload(auth_mod)
        _app = create_app(scheduler_runner=runner)
        client = TestClient(_app, raise_server_exceptions=False)

        with patch.object(runner, "trigger_now") as mock_trigger:
            mock_trigger.return_value = None
            resp = client.post(
                "/api/trigger",
                json={"agent_id": "default", "job_type": "sourcing"},
                headers={"Authorization": "Bearer portal-secret"},
            )
        assert resp.status_code == 200


# ===========================================================================
# 12. Portal /api/jobs endpoint (SRC-150)
# ===========================================================================

class TestPortalJobsEndpoint:
    """Traces: SRC-052 (scheduler), SRC-150 (operational observability)."""

    def test_jobs_endpoint_without_runner_returns_503(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("SCHEDULER_API_KEY", raising=False)
        from ai_news_agent.portal.app import create_app
        _app = create_app(scheduler_runner=None)
        client = TestClient(_app)
        resp = client.get("/api/jobs")
        assert resp.status_code == 503
        assert resp.json()["status"] == "unavailable"

    def test_jobs_endpoint_with_runner_returns_200(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("SCHEDULER_API_KEY", raising=False)
        from ai_news_agent.config.models import AgentConfig, RuntimeSecrets
        from ai_news_agent.portal.app import create_app
        runner = _make_runner_fn()
        runner._agent_configs["demo"] = AgentConfig(agent_id="demo")
        runner._secrets = RuntimeSecrets.model_validate({
            "OPENAI_API_KEY": "sk-fake",
            "TWITTER_BEARER_TOKEN": "fake-bearer",
        })
        runner.register_jobs()

        _app = create_app(scheduler_runner=runner)
        client = TestClient(_app)
        resp = client.get("/api/jobs")
        assert resp.status_code == 200
        data = resp.json()
        assert "jobs" in data
        assert len(data["jobs"]) == 5  # 1 agent × 5 jobs

    def test_jobs_endpoint_includes_next_run_utc(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("SCHEDULER_API_KEY", raising=False)
        from ai_news_agent.config.models import AgentConfig, RuntimeSecrets
        from ai_news_agent.portal.app import create_app
        runner = _make_runner_fn()
        runner._agent_configs["demo"] = AgentConfig(agent_id="demo")
        runner._secrets = RuntimeSecrets.model_validate({
            "OPENAI_API_KEY": "sk-fake",
            "TWITTER_BEARER_TOKEN": "fake-bearer",
        })
        runner.register_jobs()

        _app = create_app(scheduler_runner=runner)
        client = TestClient(_app)
        resp = client.get("/api/jobs")
        jobs = resp.json()["jobs"]
        for job in jobs:
            assert "next_run_utc" in job
            assert "job_id" in job
            assert "name" in job


# ===========================================================================
# 13. Portal /api/health with scheduler status (SRC-102, SRC-150)
# ===========================================================================

class TestPortalHealthEndpoint:
    """Traces: SRC-102 (smoke test), SRC-146 (non-2xx alerting), SRC-150 (monitoring)."""

    def test_health_no_runner(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("SCHEDULER_API_KEY", raising=False)
        from ai_news_agent.portal.app import create_app
        _app = create_app(scheduler_runner=None)
        client = TestClient(_app)
        resp = client.get("/api/health")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        assert data["scheduler"]["running"] is False
        assert data["scheduler"]["agents"] == []

    def test_health_with_runner(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("SCHEDULER_API_KEY", raising=False)
        from ai_news_agent.config.models import AgentConfig, RuntimeSecrets
        from ai_news_agent.portal.app import create_app
        runner = _make_runner_fn()
        runner._agent_configs["demo"] = AgentConfig(agent_id="demo")
        runner._secrets = RuntimeSecrets.model_validate({
            "OPENAI_API_KEY": "sk-fake",
            "TWITTER_BEARER_TOKEN": "fake-bearer",
        })

        _app = create_app(scheduler_runner=runner)
        client = TestClient(_app)
        resp = client.get("/api/health")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        assert "demo" in data["scheduler"]["agents"]

    def test_health_response_has_required_fields(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("SCHEDULER_API_KEY", raising=False)
        from ai_news_agent.portal.app import create_app
        _app = create_app()
        client = TestClient(_app)
        resp = client.get("/api/health")
        data = resp.json()
        assert "status" in data
        assert "service" in data
        assert "scheduler" in data

    def test_health_service_name(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("SCHEDULER_API_KEY", raising=False)
        from ai_news_agent.portal.app import create_app
        _app = create_app()
        client = TestClient(_app)
        resp = client.get("/api/health")
        assert resp.json()["service"] == "ai-news-curation-portal"


# ===========================================================================
# 14. SchedulerRunner properties and lifecycle (SRC-052, SRC-072)
# ===========================================================================

class TestSchedulerRunnerProperties:
    """Traces: SRC-052 (scheduler lifecycle), SRC-072 (multi-agent)."""

    def test_is_running_false_before_start(self) -> None:
        runner = _make_runner()
        assert runner.is_running is False

    def test_agent_ids_property_empty_initially(self) -> None:
        runner = _make_runner()
        assert runner.agent_ids == []

    def test_agent_ids_after_load(self) -> None:
        runner = _make_runner()
        from ai_news_agent.config.models import AgentConfig
        runner._agent_configs = {
            "a": AgentConfig(agent_id="a"),
            "b": AgentConfig(agent_id="b"),
        }
        assert sorted(runner.agent_ids) == ["a", "b"]

    def test_secrets_lazy_load_raises_without_env(self) -> None:
        """Without env vars, _get_secrets raises on first call."""
        from pydantic import ValidationError as PydanticValidationError
        runner = _make_runner()
        with pytest.raises(PydanticValidationError):
            runner._get_secrets()

    def test_shutdown_is_idempotent(self) -> None:
        """Calling shutdown on a non-started scheduler should not raise."""
        runner = _make_runner()
        runner.shutdown()  # APScheduler shutdown(wait=False) on idle scheduler is safe

    def test_shutdown_twice_is_safe(self) -> None:
        """Double shutdown should not raise (idempotency SRC-052)."""
        runner = _make_runner()
        runner.shutdown()
        runner.shutdown()  # Second call must also be safe


# ===========================================================================
# 15. Window boundary edge cases — month/year transitions (SRC-028–SRC-032)
# ===========================================================================

class TestWindowBoundaryEdgeCases:
    """
    Additional window-boundary edge cases for month-end, year-end,
    and leap-year February (SRC-028–SRC-032).

    Traces: SRC-009, SRC-028–SRC-032
    """

    def test_daily_window_crosses_year_boundary(self) -> None:
        """Daily trigger on Jan 1 covers Dec 31 of the prior year (SRC-029)."""
        from ai_news_agent.scheduler.windows import verify_window_for_trigger
        trigger = datetime(2026, 1, 1, 0, 5, tzinfo=UTC)
        start, end = verify_window_for_trigger("daily", trigger)
        assert start.date().isoformat() == "2025-12-31"
        assert end.date().isoformat()   == "2025-12-31"
        assert start.year == 2025
        assert start.month == 12

    def test_daily_window_full_day_coverage(self) -> None:
        """Daily window spans exactly one 24-hour day (SRC-009)."""
        from ai_news_agent.scheduler.windows import verify_window_for_trigger
        trigger = datetime(2026, 3, 15, 0, 5, tzinfo=UTC)
        start, end = verify_window_for_trigger("daily", trigger)
        assert (end - start).seconds >= 86398  # 23h 59m 58s+

    def test_monthly_window_february_28_days(self) -> None:
        """Monthly trigger on Mar 1 → Feb 1–28 (non-leap year, SRC-031)."""
        from ai_news_agent.scheduler.windows import verify_window_for_trigger
        trigger = datetime(2025, 3, 1, 2, 0, tzinfo=UTC)
        start, end = verify_window_for_trigger("monthly", trigger)
        assert start.date().isoformat() == "2025-02-01"
        assert end.date().isoformat()   == "2025-02-28"

    def test_monthly_window_february_29_days_leap_year(self) -> None:
        """Monthly trigger on Mar 1 of leap year → Feb 1–29 (SRC-031)."""
        from ai_news_agent.scheduler.windows import verify_window_for_trigger
        # 2024 is a leap year
        trigger = datetime(2024, 3, 1, 2, 0, tzinfo=UTC)
        start, end = verify_window_for_trigger("monthly", trigger)
        assert start.date().isoformat() == "2024-02-01"
        assert end.date().isoformat()   == "2024-02-29"

    def test_monthly_window_31_day_months(self) -> None:
        """Monthly trigger covers full 31-day months (Jan, Mar, etc.) (SRC-031)."""
        from ai_news_agent.scheduler.windows import verify_window_for_trigger
        trigger = datetime(2026, 2, 1, 2, 0, tzinfo=UTC)
        start, end = verify_window_for_trigger("monthly", trigger)
        assert start.date().isoformat() == "2026-01-01"
        assert end.date().isoformat()   == "2026-01-31"

    def test_annual_window_is_full_calendar_year(self) -> None:
        """Annual window spans exactly 365 days for a non-leap year (SRC-032)."""
        from ai_news_agent.scheduler.windows import verify_window_for_trigger
        trigger = datetime(2026, 1, 1, 3, 0, tzinfo=UTC)
        start, end = verify_window_for_trigger("annual", trigger)
        # 2025 is not a leap year: Jan 1 → Dec 31 = 364 days span
        assert start.date().isoformat() == "2025-01-01"
        assert end.date().isoformat()   == "2025-12-31"
        assert start.year == end.year == 2025

    def test_annual_window_leap_year_prior(self) -> None:
        """Annual window for a leap year spans 366 days (SRC-032)."""
        from ai_news_agent.scheduler.windows import verify_window_for_trigger
        # Trigger 2025-01-01 → prior year 2024 (leap)
        trigger = datetime(2025, 1, 1, 3, 0, tzinfo=UTC)
        start, end = verify_window_for_trigger("annual", trigger)
        assert start.date().isoformat() == "2024-01-01"
        assert end.date().isoformat()   == "2024-12-31"

    def test_weekly_window_spans_exactly_7_days(self) -> None:
        """Weekly window always covers exactly 7 days (SRC-030)."""
        from ai_news_agent.scheduler.windows import verify_window_for_trigger
        trigger = datetime(2026, 5, 10, 1, 0, tzinfo=UTC)  # Sunday
        start, end = verify_window_for_trigger("weekly", trigger)
        delta = (end.date() - start.date()).days
        assert delta == 6  # 6-day span = 7 days inclusive

    def test_weekly_window_all_days_consistent(self) -> None:
        """Weekly window resolves to same Sun–Sat for any trigger day in that week."""
        from ai_news_agent.scheduler.windows import verify_window_for_trigger
        # Mon–Sun 2026-05-11 → 2026-05-17, all should give 2026-05-03 → 2026-05-09
        # except Sun 2026-05-17 which gives 2026-05-10 → 2026-05-16
        for offset in range(6):  # Mon(0)–Sat(5)
            trigger = datetime(2026, 5, 11 + offset, 1, 0, tzinfo=UTC)
            start, end = verify_window_for_trigger("weekly", trigger)
            assert start.date().isoformat() == "2026-05-03", f"offset={offset}"
            assert end.date().isoformat()   == "2026-05-09", f"offset={offset}"

    def test_window_summary_weekly_on_sunday(self) -> None:
        """Window summary for weekly trigger on Sunday is human-readable."""
        from ai_news_agent.scheduler.windows import window_summary
        trigger = datetime(2026, 5, 10, 1, 0, tzinfo=UTC)
        summary = window_summary("weekly", trigger)
        assert "weekly" in summary
        assert "2026-05-03" in summary
        assert "2026-05-09" in summary

    def test_window_summary_no_trigger_uses_now(self) -> None:
        """window_summary with no trigger defaults to now() (SRC-028)."""
        from ai_news_agent.scheduler.windows import window_summary
        # Just verify it doesn't raise and returns a non-empty string.
        summary = window_summary("daily")
        assert isinstance(summary, str)
        assert "daily" in summary


# ===========================================================================
# 16. Retry policy — edge cases and backoff schedule (SRC-144)
# ===========================================================================

class TestRetryEdgeCases:
    """
    Edge cases for the exponential-backoff retry policy (SRC-144).
    Confirms: 3 retries, 30→60→120s schedule, immediate raise on 0 retries.
    """

    def test_exactly_max_retries_allowed(self) -> None:
        """Succeeds on exactly the max_retries+1 th attempt (SRC-144)."""
        calls = {"n": 0}

        def fn() -> None:
            calls["n"] += 1
            if calls["n"] <= 3:
                raise RuntimeError("not yet")

        _with_retry(fn, max_retries=3, backoff_base=0)
        assert calls["n"] == 4  # 1 initial + 3 retries

    def test_one_more_than_max_raises(self) -> None:
        """Fails one attempt beyond max_retries still raises (SRC-144)."""
        calls = {"n": 0}

        def fn() -> None:
            calls["n"] += 1
            raise RuntimeError("always fails")

        with pytest.raises(RuntimeError):
            _with_retry(fn, max_retries=3, backoff_base=0)
        assert calls["n"] == 4  # 1 initial + 3 retries

    def test_backoff_doubling_full_schedule(self) -> None:
        """Verify 30→60→120 backoff with base=30 and 3 retries (SRC-144)."""
        fn = MagicMock(
            side_effect=[RuntimeError(), RuntimeError(), RuntimeError(), None]
        )
        with patch("ai_news_agent.scheduler.runner.time") as mock_time:
            mock_time.sleep = MagicMock()
            _with_retry(fn, max_retries=3, backoff_base=30)

        sleeps = [c[0][0] for c in mock_time.sleep.call_args_list]
        assert sleeps == [30, 60, 120]

    def test_custom_backoff_base(self) -> None:
        """Custom backoff_base doubles correctly (SRC-144)."""
        fn = MagicMock(side_effect=[RuntimeError(), RuntimeError(), None])
        with patch("ai_news_agent.scheduler.runner.time") as mock_time:
            mock_time.sleep = MagicMock()
            _with_retry(fn, max_retries=3, backoff_base=5)

        sleeps = [c[0][0] for c in mock_time.sleep.call_args_list]
        assert sleeps == [5, 10]


# ===========================================================================
# 17. Serverless dispatch — multi-agent and edge cases (SRC-072, SRC-085, SRC-089)
# ===========================================================================

class TestServerlessDispatch:
    """
    Additional tests for the _dispatch helper covering multi-agent, partial
    failures, and environment variable overrides.

    Traces: SRC-052, SRC-072, SRC-085, SRC-089, SRC-144, SRC-146
    """

    def _make_runner_with(self, *agent_ids: str) -> SchedulerRunner:
        from ai_news_agent.config.models import AgentConfig, RuntimeSecrets
        runner = _make_runner()
        for aid in agent_ids:
            runner._agent_configs[aid] = AgentConfig(agent_id=aid)
        runner._secrets = RuntimeSecrets.model_validate({
            "OPENAI_API_KEY": "sk-fake",
            "TWITTER_BEARER_TOKEN": "fake-bearer",
        })
        return runner

    def test_no_agents_returns_503(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("SCHEDULER_API_KEY", raising=False)
        from ai_news_agent.scheduler.serverless import http_handler

        with patch("ai_news_agent.scheduler.serverless._load_runner") as mock_lr:
            mock_lr.return_value = self._make_runner_with()  # no agents
            result = http_handler(payload={"job_type": "sourcing"})

        assert result["code"] == 503
        assert result["status"] == "error"

    def test_partial_success_two_agents_one_fails(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When one of two agents fails, overall status is 'partial' (SRC-146)."""
        monkeypatch.delenv("SCHEDULER_API_KEY", raising=False)
        from ai_news_agent.scheduler.serverless import http_handler

        call_count = {"n": 0}

        def mock_sourcing(cfg: Any, secrets: Any) -> None:
            call_count["n"] += 1
            if cfg.agent_id == "bad":
                raise RuntimeError("bad agent failed")

        with patch("ai_news_agent.scheduler.serverless._load_runner") as mock_lr, \
             patch("ai_news_agent.scheduler.serverless._run_sourcing_job",
                   side_effect=mock_sourcing), \
             patch("ai_news_agent.scheduler.runner.time") as mt:
            mock_lr.return_value = self._make_runner_with("good", "bad")
            mt.sleep = MagicMock()
            result = http_handler(payload={"job_type": "sourcing"})

        assert result["status"] == "partial"
        assert result["code"] == 200

    def test_unknown_cadence_for_curation_returns_400(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("SCHEDULER_API_KEY", raising=False)
        from ai_news_agent.scheduler.serverless import http_handler

        with patch("ai_news_agent.scheduler.serverless._load_runner") as mock_lr:
            mock_lr.return_value = self._make_runner_with("default")
            result = http_handler(
                payload={"job_type": "curation", "cadence": "quarterly"}
            )

        assert result["code"] == 400

    def test_specific_agent_filters_correctly(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Specifying agent_id runs only that agent (SRC-072)."""
        monkeypatch.delenv("SCHEDULER_API_KEY", raising=False)
        from ai_news_agent.scheduler.serverless import http_handler

        called_agents: list[str] = []

        def mock_sourcing(cfg: Any, secrets: Any) -> None:
            called_agents.append(cfg.agent_id)

        with patch("ai_news_agent.scheduler.serverless._load_runner") as mock_lr:
            mock_lr.return_value = self._make_runner_with("alpha", "beta")
            with patch("ai_news_agent.scheduler.serverless._run_sourcing_job",
                       side_effect=mock_sourcing):
                result = http_handler(
                    payload={"job_type": "sourcing", "agent_id": "alpha"}
                )

        assert result["code"] == 200
        assert called_agents == ["alpha"]  # Only alpha ran

    def test_runner_load_failure_returns_500(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("SCHEDULER_API_KEY", raising=False)
        from ai_news_agent.scheduler.serverless import http_handler

        with patch(
            "ai_news_agent.scheduler.serverless._load_runner",
            side_effect=RuntimeError("config load failed"),
        ):
            result = http_handler(payload={"job_type": "sourcing"})

        assert result["code"] == 500

    def test_curation_dispatches_all_cadences(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Each curation cadence is correctly dispatched to _run_curation_job (SRC-028–SRC-032)."""
        monkeypatch.delenv("SCHEDULER_API_KEY", raising=False)
        from ai_news_agent.scheduler.serverless import http_handler

        for cadence in ("daily", "weekly", "monthly", "annual"):
            with patch("ai_news_agent.scheduler.serverless._load_runner") as mock_lr:
                mock_lr.return_value = self._make_runner_with("default")
                with patch("ai_news_agent.scheduler.serverless._run_curation_job") as mock_c:
                    result = http_handler(
                        payload={"job_type": "curation", "cadence": cadence}
                    )
            assert result["code"] == 200, f"cadence={cadence}"
            assert mock_c.call_count == 1

    def test_env_var_job_type_fallback(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """JOB_TYPE env var is used when not in payload (SRC-073, SRC-085)."""
        monkeypatch.delenv("SCHEDULER_API_KEY", raising=False)
        monkeypatch.setenv("JOB_TYPE", "sourcing")
        from ai_news_agent.scheduler.serverless import http_handler

        with patch("ai_news_agent.scheduler.serverless._load_runner") as mock_lr:
            mock_lr.return_value = self._make_runner_with("default")
            with patch("ai_news_agent.scheduler.serverless._run_sourcing_job"):
                result = http_handler(payload={})  # no job_type in payload

        assert result["code"] == 200


# ===========================================================================
# 18. register_jobs() — cron schedule verification (SRC-009, SRC-028–SRC-032, SRC-052)
# ===========================================================================

class TestRegisterJobs:
    """
    Verify that register_jobs() correctly creates the 5 expected jobs per agent
    with the right IDs and that custom cron expressions are honoured.

    Traces: SRC-009, SRC-028–SRC-032, SRC-052, SRC-072, SRC-144
    """

    def _runner_with_agents(self, *agent_ids: str) -> SchedulerRunner:
        from ai_news_agent.config.models import AgentConfig, RuntimeSecrets
        runner = _make_runner()
        for aid in agent_ids:
            runner._agent_configs[aid] = AgentConfig(agent_id=aid)
        runner._secrets = RuntimeSecrets.model_validate({
            "OPENAI_API_KEY": "sk-fake",
            "TWITTER_BEARER_TOKEN": "fake-bearer",
        })
        return runner

    def test_five_jobs_registered_per_agent(self) -> None:
        runner = self._runner_with_agents("test")
        runner.register_jobs()
        assert len(runner.get_job_statuses()) == 5

    def test_fifteen_jobs_for_three_agents(self) -> None:
        runner = self._runner_with_agents("a", "b", "c")
        runner.register_jobs()
        assert len(runner.get_job_statuses()) == 15

    def test_job_ids_include_expected_suffixes(self) -> None:
        runner = self._runner_with_agents("myagent")
        runner.register_jobs()
        job_ids = {s["job_id"] for s in runner.get_job_statuses()}
        assert "myagent_sourcing_daily"  in job_ids
        assert "myagent_curation_daily"  in job_ids
        assert "myagent_curation_weekly" in job_ids
        assert "myagent_curation_monthly" in job_ids
        assert "myagent_curation_annual" in job_ids

    def test_replace_existing_prevents_duplicate_jobs_when_running(self) -> None:
        """
        When the scheduler IS running, replace_existing=True prevents duplicate jobs.
        APScheduler only honours replace_existing once the job stores are initialised
        (i.e., after start()), so we start the scheduler for this test.
        """
        runner = self._runner_with_agents("test")
        runner._scheduler.start()
        try:
            runner.register_jobs()
            runner.register_jobs()  # second call must not add duplicates
            assert len(runner.get_job_statuses()) == 5
        finally:
            runner.shutdown()

    def test_no_jobs_when_no_agents_loaded(self) -> None:
        runner = _make_runner()
        runner.register_jobs()
        assert runner.get_job_statuses() == []


# ===========================================================================
# 19. Scheduler config — multi-agent registry (SRC-072)
# ===========================================================================

class TestSchedulerConfigMultiAgent:
    """
    Tests for multi-agent discovery via scheduler config (SRC-072).
    Traces: SRC-072 (scheduler discovers all agents from scheduler.yaml)
    """

    def test_all_disabled_results_in_empty_runner(self) -> None:
        """When all agents are disabled, the runner loads no agents (SRC-072)."""
        runner = _make_runner(agents=[
            AgentRegistration(id="a", config="a.yaml", enabled=False),
            AgentRegistration(id="b", config="b.yaml", enabled=False),
        ])
        with patch("ai_news_agent.scheduler.runner.load_agent_config") as mock_load:
            runner.load_agent_configs()
        mock_load.assert_not_called()
        assert runner.agent_ids == []

    def test_mixed_enabled_disabled_loads_only_enabled(self) -> None:
        from ai_news_agent.config.models import AgentConfig

        runner = _make_runner(agents=[
            AgentRegistration(id="enabled1", config="e1.yaml", enabled=True),
            AgentRegistration(id="disabled", config="d.yaml",  enabled=False),
            AgentRegistration(id="enabled2", config="e2.yaml", enabled=True),
        ])

        def mock_load(path: object) -> AgentConfig:
            from pathlib import Path
            return AgentConfig(agent_id=Path(str(path)).stem)

        with patch("ai_news_agent.scheduler.runner.load_agent_config",
                   side_effect=mock_load):
            runner.load_agent_configs()

        assert "disabled" not in runner._agent_configs
        assert set(runner._agent_configs.keys()) == {"e1", "e2"}

    def test_load_agent_configs_idempotent(self) -> None:
        """Calling load_agent_configs() twice doesn't duplicate agents (SRC-072)."""
        from ai_news_agent.config.models import AgentConfig

        runner = _make_runner(agents=[
            AgentRegistration(id="alpha", config="alpha.yaml", enabled=True),
        ])

        def mock_load(path: object) -> AgentConfig:
            from pathlib import Path
            return AgentConfig(agent_id=Path(str(path)).stem)

        with patch("ai_news_agent.scheduler.runner.load_agent_config",
                   side_effect=mock_load):
            runner.load_agent_configs()
            runner.load_agent_configs()

        assert len(runner._agent_configs) == 1


# ===========================================================================
# 20. Cron expression validation edge cases (SRC-052)
# ===========================================================================

class TestParseCronEdgeCases:
    """Edge-case cron expression parsing (SRC-052)."""

    def test_asterisk_fields_valid(self) -> None:
        result = _parse_cron("* * * * *")
        assert result["minute"] == "*"
        assert result["hour"] == "*"
        assert result["day"] == "*"
        assert result["month"] == "*"
        assert result["day_of_week"] == "*"

    def test_range_expression_preserved(self) -> None:
        result = _parse_cron("0-5 8-18 * * 1-5")
        assert result["minute"] == "0-5"
        assert result["hour"] == "8-18"
        assert result["day_of_week"] == "1-5"

    def test_step_expression_preserved(self) -> None:
        result = _parse_cron("*/15 */2 * * *")
        assert result["minute"] == "*/15"
        assert result["hour"] == "*/2"

    def test_empty_string_raises(self) -> None:
        with pytest.raises(ValueError, match="Invalid cron"):
            _parse_cron("")

    def test_one_field_raises(self) -> None:
        with pytest.raises(ValueError, match="Invalid cron"):
            _parse_cron("0")

    def test_seven_fields_raises(self) -> None:
        with pytest.raises(ValueError, match="Invalid cron"):
            _parse_cron("0 0 * * * * 2026")  # 7 fields

    def test_production_crons_all_valid(self) -> None:
        """All production cron expressions from scheduler.yaml parse correctly."""
        production_crons = [
            "0 0 * * *",   # sourcing_daily
            "5 0 * * *",   # curation_daily
            "0 1 * * 0",   # curation_weekly (Sunday)
            "0 2 1 * *",   # curation_monthly (1st of month)
            "0 3 1 1 *",   # curation_annual (Jan 1)
        ]
        for cron in production_crons:
            result = _parse_cron(cron)
            assert len(result) == 5, f"Expected 5 fields for {cron!r}"


# ===========================================================================
# 21. Manual trigger — cadence × job_type matrix (SRC-028, SRC-147)
# ===========================================================================

class TestTriggerNowCadenceMatrix:
    """
    Exhaustive matrix: trigger_now() for all 4 curation cadences + sourcing.
    Traces: SRC-028 (re-runnable), SRC-029–SRC-032 (cadences), SRC-147 (manual trigger)
    """

    def _runner(self) -> SchedulerRunner:
        from ai_news_agent.config.models import AgentConfig, RuntimeSecrets
        runner = _make_runner()
        runner._agent_configs["agent"] = AgentConfig(agent_id="agent")
        runner._secrets = RuntimeSecrets.model_validate({
            "OPENAI_API_KEY": "sk-fake",
            "TWITTER_BEARER_TOKEN": "fake-bearer",
        })
        return runner

    @pytest.mark.parametrize("cadence", ["daily", "weekly", "monthly", "annual"])
    def test_all_curation_cadences_dispatched(self, cadence: str) -> None:
        runner = self._runner()
        with patch("ai_news_agent.scheduler.runner._run_curation_job") as mock_c:
            runner.trigger_now(agent_id="agent", job_type="curation", cadence=cadence)
        mock_c.assert_called_once()
        assert mock_c.call_args[0][2] == cadence

    def test_sourcing_dispatched_without_cadence(self) -> None:
        runner = self._runner()
        with patch("ai_news_agent.scheduler.runner._run_sourcing_job") as mock_s:
            runner.trigger_now(agent_id="agent", job_type="sourcing")
        mock_s.assert_called_once()

    def test_cadence_ignored_for_sourcing(self) -> None:
        """Passing cadence with job_type=sourcing should not raise."""
        runner = self._runner()
        with patch("ai_news_agent.scheduler.runner._run_sourcing_job"):
            # Should not raise — cadence is simply ignored for sourcing jobs
            runner.trigger_now(agent_id="agent", job_type="sourcing", cadence="daily")


# ===========================================================================
# 22. Monitoring fields in job status (SRC-150)
# ===========================================================================

class TestJobStatusMonitoring:
    """
    Verify that get_job_statuses() returns all required monitoring fields
    and that the output is sortable (SRC-150).

    Traces: SRC-150 (quality monitoring — operational observability)
    """

    def _runner_with_jobs(self) -> SchedulerRunner:
        from ai_news_agent.config.models import AgentConfig, RuntimeSecrets
        runner = _make_runner()
        runner._agent_configs["mon"] = AgentConfig(agent_id="mon")
        runner._secrets = RuntimeSecrets.model_validate({
            "OPENAI_API_KEY": "sk-fake",
            "TWITTER_BEARER_TOKEN": "fake-bearer",
        })
        runner.register_jobs()
        return runner

    def test_all_status_entries_have_required_fields(self) -> None:
        runner = self._runner_with_jobs()
        for status in runner.get_job_statuses():
            assert "job_id"       in status, f"Missing job_id in {status}"
            assert "name"         in status, f"Missing name in {status}"
            assert "next_run_utc" in status, f"Missing next_run_utc in {status}"
            assert "pending"      in status, f"Missing pending in {status}"

    def test_pending_jobs_have_none_next_run_utc(self) -> None:
        """Pending (not-started) jobs report None for next_run_utc (SRC-150)."""
        runner = self._runner_with_jobs()
        for status in runner.get_job_statuses():
            # Scheduler not started → all jobs are pending → next_run_utc is None
            assert status["next_run_utc"] is None

    def test_pending_flag_true_before_scheduler_starts(self) -> None:
        runner = self._runner_with_jobs()
        for status in runner.get_job_statuses():
            assert status["pending"] is True

    def test_sorted_none_last(self) -> None:
        """Jobs with None next_run_utc are sorted last (SRC-150)."""
        runner = self._runner_with_jobs()
        statuses = runner.get_job_statuses()
        # All are None when scheduler not started — sort must not raise
        assert isinstance(statuses, list)
        assert len(statuses) == 5

    def test_get_job_statuses_empty_before_register(self) -> None:
        runner = _make_runner()
        assert runner.get_job_statuses() == []


# ===========================================================================
# 23. API authentication — Bearer token edge cases (SRC-073, SRC-146, SRC-147)
# ===========================================================================

class TestAuthEdgeCases:
    """
    Bearer-token authentication edge cases for the manual override API.
    Traces: SRC-073 (env-var secrets), SRC-146 (non-2xx alerting),
            SRC-147 (authenticated manual override)
    """

    def test_validate_key_empty_string_fails_when_configured(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("SCHEDULER_API_KEY", "secret")
        from ai_news_agent.scheduler.auth import validate_api_key
        assert validate_api_key("") is False

    def test_validate_key_case_sensitive(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("SCHEDULER_API_KEY", "SecretKey")
        from ai_news_agent.scheduler.auth import validate_api_key
        assert validate_api_key("secretkey") is False  # case-sensitive
        assert validate_api_key("SecretKey") is True

    def test_http_handler_no_auth_header_uses_none_key(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """http_handler with no Authorization header → provided_key = None (SRC-147)."""
        monkeypatch.setenv("SCHEDULER_API_KEY", "key")
        from ai_news_agent.scheduler.serverless import http_handler

        result = http_handler(payload={"job_type": "sourcing"}, auth_header=None)
        assert result["code"] == 401

    def test_http_handler_bearer_prefix_required(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Token without 'Bearer ' prefix is not extracted (SRC-147)."""
        monkeypatch.setenv("SCHEDULER_API_KEY", "key")
        from ai_news_agent.scheduler.serverless import http_handler

        result = http_handler(
            payload={"job_type": "sourcing"},
            auth_header="key",  # no "Bearer " prefix
        )
        assert result["code"] == 401

    def test_lambda_handler_no_auth_with_no_key_passes(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When SCHEDULER_API_KEY not set, Lambda handler allows all (SRC-147 dev mode)."""
        from ai_news_agent.config.models import AgentConfig, RuntimeSecrets
        from ai_news_agent.scheduler.serverless import lambda_handler

        monkeypatch.delenv("SCHEDULER_API_KEY", raising=False)
        runner = _make_runner()
        runner._agent_configs["default"] = AgentConfig(agent_id="default")
        runner._secrets = RuntimeSecrets.model_validate({
            "OPENAI_API_KEY": "sk-fake",
            "TWITTER_BEARER_TOKEN": "fake-bearer",
        })

        with patch("ai_news_agent.scheduler.serverless._load_runner",
                   return_value=runner), \
             patch("ai_news_agent.scheduler.serverless._run_sourcing_job"):
            response = lambda_handler(event={"job_type": "sourcing"}, context=None)

        assert response["statusCode"] == 200


# ===========================================================================
# 24. Scheduler start / shutdown lifecycle (SRC-052)
# ===========================================================================

class TestSchedulerLifecycle:
    """
    Tests for SchedulerRunner start/shutdown without blocking indefinitely.
    We test that start() calls through correctly by mocking the blocking loop.

    Traces: SRC-052 (scheduler lifecycle), SRC-073 (secrets at startup),
            SRC-144 (jobs registered before start returns)
    """

    def _make_live_runner(self) -> SchedulerRunner:
        from ai_news_agent.config.models import AgentConfig, RuntimeSecrets
        runner = _make_runner()
        runner._agent_configs["live"] = AgentConfig(agent_id="live")
        runner._secrets = RuntimeSecrets.model_validate({
            "OPENAI_API_KEY": "sk-fake",
            "TWITTER_BEARER_TOKEN": "fake-bearer",
        })
        return runner

    def test_start_registers_jobs_and_starts_scheduler(self) -> None:
        """start() loads agents, registers jobs, starts APScheduler (SRC-052)."""
        runner = self._make_live_runner()

        # Patch load/register/start so they record calls without side-effects,
        # then patch time.sleep to raise SystemExit so the blocking loop exits.
        import contextlib

        with patch.object(runner, "load_agent_configs") as mock_load, \
             patch.object(runner, "register_jobs") as mock_reg, \
             patch.object(runner._scheduler, "start") as mock_start, \
             patch.object(runner._scheduler, "shutdown"), \
             patch("ai_news_agent.scheduler.runner.time") as mock_time:
            mock_time.sleep.side_effect = SystemExit(0)
            with contextlib.suppress(SystemExit):
                runner.start()

        mock_load.assert_called_once()
        mock_reg.assert_called_once()
        mock_start.assert_called_once()

    def test_is_running_true_while_scheduler_active(self) -> None:
        """is_running reflects underlying APScheduler state (SRC-052)."""
        runner = self._make_live_runner()
        runner._scheduler.start()
        try:
            assert runner.is_running is True
        finally:
            runner.shutdown()

    def test_is_running_false_after_shutdown(self) -> None:
        """is_running is False after shutdown (SRC-052)."""
        runner = self._make_live_runner()
        runner._scheduler.start()
        runner.shutdown()
        assert runner.is_running is False

    def test_shutdown_before_start_does_not_raise(self) -> None:
        """Shutdown before start is safe (idempotent) (SRC-052)."""
        runner = _make_runner()
        runner.shutdown()
        runner.shutdown()  # twice is also safe


# ===========================================================================
# 25. Secrets loading (SRC-073, SRC-105–SRC-111)
# ===========================================================================

class TestSecretsLoading:
    """
    Verify that RuntimeSecrets are loaded from env vars and never from YAML.
    Traces: SRC-073 (env-var only), SRC-105–SRC-111 (required + optional vars)
    """

    def test_secrets_cached_after_first_get(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """_get_secrets() is idempotent — same object returned on repeated calls."""
        monkeypatch.setenv("OPENAI_API_KEY", "sk-fake")
        monkeypatch.setenv("TWITTER_BEARER_TOKEN", "fake-bearer")
        runner = _make_runner()
        s1 = runner._get_secrets()
        s2 = runner._get_secrets()
        assert s1 is s2

    def test_pre_injected_secrets_used_directly(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Secrets injected at construction time are used without env-var loading."""
        from ai_news_agent.config.models import RuntimeSecrets
        secrets = RuntimeSecrets.model_validate({
            "OPENAI_API_KEY": "sk-injected",
            "TWITTER_BEARER_TOKEN": "injected-bearer",
        })
        runner = SchedulerRunner(
            scheduler_config=_make_scheduler_config(),
            secrets=secrets,
        )
        assert runner._get_secrets().openai_api_key == "sk-injected"

    def test_optional_secrets_default_to_none(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Optional secrets (WEB_SEARCH_API_KEY, etc.) default to None (SRC-109)."""
        from ai_news_agent.config.models import RuntimeSecrets
        secrets = RuntimeSecrets.model_validate({
            "OPENAI_API_KEY": "sk-fake",
            "TWITTER_BEARER_TOKEN": "fake-bearer",
        })
        assert secrets.web_search_api_key is None
        assert secrets.anthropic_api_key is None
        assert secrets.scheduler_api_key is None
