"""
tests/unit/test_config_system.py — Comprehensive per-agent YAML configuration system tests.

Covers the full configuration contract specified in docs/requirements/requirements.md §3.5
and implemented across:
  - src/ai_news_agent/config/models.py
  - src/ai_news_agent/config/loader.py
  - src/ai_news_agent/config/schema.py

Requirement traceability:
  SRC-017–SRC-021  source tier configuration
  SRC-029–SRC-032  cadence top-N limits
  SRC-036–SRC-046  Twitter influencer handles and weights
  SRC-047          Twitter = signal, not primary news
  SRC-054          per-cadence research LLM overrides (monthly/annual)
  SRC-055–SRC-061  provider-agnostic LLM layer
  SRC-071          config must fail loudly on errors with actionable messages
  SRC-072          multiple agent configs discoverable by scheduler (no code changes)
  SRC-073          secrets from env vars ONLY — never in YAML
  SRC-105–SRC-111  required + optional secret env vars
  SRC-113          curation_prompt path
  SRC-129          prompt SHA-256 version tracing
  SRC-144          retry config (scheduler)
  SRC-145          output_dir {agent_id} placeholder resolved at load time
  SRC-147          manual override API config
  SRC-148          Twitter graceful degradation (enabled flag)
"""

from __future__ import annotations

import json
import textwrap
from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import ValidationError

from ai_news_agent.config.loader import (
    ConfigError,
    discover_agents_from_scheduler,
    load_agent_config,
    load_all_enabled_agents,
    load_scheduler_config,
    validate_no_secrets_in_yaml,
)
from ai_news_agent.config.models import (
    AgentConfig,
    AgentRegistration,
    APIConfig,
    LimitsConfig,
    LLMCadenceOverride,
    LLMConfig,
    RetryConfig,
    RuntimeSecrets,
    SchedulerConfig,
    SourcesConfig,
    TriggersConfig,
    TwitterConfig,
    TwitterHandleConfig,
)
from ai_news_agent.config.schema import (
    SchemaValidationError,
    export_schemas,
    generate_agent_schema,
    generate_scheduler_schema,
    summarise_agent_config,
    validate_agent_yaml,
    validate_scheduler_yaml,
)

# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────


def _write_yaml(path: Path, data: dict[str, Any]) -> Path:
    """Write a YAML file and return the path."""
    path.write_text(yaml.dump(data, default_flow_style=False), encoding="utf-8")
    return path


def _minimal_agent_yaml(agent_id: str = "test") -> dict[str, Any]:
    """Minimal valid agent YAML dict — only agent_id required."""
    return {"agent_id": agent_id}


def _full_agent_yaml(agent_id: str = "full") -> dict[str, Any]:
    """A fully-populated agent YAML dict exercising every field."""
    return {
        "agent_id": agent_id,
        "llm": {
            "provider": "openai",
            "model": "gpt-4o",
            "cadence_overrides": {
                "monthly": {"model": "o3", "thinking": False},
                "annual": {"model": "o3", "thinking": True},
            },
        },
        "curation_prompt": "prompts/daily.md",
        "sources": {
            "custom": ["myblog.example.com"],
            "tier_1b": ["reuters.com", "bloomberg.com"],
            "tier_2": ["openai.com", "anthropic.com"],
            "tier_3": ["techcrunch.com", "theverge.com"],
            "tier_4": ["brookings.edu", "rand.org"],
        },
        "twitter": {
            "enabled": True,
            "handles": [
                {"handle": "karpathy", "weight": 1.5},
                {"handle": "sama", "weight": 1.0},
                {"handle": "ylecun", "weight": 2.0},
            ],
        },
        "limits": {
            "daily_top_n": 8,
            "weekly_top_n": 5,
            "monthly_top_n": 10,
            "annual_top_n": 10,
        },
        "output_dir": f"outputs/{agent_id}",
    }


# ─────────────────────────────────────────────────────────────────────────────
# LLMConfig — provider-agnostic layer  (SRC-055–SRC-061)
# ─────────────────────────────────────────────────────────────────────────────


class TestLLMConfig:
    """Traces: SRC-054–SRC-061 (provider-agnostic LLM layer)."""

    def test_default_provider_is_openai(self) -> None:
        """Default provider is OpenAI — no configuration required (SRC-057)."""
        cfg = LLMConfig()
        assert cfg.provider == "openai"
        assert cfg.model == "gpt-4o"

    def test_anthropic_provider_accepted(self) -> None:
        """Switching to Anthropic requires only a field value change (SRC-056)."""
        cfg = LLMConfig(provider="anthropic", model="claude-3-7-sonnet-20250219")
        assert cfg.provider == "anthropic"
        assert cfg.model == "claude-3-7-sonnet-20250219"

    def test_unsupported_provider_rejected(self) -> None:
        """Unknown providers are rejected by the Literal type constraint."""
        with pytest.raises(ValidationError):
            LLMConfig(provider="grok")  # type: ignore[arg-type]

    def test_cadence_override_monthly(self) -> None:
        """Monthly cadence can specify a more capable research model (SRC-054)."""
        cfg = LLMConfig(
            model="gpt-4o",
            cadence_overrides={"monthly": LLMCadenceOverride(model="o3", thinking=False)},
        )
        assert cfg.cadence_overrides["monthly"].model == "o3"
        assert cfg.cadence_overrides["monthly"].thinking is False

    def test_cadence_override_annual_with_thinking(self) -> None:
        """Annual cadence can enable extended thinking mode (SRC-032, SRC-054)."""
        cfg = LLMConfig(
            model="gpt-4o",
            cadence_overrides={"annual": LLMCadenceOverride(model="o3", thinking=True)},
        )
        assert cfg.cadence_overrides["annual"].thinking is True

    def test_all_four_cadences_accepted_as_override_keys(self) -> None:
        """All four cadence keys are valid override targets."""
        cfg = LLMConfig(
            cadence_overrides={
                "daily": LLMCadenceOverride(model="gpt-4o"),
                "weekly": LLMCadenceOverride(model="gpt-4o"),
                "monthly": LLMCadenceOverride(model="o3"),
                "annual": LLMCadenceOverride(model="o3", thinking=True),
            }
        )
        assert len(cfg.cadence_overrides) == 4

    def test_unknown_cadence_key_rejected(self) -> None:
        """Unknown cadence keys are rejected with a clear error (SRC-071)."""
        with pytest.raises(ValidationError, match="Unknown cadence key"):
            LLMConfig(cadence_overrides={"biweekly": {"model": "o3"}})

    def test_model_min_length_enforced(self) -> None:
        """Empty model string is rejected."""
        with pytest.raises(ValidationError):
            LLMConfig(model="")

    def test_cadence_override_model_min_length_enforced(self) -> None:
        """Empty model string in cadence override is rejected."""
        with pytest.raises(ValidationError):
            LLMCadenceOverride(model="")

    def test_empty_cadence_overrides_is_valid(self) -> None:
        """No overrides is the common default — no error."""
        cfg = LLMConfig()
        assert cfg.cadence_overrides == {}


