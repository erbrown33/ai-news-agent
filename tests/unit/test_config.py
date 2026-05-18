"""
tests/unit/test_config.py — Config loading, schema validation, secret injection.
Traces: SRC-071–SRC-073 (config loading), SRC-098 (unit tests),
        SRC-105–SRC-111 (secrets from env vars only)
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from ai_news_agent.config.loader import ConfigError, load_agent_config, load_scheduler_config
from ai_news_agent.config.models import (
    AgentConfig,
    LLMConfig,
    RuntimeSecrets,
    SchedulerConfig,
    SourcesConfig,
    TwitterConfig,
    TwitterHandleConfig,
)

if TYPE_CHECKING:
    from pathlib import Path

# ---------------------------------------------------------------------------
# AgentConfig schema tests
# ---------------------------------------------------------------------------

class TestAgentConfigModel:
    """Traces: SRC-071–SRC-073 (config model validation)."""

    def test_defaults_are_populated(self) -> None:
        """AgentConfig defaults match the spec defaults."""
        config = AgentConfig(agent_id="test")
        assert config.llm.provider == "openai"
        assert config.llm.model == "gpt-4o"
        assert config.limits.daily_top_n == 10
        assert config.limits.annual_top_n == 10
        assert config.twitter.enabled is True

    def test_cadence_overrides_supported(self) -> None:
        """Monthly/annual can specify different models (SRC-054)."""
        config = AgentConfig(
            agent_id="test",
            llm=LLMConfig(
                provider="openai",
                model="gpt-4o",
                cadence_overrides={
                    "monthly": {"model": "o3", "thinking": False},
                    "annual": {"model": "o3", "thinking": True},
                },
            ),
        )
        assert config.llm.cadence_overrides["annual"].thinking is True
        assert config.llm.cadence_overrides["monthly"].model == "o3"

    def test_output_dir_placeholder_resolved_at_construction(self) -> None:
        """
        {agent_id} placeholder is resolved at model construction time (SRC-145).

        The model_validator fires immediately so callers always receive the
        fully-resolved path — there is never a window where the raw template
        string escapes into the rest of the system.
        """
        config = AgentConfig(agent_id="myagent", output_dir="outputs/{agent_id}")
        # Placeholder must be gone — resolved to the actual agent_id value
        assert "{agent_id}" not in config.output_dir
        assert config.output_dir == "outputs/myagent"

    def test_twitter_handles_configurable(self) -> None:
        """Handles can be added/removed/weighted without code changes (SRC-046)."""
        handles = [
            TwitterHandleConfig(handle="karpathy", weight=2.0),
            TwitterHandleConfig(handle="sama", weight=0.5),
        ]
        config = AgentConfig(agent_id="test", twitter=TwitterConfig(handles=handles))
        assert len(config.twitter.handles) == 2
        assert config.twitter.handles[0].weight == 2.0

    def test_sources_all_tiers(self) -> None:
        """All four source tiers configurable (SRC-017–SRC-021)."""
        sources = SourcesConfig(
            custom=["custom.example.com"],
            tier_1b=["reuters.com"],
            tier_2=["openai.com"],
            tier_3=["techcrunch.com"],
            tier_4=["brookings.edu"],
        )
        config = AgentConfig(agent_id="test", sources=sources)
        assert "custom.example.com" in config.sources.custom
        assert "reuters.com" in config.sources.tier_1b
        assert "brookings.edu" in config.sources.tier_4


class TestAgentConfigLoader:
    """Traces: SRC-071 (YAML loading), SRC-073 (secrets not in YAML)."""

    def test_load_default_agent_yaml(self) -> None:
        """Load the real default-agent.yaml from configs/."""
        config = load_agent_config("configs/default-agent.yaml")
        assert config.agent_id == "default"
        assert config.llm.provider == "openai"
        assert len(config.twitter.handles) == 9   # SRC-037–SRC-045: 9 default influencers
        assert "reuters.com" in config.sources.tier_1b

    def test_output_dir_resolved(self) -> None:
        """Loader resolves {agent_id} in output_dir (SRC-145)."""
        config = load_agent_config("configs/default-agent.yaml")
        assert "{agent_id}" not in config.output_dir
        assert "default" in config.output_dir

    def test_missing_file_raises_config_error(self) -> None:
        with pytest.raises(ConfigError, match="not found"):
            load_agent_config("configs/nonexistent.yaml")

    def test_invalid_yaml_raises_config_error(self, tmp_path: Path) -> None:
        bad_yaml = tmp_path / "bad.yaml"
        bad_yaml.write_text("agent_id: [invalid structure for agent_id]")
        with pytest.raises(ConfigError):
            load_agent_config(bad_yaml)

    def test_secrets_not_present_in_config(self) -> None:
        """YAML config must NOT contain any secret values (SRC-073).
        Comments may document env var names — but actual key values must not appear."""
        with open("configs/default-agent.yaml") as f:
            raw = f.read()
        # Real secret values (like actual API keys starting with "sk-") must not be present
        assert "sk-" not in raw
        # The word "OPENAI_API_KEY" may appear as a comment reference, but
        # no actual key value should be assigned in the YAML body
        lines = raw.splitlines()
        for line in lines:
            stripped = line.strip()
            # Comments (starting with #) are OK to mention env var names
            if stripped.startswith("#"):
                continue
            # Non-comment lines must not assign secret values
            assert "sk-" not in stripped, f"Secret value found in YAML line: {line}"


class TestSchedulerConfigLoader:
    """Traces: SRC-052 (scheduler config), SRC-072 (multi-agent registry), SRC-144 (retry)."""

    def test_load_scheduler_yaml(self) -> None:
        cfg = load_scheduler_config("configs/scheduler.yaml")
        assert isinstance(cfg, SchedulerConfig)
        assert cfg.scheduler.max_retries == 3
        assert cfg.scheduler.retry_backoff_base_seconds == 30

    def test_agent_registry_contains_default(self) -> None:
        cfg = load_scheduler_config("configs/scheduler.yaml")
        ids = [a.id for a in cfg.agents]
        assert "default" in ids

    def test_default_agent_enabled(self) -> None:
        cfg = load_scheduler_config("configs/scheduler.yaml")
        default = next(a for a in cfg.agents if a.id == "default")
        assert default.enabled is True

    def test_technical_agent_disabled(self) -> None:
        """Example technical agent is disabled by default."""
        cfg = load_scheduler_config("configs/scheduler.yaml")
        technical = next((a for a in cfg.agents if a.id == "technical"), None)
        if technical:
            assert technical.enabled is False


class TestRuntimeSecrets:
    """Traces: SRC-073 (secrets from env vars), SRC-107–SRC-111."""

    def test_secrets_from_env_vars(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("OPENAI_API_KEY", "sk-test-key")
        monkeypatch.setenv("TWITTER_BEARER_TOKEN", "test-bearer")
        secrets = RuntimeSecrets()  # type: ignore[call-arg]
        assert secrets.openai_api_key == "sk-test-key"
        assert secrets.twitter_bearer_token == "test-bearer"

    def test_optional_secrets_default_to_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
        monkeypatch.setenv("TWITTER_BEARER_TOKEN", "bearer-test")
        monkeypatch.delenv("WEB_SEARCH_API_KEY", raising=False)
        secrets = RuntimeSecrets()  # type: ignore[call-arg]
        assert secrets.web_search_api_key is None
