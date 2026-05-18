"""
config/models.py — Pydantic v2 configuration models.

Traces: SRC-017–SRC-021 (source tiers), SRC-029–SRC-032 (cadence limits),
        SRC-036–SRC-046 (Twitter handles/weights), SRC-054 (cadence LLM overrides),
        SRC-057 (provider config), SRC-071–SRC-073 (config system),
        SRC-105–SRC-111 (secrets from env vars only)

Design constraints (SRC-072, SRC-073):
- One YAML file per agent; multiple agents registered in scheduler.yaml.
- No secrets of any kind in YAML — only env vars / secrets manager.
- All fields must have sensible defaults so a minimal config works out of the box.
- {agent_id} placeholder in output_dir is resolved by load_agent_config() at load time.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# ---------------------------------------------------------------------------
# Provider literal type — extensible by adding to the union (SRC-055–SRC-057)
# ---------------------------------------------------------------------------

LLMProvider = Literal["openai", "anthropic", "google"]

# ---------------------------------------------------------------------------
# LLM configuration models (SRC-054, SRC-057)
# ---------------------------------------------------------------------------


class LLMCadenceOverride(BaseModel):
    """
    Per-cadence model override.

    Monthly and annual cadences benefit from higher-reasoning research-grade
    models (e.g. o3 with extended thinking).

    Traces: SRC-032 (annual high-reasoning), SRC-054 (configurable research LLM)

    YAML example::

        cadence_overrides:
          monthly:
            model: o3
            thinking: false
          annual:
            model: o3
            thinking: true
    """

    model: str = Field(
        ...,
        description=(
            "Model name for this cadence override. Examples: 'o3', 'claude-3-7-sonnet-20250219'."
        ),
        min_length=1,
    )
    thinking: bool = Field(
        default=False,
        description=(
            "Enable extended thinking / reasoning mode (e.g. o3 extended thinking). "
            "Recommended: true for annual synthesis only (SRC-032)."
        ),
    )


class LLMConfig(BaseModel):
    """
    LLM provider and model configuration.

    Traces: SRC-055–SRC-061 (provider-agnostic LLM layer),
            SRC-057 (OpenAI as default provider)

    YAML example::

        llm:
          provider: openai
          model: gpt-4o
          cadence_overrides:
            monthly:
              model: o3
              thinking: false
            annual:
              model: o3
              thinking: true
    """

    provider: LLMProvider = Field(
        default="openai",
        description=(
            "LLM provider. Supported: 'openai' (default), 'anthropic'. "
            "Provider swap requires only this field change (SRC-056)."
        ),
    )
    model: str = Field(
        default="gpt-4o",
        description=(
            "Default model name for daily and weekly curation. "
            "OpenAI examples: 'gpt-4o', 'o3-mini'. "
            "Anthropic examples: 'claude-3-7-sonnet-20250219'."
        ),
        min_length=1,
    )
    cadence_overrides: dict[str, LLMCadenceOverride] = Field(
        default_factory=dict,
        description=(
            "Per-cadence model overrides. Keys must be one of: "
            "'daily', 'weekly', 'monthly', 'annual'. "
            "Monthly and annual commonly use a more capable research model (SRC-054)."
        ),
    )

    @field_validator("cadence_overrides", mode="before")
    @classmethod
    def validate_cadence_keys(cls, v: dict[str, object]) -> dict[str, object]:
        """Reject unknown cadence keys early with a clear error."""
        valid = {"daily", "weekly", "monthly", "annual"}
        unknown = set(v.keys()) - valid
        if unknown:
            raise ValueError(
                f"Unknown cadence key(s) in cadence_overrides: {sorted(unknown)}. "
                f"Valid keys: {sorted(valid)}."
            )
        return v


# ---------------------------------------------------------------------------
# Twitter / influencer configuration (SRC-036–SRC-046)
# ---------------------------------------------------------------------------


class TwitterHandleConfig(BaseModel):
    """
    A single influencer handle with optional signal weight.

    Handles can be added, removed, or re-weighted without code changes (SRC-046).
    Twitter content is signal only — not primary news (SRC-047).

    Traces: SRC-036–SRC-046 (configurable influencer list), SRC-047 (signal role)

    YAML example::

        - handle: karpathy
          weight: 1.5
    """

    handle: str = Field(
        ...,
        description=(
            "Twitter/X handle without the @ prefix. Examples: 'karpathy', 'sama', 'ylecun'."
        ),
        min_length=1,
        pattern=r"^[A-Za-z0-9_]{1,50}$",
    )
    weight: Annotated[float, Field(gt=0.0, le=10.0)] = Field(
        default=1.0,
        description=(
            "Signal weight for this handle (0.0 < weight ≤ 10.0). "
            "Higher weight = more prominent in the LLM prompt's influencer-signal section. "
            "Default 1.0. Technical/research agents may boost specialist handles (SRC-046)."
        ),
    )


class TwitterConfig(BaseModel):
    """
    Twitter/X sourcing configuration.

    When ``enabled: false``, the sourcing agent skips Twitter entirely and
    logs a note. The digest is still produced from web sources (SRC-148).

    Traces: SRC-036–SRC-046 (configurable influencer list),
            SRC-047 (signal role — not primary news),
            SRC-148 (graceful degradation when unavailable)

    YAML example::

        twitter:
          enabled: true
          handles:
            - { handle: karpathy, weight: 1.0 }
            - { handle: sama,     weight: 1.0 }
    """

    enabled: bool = Field(
        default=True,
        description=(
            "Set false to skip Twitter/X entirely for this agent. "
            "The digest is still produced from web sources (SRC-148)."
        ),
    )
    handles: list[TwitterHandleConfig] = Field(
        default_factory=list,
        description=(
            "Influencer handles to monitor. Add, remove, or re-weight without code changes (SRC-046). "
            "If empty and enabled=true, no Twitter signal is collected."
        ),
    )


# ---------------------------------------------------------------------------
# Source tier configuration (SRC-016–SRC-021, SRC-034)
# ---------------------------------------------------------------------------


class SourcesConfig(BaseModel):
    """
    Source tier configuration.

    Five tiers map to the architecture's source hierarchy (SRC-016–SRC-021).
    All tiers are optional lists — the sourcing agent uses these as allowlists
    when ranking and filtering web search results.

    Traces: SRC-016 (tier hierarchy), SRC-017 (Tier 1a — user custom),
            SRC-018 (Tier 1b — business press), SRC-019 (Tier 2 — AI blogs),
            SRC-020 (Tier 3 — tech business press), SRC-021 (Tier 4 — policy/research),
            SRC-034 (user-configurable sources)

    YAML example::

        sources:
          custom:   []               # Tier 1a (optional)
          tier_1b:
            - reuters.com
            - bloomberg.com
          tier_2:
            - openai.com
            - anthropic.com
          tier_3:
            - techcrunch.com
          tier_4:
            - brookings.edu
    """

    custom: list[str] = Field(
        default_factory=list,
        description=(
            "Tier 1a — user-specified priority sources (SRC-017). "
            "Domain strings only (e.g. 'myblog.example.com'). "
            "These are treated with highest priority during curation scoring."
        ),
    )
    tier_1b: list[str] = Field(
        default_factory=list,
        description=(
            "Tier 1b — popular business press (SRC-018). "
            "Examples: reuters.com, bloomberg.com, wsj.com, ft.com, economist.com."
        ),
    )
    tier_2: list[str] = Field(
        default_factory=list,
        description=(
            "Tier 2 — top AI and tech company blogs (SRC-019). "
            "Examples: openai.com, anthropic.com, huggingface.co, news.ycombinator.com."
        ),
    )
    tier_3: list[str] = Field(
        default_factory=list,
        description=(
            "Tier 3 — tech business press (SRC-020). "
            "Examples: techcrunch.com, theverge.com, technologyreview.com, wired.com."
        ),
    )
    tier_4: list[str] = Field(
        default_factory=list,
        description=(
            "Tier 4 — policy and research institutions (SRC-021). "
            "Examples: brookings.edu, rand.org, hai.stanford.edu, ainowresearch.org."
        ),
    )


# ---------------------------------------------------------------------------
# Cadence limits (SRC-029–SRC-032)
# ---------------------------------------------------------------------------


class LimitsConfig(BaseModel):
    """
    Top-N article limits per cadence.

    Controls how many items the LLM is asked to select from the candidate pool.
    These are soft targets — the LLM may return fewer if quality thresholds
    are not met.

    Traces: SRC-029 (daily), SRC-030 (weekly), SRC-031 (monthly), SRC-032 (annual)

    YAML example::

        limits:
          daily_top_n:   10
          weekly_top_n:  7
          monthly_top_n: 10
          annual_top_n:  10
    """

    daily_top_n: Annotated[int, Field(ge=1, le=50)] = Field(
        default=10,
        description=("Maximum articles to select for a daily digest. Recommended: 5–15 (SRC-029)."),
    )
    weekly_top_n: Annotated[int, Field(ge=1, le=50)] = Field(
        default=7,
        description=(
            "Maximum articles to select for a weekly digest. Recommended: 5–10 (SRC-030)."
        ),
    )
    monthly_top_n: Annotated[int, Field(ge=1, le=50)] = Field(
        default=10,
        description=(
            "Maximum articles to select for a monthly digest. Recommended: 8–15 (SRC-031)."
        ),
    )
    annual_top_n: Annotated[int, Field(ge=1, le=20)] = Field(
        default=10,
        description=(
            "Top articles + predictions count for the annual digest. "
            "Spec requires exactly 10 articles + 10 predictions (SRC-032). "
            "Range: 1–20."
        ),
    )


# ---------------------------------------------------------------------------
# Full per-agent configuration (SRC-072)
# ---------------------------------------------------------------------------


class AgentConfig(BaseModel):
    """
    Full per-agent configuration.

    One YAML file per agent instance; multiple agents are registered in
    ``configs/scheduler.yaml`` and run independently as separate schedulable units.

    Key design rules (SRC-071–SRC-073):
    - ``agent_id`` must be a valid identifier (no spaces, no slashes).
    - ``output_dir`` may contain the ``{agent_id}`` placeholder, which is resolved
      by :func:`load_agent_config` at load time.
    - Secrets (API keys, tokens) are NEVER stored here — use env vars (SRC-073).
    - All fields have sensible defaults so a minimal config with just ``agent_id``
      is valid and operational.

    Traces: SRC-017–SRC-021 (source tiers), SRC-029–SRC-032 (cadence limits),
            SRC-036–SRC-046 (Twitter), SRC-054 (research LLM),
            SRC-057 (provider), SRC-071–SRC-073 (config system),
            SRC-113 (prompt path), SRC-129 (prompt versioning), SRC-145 (output dir)

    Minimal YAML example::

        agent_id: my-agent

    Full YAML example::

        agent_id: my-agent
        llm:
          provider: openai
          model: gpt-4o
        curation_prompt: prompts/daily.md
        sources:
          tier_1b: [reuters.com]
        twitter:
          enabled: true
          handles:
            - { handle: karpathy, weight: 1.0 }
        limits:
          daily_top_n: 10
        output_dir: outputs/{agent_id}
    """

    agent_id: str = Field(
        ...,
        description=(
            "Unique agent identifier. Used as the storage namespace and output directory "
            "component. Must be a valid slug: letters, digits, hyphens, underscores only."
        ),
        min_length=1,
        max_length=64,
        pattern=r"^[a-zA-Z0-9_-]+$",
    )
    llm: LLMConfig = Field(
        default_factory=LLMConfig,
        description="LLM provider and model configuration (SRC-057).",
    )
    curation_prompt: str = Field(
        default="prompts/daily.md",
        description=(
            "Path to the base curation prompt template directory or a specific file. "
            "When a directory is given, the appropriate cadence file "
            "(daily.md / weekly.md / monthly.md / annual.md) is selected at runtime. "
            "Prompt SHA-256 is recorded in all digest outputs (SRC-113, SRC-129)."
        ),
        min_length=1,
    )
    sources: SourcesConfig = Field(
        default_factory=SourcesConfig,
        description="Configurable source tier lists (SRC-016–SRC-021).",
    )
    twitter: TwitterConfig = Field(
        default_factory=TwitterConfig,
        description="Twitter/X influencer configuration (SRC-036–SRC-046).",
    )
    limits: LimitsConfig = Field(
        default_factory=LimitsConfig,
        description="Top-N article limits per cadence (SRC-029–SRC-032).",
    )
    output_dir: str = Field(
        default="outputs/{agent_id}",
        description=(
            "Base output directory for digest files. "
            "The placeholder ``{agent_id}`` is resolved to the agent's ``agent_id`` "
            "at load time (SRC-145). "
            "Output file pattern: ``{output_dir}/YYYY-MM-DD-{cadence}.{md|html|json}``."
        ),
        min_length=1,
    )

    @model_validator(mode="after")
    def resolve_output_dir(self) -> AgentConfig:
        """
        Resolve ``{agent_id}`` placeholder in ``output_dir`` at model validation time.

        This means ``load_agent_config()`` does NOT need a separate post-processing
        step — the resolution happens automatically during Pydantic validation.

        Traces: SRC-145 (date-stamped, idempotent output filenames)
        """
        if "{agent_id}" in self.output_dir:
            self.output_dir = self.output_dir.replace("{agent_id}", self.agent_id)
        return self


# ---------------------------------------------------------------------------
# Scheduler configuration models (SRC-052, SRC-072, SRC-144, SRC-147)
# ---------------------------------------------------------------------------


class APIConfig(BaseModel):
    """
    Manual override API settings for the scheduler.

    Exposes a ``POST /api/trigger`` endpoint for on-demand job execution —
    useful for backfills and misfire recovery (SRC-147).

    Traces: SRC-147 (manual override endpoint)

    YAML example::

        api:
          enabled: true
          host: "0.0.0.0"
          port: 8081
    """

    enabled: bool = Field(
        default=True,
        description="Enable the manual override HTTP API.",
    )
    host: str = Field(
        default="0.0.0.0",
        description="Bind host for the scheduler API.",
    )
    port: Annotated[int, Field(ge=1, le=65535)] = Field(
        default=8081,
        description="TCP port for the scheduler API (default: 8081).",
    )


class TriggersConfig(BaseModel):
    """
    Cron expressions for all sourcing and curation jobs (UTC).

    All expressions are five-field standard cron format:
    ``minute hour day_of_month month day_of_week``

    Traces: SRC-009 (daily sourcing 00:00 UTC),
            SRC-028 (curation at beginning of next window),
            SRC-029 (daily curation), SRC-030 (weekly), SRC-031 (monthly),
            SRC-032 (annual Jan 1)

    YAML example::

        triggers:
          sourcing_daily:   "0 0 * * *"
          curation_daily:   "5 0 * * *"
          curation_weekly:  "0 1 * * 0"
          curation_monthly: "0 2 1 * *"
          curation_annual:  "0 3 1 1 *"
    """

    sourcing_daily: str = Field(
        default="0 0 * * *",
        description="Cron for daily sourcing run — 00:00 UTC (SRC-009).",
    )
    curation_daily: str = Field(
        default="5 0 * * *",
        description="Cron for daily curation run — 00:05 UTC (SRC-029).",
    )
    curation_weekly: str = Field(
        default="0 1 * * 0",
        description="Cron for weekly curation run — 01:00 UTC Sunday (SRC-030).",
    )
    curation_monthly: str = Field(
        default="0 2 1 * *",
        description="Cron for monthly curation run — 02:00 UTC 1st of month (SRC-031).",
    )
    curation_annual: str = Field(
        default="0 3 1 1 *",
        description="Cron for annual curation run — 03:00 UTC January 1st (SRC-032).",
    )

    @field_validator(
        "sourcing_daily",
        "curation_daily",
        "curation_weekly",
        "curation_monthly",
        "curation_annual",
        mode="before",
    )
    @classmethod
    def validate_cron_fields(cls, v: str) -> str:
        """Reject cron expressions that don't have exactly 5 fields."""
        if len(str(v).strip().split()) != 5:
            raise ValueError(
                f"Invalid cron expression {v!r} — must have exactly 5 fields "
                "(minute hour day_of_month month day_of_week)."
            )
        return v