# ─────────────────────────────────────────────────────────────────────────────
# TwitterHandleConfig + TwitterConfig  (SRC-036–SRC-048)
# ─────────────────────────────────────────────────────────────────────────────


class TestTwitterHandleConfig:
    """Traces: SRC-036–SRC-046 (configurable influencer list), SRC-047 (signal role)."""

    def test_valid_handle(self) -> None:
        h = TwitterHandleConfig(handle="karpathy")
        assert h.handle == "karpathy"
        assert h.weight == 1.0

    def test_custom_weight_accepted(self) -> None:
        """Handles can be re-weighted without code changes (SRC-046)."""
        h = TwitterHandleConfig(handle="ylecun", weight=2.0)
        assert h.weight == 2.0

    def test_weight_above_maximum_rejected(self) -> None:
        """Weight > 10.0 is rejected."""
        with pytest.raises(ValidationError):
            TwitterHandleConfig(handle="test", weight=11.0)

    def test_weight_zero_rejected(self) -> None:
        """Weight must be strictly positive."""
        with pytest.raises(ValidationError):
            TwitterHandleConfig(handle="test", weight=0.0)

    def test_weight_negative_rejected(self) -> None:
        with pytest.raises(ValidationError):
            TwitterHandleConfig(handle="test", weight=-1.0)

    def test_handle_with_at_prefix_rejected(self) -> None:
        """Handle must NOT include the @ prefix — pattern only allows [A-Za-z0-9_]."""
        with pytest.raises(ValidationError):
            TwitterHandleConfig(handle="@karpathy")

    def test_handle_empty_rejected(self) -> None:
        with pytest.raises(ValidationError):
            TwitterHandleConfig(handle="")

    def test_handle_with_spaces_rejected(self) -> None:
        with pytest.raises(ValidationError):
            TwitterHandleConfig(handle="bad handle")

    def test_all_default_9_handles_valid(self) -> None:
        """All 9 spec-mandated default handles are valid (SRC-037–SRC-045)."""
        default_handles = [
            "karpathy",
            "sama",
            "demishassabis",
            "DarioAmodei",
            "ylecun",
            "AndrewYNg",
            "fchollet",
            "drfeifei",
            "emilymbender",
        ]
        for handle in default_handles:
            h = TwitterHandleConfig(handle=handle)
            assert h.handle == handle


class TestTwitterConfig:
    """Traces: SRC-036–SRC-046, SRC-047, SRC-148."""

    def test_enabled_by_default(self) -> None:
        cfg = TwitterConfig()
        assert cfg.enabled is True

    def test_disabled_flag(self) -> None:
        """enabled=false triggers graceful degradation (SRC-148)."""
        cfg = TwitterConfig(enabled=False)
        assert cfg.enabled is False

    def test_empty_handles_valid(self) -> None:
        """Empty handle list is valid — no Twitter signal collected."""
        cfg = TwitterConfig(enabled=True, handles=[])
        assert cfg.handles == []

    def test_handles_list_populated(self) -> None:
        handles = [
            TwitterHandleConfig(handle="karpathy", weight=1.0),
            TwitterHandleConfig(handle="sama", weight=0.5),
        ]
        cfg = TwitterConfig(handles=handles)
        assert len(cfg.handles) == 2

    def test_handles_add_remove_without_code_changes(self) -> None:
        """Adding/removing/re-weighting handles requires only YAML edits (SRC-046)."""
        # Simulate "adding" by constructing a new list in config
        before = TwitterConfig(handles=[TwitterHandleConfig(handle="karpathy")])
        after = TwitterConfig(
            handles=[
                TwitterHandleConfig(handle="karpathy"),
                TwitterHandleConfig(handle="timnitgebru", weight=2.0),
            ]
        )
        assert len(after.handles) == len(before.handles) + 1
        assert after.handles[1].handle == "timnitgebru"
        assert after.handles[1].weight == 2.0


# ─────────────────────────────────────────────────────────────────────────────
# SourcesConfig  (SRC-016–SRC-021, SRC-034)
# ─────────────────────────────────────────────────────────────────────────────


class TestSourcesConfig:
    """Traces: SRC-016–SRC-021, SRC-034."""

    def test_all_tiers_empty_by_default(self) -> None:
        src = SourcesConfig()
        assert src.custom == []
        assert src.tier_1b == []
        assert src.tier_2 == []
        assert src.tier_3 == []
        assert src.tier_4 == []

    def test_custom_tier_1a(self) -> None:
        """User can add custom priority sources (SRC-017)."""
        src = SourcesConfig(custom=["myblog.example.com", "internal.corp.com"])
        assert "myblog.example.com" in src.custom

    def test_tier_1b_business_press(self) -> None:
        """Tier 1b covers popular business press (SRC-018)."""
        src = SourcesConfig(tier_1b=["reuters.com", "bloomberg.com", "wsj.com"])
        assert "reuters.com" in src.tier_1b

    def test_tier_2_ai_blogs(self) -> None:
        """Tier 2 covers top AI/tech company blogs (SRC-019)."""
        src = SourcesConfig(tier_2=["openai.com", "anthropic.com", "huggingface.co"])
        assert "openai.com" in src.tier_2

    def test_tier_3_tech_press(self) -> None:
        """Tier 3 covers tech business press (SRC-020)."""
        src = SourcesConfig(tier_3=["techcrunch.com", "theverge.com", "wired.com"])
        assert "techcrunch.com" in src.tier_3

    def test_tier_4_policy_research(self) -> None:
        """Tier 4 covers policy/research institutions (SRC-021)."""
        src = SourcesConfig(tier_4=["brookings.edu", "rand.org", "hai.stanford.edu"])
        assert "brookings.edu" in src.tier_4

    def test_all_tiers_simultaneously(self) -> None:
        src = SourcesConfig(
            custom=["custom.example.com"],
            tier_1b=["reuters.com"],
            tier_2=["openai.com"],
            tier_3=["techcrunch.com"],
            tier_4=["brookings.edu"],
        )
        assert len(src.custom) == 1
        assert len(src.tier_1b) == 1
        assert len(src.tier_2) == 1
        assert len(src.tier_3) == 1
        assert len(src.tier_4) == 1


# ─────────────────────────────────────────────────────────────────────────────
# LimitsConfig  (SRC-029–SRC-032)
# ─────────────────────────────────────────────────────────────────────────────


class TestLimitsConfig:
    """Traces: SRC-029–SRC-032 (top-N limits per cadence)."""

    def test_defaults(self) -> None:
        lim = LimitsConfig()
        assert lim.daily_top_n == 10
        assert lim.weekly_top_n == 7
        assert lim.monthly_top_n == 10
        assert lim.annual_top_n == 10

    def test_custom_limits(self) -> None:
        lim = LimitsConfig(daily_top_n=5, weekly_top_n=3, monthly_top_n=8, annual_top_n=10)
        assert lim.daily_top_n == 5

    def test_daily_minimum_enforced(self) -> None:
        """daily_top_n must be ≥ 1."""
        with pytest.raises(ValidationError):
            LimitsConfig(daily_top_n=0)

    def test_daily_maximum_enforced(self) -> None:
        """daily_top_n must be ≤ 50."""
        with pytest.raises(ValidationError):
            LimitsConfig(daily_top_n=51)

    def test_annual_maximum_20(self) -> None:
        """annual_top_n has a tighter cap of 20."""
        with pytest.raises(ValidationError):
            LimitsConfig(annual_top_n=21)

    def test_annual_minimum_enforced(self) -> None:
        with pytest.raises(ValidationError):
            LimitsConfig(annual_top_n=0)

    def test_all_limits_at_boundary_values(self) -> None:
        """Boundary values must be accepted."""
        lim = LimitsConfig(
            daily_top_n=1,
            weekly_top_n=50,
            monthly_top_n=50,
            annual_top_n=20,
        )
        assert lim.daily_top_n == 1
        assert lim.annual_top_n == 20


# ─────────────────────────────────────────────────────────────────────────────
# AgentConfig  (SRC-071–SRC-073)
# ─────────────────────────────────────────────────────────────────────────────


class TestAgentConfigModel:
    """Traces: SRC-071–SRC-073, SRC-113, SRC-129, SRC-145."""

    def test_minimal_config_only_agent_id_required(self) -> None:
        """A config with only agent_id is fully valid — all other fields have defaults."""
        config = AgentConfig(agent_id="minimal")
        assert config.agent_id == "minimal"
        assert config.llm.provider == "openai"
        assert config.llm.model == "gpt-4o"
        assert config.sources.custom == []
        assert config.twitter.enabled is True
        assert config.twitter.handles == []
        assert config.limits.daily_top_n == 10

    def test_agent_id_slug_pattern_accepted(self) -> None:
        """Valid slug patterns: letters, digits, hyphens, underscores."""
        for valid_id in ["default", "my-agent", "agent_2", "AI-News-Agent", "a1b2"]:
            config = AgentConfig(agent_id=valid_id)
            assert config.agent_id == valid_id

    def test_agent_id_with_space_rejected(self) -> None:
        with pytest.raises(ValidationError):
            AgentConfig(agent_id="bad id")

    def test_agent_id_with_slash_rejected(self) -> None:
        with pytest.raises(ValidationError):
            AgentConfig(agent_id="bad/id")

    def test_agent_id_empty_rejected(self) -> None:
        with pytest.raises(ValidationError):
            AgentConfig(agent_id="")

    def test_agent_id_max_length_64(self) -> None:
        long_id = "a" * 65
        with pytest.raises(ValidationError):
            AgentConfig(agent_id=long_id)

    def test_agent_id_exactly_64_chars_accepted(self) -> None:
        config = AgentConfig(agent_id="a" * 64)
        assert len(config.agent_id) == 64

    def test_output_dir_placeholder_resolved_immediately(self) -> None:
        """
        {agent_id} is resolved at model construction time (SRC-145).

        The model validator fires during __init__ so the raw template string
        never escapes into application code.
        """
        config = AgentConfig(agent_id="myagent", output_dir="outputs/{agent_id}")
        assert config.output_dir == "outputs/myagent"
        assert "{agent_id}" not in config.output_dir

    def test_output_dir_no_placeholder_passes_through(self) -> None:
        """Output dir without the placeholder is used as-is."""
        config = AgentConfig(agent_id="test", output_dir="/data/digests")
        assert config.output_dir == "/data/digests"

    def test_output_dir_default_uses_agent_id(self) -> None:
        """Default output_dir resolves {agent_id} to the agent's actual ID."""
        config = AgentConfig(agent_id="business")
        assert config.output_dir == "outputs/business"

    def test_curation_prompt_default(self) -> None:
        """Default curation prompt path (SRC-113)."""
        config = AgentConfig(agent_id="test")
        assert config.curation_prompt == "prompts/daily.md"

    def test_curation_prompt_custom_path(self) -> None:
        config = AgentConfig(agent_id="test", curation_prompt="prompts/custom-theme.md")
        assert config.curation_prompt == "prompts/custom-theme.md"

    def test_curation_prompt_empty_rejected(self) -> None:
        with pytest.raises(ValidationError):
            AgentConfig(agent_id="test", curation_prompt="")

    def test_full_config_round_trips_via_model_validate(self) -> None:
        """Full config dict can be parsed and serialised round-trip."""
        data = _full_agent_yaml("roundtrip")
        config = AgentConfig.model_validate(data)
        assert config.agent_id == "roundtrip"
        assert config.llm.cadence_overrides["annual"].thinking is True
        assert "myblog.example.com" in config.sources.custom
        assert config.twitter.handles[0].handle == "karpathy"
        assert config.twitter.handles[0].weight == 1.5
        assert config.limits.daily_top_n == 8

    def test_model_dump_is_json_serialisable(self) -> None:
        config = AgentConfig(agent_id="serial")
        dumped = config.model_dump()
        # Should not raise
        json.dumps(dumped)

    def test_multiple_independent_agents_dont_share_state(self) -> None:
        """Two AgentConfig instances are independent — no shared mutable state."""
        a = AgentConfig(agent_id="agent-a", sources=SourcesConfig(custom=["a.com"]))
        b = AgentConfig(agent_id="agent-b", sources=SourcesConfig(custom=["b.com"]))
        assert a.sources.custom == ["a.com"]
        assert b.sources.custom == ["b.com"]
        # Mutating one should not affect the other
        a.sources.custom.append("extra.com")
        assert "extra.com" not in b.sources.custom

    def test_anthropic_provider_in_full_config(self) -> None:
        """Switching provider to Anthropic requires only the provider field (SRC-056)."""
        config = AgentConfig(
            agent_id="anthropic-test",
            llm=LLMConfig(provider="anthropic", model="claude-3-7-sonnet-20250219"),
        )
        assert config.llm.provider == "anthropic"

    def test_twitter_disabled_graceful_degradation_flag(self) -> None:
        """twitter.enabled=false documents the graceful degradation path (SRC-148)."""
        config = AgentConfig(
            agent_id="no-twitter",
            twitter=TwitterConfig(enabled=False),
        )
        assert config.twitter.enabled is False


# ─────────────────────────────────────────────────────────────────────────────
# SecretScanner  (SRC-073)
# ─────────────────────────────────────────────────────────────────────────────