class AgentRegistration(BaseModel):
    """
    A single entry in the ``scheduler.yaml`` agents registry.

    Each registration links an agent ID to its per-agent YAML config file.
    Disabled agents are skipped at scheduler startup without errors (SRC-072).

    Traces: SRC-072 (multi-agent discovery from scheduler.yaml)

    YAML example::

        agents:
          - id: default
            config: configs/default-agent.yaml
            enabled: true
            description: "Default business + society AI news curation"
    """

    id: str = Field(
        ...,
        description="Unique agent identifier. Must match the ``agent_id`` in the referenced YAML.",
        min_length=1,
        max_length=64,
        pattern=r"^[a-zA-Z0-9_-]+$",
    )
    config: str = Field(
        ...,
        description="Relative or absolute path to the per-agent YAML configuration file.",
        min_length=1,
    )
    enabled: bool = Field(
        default=True,
        description=(
            "Set false to skip this agent at scheduler startup. "
            "Useful for staging configs that are not yet ready (SRC-072)."
        ),
    )
    description: str = Field(
        default="",
        description="Human-readable description of this agent's curation theme.",
    )


class RetryConfig(BaseModel):
    """
    Retry policy for failed scheduler jobs.

    Implements exponential backoff: ``base → base*2 → base*4``.
    Default: 30s → 60s → 120s (three retries).

    Traces: SRC-144 (3 retries with exponential backoff)

    YAML example::

        scheduler:
          max_retries: 3
          retry_backoff_base_seconds: 30
    """

    max_retries: Annotated[int, Field(ge=0, le=10)] = Field(
        default=3,
        description="Number of retry attempts after first failure (SRC-144).",
    )
    retry_backoff_base_seconds: Annotated[int, Field(ge=1, le=300)] = Field(
        default=30,
        description=(
            "Base sleep interval in seconds for exponential backoff. "
            "Pattern: base → base*2 → base*4 (SRC-144). "
            "Default: 30 → 60 → 120 seconds."
        ),
    )