class TestSecretScanner:
    """Traces: SRC-073 (secrets from env vars ONLY — never in YAML)."""

    def test_clean_yaml_passes(self) -> None:
        """A YAML file with no secrets passes without error."""
        clean = textwrap.dedent("""\
            agent_id: default
            llm:
              provider: openai
              model: gpt-4o
        """)
        # Must not raise
        validate_no_secrets_in_yaml(clean, source_name="test.yaml")

    def test_openai_key_in_yaml_raises(self) -> None:
        """OpenAI sk- prefix in YAML body triggers ConfigError (SRC-073).

        The scanner must match both the classic ``sk-<token>`` format AND the
        newer ``sk-proj-<token>`` project-scoped format.
        """
        # Classic format: sk- followed by 20+ alphanumeric chars (no hyphens)
        bad_classic = textwrap.dedent("""\
            agent_id: bad
            api_key: sk-abc12345678901234567890
        """)
        with pytest.raises(ConfigError, match="secret"):
            validate_no_secrets_in_yaml(bad_classic, source_name="bad.yaml")

        # Modern project-scoped format: sk-proj-<alphanumeric-and-hyphens>
        bad_proj = textwrap.dedent("""\
            agent_id: bad
            api_key: sk-proj-abc123REALLYLONG456789012345678
        """)
        with pytest.raises(ConfigError, match="secret"):
            validate_no_secrets_in_yaml(bad_proj, source_name="bad-proj.yaml")

    def test_openai_key_in_comment_is_allowed(self) -> None:
        """A comment referencing the env var name is OK — not a secret value."""
        yaml_with_comment = textwrap.dedent("""\
            agent_id: default
            # Required env var: OPENAI_API_KEY (set sk-... value in env, never here)
            llm:
              provider: openai
        """)
        # Must not raise — comments are skipped by the scanner
        validate_no_secrets_in_yaml(yaml_with_comment, source_name="comment.yaml")

    def test_twitter_bearer_token_pattern_detected(self) -> None:
        """Pasting a Twitter bearer token raises ConfigError."""
        bad = textwrap.dedent("""\
            agent_id: bad
            bearer: AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAhexhex
        """)
        with pytest.raises(ConfigError, match="secret"):
            validate_no_secrets_in_yaml(bad, source_name="bad.yaml")

    def test_brave_search_key_pattern_detected(self) -> None:
        bad = textwrap.dedent("""\
            agent_id: bad
            search_key: BSCabc123456789012345678
        """)
        with pytest.raises(ConfigError, match="secret"):
            validate_no_secrets_in_yaml(bad, source_name="bad.yaml")

    def test_empty_yaml_passes(self) -> None:
        validate_no_secrets_in_yaml("", source_name="empty.yaml")

    def test_comment_only_yaml_passes(self) -> None:
        yaml_comments_only = textwrap.dedent("""\
            # This is a comment
            # OPENAI_API_KEY: sk-...  ← put this in .env, NOT here
        """)
        validate_no_secrets_in_yaml(yaml_comments_only, source_name="comments.yaml")


# ─────────────────────────────────────────────────────────────────────────────
# load_agent_config  (SRC-071–SRC-073, SRC-145)
# ─────────────────────────────────────────────────────────────────────────────


class TestLoadAgentConfig:
    """Traces: SRC-071 (fail loudly), SRC-073 (no secrets), SRC-145 (placeholder)."""

    def test_load_minimal_yaml(self, tmp_path: Path) -> None:
        """Minimal YAML with only agent_id loads successfully."""
        f = _write_yaml(tmp_path / "minimal.yaml", _minimal_agent_yaml("minimal-test"))
        config = load_agent_config(f)
        assert config.agent_id == "minimal-test"

    def test_load_full_yaml(self, tmp_path: Path) -> None:
        """Fully-populated YAML loads all fields correctly."""
        f = _write_yaml(tmp_path / "full.yaml", _full_agent_yaml("full-test"))
        config = load_agent_config(f)
        assert config.agent_id == "full-test"
        assert config.llm.provider == "openai"
        assert config.llm.cadence_overrides["annual"].thinking is True
        assert "myblog.example.com" in config.sources.custom
        assert config.twitter.handles[0].handle == "karpathy"

    def test_output_dir_placeholder_resolved(self, tmp_path: Path) -> None:
        """Loader resolves {agent_id} in output_dir at load time (SRC-145)."""
        data = {"agent_id": "myagent", "output_dir": "outputs/{agent_id}"}
        f = _write_yaml(tmp_path / "agent.yaml", data)
        config = load_agent_config(f)
        assert config.output_dir == "outputs/myagent"
        assert "{agent_id}" not in config.output_dir

    def test_missing_file_raises_config_error(self) -> None:
        with pytest.raises(ConfigError, match="not found"):
            load_agent_config("/nonexistent/path/agent.yaml")

    def test_malformed_yaml_raises_config_error(self, tmp_path: Path) -> None:
        """YAML parse errors surface as ConfigError (SRC-071)."""
        bad = tmp_path / "bad.yaml"
        bad.write_text("agent_id: [invalid: {malformed", encoding="utf-8")
        with pytest.raises(ConfigError):
            load_agent_config(bad)

    def test_schema_validation_error_raised(self, tmp_path: Path) -> None:
        """Invalid field values surface as ConfigError with context (SRC-071)."""
        data = {"agent_id": "test", "limits": {"daily_top_n": 999}}  # > 50
        f = _write_yaml(tmp_path / "invalid.yaml", data)
        with pytest.raises(ConfigError):
            load_agent_config(f)

    def test_secret_in_yaml_raises_config_error(self, tmp_path: Path) -> None:
        """A YAML with a secret value raises ConfigError at load time (SRC-073).

        Uses a classic sk-<token> (no hyphens in the token) which the scanner
        reliably matches regardless of OpenAI key format variations.
        """
        secret_yaml = tmp_path / "secret.yaml"
        # 24 alphanum chars after sk- — well above the 20-char minimum
        secret_yaml.write_text(
            "agent_id: test\napi_key: sk-abc12345678901234567890abcd\n",
            encoding="utf-8",
        )
        with pytest.raises(ConfigError, match="secret"):
            load_agent_config(secret_yaml)

    def test_missing_agent_id_raises_config_error(self, tmp_path: Path) -> None:
        """YAML missing the required agent_id field raises ConfigError."""
        data = {"llm": {"provider": "openai"}}  # no agent_id
        f = _write_yaml(tmp_path / "no-id.yaml", data)
        with pytest.raises(ConfigError):
            load_agent_config(f)

    def test_load_real_default_agent_yaml(self) -> None:
        """The shipped default-agent.yaml passes validation end-to-end."""
        config = load_agent_config("configs/default-agent.yaml")
        assert config.agent_id == "default"
        assert config.llm.provider == "openai"
        assert config.llm.model == "gpt-4o"
        # All 9 default handles present (SRC-037–SRC-045)
        handles = {h.handle for h in config.twitter.handles}
        for expected in [
            "karpathy",
            "sama",
            "demishassabis",
            "DarioAmodei",
            "ylecun",
            "AndrewYNg",
            "fchollet",
            "drfeifei",
            "emilymbender",
        ]:
            assert expected in handles, f"Missing default handle: @{expected}"
        # All required tier lists present (SRC-017–SRC-021)
        assert "reuters.com" in config.sources.tier_1b
        assert "openai.com" in config.sources.tier_2
        assert "techcrunch.com" in config.sources.tier_3
        assert "brookings.edu" in config.sources.tier_4
        # Output dir resolved
        assert "{agent_id}" not in config.output_dir

    def test_load_real_technical_agent_yaml(self) -> None:
        """The shipped technical example agent YAML passes validation."""
        config = load_agent_config("configs/example-technical-agent.yaml")
        assert config.agent_id == "technical"
        assert "arxiv.org" in config.sources.custom

    def test_load_real_policy_agent_yaml(self) -> None:
        """The shipped policy example agent YAML passes validation."""
        config = load_agent_config("configs/example-policy-agent.yaml")
        assert config.agent_id == "policy"
        assert "whitehouse.gov" in config.sources.custom


# ─────────────────────────────────────────────────────────────────────────────
# load_scheduler_config  (SRC-052, SRC-072, SRC-144, SRC-147)
# ─────────────────────────────────────────────────────────────────────────────


class TestLoadSchedulerConfig:
    """Traces: SRC-052, SRC-072, SRC-144, SRC-147."""

    def test_load_real_scheduler_yaml(self) -> None:
        cfg = load_scheduler_config("configs/scheduler.yaml")
        assert isinstance(cfg, SchedulerConfig)
        assert cfg.scheduler.max_retries == 3
        assert cfg.scheduler.retry_backoff_base_seconds == 30

    def test_retry_policy_present(self) -> None:
        cfg = load_scheduler_config("configs/scheduler.yaml")
        # SRC-144: 3 retries, 30s base → 30s, 60s, 120s
        assert cfg.scheduler.max_retries == 3
        assert cfg.scheduler.retry_backoff_base_seconds == 30

    def test_all_five_triggers_present(self) -> None:
        cfg = load_scheduler_config("configs/scheduler.yaml")
        assert cfg.triggers.sourcing_daily == "0 0 * * *"
        assert cfg.triggers.curation_daily == "5 0 * * *"
        assert cfg.triggers.curation_weekly == "0 1 * * 0"
        assert cfg.triggers.curation_monthly == "0 2 1 * *"
        assert cfg.triggers.curation_annual == "0 3 1 1 *"

    def test_api_config_present(self) -> None:
        """Manual override API is configured (SRC-147)."""
        cfg = load_scheduler_config("configs/scheduler.yaml")
        assert cfg.api.enabled is True
        assert cfg.api.port == 8081

    def test_agents_registry_populated(self) -> None:
        """Agents registry contains at least the default agent (SRC-072)."""
        cfg = load_scheduler_config("configs/scheduler.yaml")
        ids = [a.id for a in cfg.agents]
        assert "default" in ids

    def test_default_agent_enabled(self) -> None:
        cfg = load_scheduler_config("configs/scheduler.yaml")
        default = next(a for a in cfg.agents if a.id == "default")
        assert default.enabled is True

    def test_example_agents_disabled(self) -> None:
        """Technical and policy example agents are disabled by default (SRC-072)."""
        cfg = load_scheduler_config("configs/scheduler.yaml")
        for agent in cfg.agents:
            if agent.id in ("technical", "policy"):
                assert agent.enabled is False, (
                    f"Example agent '{agent.id}' should be disabled by default"
                )

    def test_missing_scheduler_yaml_raises_config_error(self) -> None:
        with pytest.raises(ConfigError, match="not found"):
            load_scheduler_config("/nonexistent/scheduler.yaml")

    def test_invalid_cron_expression_rejected(self, tmp_path: Path) -> None:
        """Invalid cron expression in triggers raises ConfigError (SRC-071)."""
        data = {
            "scheduler": {"max_retries": 3, "retry_backoff_base_seconds": 30},
            "triggers": {"sourcing_daily": "bad cron"},  # only 2 fields
        }
        f = tmp_path / "sched.yaml"
        f.write_text(yaml.dump(data), encoding="utf-8")
        with pytest.raises(ConfigError):
            load_scheduler_config(f)


# ─────────────────────────────────────────────────────────────────────────────
# load_all_enabled_agents  (SRC-072)
# ─────────────────────────────────────────────────────────────────────────────


class TestLoadAllEnabledAgents:
    """Traces: SRC-072 (multi-agent discovery; independent scheduling per agent)."""

    def _sched_cfg(self, agents: list[AgentRegistration]) -> SchedulerConfig:
        return SchedulerConfig(
            scheduler=RetryConfig(max_retries=3, retry_backoff_base_seconds=30),
            triggers=TriggersConfig(),
            agents=agents,
        )

    def test_only_enabled_agents_loaded(self, tmp_path: Path) -> None:
        """Disabled agents are not returned (SRC-072)."""
        good_yaml = _write_yaml(tmp_path / "good.yaml", _minimal_agent_yaml("good"))
        cfg = self._sched_cfg(
            [
                AgentRegistration(id="good", config=str(good_yaml), enabled=True),
                AgentRegistration(id="disabled", config="/nonexistent.yaml", enabled=False),
            ]
        )
        result = load_all_enabled_agents(cfg)
        assert "good" in result
        assert "disabled" not in result

    def test_bad_config_does_not_abort_others(self, tmp_path: Path) -> None:
        """One bad config file does not prevent others from loading (SRC-072)."""
        good_yaml = _write_yaml(tmp_path / "good.yaml", _minimal_agent_yaml("good"))
        cfg = self._sched_cfg(
            [
                AgentRegistration(id="good", config=str(good_yaml), enabled=True),
                AgentRegistration(id="bad", config="/nonexistent.yaml", enabled=True),
            ]
        )
        result = load_all_enabled_agents(cfg)
        assert "good" in result
        assert "bad" not in result

    def test_empty_registry_returns_empty_dict(self) -> None:
        cfg = self._sched_cfg([])
        result = load_all_enabled_agents(cfg)
        assert result == {}

    def test_multiple_agents_loaded_independently(self, tmp_path: Path) -> None:
        """Multiple enabled agents each get their own AgentConfig (SRC-072)."""
        yaml_a = _write_yaml(tmp_path / "a.yaml", _minimal_agent_yaml("agent-a"))
        yaml_b = _write_yaml(tmp_path / "b.yaml", _full_agent_yaml("agent-b"))
        cfg = self._sched_cfg(
            [
                AgentRegistration(id="agent-a", config=str(yaml_a), enabled=True),
                AgentRegistration(id="agent-b", config=str(yaml_b), enabled=True),
            ]
        )
        result = load_all_enabled_agents(cfg)
        assert set(result.keys()) == {"agent-a", "agent-b"}
        assert result["agent-a"].llm.model == "gpt-4o"  # default
        assert result["agent-b"].twitter.handles[0].handle == "karpathy"

    def test_id_mismatch_logs_warning_but_loads(self, tmp_path: Path) -> None:
        """
        When registry id != file agent_id, loading succeeds with a warning.
        The file's agent_id is used as the dict key (SRC-072).
        """
        yaml_path = _write_yaml(tmp_path / "mismatch.yaml", _minimal_agent_yaml("file-id"))
        cfg = self._sched_cfg(
            [
                AgentRegistration(id="registry-id", config=str(yaml_path), enabled=True),
            ]
        )
        result = load_all_enabled_agents(cfg)
        # File's agent_id wins
        assert "file-id" in result
        assert "registry-id" not in result