class SchedulerConfig(BaseModel):
    """
    Root scheduler configuration, loaded from ``configs/scheduler.yaml``.

    Contains the global retry policy, cron trigger times, API settings,
    and the registry of all agent configurations.

    Traces: SRC-052 (scheduler), SRC-072 (multi-agent registry),
            SRC-144 (retry policy), SRC-147 (manual override)

    YAML example::

        scheduler:
          max_retries: 3
          retry_backoff_base_seconds: 30
        api:
          enabled: true
          host: "0.0.0.0"
          port: 8081
        triggers:
          sourcing_daily: "0 0 * * *"
          curation_daily: "5 0 * * *"
        agents:
          - id: default
            config: configs/default-agent.yaml
            enabled: true
    """

    scheduler: RetryConfig = Field(
        default_factory=RetryConfig,
        description="Retry policy for failed jobs (SRC-144).",
    )
    api: APIConfig = Field(
        default_factory=APIConfig,
        description="Manual override API settings (SRC-147).",
    )
    triggers: TriggersConfig = Field(
        default_factory=TriggersConfig,
        description="Cron trigger times for all jobs (SRC-052).",
    )
    agents: list[AgentRegistration] = Field(
        default_factory=list,
        description=(
            "Registry of all agent configurations (SRC-072). "
            "Each enabled agent gets a full set of 5 scheduled jobs "
            "(sourcing + 4 curation cadences)."
        ),
    )