# ─────────────────────────────────────────────────────────────────────────────
# discover_agents_from_scheduler  (SRC-072)
# ─────────────────────────────────────────────────────────────────────────────


class TestDiscoverAgentsFromScheduler:
    """Traces: SRC-072 (scheduler discovers agents without code changes)."""

    def test_discovers_default_agent(self) -> None:
        """The real scheduler.yaml discovers the default agent."""
        result = discover_agents_from_scheduler("configs/scheduler.yaml")
        assert "default" in result
        assert result["default"].agent_id == "default"

    def test_missing_scheduler_raises(self) -> None:
        with pytest.raises(ConfigError):
            discover_agents_from_scheduler("/nonexistent/scheduler.yaml")


# ─────────────────────────────────────────────────────────────────────────────
# RetryConfig / TriggersConfig / SchedulerConfig  (SRC-144, SRC-052, SRC-147)
# ─────────────────────────────────────────────────────────────────────────────


class TestRetryConfig:
    """Traces: SRC-144 (exponential backoff, 3 retries)."""

    def test_defaults(self) -> None:
        retry = RetryConfig()
        assert retry.max_retries == 3
        assert retry.retry_backoff_base_seconds == 30

    def test_max_retries_zero_allowed(self) -> None:
        retry = RetryConfig(max_retries=0)
        assert retry.max_retries == 0

    def test_max_retries_max_10_enforced(self) -> None:
        with pytest.raises(ValidationError):
            RetryConfig(max_retries=11)

    def test_backoff_base_minimum_1(self) -> None:
        with pytest.raises(ValidationError):
            RetryConfig(retry_backoff_base_seconds=0)

    def test_backoff_base_maximum_300(self) -> None:
        with pytest.raises(ValidationError):
            RetryConfig(retry_backoff_base_seconds=301)


class TestTriggersConfig:
    """Traces: SRC-009, SRC-028–SRC-032, SRC-052."""

    def test_default_cron_expressions(self) -> None:
        t = TriggersConfig()
        assert t.sourcing_daily == "0 0 * * *"  # 00:00 UTC daily  (SRC-009)
        assert t.curation_daily == "5 0 * * *"  # 00:05 UTC daily
        assert t.curation_weekly == "0 1 * * 0"  # 01:00 UTC Sunday
        assert t.curation_monthly == "0 2 1 * *"  # 02:00 UTC 1st of month
        assert t.curation_annual == "0 3 1 1 *"  # 03:00 UTC January 1st

    def test_invalid_cron_wrong_field_count(self) -> None:
        with pytest.raises(ValidationError, match="5 fields"):
            TriggersConfig(sourcing_daily="0 0 *")  # 3 fields, not 5

    def test_custom_cron_accepted(self) -> None:
        t = TriggersConfig(curation_weekly="30 2 * * 1")  # Monday 02:30
        assert t.curation_weekly == "30 2 * * 1"


class TestAPIConfig:
    """Traces: SRC-147 (manual override API)."""

    def test_defaults(self) -> None:
        api = APIConfig()
        assert api.enabled is True
        assert api.host == "0.0.0.0"
        assert api.port == 8081

    def test_port_boundaries(self) -> None:
        APIConfig(port=1)
        APIConfig(port=65535)
        with pytest.raises(ValidationError):
            APIConfig(port=0)
        with pytest.raises(ValidationError):
            APIConfig(port=65536)


# ─────────────────────────────────────────────────────────────────────────────
# RuntimeSecrets  (SRC-073, SRC-105–SRC-111)
# ─────────────────────────────────────────────────────────────────────────────