# ---------------------------------------------------------------------------
# Runtime secrets — env vars ONLY (SRC-073, SRC-105–SRC-111)
# ---------------------------------------------------------------------------


class RuntimeSecrets(BaseSettings):
    """
    All secrets are read from environment variables ONLY.

    NEVER place secret values in YAML config files or Docker images (SRC-073, SRC-111).

    Required environment variables:
    - ``OPENAI_API_KEY``         — LLM API key for OpenAI provider (SRC-107)
    - ``TWITTER_BEARER_TOKEN``   — Twitter/X v2 API bearer token (SRC-108)

    Optional environment variables:
    - ``ANTHROPIC_API_KEY``      — LLM API key for Anthropic provider (SRC-055)
    - ``WEB_SEARCH_API_KEY``     — Key for Brave/Tavily search fallback (SRC-109)
    - ``WEB_SEARCH_PROVIDER``    — "brave" | "tavily" | "native" (SRC-060)
    - ``SCHEDULER_API_KEY``      — Bearer token for POST /api/trigger (SRC-147)

    In production, these are injected by the cloud's secrets manager at container
    startup — never baked into the image (SRC-111).

    Traces: SRC-073 (secrets in env vars), SRC-105–SRC-111 (required + optional secrets)
    """

    openai_api_key: str = Field(
        alias="OPENAI_API_KEY",
        description="OpenAI API key (SRC-107). Required when provider=openai.",
    )
    twitter_bearer_token: str = Field(
        alias="TWITTER_BEARER_TOKEN",
        description="Twitter/X v2 API bearer token (SRC-108). Required for Twitter sourcing.",
    )
    web_search_api_key: str | None = Field(
        default=None,
        alias="WEB_SEARCH_API_KEY",
        description="API key for Brave or Tavily search (SRC-109). Optional.",
    )
    web_search_provider: str | None = Field(
        default=None,
        alias="WEB_SEARCH_PROVIDER",
        description=(
            "Search provider override: 'native' | 'brave' | 'tavily' (SRC-060). "
            "Defaults to 'native' (OpenAI built-in) when not set."
        ),
    )
    scheduler_api_key: str | None = Field(
        default=None,
        alias="SCHEDULER_API_KEY",
        description="Bearer token for the POST /api/trigger manual override endpoint (SRC-147).",
    )
    anthropic_api_key: str | None = Field(
        default=None,
        alias="ANTHROPIC_API_KEY",
        description="Anthropic API key (SRC-055). Required when provider=anthropic.",
    )
    google_api_key: str | None = Field(
        default=None,
        alias="GOOGLE_API_KEY",
        description=(
            "Google AI / Vertex AI API key (SRC-055). "
            "Required when provider=google. "
            "Alternatively, use Application Default Credentials for Vertex AI."
        ),
    )

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        populate_by_name=True,
        extra="ignore",
    )