class TestRuntimeSecrets:
    """Traces: SRC-073, SRC-105–SRC-111 (secrets from env vars ONLY)."""

    def test_required_secrets_loaded_from_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("OPENAI_API_KEY", "sk-test-openai")
        monkeypatch.setenv("TWITTER_BEARER_TOKEN", "test-bearer-token")
        secrets = RuntimeSecrets()  # type: ignore[call-arg]
        assert secrets.openai_api_key == "sk-test-openai"
        assert secrets.twitter_bearer_token == "test-bearer-token"

    def test_optional_secrets_default_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
        monkeypatch.setenv("TWITTER_BEARER_TOKEN", "bearer")
        monkeypatch.delenv("WEB_SEARCH_API_KEY", raising=False)
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.delenv("SCHEDULER_API_KEY", raising=False)
        monkeypatch.delenv("WEB_SEARCH_PROVIDER", raising=False)
        secrets = RuntimeSecrets()  # type: ignore[call-arg]
        assert secrets.web_search_api_key is None
        assert secrets.anthropic_api_key is None
        assert secrets.scheduler_api_key is None
        assert secrets.web_search_provider is None

    def test_all_optional_secrets_loadable(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
        monkeypatch.setenv("TWITTER_BEARER_TOKEN", "bearer")
        monkeypatch.setenv("WEB_SEARCH_API_KEY", "brave-key")
        monkeypatch.setenv("ANTHROPIC_API_KEY", "ant-key")
        monkeypatch.setenv("SCHEDULER_API_KEY", "sched-key")
        monkeypatch.setenv("WEB_SEARCH_PROVIDER", "brave")
        secrets = RuntimeSecrets()  # type: ignore[call-arg]
        assert secrets.web_search_api_key == "brave-key"
        assert secrets.anthropic_api_key == "ant-key"
        assert secrets.scheduler_api_key == "sched-key"
        assert secrets.web_search_provider == "brave"

    def test_runtime_secrets_are_not_in_agent_config(self) -> None:
        """AgentConfig has no secret fields — verified by inspecting model fields."""
        config = AgentConfig(agent_id="test")
        dumped = config.model_dump()
        secret_keywords = ["api_key", "bearer_token", "secret", "password", "token"]
        for key in dumped:
            assert not any(kw in key.lower() for kw in secret_keywords), (
                f"Possible secret field found in AgentConfig: '{key}'"
            )

    def test_real_config_files_contain_no_secrets(self) -> None:
        """All shipped YAML config files are free of secret values (SRC-073)."""
        config_files = list(Path("configs").glob("*.yaml"))
        assert config_files, "No YAML files found in configs/"
        for config_file in config_files:
            raw = config_file.read_text(encoding="utf-8")
            try:
                validate_no_secrets_in_yaml(raw, source_name=str(config_file))
            except ConfigError as exc:
                pytest.fail(f"Secret detected in {config_file}: {exc}")


# ─────────────────────────────────────────────────────────────────────────────
# JSON Schema generation and validation  (SRC-071, SRC-072)
# ─────────────────────────────────────────────────────────────────────────────


class TestSchemaGeneration:
    """Traces: SRC-071 (fail loudly with schema validation), SRC-072 (per-agent schemas)."""

    def test_agent_schema_has_required_agent_id(self) -> None:
        """agent_id must be required in the JSON Schema."""
        schema = generate_agent_schema()
        assert "agent_id" in schema.get("required", [])

    def test_agent_schema_has_dollar_schema(self) -> None:
        schema = generate_agent_schema()
        assert schema.get("$schema") == "http://json-schema.org/draft-07/schema#"

    def test_agent_schema_has_description(self) -> None:
        schema = generate_agent_schema()
        assert "description" in schema
        assert "SRC-" in schema["description"]  # traces present

    def test_agent_schema_has_comment_with_regen_instructions(self) -> None:
        schema = generate_agent_schema()
        assert "$comment" in schema
        assert "export-dir" in schema["$comment"]

    def test_scheduler_schema_has_required_structure(self) -> None:
        schema = generate_scheduler_schema()
        assert schema.get("$schema") == "http://json-schema.org/draft-07/schema#"
        assert "description" in schema

    def test_agent_schema_defines_llm_provider_enum(self) -> None:
        """Schema must enumerate valid LLM providers (SRC-056)."""
        schema = generate_agent_schema()
        defs = schema.get("$defs", {})
        llm_cfg = defs.get("LLMConfig", {})
        provider_prop = llm_cfg.get("properties", {}).get("provider", {})
        assert "openai" in provider_prop.get("enum", [])
        assert "anthropic" in provider_prop.get("enum", [])

    def test_export_schemas_writes_files(self, tmp_path: Path) -> None:
        """export_schemas writes both JSON schema files to the target directory."""
        export_schemas(tmp_path)
        agent_schema_path = tmp_path / "agent-config.schema.json"
        scheduler_schema_path = tmp_path / "scheduler.schema.json"
        assert agent_schema_path.exists()
        assert scheduler_schema_path.exists()
        # Both files must be valid JSON
        agent_schema = json.loads(agent_schema_path.read_text())
        sched_schema = json.loads(scheduler_schema_path.read_text())
        assert "agent_id" in agent_schema.get("required", [])
        assert isinstance(sched_schema, dict)

    def test_exported_schema_matches_in_memory_schema(self, tmp_path: Path) -> None:
        """Exported file content matches the in-memory generated schema."""
        export_schemas(tmp_path)
        from_file = json.loads((tmp_path / "agent-config.schema.json").read_text())
        in_memory = generate_agent_schema()
        # Key structure must match
        assert from_file.get("required") == in_memory.get("required")
        assert set(from_file.get("$defs", {}).keys()) == set(in_memory.get("$defs", {}).keys())


class TestValidateAgentYaml:
    """Traces: SRC-071 (validation with clear error messages)."""

    def test_valid_default_agent_passes(self) -> None:
        config = validate_agent_yaml("configs/default-agent.yaml")
        assert config.agent_id == "default"

    def test_valid_minimal_yaml_passes(self, tmp_path: Path) -> None:
        f = _write_yaml(tmp_path / "min.yaml", _minimal_agent_yaml("min"))
        config = validate_agent_yaml(f)
        assert config.agent_id == "min"

    def test_invalid_yaml_raises_schema_validation_error(self, tmp_path: Path) -> None:
        """Invalid config raises SchemaValidationError (not raw ValidationError)."""
        data = {"agent_id": "test", "limits": {"daily_top_n": -1}}
        f = _write_yaml(tmp_path / "invalid.yaml", data)
        with pytest.raises(SchemaValidationError) as exc_info:
            validate_agent_yaml(f)
        assert exc_info.value.errors  # must have at least one error message
        assert exc_info.value.source  # must report source file

    def test_missing_file_raises_schema_validation_error(self) -> None:
        with pytest.raises(SchemaValidationError, match="not found"):
            validate_agent_yaml("/nonexistent/file.yaml")

    def test_error_message_contains_field_location(self, tmp_path: Path) -> None:
        """Validation errors include the dotted field path for fast diagnosis (SRC-071)."""
        data = {"agent_id": "test", "limits": {"annual_top_n": 999}}
        f = _write_yaml(tmp_path / "bad-limit.yaml", data)
        with pytest.raises(SchemaValidationError) as exc_info:
            validate_agent_yaml(f)
        combined = "\n".join(exc_info.value.errors)
        # Error must mention the field (annual_top_n) or its path
        assert "annual_top_n" in combined or "annual" in combined.lower()


class TestValidateSchedulerYaml:
    def test_valid_scheduler_passes(self) -> None:
        cfg = validate_scheduler_yaml("configs/scheduler.yaml")
        assert isinstance(cfg, SchedulerConfig)

    def test_missing_file_raises_schema_validation_error(self) -> None:
        with pytest.raises(SchemaValidationError, match="not found"):
            validate_scheduler_yaml("/nonexistent/scheduler.yaml")


class TestSummariseAgentConfig:
    """Traces: SRC-072 (config visibility and discoverability)."""

    def test_summary_contains_agent_id(self) -> None:
        config = AgentConfig(agent_id="my-test-agent")
        summary = summarise_agent_config(config)
        assert "my-test-agent" in summary

    def test_summary_contains_provider_and_model(self) -> None:
        config = AgentConfig(agent_id="test", llm=LLMConfig(provider="openai", model="gpt-4o"))
        summary = summarise_agent_config(config)
        assert "openai" in summary
        assert "gpt-4o" in summary

    def test_summary_contains_twitter_status(self) -> None:
        config = AgentConfig(agent_id="test", twitter=TwitterConfig(enabled=False))
        summary = summarise_agent_config(config)
        assert "disabled" in summary

    def test_summary_contains_handle_count(self) -> None:
        config = AgentConfig(
            agent_id="test",
            twitter=TwitterConfig(
                handles=[
                    TwitterHandleConfig(handle="karpathy"),
                    TwitterHandleConfig(handle="sama"),
                ]
            ),
        )
        summary = summarise_agent_config(config)
        assert "karpathy" in summary

    def test_summary_contains_output_dir(self) -> None:
        config = AgentConfig(agent_id="test", output_dir="outputs/test")
        summary = summarise_agent_config(config)
        assert "outputs/test" in summary

    def test_summary_contains_cadence_overrides(self) -> None:
        config = AgentConfig(
            agent_id="test",
            llm=LLMConfig(
                cadence_overrides={"annual": LLMCadenceOverride(model="o3", thinking=True)}
            ),
        )
        summary = summarise_agent_config(config)
        assert "annual" in summary


# ─────────────────────────────────────────────────────────────────────────────
# Multi-agent discovery end-to-end  (SRC-072)
# ─────────────────────────────────────────────────────────────────────────────


class TestMultiAgentDiscovery:
    """
    End-to-end tests for the multi-agent configuration system.

    Validates that multiple agents with different configs can be spun up
    and discovered without any code changes (SRC-072).
    """

    def test_two_agents_independent_configs(self, tmp_path: Path) -> None:
        """Two agents with completely different configs are independently loaded."""
        agent_a_data = {
            "agent_id": "business",
            "llm": {"provider": "openai", "model": "gpt-4o"},
            "sources": {"tier_1b": ["reuters.com", "bloomberg.com"]},
            "twitter": {"enabled": True, "handles": [{"handle": "sama", "weight": 1.0}]},
            "limits": {"daily_top_n": 10},
        }
        agent_b_data = {
            "agent_id": "policy",
            "llm": {"provider": "anthropic", "model": "claude-3-7-sonnet-20250219"},
            "sources": {"tier_4": ["brookings.edu", "rand.org"]},
            "twitter": {"enabled": False},
            "limits": {"daily_top_n": 5},
        }
        yaml_a = _write_yaml(tmp_path / "business.yaml", agent_a_data)
        yaml_b = _write_yaml(tmp_path / "policy.yaml", agent_b_data)

        sched_cfg = SchedulerConfig(
            agents=[
                AgentRegistration(id="business", config=str(yaml_a), enabled=True),
                AgentRegistration(id="policy", config=str(yaml_b), enabled=True),
            ]
        )
        agents = load_all_enabled_agents(sched_cfg)

        assert set(agents.keys()) == {"business", "policy"}
        assert agents["business"].llm.provider == "openai"
        assert agents["policy"].llm.provider == "anthropic"
        assert agents["business"].twitter.enabled is True
        assert agents["policy"].twitter.enabled is False
        assert agents["business"].limits.daily_top_n == 10
        assert agents["policy"].limits.daily_top_n == 5

    def test_adding_agent_no_code_changes_needed(self, tmp_path: Path) -> None:
        """
        Adding a new agent requires only a new YAML file and a registry entry —
        no code changes (SRC-072).

        Simulated here by constructing a third agent config entirely in YAML.
        """
        new_agent_data = {
            "agent_id": "healthcare-ai",
            "llm": {"provider": "openai", "model": "gpt-4o"},
            "sources": {
                "custom": ["healthcareitnews.com", "statnews.com"],
                "tier_3": ["wired.com"],
            },
            "twitter": {"enabled": True, "handles": [{"handle": "drfeifei", "weight": 2.0}]},
            "curation_prompt": "prompts/daily.md",
            "output_dir": "outputs/healthcare-ai",
        }
        yaml_path = _write_yaml(tmp_path / "healthcare-ai.yaml", new_agent_data)
        config = load_agent_config(yaml_path)
        assert config.agent_id == "healthcare-ai"
        assert "healthcareitnews.com" in config.sources.custom
        assert config.twitter.handles[0].handle == "drfeifei"
        assert config.twitter.handles[0].weight == 2.0

    def test_scheduler_config_no_code_changes_for_new_agent(self, tmp_path: Path) -> None:
        """
        Scheduler YAML registry can be extended without code changes (SRC-072).
        """
        agent_yaml = _write_yaml(tmp_path / "new.yaml", _minimal_agent_yaml("new"))
        sched_data = {
            "scheduler": {"max_retries": 3, "retry_backoff_base_seconds": 30},
            "triggers": {
                "sourcing_daily": "0 0 * * *",
                "curation_daily": "5 0 * * *",
                "curation_weekly": "0 1 * * 0",
                "curation_monthly": "0 2 1 * *",
                "curation_annual": "0 3 1 1 *",
            },
            "api": {"enabled": True, "host": "0.0.0.0", "port": 8081},
            "agents": [
                {
                    "id": "new",
                    "config": str(agent_yaml),
                    "enabled": True,
                    "description": "A brand new agent added without code changes",
                }
            ],
        }
        sched_yaml = _write_yaml(tmp_path / "scheduler.yaml", sched_data)
        cfg = load_scheduler_config(sched_yaml)
        assert len(cfg.agents) == 1
        assert cfg.agents[0].id == "new"
        agents = load_all_enabled_agents(cfg)
        assert "new" in agents


# ─────────────────────────────────────────────────────────────────────────────
# CLI validator (schema.py __main__)  (SRC-071, SRC-072)
# ─────────────────────────────────────────────────────────────────────────────


class TestSchemaCLI:
    """Traces: SRC-071 (CLI validator), SRC-072 (per-config validation)."""

    def test_cli_validate_valid_agent(self) -> None:
        from ai_news_agent.config.schema import _cli_main

        rc = _cli_main(["--validate", "configs/default-agent.yaml"])
        assert rc == 0

    def test_cli_validate_invalid_agent_exits_1(self, tmp_path: Path) -> None:
        from ai_news_agent.config.schema import _cli_main

        bad = tmp_path / "bad.yaml"
        bad.write_text("agent_id: [broken list\n", encoding="utf-8")
        rc = _cli_main(["--validate", str(bad)])
        assert rc == 1

    def test_cli_validate_scheduler(self) -> None:
        from ai_news_agent.config.schema import _cli_main

        rc = _cli_main(["--validate", "configs/scheduler.yaml", "--type", "scheduler"])
        assert rc == 0

    def test_cli_export_schemas(self, tmp_path: Path) -> None:
        from ai_news_agent.config.schema import _cli_main

        rc = _cli_main(["--export-dir", str(tmp_path)])
        assert rc == 0
        assert (tmp_path / "agent-config.schema.json").exists()
        assert (tmp_path / "scheduler.schema.json").exists()

    def test_cli_validate_with_summary_exits_0(self) -> None:
        from ai_news_agent.config.schema import _cli_main

        rc = _cli_main(["--validate", "configs/default-agent.yaml", "--summary"])
        assert rc == 0

    def test_cli_validate_scheduler_with_summary(self) -> None:
        from ai_news_agent.config.schema import _cli_main

        rc = _cli_main(
            [
                "--validate",
                "configs/scheduler.yaml",
                "--type",
                "scheduler",
                "--summary",
            ]
        )
        assert rc == 0

    def test_cli_validate_missing_file_exits_1(self) -> None:
        from ai_news_agent.config.schema import _cli_main

        rc = _cli_main(["--validate", "/nonexistent/file.yaml"])
        assert rc == 1
