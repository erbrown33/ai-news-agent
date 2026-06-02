# Architecture — AI News Curation Agent

> **Generated / Last Verified:** 2026-05-10
> **Status:** Authoritative — governs all implementation stages (SLICE-002 through SLICE-006)
> **Compliance:** VAL-001 (every artifact references SRC-* IDs) · VAL-002 (requirements.md read before this document was written) · AC-001 (full SRC-001–SRC-150 coverage verified below)
> **Source-of-Truth Order:** `requirements.md` ▶ `spec.md` ▶ `implementation-plan.md` ▶ `backlog.md`
> **Coverage:** All 150 source requirements (SRC-001–SRC-150) are addressed herein.

---

## Table of Contents

1. [System Overview](#1-system-overview)
2. [Repository Layout](#2-repository-layout)
3. [Module-by-Module Design](#3-module-by-module-design)
   - 3.1 [config — Configuration System](#31-config--configuration-system)
   - 3.2 [llm — Provider-Agnostic LLM Abstraction](#32-llm--provider-agnostic-llm-abstraction)
   - 3.3 [storage — Document Store and Deduplication](#33-storage--document-store-and-deduplication)
   - 3.4 [twitter — Twitter/X Integration via tweepy](#34-twitter--twitterx-integration-via-tweepy)
   - 3.5 [sourcing — Sourcing Agent](#35-sourcing--sourcing-agent)
   - 3.6 [curation — Curation Agent](#36-curation--curation-agent)
   - 3.7 [rendering — Rendering Agent](#37-rendering--rendering-agent)
   - 3.8 [scheduler — Multi-Agent Scheduler](#38-scheduler--multi-agent-scheduler)
   - 3.9 [portal — Web Portal](#39-portal--web-portal)
4. [Per-Agent YAML Configuration Schema](#4-per-agent-yaml-configuration-schema)
5. [Prompt Versioning System](#5-prompt-versioning-system)
6. [Data Models and Schemas](#6-data-models-and-schemas)
7. [Agent Data Flow (End-to-End)](#7-agent-data-flow-end-to-end)
8. [Deployment Architecture](#8-deployment-architecture)
9. [CI/CD Pipeline](#9-cicd-pipeline)
10. [Quality Monitoring and Observability](#10-quality-monitoring-and-observability)
11. [Out of Scope — v1](#11-out-of-scope--v1)
12. [Traceability Rules and SRC-* Coverage Map](#12-traceability-rules-and-src--coverage-map)

---

## 1. System Overview

The AI News Curation Agent is a **multi-agent platform** that autonomously sources, curates, and renders AI-industry news digests on **daily, weekly, monthly, and annual cadences**. (SRC-003)

**Two non-negotiable deliverables (SRC-004):**
1. A well-designed **web portal** for browsing curated digests by timeframe and agent config, with theme visualization and export downloads.
2. **Structured export files** — Markdown, HTML, JSON — produced for every curation run; downloadable from the portal; designed to be pasted into email, Slack/Teams, or synced to a static site without code changes.

**Five non-negotiable architectural constraints:**

| Constraint | Detail | Source |
|-----------|--------|--------|
| Provider-agnostic LLM layer | Default **OpenAI Agents SDK**; swap provider without pipeline changes | SRC-055–SRC-061 |
| Twitter/X via tweepy | Bearer-token auth; signal-only role; graceful degradation if unavailable | SRC-062–SRC-070, SRC-148 |
| Per-agent YAML configs | Runtime behavior without code changes; secrets always in env vars, never YAML | SRC-072–SRC-073 |
| Serverless containers | GCP Cloud Run / AWS App Runner / Azure Container Apps | SRC-080–SRC-086 |
| Working links enforced | Every curated item must have a retrievable URL; no URL = dropped at renderer (non-negotiable) | SRC-049, SRC-141 |

**Three specialized agents (SRC-006):**

| Agent | Responsibility | Source |
|-------|---------------|--------|
| Sourcing Agent | Fetch candidate articles from web + Twitter/X; deduplicate; store. Strictly sourcing only — no curation. | SRC-007–SRC-013, SRC-033–SRC-053 |
| Curation Agent | Score, rank, and summarize candidates via LLM for each cadence window | SRC-014–SRC-032, SRC-112–SRC-131 |
| Rendering Agent | Write Markdown, HTML, JSON digest outputs to date-stamped files | SRC-004, SRC-135–SRC-141 |

**Five supporting subsystems:**

| Subsystem | Responsibility | Source |
|-----------|---------------|--------|
| Configuration System | Per-agent YAML + Pydantic validation; secrets from env vars only | SRC-071–SRC-073 |
| LLM Abstraction Layer | Provider-agnostic client interface; OpenAI default; factory pattern | SRC-055–SRC-061 |
| Document Store | TinyDB default; dedup by URL hash; shared by sourcing + curation | SRC-011–SRC-012, SRC-053 |
| Scheduler | APScheduler; reads scheduler.yaml; multi-agent; retry + manual override | SRC-052, SRC-072, SRC-144, SRC-147 |
| Web Portal | FastAPI + Jinja2; cadence-specific views; agent switcher; export downloads | SRC-133–SRC-134 |

---

## 2. Repository Layout

```
ai-news-agent/
├── pyproject.toml                        # PEP 517/518 build + tool config (ruff, pytest, mypy)
├── Dockerfile                            # Multi-stage; same image local → CI → prod (SRC-085)
├── .dockerignore                         # Excludes tests/, docs/, outputs/ from image
├── .env.example                          # Documents all required env vars (SRC-073, SRC-111)
│
├── .github/
│   └── workflows/
│       ├── ci.yml                        # Lint + type-check + test + build (all branches)
│       └── deploy.yml                    # Push to registry + deploy (main branch only)
│                                         # SRC-098–SRC-102, SRC-104
│
├── configs/                              # Runtime configuration — no secrets here (SRC-072–SRC-073)
│   ├── scheduler.yaml                    # Root scheduler: trigger times, retry, all agent registry
│   ├── default-agent.yaml                # Default agent: business + society AI focus
│   └── example-technical-agent.yaml      # Example alternate-theme agent (disabled by default)
│
├── prompts/                              # Versioned curation prompt templates (SRC-113, SRC-127)
│   ├── daily.md                          # Daily curation prompt (SRC-115–SRC-123)
│   ├── weekly.md                         # Weekly curation prompt (SRC-115–SRC-123, SRC-030)
│   ├── monthly.md                        # Monthly curation prompt — research LLM (SRC-031, SRC-054)
│   └── annual.md                         # Annual prompt — predictions + inflection points (SRC-124)
│
├── src/
│   └── ai_news_agent/
│       ├── __init__.py                   # Package version + public re-exports
│       │
│       ├── config/                       # Config loading + validation (SRC-071–SRC-073)
│       │   ├── __init__.py
│       │   ├── models.py                 # Pydantic v2 config models — full schema
│       │   └── loader.py                 # YAML loader; Pydantic validation; env-var secrets
│       │
│       ├── llm/                          # Provider-agnostic LLM abstraction (SRC-055–SRC-061)
│       │   ├── __init__.py
│       │   ├── base.py                   # AbstractLLMClient + SearchResult dataclass
│       │   ├── openai_client.py          # Concrete: OpenAI Agents SDK (DEFAULT, SRC-057)
│       │   ├── anthropic_client.py       # Concrete: Anthropic Claude provider (SRC-055–SRC-056)
│       │   ├── search_tools.py           # AbstractSearchTool + Brave/Tavily adapters (SRC-060)
│       │   └── factory.py               # get_llm_client() + get_search_tool() factories
│       │
│       ├── storage/                      # Document store abstraction (SRC-011–SRC-012, SRC-053)
│       │   ├── __init__.py
│       │   ├── base.py                   # AbstractArticleStore interface
│       │   ├── tinydb_store.py           # TinyDB concrete implementation (default)
│       │   └── models.py                 # ArticleRecord, TweetSignal, CuratedItem, DigestMetadata
│       │
│       ├── twitter/                      # Twitter/X integration via tweepy (SRC-062–SRC-070)
│       │   ├── __init__.py
│       │   └── client.py                 # tweepy wrapper; filter logic; graceful degradation
│       │
│       ├── sourcing/                     # Sourcing Agent (SRC-006–SRC-013, SRC-033–SRC-049)
│       │   ├── __init__.py
│       │   ├── agent.py                  # SourcingAgent orchestrator + cli_main entry point
│       │   ├── web_fetcher.py            # Web search strategies: LLM-native / Brave / Tavily
│       │   └── twitter_fetcher.py        # Thin adapter: calls twitter.client, normalizes output
│       │
│       ├── curation/                     # Curation Agent (SRC-014–SRC-032, SRC-112–SRC-131)
│       │   ├── __init__.py
│       │   ├── agent.py                  # CurationAgent orchestrator + cli_main entry point
│       │   ├── prompt_builder.py         # Injects ISO dates, tiered candidates, Twitter signal
│       │   └── scorer.py                 # Post-LLM tier-weighted re-ranking + URL enforcement
│       │
│       ├── rendering/                    # Rendering Agent (SRC-004, SRC-135–SRC-141, SRC-145)
│       │   ├── __init__.py
│       │   ├── agent.py                  # RenderingAgent orchestrator + cli_main entry point
│       │   ├── markdown_renderer.py      # Renders .md (Slack/Teams paste-ready)
│       │   ├── html_renderer.py          # Renders .html (email-client paste-ready)
│       │   └── json_renderer.py          # Renders .json (machine-readable archive)
│       │
│       ├── scheduler/                    # APScheduler orchestration (SRC-052, SRC-072, SRC-144)
│       │   ├── __init__.py
│       │   └── runner.py                 # AgentScheduler; cadence jobs; retry; cli_main
│       │
│       └── portal/                       # Web Portal (SRC-004, SRC-133–SRC-134)
│           ├── __init__.py
│           ├── app.py                    # FastAPI application factory + cli_main
│           ├── routes.py                 # HTTP route handlers
│           ├── templates/                # Jinja2 HTML templates
│           │   ├── base.html             # Layout, nav, agent config switcher, download bar
│           │   ├── index.html            # Landing: digest list by cadence + agent config
│           │   ├── daily.html            # Article card list + why-it-matters + impact tags
│           │   ├── weekly.html           # Theme section + top articles + week outlook
│           │   ├── monthly.html          # Big-picture themes + anticipated news
│           │   └── annual.html           # Top 10 articles + predictions + year-in-review
│           └── static/
│               ├── css/
│               │   └── app.css           # Responsive CSS (Tailwind-compiled or hand-crafted)
│               └── js/
│                   └── app.js            # Tag cloud, tier/theme filter, download triggers
│
├── tests/
│   ├── __init__.py
│   ├── conftest.py                       # Shared fixtures; LLM + Twitter mocks (SRC-098)
│   ├── unit/
│   │   ├── __init__.py
│   │   ├── test_config.py                # Config loading, schema validation, secret injection
│   │   ├── test_llm_base.py              # AbstractLLMClient contract tests (mock concrete)
│   │   ├── test_sourcing.py              # SourcingAgent: fetch, dedup, store behavior
│   │   ├── test_curation.py              # CurationAgent: prompt build, scoring, URL drop
│   │   ├── test_rendering.py             # All three renderers; URL-drop enforcement
│   │   ├── test_storage.py               # TinyDB: insert_if_new, dedup, lookback query
│   │   ├── test_scheduler.py             # Cadence trigger logic; retry backoff behavior
│   │   └── test_twitter.py               # tweepy wrapper; filter logic; degradation path
│   └── integration/
│       ├── __init__.py
│       └── test_pipeline_smoke.py        # Dry-run end-to-end; no real LLM/Twitter calls
│
└── outputs/                              # Runtime output directory — gitignored
    └── .gitkeep
```

---

## 3. Module-by-Module Design

### 3.1 `config` — Configuration System

**Source requirements:** SRC-017, SRC-034, SRC-036–SRC-046, SRC-053–SRC-054, SRC-057, SRC-071–SRC-073, SRC-105–SRC-111

#### Responsibilities
- Load and validate per-agent YAML configuration files at startup.
- Inject secrets from environment variables — never from YAML files (SRC-073).
- Expose a typed, immutable `AgentConfig` object to all other modules.
- Support multiple simultaneous agent instances from one scheduler run (SRC-072).
- Fail loudly at startup with a clear error if required secrets are missing.

#### `config/models.py` — Pydantic v2 models

```python
# Traces: SRC-017–SRC-021 (source tiers), SRC-029–SRC-032 (limits),
#         SRC-036–SRC-046 (Twitter handles), SRC-054 (cadence LLM overrides),
#         SRC-057 (provider config), SRC-071–SRC-073 (config system)

from __future__ import annotations
from typing import Literal
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class LLMCadenceOverride(BaseModel):
    """Per-cadence model override — e.g. research model for annual (SRC-032, SRC-054)."""
    model: str
    thinking: bool = False   # Extended thinking for annual o3 runs (SRC-032)


class LLMConfig(BaseModel):
    """LLM provider and model configuration (SRC-057)."""
    provider: Literal["openai", "anthropic", "google"] = "openai"
    model: str = "gpt-4o"
    # Per-cadence overrides; keys are "daily" | "weekly" | "monthly" | "annual"
    cadence_overrides: dict[str, LLMCadenceOverride] = Field(default_factory=dict)


class TwitterHandleConfig(BaseModel):
    """A single influencer handle with optional signal weight (SRC-046)."""
    handle: str             # without @ prefix
    weight: float = 1.0     # higher = more prominent in prompt signal section


class SourcesConfig(BaseModel):
    """Source tier configuration (SRC-017–SRC-021, SRC-034)."""
    custom:  list[str] = Field(default_factory=list)   # Tier 1a — user-supplied (SRC-017)
    tier_1b: list[str] = Field(default_factory=list)   # Popular business press (SRC-018)
    tier_2:  list[str] = Field(default_factory=list)   # Top tech/AI blogs (SRC-019)
    tier_3:  list[str] = Field(default_factory=list)   # Tech business press (SRC-020)
    tier_4:  list[str] = Field(default_factory=list)   # Policy/research (SRC-021)


class TwitterConfig(BaseModel):
    """Twitter/X sourcing configuration (SRC-036–SRC-046)."""
    enabled: bool = True
    handles: list[TwitterHandleConfig] = Field(default_factory=list)


class LimitsConfig(BaseModel):
    """Top-N article limits per cadence (SRC-029–SRC-032)."""
    daily_top_n:   int = 10   # SRC-029
    weekly_top_n:  int = 7    # SRC-030
    monthly_top_n: int = 10   # SRC-031
    annual_top_n:  int = 10   # SRC-032


class AgentConfig(BaseModel):
    """
    Full per-agent configuration. One YAML file per agent instance. (SRC-072)
    Multiple agents can be registered in scheduler.yaml and run independently.
    """
    agent_id:        str
    llm:             LLMConfig = Field(default_factory=LLMConfig)
    curation_prompt: str = "prompts/daily.md"  # path into prompts/ (SRC-113)
    sources:         SourcesConfig = Field(default_factory=SourcesConfig)
    twitter:         TwitterConfig = Field(default_factory=TwitterConfig)
    limits:          LimitsConfig = Field(default_factory=LimitsConfig)
    output_dir:      str = "outputs/{agent_id}"  # {agent_id} placeholder resolved at runtime


class APIConfig(BaseModel):
    """Manual override API settings (SRC-147)."""
    enabled: bool = True
    host:    str = "0.0.0.0"
    port:    int = 8081


class TriggersConfig(BaseModel):
    """Cron expressions for all sourcing/curation jobs (SRC-009, SRC-028–SRC-032)."""
    sourcing_daily:   str = "0 0 * * *"    # 00:00 UTC daily
    curation_daily:   str = "5 0 * * *"    # 00:05 UTC daily
    curation_weekly:  str = "0 1 * * 0"    # 01:00 UTC Sunday
    curation_monthly: str = "0 2 1 * *"    # 02:00 UTC 1st of month
    curation_annual:  str = "0 3 1 1 *"    # 03:00 UTC January 1st


class AgentRegistration(BaseModel):
    """Entry in scheduler.yaml agents list (SRC-072)."""
    id:          str
    config:      str    # path to per-agent YAML file
    enabled:     bool = True
    description: str = ""


class RetryConfig(BaseModel):
    """Retry policy for failed scheduler jobs (SRC-144)."""
    max_retries:                int = 3
    retry_backoff_base_seconds: int = 30   # 30 → 60 → 120 seconds


class SchedulerConfig(BaseModel):
    """Root scheduler configuration loaded from configs/scheduler.yaml. (SRC-072)"""
    scheduler: RetryConfig = Field(default_factory=RetryConfig)
    api:       APIConfig = Field(default_factory=APIConfig)
    triggers:  TriggersConfig = Field(default_factory=TriggersConfig)
    agents:    list[AgentRegistration] = Field(default_factory=list)


class RuntimeSecrets(BaseSettings):
    """
    All secrets are read from environment variables ONLY.
    Never placed in YAML config files, never baked into images. (SRC-073, SRC-111)
    """
    openai_api_key:        str        = Field(alias="OPENAI_API_KEY")          # SRC-107
    twitter_bearer_token:  str        = Field(alias="TWITTER_BEARER_TOKEN")    # SRC-108
    web_search_api_key:    str | None = Field(None, alias="WEB_SEARCH_API_KEY") # SRC-109
    web_search_provider:   str | None = Field(None, alias="WEB_SEARCH_PROVIDER")
    scheduler_api_key:     str | None = Field(None, alias="SCHEDULER_API_KEY") # SRC-147
    anthropic_api_key:     str | None = Field(None, alias="ANTHROPIC_API_KEY") # SRC-055
    google_api_key:        str | None = Field(None, alias="GOOGLE_API_KEY")    # SRC-055

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        populate_by_name=True,
    )
```

#### `config/loader.py` — Loading logic

```python
# Traces: SRC-071 (config loading), SRC-072 (multiple agents), SRC-073 (secrets from env)

import yaml
from pathlib import Path
from .models import AgentConfig, SchedulerConfig


class ConfigError(Exception):
    """Raised for YAML schema violations or missing required secrets."""


def load_agent_config(path: str | Path) -> AgentConfig:
    """
    1. Read YAML from `path`.
    2. Parse into AgentConfig via Pydantic (schema validation).
    3. Resolve '{agent_id}' placeholder in output_dir.
    4. Secrets are NEVER read from YAML — they come from RuntimeSecrets (env vars)
       validated separately at startup. (SRC-073)
    Returns an immutable AgentConfig instance.
    Raises ConfigError if YAML is invalid or required fields are missing.
    """


def load_scheduler_config(path: str | Path = "configs/scheduler.yaml") -> SchedulerConfig:
    """
    Load and validate the root scheduler config.
    Raises ConfigError on schema violations.
    """
```

---

### 3.2 `llm` — Provider-Agnostic LLM Abstraction

**Source requirements:** SRC-027, SRC-053–SRC-061, SRC-107, SRC-109, SRC-112–SRC-113, SRC-121

#### Design principle (SRC-056)

The LLM provider is swappable **entirely within the concrete `*_client.py` implementation**. Every other module (sourcing, curation, rendering, scheduler) depends **only** on `AbstractLLMClient`. No provider-specific calling conventions, authentication patterns, output formats, or structured-output features leak into the pipeline above this layer.

This means:
- Prompts are plain natural language — no provider-specific formatting (SRC-059).
- Tool use is described abstractly; native tools used if available, otherwise adapter fallback (SRC-060).
- Output parsing is based on Markdown + embedded JSON — not provider schema-enforcement (SRC-061).

#### Class hierarchy

```
AbstractLLMClient          (llm/base.py)
  ├── complete(messages, model, **kwargs) → str
  ├── search(query, n_results, budget_hint) → list[SearchResult]
  └── parse_structured(raw_text, schema_cls) → T

OpenAILLMClient            (llm/openai_client.py)   ← DEFAULT (SRC-057)
AnthropicLLMClient         (llm/anthropic_client.py) ← Anthropic Claude (SRC-055–SRC-056)

AbstractSearchTool         (llm/search_tools.py)
  ├── search(query, n) → list[SearchResult]
  └── hydrate_url(url) → str | None

NativeOpenAISearchTool     (llm/search_tools.py)    # OpenAI built-in web search
BraveSearchTool            (llm/search_tools.py)    # Brave Search API (SRC-060 fallback)
TavilySearchTool           (llm/search_tools.py)    # Tavily API (SRC-060 fallback)

get_llm_client(cfg, secrets) → AbstractLLMClient    (llm/factory.py)
get_search_tool(cfg, secrets) → AbstractSearchTool  (llm/factory.py)
```

#### `llm/base.py` — Abstract interface

```python
# Traces: SRC-056 (provider-agnostic), SRC-059 (plain language prompts),
#         SRC-060 (abstract tool use), SRC-061 (output parsing contract)

from __future__ import annotations
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, TypeVar

T = TypeVar("T")

@dataclass
class SearchResult:
    """Normalized search result — same shape regardless of provider or search tool."""
    url:     str
    title:   str
    snippet: str
    source:  str   # domain name


class AbstractLLMClient(ABC):
    """
    Provider-agnostic LLM client interface.
    All pipeline code depends only on this interface — never on concrete subclasses.
    Satisfies: SRC-056 (swap without pipeline changes), SRC-057 (OpenAI default).
    """

    @abstractmethod
    def complete(
        self,
        messages: list[dict[str, str]],
        model: str,
        temperature: float = 0.2,
        **kwargs: Any,
    ) -> str:
        """
        Send a chat completion request.
        Returns the full assistant text content as a plain string.

        Output parsing (SRC-061) is done by the caller using parse_structured()
        on the returned string. Provider structured-output is NEVER relied on
        by the pipeline — it may be used as an optional enhancement inside
        the concrete implementation only.
        """

    @abstractmethod
    def search(
        self,
        query: str,
        n_results: int = 10,
        budget_hint: str = "normal",  # "normal" | "deep" — more results for monthly/annual
    ) -> list[SearchResult]:
        """
        Execute a web search. (SRC-121)
        Uses provider's native search tool if available; otherwise
        delegates to configured AbstractSearchTool adapter. (SRC-060)
        """

    @abstractmethod
    def parse_structured(self, raw: str, schema_cls: type[T]) -> T:
        """
        Parse the LLM's plain-text response into a typed Pydantic schema.
        Extracts the ```json ... ``` block embedded in the LLM response.
        Falls back to lenient JSON extraction if no fenced block is present.
        NEVER depends on provider schema-enforcement. (SRC-061)
        """
```

#### `llm/openai_client.py` — Default implementation

```python
# Traces: SRC-057 (OpenAI as default), SRC-060 (native search tool),
#         SRC-061 (output parsing from text), SRC-032/SRC-054 (extended thinking)

class OpenAILLMClient(AbstractLLMClient):
    """
    Wraps the OpenAI Agents SDK (openai-agents package) for all LLM interactions.

    Sourcing: uses the Responses API with web_search tool (native) or falls back
    to BraveSearchTool / TavilySearchTool matching the same AbstractSearchTool interface.

    Curation: sends structured prompt; parses embedded JSON block from response.

    Annual/monthly: supports o3 model with extended thinking when thinking=True
    is passed via kwargs (SRC-032, SRC-054).
    """

    def __init__(self, api_key: str, search_tool: AbstractSearchTool) -> None:
        self._client      = openai.OpenAI(api_key=api_key)
        self._search_tool = search_tool

    def complete(self, messages, model, temperature=0.2, **kwargs) -> str:
        # Uses openai.OpenAI().chat.completions.create() (or Responses API for agents)
        # kwargs["thinking"] = True enables extended thinking for o3 (SRC-032)
        ...

    def search(self, query, n_results=10, budget_hint="normal") -> list[SearchResult]:
        # Delegates to self._search_tool.search()
        # budget_hint="deep" → more results for monthly/annual cadences (SRC-121)
        ...

    def parse_structured(self, raw: str, schema_cls: type[T]) -> T:
        # 1. Find ```json ... ``` block via regex
        # 2. json.loads() the extracted block
        # 3. schema_cls.model_validate(parsed_dict)
        # 4. On failure: fallback to lenient extraction (strip leading/trailing text)
        ...
```

#### Output parsing contract (SRC-061)

All prompts instruct the model to produce output in this structure:

```
[Narrative Markdown content — displayed by portal; used for themes, outlook, etc.]

```json
{ ... structured data as CurationResponse schema ... }
```
```

`parse_structured()` extracts and validates the JSON block. The surrounding Markdown narrative is used by the renderer for portal theme and summary sections. This contract works identically across OpenAI, Anthropic, and any other frontier model — no provider-specific structured output is required.

#### `llm/search_tools.py` — Search tool adapters

```python
# Traces: SRC-060 (abstract tool use; Brave/Tavily fallback), SRC-109 (WEB_SEARCH_API_KEY)

class AbstractSearchTool(ABC):
    @abstractmethod
    def search(self, query: str, n: int = 10) -> list[SearchResult]: ...

    @abstractmethod
    def hydrate_url(self, url: str) -> str | None:
        """Fetch and return page text for URL hydration. Returns None on failure."""


class NativeOpenAISearchTool(AbstractSearchTool):
    """Uses OpenAI's built-in web search (via Responses API or tool call)."""

class BraveSearchTool(AbstractSearchTool):
    """Brave Search API via httpx. API key from WEB_SEARCH_API_KEY env var. (SRC-109)"""

class TavilySearchTool(AbstractSearchTool):
    """Tavily Search API via tavily-python. API key from WEB_SEARCH_API_KEY env var."""
```

#### `llm/factory.py` — Factory functions

```python
# Traces: SRC-055–SRC-057 (provider-agnostic; OpenAI default), SRC-060 (search tool selection)

def get_llm_client(llm_cfg: LLMConfig, secrets: RuntimeSecrets) -> AbstractLLMClient:
    """
    Returns the correct concrete client for llm_cfg.provider.
    Pipeline code NEVER instantiates provider clients directly.
    Raises ConfigError for unknown providers.
    """
    match llm_cfg.provider:
        case "openai":
            search = get_search_tool(llm_cfg, secrets)
            return OpenAILLMClient(api_key=secrets.openai_api_key, search_tool=search)
        case "anthropic":
            return AnthropicLLMClient(api_key=secrets.anthropic_api_key)
        case _:
            raise ConfigError(f"Unknown LLM provider: {llm_cfg.provider!r}")


def get_search_tool(llm_cfg: LLMConfig, secrets: RuntimeSecrets) -> AbstractSearchTool:
    """
    Priority: OpenAI native (if provider=openai) → Tavily (if key present)
              → Brave (if key present) → raise ConfigError.
    """
```

---

### 3.3 `storage` — Document Store and Deduplication

**Source requirements:** SRC-008–SRC-013, SRC-053

#### Store choice: TinyDB (default) or SQLite

The backend is selected via `store_backend` in the agent YAML (default: `tinydb`).

| Backend | File | Use case |
|---------|------|----------|
| `tinydb` | `outputs/{agent_id}/store.json` | Local dev, low volume (<50 k records) |
| `sqlite` | `outputs/{agent_id}/store.db` | Production, higher volumes, concurrent reads |

**TinyDB rationale (SRC-053, SRC-076):**
- Zero external infrastructure — JSON file on disk; ideal for local dev and serverless containers where storage is a mounted volume or synced from cloud storage.
- Native Python; no server process; stateless container model (SRC-085).

**SQLite rationale (SRC-053, SRC-085):**
- Indexed `(url_hash, agent_id)` dedup checks: O(log n) vs TinyDB's O(n) full scan.
- Indexed window queries on `(agent_id, pub_date)` for the same efficiency gain.
- WAL mode supports concurrent reads while a write is in progress.
- More compact storage for large article volumes; SQLite is in Python's stdlib — no added dependency.

**Swap path:** `AbstractArticleStore` interface means either backend can be replaced with DynamoDB (AWS), Firestore (GCP), or Cosmos DB (Azure) with zero changes to sourcing or curation modules.

**Storage file location:** `outputs/{agent_id}/store.{json|db}` — one file per agent so multiple agent configurations never interfere with each other (SRC-072).

**Auto-migration:** When an agent switches from `tinydb` to `sqlite`, `StoreFactory.create()` detects the existing `store.json` and imports all records (articles, tweets, digests) into the new SQLite store on the first run. The original `store.json` is kept as a backup.

#### Deduplication strategy (SRC-012)

**Primary dedup key: normalized URL hash**

```python
def normalize_url(raw_url: str) -> str:
    """
    Canonical URL normalization for deduplication (SRC-012):
    1. Parse with urllib.parse.urlparse().
    2. Lowercase scheme + netloc.
    3. Strip tracking query params: utm_*, fbclid, ref, source, campaign, medium, content.
    4. Strip trailing slash from path.
    5. Rebuild canonical URL string.
    """

def url_hash(canonical_url: str) -> str:
    """SHA-256 hex digest of canonical URL string — used as dedup key (SRC-012)."""
    return hashlib.sha256(canonical_url.encode()).hexdigest()
```

**Secondary dedup signal:** Levenshtein headline similarity ≥ 0.85 → flag as likely duplicate for review in run logs. Handles AMP vs canonical URLs, regional redirects, or minor headline edits.

#### `storage/base.py` — Abstract interface

```python
# Traces: SRC-008–SRC-012 (lookback windows, dedup), SRC-053 (document store)

class AbstractArticleStore(ABC):
    @abstractmethod
    def insert_if_new(self, article: ArticleRecord) -> bool:
        """
        Insert article only if url_hash not already present for this agent_id.
        Returns True if inserted (new), False if duplicate. (SRC-012)
        Lookback filtering is handled at query time, not insertion time.
        """

    @abstractmethod
    def get_window(
        self,
        agent_id:     str,
        window_start: datetime,
        window_end:   datetime,
    ) -> list[ArticleRecord]:
        """Return all ArticleRecords for agent_id within [window_start, window_end]."""

    @abstractmethod
    def insert_tweet_signal(self, signal: TweetSignal) -> bool:
        """Insert TweetSignal if tweet_id not already stored. Returns True if new."""

    @abstractmethod
    def get_tweet_signals(
        self,
        agent_id:     str,
        window_start: datetime,
        window_end:   datetime,
    ) -> list[TweetSignal]:
        """Return all TweetSignals for agent_id within [window_start, window_end]."""
```

#### `storage/tinydb_store.py` — Concrete implementation

```python
# Traces: SRC-012 (dedup), SRC-053 (TinyDB document store)

from tinydb import TinyDB, Query

class TinyDBArticleStore(AbstractArticleStore):
    """
    TinyDB-backed article store. One JSON file per agent at:
      outputs/{agent_id}/store.json

    Two tables:
      'articles' — ArticleRecord documents, keyed by url_hash + agent_id
      'tweets'   — TweetSignal documents, keyed by tweet_id + agent_id
    """

    def __init__(self, db_path: str) -> None:
        self._db = TinyDB(db_path)
        self._articles = self._db.table("articles")
        self._tweets   = self._db.table("tweets")

    def insert_if_new(self, article: ArticleRecord) -> bool:
        q = Query()
        exists = self._articles.contains(
            (q.url_hash == article.url_hash) & (q.agent_id == article.agent_id)
        )
        if not exists:
            self._articles.insert(asdict(article))
            return True
        return False
```

---

### 3.4 `twitter` — Twitter/X Integration via tweepy

**Source requirements:** SRC-035–SRC-036, SRC-046–SRC-047, SRC-062–SRC-070, SRC-148

#### Library and auth (SRC-063–SRC-065)

- **Library:** `tweepy >= 4.14.0` — supports Twitter API v2; best-maintained Python client (SRC-063).
- **Auth:** Bearer Token via `TWITTER_BEARER_TOKEN` env var. OAuth 2.0 user context not used in v1 (read-only) (SRC-064).
- **API Tier:** Basic tier minimum (SRC-065). Free tier rate limits and 7-day search depth are insufficient for reliable lookback windows. **Documented decision point** — confirm current tier pricing before provisioning.
- **Read-only scope:** v1 only fetches — no posting, no DMs.

#### `twitter/client.py` — TwitterClient

```python
# Traces: SRC-063 (tweepy), SRC-064 (bearer token), SRC-066–SRC-069 (fetch/filter/hydrate),
#         SRC-070 (influencer signal role), SRC-148 (graceful degradation)

import tweepy
from ..config.models import TwitterHandleConfig
from ..storage.models import TweetSignal

class TwitterClient:
    """
    tweepy-based Twitter/X client.
    Role: signal and lead-generation only — not primary news (SRC-047, SRC-070).

    On any tweepy error: logs warning, returns empty list with available=False.
    Caller appends a degradation note to the digest (SRC-148).
    """

    def __init__(self, bearer_token: str, handles: list[TwitterHandleConfig]) -> None:
        self._client  = tweepy.Client(bearer_token=bearer_token, wait_on_rate_limit=True)
        self._handles = handles

    def fetch_signals(
        self,
        window_start: datetime,
        window_end:   datetime,
        agent_id:     str,
    ) -> tuple[list[TweetSignal], bool]:
        """
        Returns (signals, twitter_available).
        twitter_available=False → sourcing continues web-only; digest notes degradation. (SRC-148)
        """
        try:
            signals: list[TweetSignal] = []
            for handle_cfg in self._handles:
                user = self._resolve_user(handle_cfg.handle)
                if not user:
                    continue
                tweets = self._fetch_tweets(user.id, window_start, window_end)
                for tweet in tweets:
                    if self._is_substantive(tweet):
                        signals.append(self._normalize(tweet, handle_cfg, agent_id))
            return signals, True
        except tweepy.TweepyException as exc:
            log.warning("twitter_unavailable", error=str(exc))
            return [], False

    def _is_substantive(self, tweet: tweepy.Tweet) -> bool:
        """
        Filter rules (SRC-068):
        - SKIP: pure replies (text starts with '@handle' with no preceding content)
        - SKIP: bare retweets (text starts with 'RT @')
        - KEEP: tweets containing a URL regardless of length
        - KEEP: original tweets >= 50 characters
        """

    def _fetch_tweets(self, user_id: str, start: datetime, end: datetime) -> list:
        """
        tweepy.Client.get_users_tweets() with (SRC-067):
        - start_time / end_time = lookback window boundaries
        - tweet.fields: ['created_at', 'entities', 'text', 'referenced_tweets']
        - expansions: ['attachments.media_keys']
        - max_results: 100 per page; pagination handled automatically
        """

    def _hydrate_urls(self, tweet: tweepy.Tweet) -> list[str]:
        """
        Expand t.co short links from tweet.entities.urls.
        Returns list of expanded (canonical) URLs. (SRC-069)
        These URLs are fed back to web fetching for primary reporting.
        """

    def _normalize(
        self, tweet: tweepy.Tweet, handle_cfg: TwitterHandleConfig, agent_id: str
    ) -> TweetSignal:
        """Convert tweepy.Tweet to TweetSignal dataclass."""
```

#### Twitter's role in curation (SRC-047, SRC-070)

Tweets are **signal and lead-generation context**, not primary sources. The curation prompt receives a clearly labeled `## Influencer Signal` section:

```
## Influencer Signal — For Context and Lead Generation Only
The following tweets from tracked AI influencers are provided as context
and lead-generation hints for this curation period.

Use them to identify topics worth investigating via web search.
Do NOT cite a tweet as a primary news source unless the tweet itself IS the
news (e.g., an executive announcement made on X before press coverage exists).

Ground all recommendations in primary web reporting. (SRC-070)
```

#### Graceful degradation (SRC-148)

If `fetch_signals()` returns `twitter_available=False`:
- Sourcing continues with web-only sources.
- `SourcingRunResult.twitter_available = False`.
- Rendering agent adds a banner in all three output formats:
  > _"Note: Influencer signal was unavailable for this run (Twitter API error). This digest was sourced from web sources only."_

Twitter is **signal, not a hard dependency**. A degraded run still produces a complete digest.

---

### 3.5 `sourcing` — Sourcing Agent

**Source requirements:** SRC-006–SRC-013, SRC-033–SRC-053, SRC-060, SRC-148, SRC-150

#### Responsibilities
- Run daily at 00:00 UTC (SRC-009); can be run multiple times per day to capture articles missed earlier — adds new ones only, never duplicates (SRC-010, SRC-012).
- Fetch candidate articles from configured tier sources + Twitter/X handles within the lookback window.
- Normalize URLs, deduplicate against the store, insert new records.
- **Strictly source only — no curation (SRC-013).**
- Log quality monitoring metrics per run (SRC-150).

#### Sourcing tiers (SRC-016–SRC-021)

| Tier | Content | Config Key |
|------|---------|------------|
| **1a** | User-configured custom sources — optional (SRC-017) | `sources.custom` |
| **1b** | Business press: Reuters, Bloomberg, WSJ, FT, The Economist, Axios (SRC-018) | `sources.tier_1b` |
| **2** | Tech/AI blogs: YCombinator, Netflix, Anthropic, OpenAI, HuggingFace, Towards AI (SRC-019) | `sources.tier_2` |
| **3** | Tech press: The Information, Stratechery, TechCrunch, The Verge, Wired, MIT Tech Review (SRC-020) | `sources.tier_3` |
| **4** | Policy/research: Brookings, RAND, Stanford HAI, AI Now Institute (SRC-021) | `sources.tier_4` |

#### Default Twitter influencer handles (SRC-037–SRC-046)

All configurable without code changes (SRC-046):

| Handle | Name |
|--------|------|
| @karpathy | Andrej Karpathy |
| @sama | Sam Altman |
| @demishassabis | Demis Hassabis |
| @DarioAmodei | Dario Amodei |
| @ylecun | Yann LeCun |
| @AndrewYNg | Andrew Ng |
| @fchollet | François Chollet |
| @drfeifei | Fei-Fei Li |
| @emilymbender | Emily M. Bender |

#### `sourcing/agent.py` — SourcingAgent

```python
# Traces: SRC-006–SRC-013 (sourcing agent), SRC-033–SRC-049 (tiers, dedup, storage),
#         SRC-148 (Twitter degradation), SRC-150 (run metrics)

class SourcingAgent:
    """
    Orchestrates web + Twitter sourcing for one agent configuration.
    Produces a SourcingRunResult for quality monitoring logging.
    """

    def __init__(
        self,
        config:  AgentConfig,
        secrets: RuntimeSecrets,
        store:   AbstractArticleStore,
        llm:     AbstractLLMClient,
        twitter: TwitterClient,
    ) -> None: ...

    def run(self, window_start: datetime, window_end: datetime) -> SourcingRunResult:
        """
        1. TwitterFetcher.fetch(window) → list[TweetSignal] + twitter_available flag.
           Store each TweetSignal via store.insert_tweet_signal() (SRC-067–069).
        2. For each configured tier in priority order (1a → 1b → 2 → 3 → 4):
             a. Build focused search queries per source domain.
             b. Call llm.search(query, budget_hint="normal") → list[SearchResult].
             c. For each result:
                  - normalize_url() → url_hash
                  - store.insert_if_new(article) → inserted (SRC-012)
                  - classify tier from config.sources
        3. Log and return SourcingRunResult (SRC-150).
        """


@dataclass
class SourcingRunResult:
    """Quality monitoring output from a sourcing run (SRC-150)."""
    agent_id:             str
    run_time:             datetime
    window_start:         datetime
    window_end:           datetime
    articles_considered:  int
    articles_inserted:    int       # new articles stored (not duplicates)
    articles_by_tier:     dict[str, int]   # {"1a": n, "1b": n, "2": n, "3": n, "4": n}
    twitter_available:    bool      # SRC-148
    tweet_signals_stored: int


def cli_main() -> None:
    """CLI entry point: ai-news-source --agent <id> --cadence <daily|weekly|...>"""
```

#### `sourcing/web_fetcher.py` — Web search strategies

```python
# Traces: SRC-053 (configurable fetch methods), SRC-060 (abstract tool use)

class WebFetcher:
    """
    Strategy-pattern web fetcher for each source tier.

    For each (source_domain, window):
    - Constructs a focused search query: site:{domain} AI news {date_range}
    - Calls AbstractLLMClient.search() which dispatches to the configured search tool
    - Returns normalized SearchResult list for dedup + storage

    The search tool is injected — LLM-native / Brave / Tavily all produce the same
    SearchResult interface (SRC-060).
    """

    def fetch_tier(
        self,
        tier:         str,
        domains:      list[str],
        window_start: datetime,
        window_end:   datetime,
        n_per_domain: int = 5,
    ) -> list[SearchResult]:
        """For each domain: build query, call llm.search(), collect results."""
```

#### `sourcing/twitter_fetcher.py` — Twitter adapter

```python
# Traces: SRC-062–SRC-070 (Twitter integration), SRC-148 (degradation)

class TwitterFetcher:
    """
    Thin adapter: calls TwitterClient.fetch_signals() and returns normalized output.
    Separation of concerns: SourcingAgent doesn't need to know about tweepy directly.
    """

    def fetch(
        self, window_start: datetime, window_end: datetime, agent_id: str
    ) -> tuple[list[TweetSignal], bool]:
        """Delegates to TwitterClient.fetch_signals(). Returns (signals, available)."""
```

---

### 3.6 `curation` — Curation Agent

**Source requirements:** SRC-014–SRC-032, SRC-047–SRC-049, SRC-054, SRC-070, SRC-112–SRC-131, SRC-150

#### Responsibilities
- Runs at cadence boundaries (SRC-028); re-runnable on user demand for any past window.
- Reads all candidate articles from the store for the lookback window.
- Builds a prompt with injected ISO dates, tiered candidates, and labeled Twitter signal.
- Calls the LLM (model selected per cadence via `cadence_overrides`) and parses structured output.
- Drops any item missing a working URL before scoring (SRC-049).
- Records the prompt file SHA-256 hash in all output metadata (SRC-129).
- Returns `CurationRunResult` + `DigestMetadata` for the rendering agent and quality monitoring.

#### Cadence behavior matrix

| Cadence | Window | Default Trigger | LLM Model | Output Focus |
|---------|--------|----------------|-----------|--------------|
| Daily | Prev day 00:00–23:59 UTC | 00:05 UTC daily | `gpt-4o` | Top N articles; headline + source + link + 2–3 sentence "why it matters" (SRC-029) |
| Weekly | Sun–Sat prior week | Sunday 01:00 UTC | `gpt-4o` | 2–3 themes; week outlook; top articles; intelligent summary (SRC-030) |
| Monthly | Month 1–last day (prior month) | 1st of month 02:00 UTC | `o3` (override) | Bigger-picture themes; anticipated news; top articles (SRC-031, SRC-054) |
| Annual | Full prior year | Jan 1st 03:00 UTC | `o3` + `thinking=True` | Top 10 articles; top 10 predictions grounded in observed trends with reasoning shown (SRC-032, SRC-054) |

#### Default curation criteria (SRC-022–SRC-027)

| Type | Criterion | Source |
|------|-----------|--------|
| ✅ Include | **Business impact** — changes how companies create value, compete, or operate | SRC-023 |
| ✅ Include | **Workforce/societal impact** — changes how people work, learn, or live | SRC-024 |
| ✅ Include | **Strategic/policy impact** — changes the rules of the game (legislation, geopolitics, safety incidents) | SRC-025 |
| ❌ Disqualify (default only) | **Technical depth** — tutorials, architecture papers, benchmarks, framework comparisons, code walkthroughs | SRC-026 |

All prioritization is executed through the LLM — not rule-based heuristics (SRC-027).

#### `curation/agent.py` — CurationAgent

```python
# Traces: SRC-014–SRC-032 (curation cadences), SRC-049 (URL enforcement),
#         SRC-054 (research LLM), SRC-129 (prompt version), SRC-150 (metrics)

class CurationAgent:
    """
    Orchestrates LLM-based curation for one agent config and cadence.
    The LLM provider is injected — never instantiated directly (SRC-055–SRC-057).
    """

    def run(
        self,
        cadence:      Literal["daily", "weekly", "monthly", "annual"],
        window_start: datetime,
        window_end:   datetime,
    ) -> CurationRunResult:
        """
        1. Determine LLM model + thinking flag for this cadence (SRC-054):
             daily/weekly: config.llm.model (default gpt-4o)
             monthly:      cadence_overrides["monthly"].model (e.g. o3)
             annual:       cadence_overrides["annual"].model + thinking=True (SRC-032)
        2. store.get_window(agent_id, window) → articles + tweet signals.
        3. PromptBuilder.build() → (prompt_text, sha256_hash) (SRC-115–SRC-124).
        4. llm.complete(prompt, model, thinking=...) → raw response string (SRC-027).
        5. llm.parse_structured(raw, CurationResponse) → CurationResponse (SRC-061).
        6. Drop items with url=None or url="" (SRC-049) — log count.
        7. Scorer.rank(items, top_n) → final ranked list.
        8. Build CurationRunResult + DigestMetadata (SRC-150).
        """


@dataclass
class CurationRunResult:
    """Output of a curation run — consumed by RenderingAgent."""
    agent_id:       str
    cadence:        str
    run_date:       date
    window_start:   datetime
    window_end:     datetime
    items:          list[CuratedItem]
    themes:         list[str]        # weekly/monthly/annual theme labels
    outlook:        str              # weekly/monthly forward-looking paragraph
    predictions:    list[str]        # annual only (SRC-032)
    narrative_md:   str              # raw Markdown narrative from LLM (portal display)
    meta:           DigestMetadata
```

#### `curation/prompt_builder.py` — Prompt assembly

```python
# Traces: SRC-115–SRC-124 (prompt structure requirements), SRC-116 (ISO dates),
#         SRC-119 (Twitter signal section), SRC-120 (output format), SRC-121 (search budget),
#         SRC-129 (prompt SHA-256 hash)

class PromptBuilder:
    """
    Builds the final curation prompt by injecting dynamic context into a
    versioned template file. Satisfies all 9 prompt structure requirements (SRC-115–SRC-123)
    plus annual-specific requirements (SRC-124).
    """

    def build(
        self,
        template_path:     str | Path,
        cadence:           str,
        window_start:      datetime,
        window_end:        datetime,
        articles:          list[ArticleRecord],
        tweet_signals:     list[TweetSignal],
        config:            AgentConfig,
        twitter_available: bool,
    ) -> tuple[str, str]:   # (final_prompt_text, prompt_sha256_hash)
        """
        Injections performed:
        - {{cadence_label}}           — "Daily" | "Weekly" | "Monthly" | "Annual"
        - {{window_start_iso}}        — concrete ISO-8601 date (SRC-116; never relative)
        - {{window_end_iso}}          — concrete ISO-8601 date
        - {{tier_1a_articles}} etc.   — sorted by tier (1a → 1b → 2 → 3 → 4)
        - {{twitter_signal_section}}  — labeled signal context or degradation note (SRC-119, SRC-148)
        - {{search_budget_directive}} — search budget scaled to cadence (SRC-121)
        - {{top_n}}                   — from config.limits for this cadence
        - (annual only) {{year}}, {{year_plus_1}} — for predictions section (SRC-124)

        Returns (assembled_prompt, sha256_hash_of_template_file).
        The sha256 hash is embedded in all digest outputs for regression tracing (SRC-129).
        """

    @staticmethod
    def sha256_of_file(path: str | Path) -> str:
        """SHA-256 of the prompt template file bytes. (SRC-129)"""
        return "sha256:" + hashlib.sha256(Path(path).read_bytes()).hexdigest()
```

#### `curation/scorer.py` — Post-LLM scoring

```python
# Traces: SRC-027 (LLM as primary scoring), SRC-049 (URL enforcement)

class Scorer:
    """
    After LLM response is parsed, applies tier-weighted re-ranking as a deterministic
    safety net. LLM scoring is the primary intelligence path (SRC-027); this is a
    lightweight structural adjustment to account for tier signal.

    Tier weights: 1a=1.5, 1b=1.3, 2=1.1, 3=1.0, 4=0.9
    Final score = llm_rank_score * tier_weight
    Items sorted descending; top_n selected.
    Items with url=None or url="" are removed before ranking (SRC-049).
    """

    def rank(self, items: list[CuratedItem], top_n: int) -> list[CuratedItem]:
        """Remove no-URL items, apply tier weighting, return top_n."""
```

---

### 3.7 `rendering` — Rendering Agent

**Source requirements:** SRC-004, SRC-048–SRC-049, SRC-120, SRC-135–SRC-141, SRC-145

#### Responsibilities
- Receives a `CurationRunResult` and writes three output files per run (SRC-004, SRC-136).
- Enforces URL presence as the **final safety check** — items without a `url` field are **dropped, not truncated** (SRC-141).
- Date-stamped filenames make re-runs idempotent — overwrites clean (SRC-145).

#### Output file naming convention

```
outputs/{agent_id}/{YYYY-MM-DD}-{cadence}.md
outputs/{agent_id}/{YYYY-MM-DD}-{cadence}.html
outputs/{agent_id}/{YYYY-MM-DD}-{cadence}.json
```

Examples:
- `outputs/default/2026-05-10-daily.md`
- `outputs/default/2026-05-10-daily.html`
- `outputs/default/2026-05-10-daily.json`
- `outputs/default/2026-01-01-annual.json`

#### `rendering/agent.py` — RenderingAgent

```python
# Traces: SRC-004 (three output formats), SRC-141 (URL drop enforcement),
#         SRC-145 (idempotent date-stamped filenames)

class RenderingAgent:
    def __init__(self, config: AgentConfig) -> None: ...

    def run(self, result: CurationRunResult) -> RenderingRunResult:
        """
        1. FINAL URL enforcement: filter items where item.url is None or "".
           Log count of dropped items. (SRC-141)
        2. Compute output_dir = config.output_dir
        3. Compute base filename: {result.run_date:%Y-%m-%d}-{result.cadence}
        4. markdown_renderer.render(items, meta) → write .md (SRC-138)
        5. html_renderer.render(items, meta) → write .html (SRC-137)
        6. json_renderer.render(items, meta) → write .json (SRC-140)
        7. Return RenderingRunResult with paths (portal uses these for downloads).
        """


@dataclass
class RenderingRunResult:
    """Paths to rendered files for this run."""
    md_path:   Path
    html_path: Path
    json_path: Path
    items_dropped_no_url: int   # SRC-141 — auditable
```

#### `rendering/markdown_renderer.py` — Markdown output

```python
# Traces: SRC-004 (MD format), SRC-138 (Slack/Teams paste-ready)

def render(items: list[CuratedItem], meta: DigestMetadata) -> str:
    """
    Produces Slack/Teams paste-ready Markdown.

    Daily format:
      # AI News Digest — {date}
      *{agent_id} · {n} stories · daily*

      ## 1. {headline}
      **Source:** [{source_name}]({url}) · {pub_date} · Tier {tier}
      **Why it matters:** {why_it_matters}
      **Impact:** {impact_tags joined}
      [Optional] Twitter signal: @{twitter_handle} — {tweet_url}

      ---

    Weekly/monthly: adds ## Themes section + ## What to Watch Next.
    Annual: adds ## Predictions for {year+1} with numbered prediction cards.
    Footer: prompt_version sha256 hash for regression tracing (SRC-129).
    """
```

#### `rendering/html_renderer.py` — HTML output

```python
# Traces: SRC-004 (HTML format), SRC-137 (email-client paste-ready)

def render(items: list[CuratedItem], meta: DigestMetadata) -> str:
    """
    Produces self-contained HTML with inline CSS (email-client compatible).
    - No external stylesheets or CDN dependencies (inline styles throughout).
    - Article cards with clickable headline anchors (every URL required — SRC-049).
    - Impact tag badges (colored inline spans).
    - Weekly/monthly: theme grouping sections.
    - Annual: numbered prediction cards + year-in-review banner.
    - Footer: prompt version, run timestamp, twitter_signal_available flag.
    """
```

#### `rendering/json_renderer.py` — JSON output

```python
# Traces: SRC-004 (JSON format), SRC-140 (machine-readable archive)

def render(items: list[CuratedItem], meta: DigestMetadata) -> str:
    """
    Produces machine-readable JSON archive. Schema:
    {
      "meta": {
        "agent_id":                 str,
        "cadence":                  str,
        "run_date":                 "YYYY-MM-DD",
        "window_start":             "ISO-8601",
        "window_end":               "ISO-8601",
        "prompt_version":           "sha256:<64-char hex>",
        "llm_provider":             str,
        "llm_model":                str,
        "items_considered":         int,
        "items_included":           int,
        "items_by_tier":            {"1a": n, "1b": n, "2": n, "3": n, "4": n},
        "items_by_source_class":    {"web": n, "twitter": n},
        "twitter_signal_available": bool,
        "tweet_api_call_count":     int,
        "token_usage":              int
      },
      "themes":      ["theme1", ...],
      "outlook":     "...",
      "predictions": ["...", ...],
      "items": [
        {
          "headline":       str,
          "source_name":    str,
          "url":            str,
          "pub_date":       "YYYY-MM-DD",
          "why_it_matters": str,
          "impact_tags":    ["business_impact", ...],
          "tier":           "1b",
          "cross_refs":     ["url", ...],
          "twitter_handle": str | null,
          "tweet_url":      str | null,
          "prompt_version": "sha256:..."
        }
      ]
    }
    """
```

---

### 3.8 `scheduler` — Multi-Agent Scheduler

**Source requirements:** SRC-009, SRC-028–SRC-032, SRC-052, SRC-072, SRC-144, SRC-147–SRC-148

#### Design

The scheduler is the **entry point for automated operation**. It reads `configs/scheduler.yaml` at startup, discovers all enabled agent configurations, and registers independent APScheduler jobs for each agent's sourcing and curation cadences.

**Library:** `apscheduler >= 3.10.4` (AsyncIOScheduler) — cron-expression support, misfire handling, Python-native, cloud-scheduler-replaceable.

Key design properties:
- Each agent runs its full `sourcing → curation → rendering` pipeline **independently** (SRC-072).
- Outputs land in agent-scoped directories (`outputs/{agent_id}/`) — no cross-agent collisions.
- The same scheduler code works locally (APScheduler) and in cloud (cloud schedulers invoke the CLI entry points directly).

#### Multi-agent scheduling behavior (SRC-072)

The root `configs/scheduler.yaml` lists all agent configs. At startup the scheduler:

1. Reads `configs/scheduler.yaml` → discovers all `agents` entries.
2. For each `enabled: true` agent: loads the per-agent YAML from `config:` path.
3. Instantiates an independent agent graph: `SourcingAgent + CurationAgent + RenderingAgent`.
4. Registers all five cadence jobs scoped to that agent.

Different agents (with different `agent_id` values) produce independent outputs in `outputs/{agent_id}/` and appear as separate options in the portal's agent switcher. Adding a new agent requires only: (1) create a new YAML file, (2) add an entry to `scheduler.yaml`.

#### `scheduler/runner.py` — AgentScheduler

```python
# Traces: SRC-009 (daily sourcing), SRC-028–SRC-032 (cadence triggers),
#         SRC-052 (scheduler), SRC-072 (multi-agent), SRC-144 (retry),
#         SRC-147 (manual override endpoint)

from apscheduler.schedulers.asyncio import AsyncIOScheduler

class AgentScheduler:
    """
    Reads scheduler.yaml → registers APScheduler jobs for each enabled agent.
    Each agent has independent trigger times; different agents can run on different
    models, prompts, and sources without code changes. (SRC-072)
    """

    def __init__(self, scheduler_config: SchedulerConfig, secrets: RuntimeSecrets) -> None:
        self._sched   = AsyncIOScheduler()
        self._config  = scheduler_config
        self._secrets = secrets

    def register_jobs(self) -> None:
        """
        For each agent in scheduler_config.agents (where enabled=True):
          - sourcing_daily job  @ triggers.sourcing_daily  cron  (SRC-009)
          - curation_daily job  @ triggers.curation_daily  cron  (SRC-029)
          - curation_weekly job @ triggers.curation_weekly cron  (SRC-030)
          - curation_monthly job@ triggers.curation_monthly cron (SRC-031)
          - curation_annual job @ triggers.curation_annual cron  (SRC-032)

        Retry policy applied to each job (SRC-144):
          max_instances=1, misfire_grace_time=300
          On failure: caught in _run_*; structlog CRITICAL + retry via APScheduler
          Exponential backoff: 30s → 60s → 120s (3 attempts max)
        """

    async def _run_sourcing(self, agent_id: str, config_path: str) -> None:
        """Load agent config, wire agent graph, run sourcing for today's window."""

    async def _run_curation(
        self, agent_id: str, config_path: str,
        cadence: str, window_start: datetime, window_end: datetime,
    ) -> None:
        """Load agent config, run curation → rendering pipeline."""

    async def trigger_on_demand(
        self,
        agent_id:     str,
        job_type:     Literal["sourcing", "curation"],
        cadence:      str | None = None,
        window_start: str | None = None,
        window_end:   str | None = None,
    ) -> dict:
        """
        Manual override endpoint handler (SRC-147).
        Called by POST /api/trigger — useful for backfills and misfire recovery.
        """


def cli_main() -> None:
    """
    CLI entry point: ai-news-schedule
    Loads scheduler.yaml, registers all jobs, starts APScheduler event loop.
    Also exposes POST /api/trigger (SRC-147) on scheduler.api.port.
    """
```

#### Retry and reliability (SRC-144)

APScheduler job failure sequence:
```
Attempt 1 → fail → wait 30s  (backoff_base_seconds × 2^0)
Attempt 2 → fail → wait 60s  (backoff_base_seconds × 2^1)
Attempt 3 → fail → wait 120s (backoff_base_seconds × 2^2)
→ structlog CRITICAL event emitted (captured by cloud-native logging alert — SRC-146)
```

#### Manual override (SRC-147)

The scheduler exposes a minimal HTTP endpoint on `scheduler.api.port` (default 8081):

```
POST /api/trigger
Content-Type: application/json
Authorization: Bearer {SCHEDULER_API_KEY}   ← from env var (SRC-073)

{
  "agent_id":     "default",
  "job_type":     "curation",
  "cadence":      "weekly",
  "window_start": "2026-05-03",
  "window_end":   "2026-05-09"
}
```

Used for: backfills, misfire recovery, prompt iteration testing.

---

### 3.9 `portal` — Web Portal

**Source requirements:** SRC-004, SRC-133–SRC-134, SRC-136, SRC-140–SRC-141

#### Stack
- **FastAPI** — async Python web framework; serves portal routes and the manual-override trigger API.
- **Jinja2** — server-side HTML templates; theme-aware rendering per cadence.
- **Static files** — minimal hand-crafted CSS + vanilla JS (tag cloud, filter, download triggers).
- **No authentication in v1** (SRC-134) — portal is publicly read-only.
- **No portal-driven configuration in v1** (SRC-072) — configuration is YAML-only; future enhancement.

#### `portal/app.py`

```python
# Traces: SRC-133 (web portal), SRC-134 (no auth in v1), SRC-147 (manual trigger API)

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

def create_app() -> FastAPI:
    app = FastAPI(
        title="AI News Portal",
        description="AI News Curation Agent — Web Portal",
        docs_url=None,   # disable Swagger in production
    )
    app.mount("/static", StaticFiles(directory="src/ai_news_agent/portal/static"), name="static")
    app.include_router(portal_router)
    return app

app = create_app()

def cli_main() -> None:
    """CLI entry point: ai-news-portal"""
    uvicorn.run("ai_news_agent.portal.app:app", host="0.0.0.0", port=8080, workers=1)
```

#### `portal/routes.py` — Route definitions

```
GET  /
    → index.html: list all available digests grouped by agent + cadence;
      agent config switcher (display-only); most-recent digest highlighted

GET  /digest/{agent_id}/{date}/{cadence}
    → cadence-specific template:
        daily   → daily.html   (article card list + why-it-matters + impact tags)
        weekly  → weekly.html  (theme section + top articles + week outlook)
        monthly → monthly.html (big-picture themes + anticipated news)
        annual  → annual.html  (top 10 articles + predictions + tag cloud)

GET  /download/{agent_id}/{date}/{cadence}/{fmt}
    → Serve .md / .html / .json file as download (SRC-136)
    → fmt: "md" | "html" | "json"
    → Returns 404 if file not found; 200 with Content-Disposition: attachment

POST /api/trigger
    → Manual scheduler override (SRC-147)
    → Authenticated by SCHEDULER_API_KEY header (env var; SRC-073)
    → Body: {"agent_id", "job_type", "cadence", "window_start", "window_end"}
```

#### Template responsibilities (SRC-134)

| Template | Cadence | Content Focus |
|----------|---------|---------------|
| `base.html` | All | Layout, nav, agent config switcher dropdown, download bar scaffold |
| `index.html` | Landing | All digests grouped by agent + cadence; most-recent digest at top; per-agent archive |
| `daily.html` | Daily | Article card list — headline, source badge (tier chip), link, why-it-matters, impact tag chips |
| `weekly.html` | Weekly | Theme section (word/tag cloud from impact_tags + theme labels); top article cards; outlook paragraph |
| `monthly.html` | Monthly | Big-picture theme section; anticipated news sidebar; top article cards; download bar |
| `annual.html` | Annual | Year-in-review banner; top 10 article cards; predictions cards (numbered, with reasoning); full lookback navigation |

#### Visual theme features (SRC-134)

- **Tag cloud / word cloud** (JS): generated from `impact_tags` across all items; clickable to filter displayed articles.
- **Source tier filter** (JS): toggle to show/hide articles by tier (e.g., Tier 1 only).
- **Provider/model indicator**: badge showing LLM model used for this run.
- **Agent config switcher** (nav): dropdown to switch between configured agent views — display-only in v1.
- **Download bar**: `.md` / `.html` / `.json` download buttons visible on every digest page (SRC-136).
- **Twitter degradation banner**: shown when `twitter_signal_available=false` (SRC-148).

---

## 4. Per-Agent YAML Configuration Schema

**Source requirements:** SRC-017–SRC-021, SRC-029–SRC-032, SRC-036–SRC-046, SRC-053–SRC-054, SRC-057, SRC-071–SRC-073

### 4.1 Full annotated `configs/default-agent.yaml`

```yaml
# configs/default-agent.yaml
# Traces: SRC-046 (configurable handles), SRC-071–SRC-073 (runtime config without code changes),
#         SRC-017–SRC-021 (source tiers), SRC-029–SRC-032 (output limits per cadence),
#         SRC-054 (research LLM for monthly/annual), SRC-057 (provider selection)
#
# SECRETS: NEVER place API keys or tokens here.
# Use environment variables: OPENAI_API_KEY, TWITTER_BEARER_TOKEN, WEB_SEARCH_API_KEY
# (SRC-073, SRC-111)

agent_id: default

# ─────────────────────────────────────────────────────────────────────────────
# LLM configuration (SRC-057, SRC-054)
# ─────────────────────────────────────────────────────────────────────────────
llm:
  provider: openai                  # "openai" | "anthropic" (SRC-057)
  model: gpt-4o                     # Default for daily + weekly runs

  # Per-cadence model overrides (SRC-054)
  cadence_overrides:
    monthly:
      model: o3                     # Research-grade model (SRC-054)
      thinking: false
    annual:
      model: o3
      thinking: true                # Extended thinking enabled (SRC-032)

# Path to the prompt template. Cadence-specific file resolved at runtime:
# daily → prompts/daily.md, weekly → prompts/weekly.md, etc. (SRC-113)
curation_prompt: prompts/daily.md

# ─────────────────────────────────────────────────────────────────────────────
# Source tier configuration (SRC-016–SRC-021, SRC-034)
# ─────────────────────────────────────────────────────────────────────────────
sources:
  custom: []                        # Tier 1a — optional user sources (SRC-017)

  tier_1b:                          # Popular business press (SRC-018)
    - reuters.com
    - bloomberg.com
    - wsj.com
    - ft.com
    - economist.com
    - axios.com

  tier_2:                           # Top tech and AI blogs (SRC-019)
    - news.ycombinator.com
    - techblog.netflix.com
    - anthropic.com
    - openai.com
    - huggingface.co
    - towardsai.net

  tier_3:                           # Tech business press (SRC-020)
    - theinformation.com
    - stratechery.com
    - platformer.news
    - techcrunch.com
    - theverge.com
    - technologyreview.com
    - wired.com
    - fastcompany.com

  tier_4:                           # Policy and research (SRC-021)
    - brookings.edu
    - rand.org
    - hai.stanford.edu
    - ainowresearch.org

# ─────────────────────────────────────────────────────────────────────────────
# Twitter/X influencer configuration (SRC-036–SRC-046)
# Add, remove, or re-weight handles without code changes (SRC-046)
# ─────────────────────────────────────────────────────────────────────────────
twitter:
  enabled: true
  handles:
    - { handle: karpathy,      weight: 1.0 }   # Andrej Karpathy   (SRC-037)
    - { handle: sama,          weight: 1.0 }   # Sam Altman        (SRC-038)
    - { handle: demishassabis, weight: 1.0 }   # Demis Hassabis    (SRC-039)
    - { handle: DarioAmodei,   weight: 1.0 }   # Dario Amodei      (SRC-040)
    - { handle: ylecun,        weight: 1.0 }   # Yann LeCun        (SRC-041)
    - { handle: AndrewYNg,     weight: 1.0 }   # Andrew Ng         (SRC-042)
    - { handle: fchollet,      weight: 1.0 }   # François Chollet  (SRC-043)
    - { handle: drfeifei,      weight: 1.0 }   # Fei-Fei Li        (SRC-044)
    - { handle: emilymbender,  weight: 1.0 }   # Emily M. Bender   (SRC-045)

# ─────────────────────────────────────────────────────────────────────────────
# Top-N article limits per cadence (SRC-029–SRC-032)
# ─────────────────────────────────────────────────────────────────────────────
limits:
  daily_top_n:   10    # SRC-029
  weekly_top_n:  7     # SRC-030
  monthly_top_n: 10    # SRC-031
  annual_top_n:  10    # SRC-032

# Base output directory (SRC-145)
output_dir: outputs/default
```

### 4.2 Root `configs/scheduler.yaml` — multi-agent registry

```yaml
# configs/scheduler.yaml
# Traces: SRC-052 (scheduler), SRC-072 (per-agent configs), SRC-144 (retry), SRC-147 (API)
# SECRETS: Never here — env vars only. (SRC-073)

scheduler:
  max_retries: 3
  retry_backoff_base_seconds: 30   # exponential: 30s → 60s → 120s (SRC-144)

api:
  enabled: true                    # manual override endpoint (SRC-147)
  host: "0.0.0.0"
  port: 8081

triggers:                          # cron expressions (UTC)
  sourcing_daily:    "0 0 * * *"   # 00:00 UTC — daily sourcing (SRC-009)
  curation_daily:    "5 0 * * *"   # 00:05 UTC — daily curation (SRC-029)
  curation_weekly:   "0 1 * * 0"   # 01:00 UTC Sunday — weekly (SRC-030)
  curation_monthly:  "0 2 1 * *"   # 02:00 UTC 1st of month (SRC-031)
  curation_annual:   "0 3 1 1 *"   # 03:00 UTC January 1st (SRC-032)

agents:                            # (SRC-072)
  - id: default
    config: configs/default-agent.yaml
    enabled: true
    description: "Default business + society AI news curation"

  - id: technical
    config: configs/example-technical-agent.yaml
    enabled: false                 # Disabled by default — enable when ready
    description: "Technical AI developments curation (example alternate theme)"
```

### 4.3 Adding a new agent (SRC-072)

To add a new agent configuration:
1. Create a new YAML file (e.g., `configs/my-agent.yaml`) following the schema above.
2. Add an entry to `configs/scheduler.yaml` under `agents:`.
3. Set `enabled: true` when ready to run.
4. The new agent will appear in the portal's agent switcher automatically.

No code changes required.

---

## 5. Prompt Versioning System

**Source requirements:** SRC-059, SRC-113, SRC-115–SRC-131

### 5.1 File location and ownership

```
prompts/
├── daily.md      # Daily curation prompt template (SRC-029, SRC-115–SRC-123)
├── weekly.md     # Weekly curation prompt template (SRC-030, SRC-115–SRC-123)
├── monthly.md    # Monthly curation prompt — research LLM (SRC-031, SRC-054)
└── annual.md     # Annual prompt — predictions + inflection points (SRC-032, SRC-124)
```

All prompt files are **versioned in source control alongside code** (SRC-127). Prompt changes:
- Require code review with at least one reviewer beyond the author (SRC-128).
- Expected to be iterated frequently in the first 4–6 weeks; then quarterly (SRC-130).
- Must not use provider-specific formatting tricks — any frontier model must execute them with at most minor tuning (SRC-059).
- A more formal owner can be designated later once usage patterns settle (SRC-131).

### 5.2 Required sections in every prompt template (SRC-115–SRC-123)

Every prompt template **must** contain all of the following:

| # | Section / Injection | Requirement |
|---|---------------------|-------------|
| 1 | Concrete ISO date range (`{{window_start_iso}}` – `{{window_end_iso}}`) | SRC-116 — never "last week" |
| 2 | Explicit exclusion list (tutorials, architecture papers, benchmarks, framework comparisons, code walkthroughs) | SRC-117 |
| 3 | Inclusion criteria with examples (business impact, workforce/societal impact, strategic/policy impact) | SRC-118 |
| 4 | Labeled Twitter influencer signal section — clearly marked as context/lead-gen; not primary citation unless tweet IS the news | SRC-119 |
| 5 | Strict output format constraint — Markdown + embedded ```json block; enables deterministic parsing | SRC-120 |
| 6 | Search budget directive scaled to cadence — more for monthly/annual | SRC-121 |
| 7 | "Why it matters" justification required per item (2–3 sentences) | SRC-122 |
| 8 | Working source URL required per item — items without verifiable URL must be omitted | SRC-123 |
| 9 | (**Annual only**) Identify themes/inflection points across the year; produce 10 predictions grounded in observed trends with reasoning shown | SRC-124 |

### 5.3 Prompt template structure (annotated)

```markdown
# AI News Digest — {{cadence_label}} Curation

## Time Window  (SRC-116)
You are curating AI news for the period: **{{window_start_iso}} to {{window_end_iso}}**.
Use these exact dates. Do not use relative phrases like "last week".

## Inclusion Criteria  (SRC-118)
Include items demonstrating at least one of:
- **Business impact:** changes how companies create value, compete, or operate...
- **Workforce/societal impact:** changes how people work, learn, or live...
- **Strategic/policy impact:** changes the rules of the game...

## Exclusion Criteria  (SRC-117)
Exclude items whose primary content is:
- Implementation tutorials or coding walkthroughs
- Model architecture papers or benchmark deep-dives
- Framework comparisons or code-only content

## Candidate Articles  (tiered list injected by PromptBuilder)
**Tier 1a — User-configured priority sources:**
{{tier_1a_articles}}
[... tiers 1b through 4 ...]

## Influencer Signal — For Context and Lead Generation Only  (SRC-119)
{{twitter_signal_section}}
[If unavailable: "Twitter influencer signal was unavailable for this run."]

## Search Budget  (SRC-121)
{{search_budget_directive}}
[daily: up to 5 searches; monthly/annual: up to 25–30 searches]

## Output Requirements  (SRC-120, SRC-122, SRC-123)
Select the top {{top_n}} items. For EACH item provide:
- "why it matters" (2–3 sentences mandatory)
- working source URL (mandatory; items without verifiable URL must be OMITTED)

[Markdown narrative]

```json
{
  "items": [{
    "headline": "...", "source_name": "...", "url": "...", "pub_date": "YYYY-MM-DD",
    "why_it_matters": "...", "impact_tags": [...], "tier": "1b",
    "cross_refs": [], "twitter_handle": null, "tweet_url": null
  }],
  "themes": ["..."],
  "outlook": "...",
  "predictions": []
}
```
```

**Annual-only additional section (SRC-124):**
```markdown
## Annual Analysis Requirements
1. Identify 3–5 major inflection points that defined {{year}}.
2. Produce a "Top 10 Predictions for {{year_plus_1}}" section in the JSON "predictions" array.
   Each prediction MUST be grounded in specific observed trends from {{year}} with reasoning shown.
   Be punchy, impactful, and well-argued.
```

### 5.4 Version tracking (SRC-129)

```python
import hashlib
from pathlib import Path

def sha256_of_file(path: str | Path) -> str:
    """Return 'sha256:<hex>' of the prompt template file bytes."""
    return "sha256:" + hashlib.sha256(Path(path).read_bytes()).hexdigest()
```

The SHA-256 hash of the prompt template file is:
1. Embedded in every `CuratedItem.prompt_version` field.
2. Included in `DigestMetadata.prompt_version` (present in all three output formats).
3. Logged in the structlog `run_complete` event alongside `llm_model` and `llm_provider` (SRC-150).

This enables regression tracing: if digest quality drops, the exact prompt version active at the time is identifiable from the output file itself — without any additional lookup.

---

## 6. Data Models and Schemas

**Source requirements:** SRC-011, SRC-048, SRC-129, SRC-150

### 6.1 ArticleRecord (storage layer — `storage/models.py`)

```python
# Traces: SRC-011 (storage fields), SRC-012 (url_hash dedup key), SRC-048 (twitter fields)

@dataclass
class ArticleRecord:
    # Primary identifier
    url_hash:       str       # SHA-256 of normalized_url — dedup key (SRC-012)
    url:            str       # canonical URL — required; SRC-049 enforced at curation + rendering
    headline:       str
    abstract:       str | None
    source_name:    str
    pub_date:       datetime
    fetched_at:     datetime  # when sourcing agent retrieved it
    tier:           str       # "1a" | "1b" | "2" | "3" | "4"
    source_class:   str       # "web" | "twitter"  (SRC-150 quality monitoring)
    agent_id:       str       # scopes record to this agent config (SRC-072)

    # Twitter provenance (present only when source_class = "twitter") (SRC-048)
    twitter_handle: str | None = None
    tweet_url:      str | None = None
```

### 6.2 TweetSignal (storage layer — `storage/models.py`)

```python
# Traces: SRC-047 (influencer signal role), SRC-067–SRC-069 (fetch/filter/hydrate)

@dataclass
class TweetSignal:
    tweet_id:     str
    handle:       str
    text:         str
    created_at:   datetime
    linked_urls:  list[str]   # hydrated expanded URLs from t.co (SRC-069)
    agent_id:     str
    fetched_at:   datetime
    weight:       float = 1.0  # handle weight from config (SRC-046)
```

### 6.3 CuratedItem (curation → rendering → portal — `storage/models.py`)

```python
# Traces: SRC-048 (curated item schema), SRC-049 (URL required), SRC-129 (prompt_version)

@dataclass
class CuratedItem:
    headline:       str
    source_name:    str
    url:            str           # REQUIRED — empty/None = item dropped (SRC-049, SRC-141)
    pub_date:       date
    why_it_matters: str           # 2–3 sentences (SRC-048, SRC-122)
    impact_tags:    list[str]     # "business_impact" | "workforce_impact" | "policy_impact"
    tier:           str           # "1a" | "1b" | "2" | "3" | "4"
    cross_refs:     list[str]     # related item URLs (SRC-048)
    twitter_handle: str | None    # SRC-048 — null if web-sourced
    tweet_url:      str | None    # SRC-048 — null if web-sourced
    prompt_version: str           # "sha256:<64-char hex>" (SRC-129)
```

### 6.4 DigestMetadata (attached to every output — `storage/models.py`)

```python
# Traces: SRC-129 (prompt_version), SRC-148 (twitter_signal_available),
#         SRC-150 (all monitoring fields)

@dataclass
class DigestMetadata:
    agent_id:                  str
    cadence:                   str      # "daily" | "weekly" | "monthly" | "annual"
    run_date:                  date
    window_start:              datetime
    window_end:                datetime
    prompt_version:            str      # SRC-129 — "sha256:<hex>"
    llm_provider:              str      # SRC-150
    llm_model:                 str      # SRC-150
    items_considered:          int      # SRC-150 — total candidates from store
    items_included:            int      # SRC-150 — after LLM selection + URL drop
    items_by_tier:             dict[str, int]   # SRC-150 — {"1a": n, "1b": n, ...}
    items_by_source_class:     dict[str, int]   # SRC-150 — {"web": n, "twitter": n}
    twitter_signal_available:  bool     # SRC-148
    tweet_api_call_count:      int      # SRC-150 — 0 if degraded
    token_usage:               int      # SRC-150 — total tokens consumed
```

### 6.5 CurationResponse (LLM output schema — parsed by `parse_structured`)

```python
# Traces: SRC-061 (output parsing from plain text), SRC-120 (output format constraint)

class CurationResponse(BaseModel):
    """Schema the LLM is instructed to produce in the ```json block."""
    items:       list[CuratedItemRaw]
    themes:      list[str] = []
    outlook:     str = ""
    predictions: list[str] = []   # annual only (SRC-124)
```

---

## 7. Agent Data Flow (End-to-End)

```
┌─────────────────────────────────────────────────────────────────────────┐
│                    Scheduler (scheduler/runner.py)                       │
│  Reads configs/scheduler.yaml → discovers all enabled agent configs      │
│  Registers independent APScheduler jobs per agent_id (SRC-072)           │
│                                                                          │
│  sourcing_daily    → 00:00 UTC every day         (SRC-009, SRC-052)     │
│  curation_daily    → 00:05 UTC every day         (SRC-029)              │
│  curation_weekly   → 01:00 UTC Sunday            (SRC-030)              │
│  curation_monthly  → 02:00 UTC 1st of month      (SRC-031)              │
│  curation_annual   → 03:00 UTC January 1st       (SRC-032)              │
│                                                                          │
│  Retry: 3× exponential backoff 30→60→120s (SRC-144)                     │
│  Manual override: POST /api/trigger (SRC-147)                            │
└──────────────────────────┬──────────────────────────────────────────────┘
                           │ sourcing trigger (per agent config)
                           ▼
┌─────────────────────────────────────────────────────────────────────────┐
│                  Sourcing Agent (sourcing/agent.py)                      │
│  SRC-006–SRC-013, SRC-033–SRC-053, SRC-148, SRC-150                    │
│                                                                          │
│  1. TwitterFetcher.fetch(window) → list[TweetSignal]                    │
│     → graceful degrade if API unavailable (SRC-148)                     │
│  2. For each tier (1a→1b→2→3→4):                                        │
│       WebFetcher.fetch_tier(domains, window) → list[SearchResult]        │
│       via LLM-native / Brave / Tavily search (SRC-060)                  │
│  3. For each result:                                                     │
│       normalize_url → url_hash → store.insert_if_new() (SRC-012)       │
│  4. store.insert_tweet_signal() for each TweetSignal                    │
│  5. Log SourcingRunResult (SRC-150)                                      │
└──────────────────────────┬──────────────────────────────────────────────┘
                           │ ArticleRecords + TweetSignals stored in TinyDB
                           │ (curation trigger fires at cadence boundary)
                           ▼
┌─────────────────────────────────────────────────────────────────────────┐
│           Document Store — TinyDB (storage/tinydb_store.py)             │
│  SRC-011–SRC-012, SRC-053                                               │
│  File: outputs/{agent_id}/store.json                                    │
│  Tables: 'articles' (keyed url_hash+agent_id), 'tweets' (tweet_id)     │
│  Shared read/write by both Sourcing and Curation Agents                 │
└──────────────────────────┬──────────────────────────────────────────────┘
                           │ curation trigger
                           ▼
┌─────────────────────────────────────────────────────────────────────────┐
│                Curation Agent (curation/agent.py)                        │
│  SRC-014–SRC-032, SRC-047–SRC-049, SRC-054, SRC-112–SRC-131            │
│                                                                          │
│  1. store.get_window(agent_id, window) → articles + tweet signals       │
│  2. Determine model: cadence_overrides or default (SRC-054)             │
│  3. PromptBuilder.build(template, articles, signals, dates, config)     │
│     → (prompt_text, sha256_hash) (SRC-115–SRC-124)                     │
│  4. llm.complete(prompt, model, thinking=...) → raw string (SRC-027)    │
│  5. llm.parse_structured(raw, CurationResponse) (SRC-061)               │
│  6. Drop items with url=None (SRC-049) → log dropped count              │
│  7. Scorer.rank(items, top_n) → final ranked list                       │
│  8. Build CurationRunResult + DigestMetadata (SRC-150)                   │
└──────────────────────────┬──────────────────────────────────────────────┘
                           │ CurationRunResult
                           ▼
┌─────────────────────────────────────────────────────────────────────────┐
│               Rendering Agent (rendering/agent.py)                       │
│  SRC-004, SRC-135–SRC-141, SRC-145                                      │
│                                                                          │
│  1. FINAL URL enforcement: drop items where url is None/empty (SRC-141) │
│  2. markdown_renderer.render() →                                        │
│       outputs/{agent_id}/{YYYY-MM-DD}-{cadence}.md   (SRC-138)         │
│  3. html_renderer.render() →                                            │
│       outputs/{agent_id}/{YYYY-MM-DD}-{cadence}.html (SRC-137)         │
│  4. json_renderer.render() →                                            │
│       outputs/{agent_id}/{YYYY-MM-DD}-{cadence}.json (SRC-140)         │
│  Date-stamped filenames = idempotent re-runs (SRC-145)                  │
└──────────────────────────┬──────────────────────────────────────────────┘
                           │ rendered files on disk
                           ▼
┌─────────────────────────────────────────────────────────────────────────┐
│                      Web Portal (portal/)                                │
│  SRC-004, SRC-133–SRC-134, SRC-136                                      │
│  FastAPI + Jinja2                                                        │
│                                                                          │
│  GET /                     → index.html: list digests by agent+cadence  │
│  GET /digest/{a}/{d}/{c}   → cadence-specific view:                     │
│    daily   → article cards + why-it-matters + impact tags               │
│    weekly  → theme sections + top articles + week outlook               │
│    monthly → big-picture themes + anticipated news                      │
│    annual  → top 10 + predictions + year-in-review                      │
│  GET /download/{a}/{d}/{c}/{fmt} → serve .md/.html/.json (SRC-136)     │
│                                                                          │
│  Tag cloud + tier filter (JS) (SRC-134)                                 │
│  Agent config switcher — display-only v1 (SRC-134)                     │
│  No authentication in v1 (SRC-134)                                      │
└─────────────────────────────────────────────────────────────────────────┘
```

---

## 8. Deployment Architecture

**Source requirements:** SRC-074–SRC-094

### 8.1 Phase 1 — Local Development (SRC-075–SRC-077)

```bash
# Clone + install
git clone <repo>
cd ai-news-agent
cp .env.example .env    # Fill in API keys — never commit .env
pip install -e ".[dev,tavily]"

# Run individual agents
ai-news-source --agent default --cadence daily
ai-news-curate --agent default --cadence daily
ai-news-render --agent default --cadence daily

# Run the scheduler (fires all jobs on schedule)
ai-news-schedule

# Run the portal
ai-news-portal        # http://localhost:8080

# Or use Docker (same image as production)
docker build -t ai-news-agent .
docker run --env-file .env -p 8080:8080 ai-news-agent
```

Iterate on prompt quality locally before deploying (SRC-077). The Docker container is identical to production — no environment-specific branches.

### 8.2 Phase 2 — Serverless Containers (SRC-078–SRC-086)

| Component | GCP | AWS | Azure |
|-----------|-----|-----|-------|
| Scheduler | Cloud Scheduler | EventBridge Scheduler | Logic Apps / Timer trigger |
| Compute | Cloud Run | App Runner or Fargate† | Container Apps |
| Storage (outputs) | Cloud Storage (mount or sync) | S3 (sync) | Blob Storage |
| Secrets | Secret Manager | Secrets Manager | Key Vault |
| Logs | Cloud Logging | CloudWatch | Application Insights |

† AWS Lambda: 15-min hard timeout is fine for daily/weekly/monthly. Annual synthesis requires App Runner or Fargate to avoid the limit (SRC-090).

#### Container design principles (SRC-085)
- **Single multi-stage Dockerfile** — builder stage (install deps) + runtime stage (minimal Python image).
- Same image runs locally, in CI, and in production — no environment-specific builds.
- Non-root user (`appuser`) in runtime stage — security posture.
- Secrets injected at container start via env vars from the cloud's secrets manager (SRC-111).
- Output directory mounted from cloud storage volume or synced post-run.

### 8.3 Dockerfile shape — SRC-085, SRC-099

```dockerfile
# Stage 1: builder — install dependencies into /install
FROM python:3.12-slim AS builder
WORKDIR /build
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential curl && rm -rf /var/lib/apt/lists/*
COPY pyproject.toml ./
COPY src/ ./src/
RUN pip install --upgrade pip \
    && pip install --prefix=/install --no-cache-dir ".[tavily]"

# Stage 2: runtime — minimal image
FROM python:3.12-slim AS runtime
WORKDIR /app
RUN useradd --create-home --shell /bin/bash appuser
COPY --from=builder /install /usr/local
COPY src/     ./src/
COPY configs/ ./configs/
COPY prompts/ ./prompts/
RUN mkdir -p /app/outputs && chown appuser:appuser /app/outputs
USER appuser
EXPOSE 8080

# Default CMD: portal. Cloud scheduler overrides CMD for agent runs.
CMD ["python", "-m", "uvicorn", "ai_news_agent.portal.app:app", \
     "--host", "0.0.0.0", "--port", "8080", "--workers", "1"]

LABEL org.opencontainers.image.title="AI News Curation Agent"
LABEL org.opencontainers.image.description="Multi-agent AI News platform"
```

Cloud scheduler jobs override CMD for agent runs:
- Sourcing: `["ai-news-source", "--agent", "default", "--cadence", "daily"]`
- Curation: `["ai-news-curate", "--agent", "default", "--cadence", "weekly"]`

### 8.4 Why not the alternatives (SRC-091–SRC-094)
- **Always-on VM (SRC-092):** wastes money — idle >99% of the time.
- **Kubernetes (SRC-093):** massive operational overhead for a few cron jobs.
- **GitHub Actions on a schedule (SRC-094):** workable for Phase 1.5; less ideal for annual due to timeout caps and weaker observability.

---

## 9. CI/CD Pipeline

**Source requirements:** SRC-095–SRC-111

```
Stage 1: Lint        ruff check src/ tests/                     (SRC-098)
Stage 2: Type-check  mypy src/                                   (SRC-098)
Stage 3: Test        pytest --cov=src/ (LLM + Twitter mocked)    (SRC-098)
Stage 4: Build       docker build -t ai-news-agent .             (SRC-099)
Stage 5: Push        docker push to registry (main branch only)  (SRC-100)
Stage 6: Deploy      cloud-specific CLI deploy command           (SRC-101)
Stage 7: Smoke       --dry-run mode → scratch location           (SRC-102)
           Assert: non-empty output, all required fields,
           no items with missing URLs in any format.
```

**GitHub Actions** is the default CI platform (SRC-104). The only stage that differs across cloud targets is Deploy — lint, test, build, push, and smoke are identical.

**Dry-run mode (SRC-102):** `ai-news-curate --dry-run` produces a full digest but writes only to a scratch location (`outputs/scratch/`). Used in CI smoke test and manual validation. Verifies non-empty output, all required fields present, and no items with missing URLs.

### Required secrets at runtime (SRC-105–SRC-111)

| Env Var | Required | Purpose | Source |
|---------|----------|---------|--------|
| `OPENAI_API_KEY` | Yes (default provider) | LLM completions + native search | SRC-107 |
| `TWITTER_BEARER_TOKEN` | Yes | tweepy auth (Basic tier+) | SRC-108 |
| `WEB_SEARCH_API_KEY` | Optional | Brave/Tavily fallback search | SRC-109 |
| `WEB_SEARCH_PROVIDER` | Optional | `"brave"` \| `"tavily"` | SRC-060 |
| `SCHEDULER_API_KEY` | Optional | Manual override endpoint auth | SRC-147 |

All secrets sourced from cloud secrets manager at container start. **Never baked into images. Never committed.** (SRC-111)

---

## 10. Quality Monitoring and Observability

**Source requirements:** SRC-149–SRC-150

Every run emits a structured log event via `structlog` (SRC-149–SRC-150):

```python
# Traces: SRC-129 (prompt_version), SRC-148 (twitter_available), SRC-150 (all fields)

log.info(
    "run_complete",
    agent_id              = meta.agent_id,
    cadence               = meta.cadence,
    run_date              = str(meta.run_date),
    llm_provider          = meta.llm_provider,
    llm_model             = meta.llm_model,
    prompt_version        = meta.prompt_version,        # sha256:... (SRC-129)
    items_considered      = meta.items_considered,
    items_included        = meta.items_included,
    items_by_tier         = meta.items_by_tier,         # {"1a":n, "1b":n, "2":n, "3":n, "4":n}
    items_by_source_class = meta.items_by_source_class, # {"web":n, "twitter":n}
    twitter_available     = meta.twitter_signal_available,  # SRC-148
    tweet_api_call_count  = meta.tweet_api_call_count,
    token_usage           = meta.token_usage,
)
```

These events ship to the cloud's native observability stack automatically (Cloud Logging, CloudWatch, Application Insights) — no additional agent required (SRC-086).

**Alerting (SRC-146):** Cloud-native logging alert configured on any CRITICAL-level log event (scheduler retry exhaustion, LLM call failures, rendering failures).

**Post-launch review triggers (SRC-150):**
After 4–6 weeks of operation, review these signals:
1. **Source dominance** — if the same 2–3 domains appear in every digest, they may be overweighted; re-tune tier scoring or source list.
2. **Technical slip-through** — if disqualified content (tutorials, benchmark papers) appears in output, prompt iteration is needed.
3. **Twitter signal ROI** — if Twitter-originated items never make the final cut, evaluate disabling the integration to reduce API cost and complexity.

---

## 11. Out of Scope — v1

| Capability | Notes | Source |
|------------|-------|--------|
| Automated distribution (email send, Slack webhook posting) | Export files written to disk; user pastes/syncs manually | SRC-004, SRC-137–SRC-139 |
| Portal authentication | v1 is read-only and unauthenticated | SRC-134 |
| Portal-driven configuration changes | YAML-only in v1; portal config is an explicit future enhancement | SRC-072 |
| Kubernetes deployment | Excessive operational overhead for this workload shape | SRC-093 |
| Twitter API tier selection | Documented decision point; Basic tier likely minimum — confirm before provisioning | SRC-065 |
| Anthropic/Google provider (active) | Stub class exists; not activated in v1 | SRC-055–SRC-056 |
| Automated prompt review / formal owner designation | Prompt iteration expected first 4–6 weeks; formal owner is future | SRC-130–SRC-131 |

---

## 12. Traceability Rules and SRC-* Coverage Map

Per `spec.md` VAL-001, VAL-002, AC-001:

1. **VAL-001:** Every implementation artifact (module, test, config, prompt) must reference the SRC-* IDs it satisfies in its module docstring or file header comment. Missing traceability fails code review.
2. **VAL-002:** Every implementation stage must read `docs/requirements/requirements.md` **before** editing code. This architecture document was produced after reading all four planning artifacts in full.
3. **AC-001:** SRC-001–SRC-150 coverage must be verified before any increment is considered complete.

### SRC-* Coverage Map — All 150 Requirements

| SRC Range | Requirement Area | Architecture Section(s) |
|-----------|-----------------|------------------------|
| SRC-001–SRC-006 | Overview (SRC-001–SRC-002), product goals (SRC-003–SRC-004), functional requirements header (SRC-005), multi-agent decomposition (SRC-006) | §1 |
| SRC-007–SRC-013 | Sourcing agent — lookback windows, dedup, storage, no-curate rule (SRC-013) | §3.5, §3.3 |
| SRC-014–SRC-015 | Curation agent responsibility — runs at least once per day (SRC-015), sifts candidates by lookback (SRC-014) | §3.6 |
| SRC-016–SRC-032 | Source tiers definition (SRC-016), tier 1a–4 lists (SRC-017–SRC-021), scoring criteria (SRC-022–SRC-026), LLM prioritization (SRC-027), cadence windows daily/weekly/monthly/annual (SRC-028–SRC-032) | §3.6 |
| SRC-033–SRC-049 | Source coverage guidance (SRC-033–SRC-034), Twitter/X sources (SRC-035–SRC-046), Twitter signal role (SRC-047), curated item schema (SRC-048), URL enforcement (SRC-049) | §3.4, §3.5, §3.6, §6 |
| SRC-050–SRC-054 | Architecture section header (SRC-050), high-level flow (SRC-051), scheduler role (SRC-052), sourcing/curation store (SRC-053), research LLM for monthly/annual (SRC-054) | §1, §3.2, §3.6, §7 |
| SRC-055–SRC-061 | Provider-agnostic design (SRC-055–SRC-056), OpenAI default (SRC-057), design-in-practice intro (SRC-058), plain language prompts (SRC-059), abstract tool use (SRC-060), output parsing (SRC-061) | §3.2 |
| SRC-062–SRC-070 | Twitter/X integration — tweepy (SRC-063), auth (SRC-064), tier (SRC-065), fetch (SRC-066–SRC-067), filter (SRC-068), hydrate URLs (SRC-069), curation role (SRC-070) | §3.4 |
| SRC-071–SRC-073 | Configuration section header (SRC-071), per-agent YAML + multi-agent scheduler awareness (SRC-072), secrets in env vars only (SRC-073) | §3.1, §4 |
| SRC-074–SRC-094 | Deployment section (SRC-074), local phase (SRC-075–SRC-077), serverless phase (SRC-078–SRC-079), stateless shape (SRC-080–SRC-086), cloud equivalents (SRC-087–SRC-089), Lambda timeout note (SRC-090), alternatives (SRC-091–SRC-094) | §8 |
| SRC-095–SRC-111 | CI/CD pipeline section (SRC-095–SRC-096), stages intro (SRC-097), lint+test (SRC-098), build (SRC-099), push (SRC-100), deploy (SRC-101), smoke (SRC-102), pipeline choice (SRC-103–SRC-104), secrets section (SRC-105–SRC-106), LLM key (SRC-107), Twitter token (SRC-108), search key (SRC-109), cloud credentials (SRC-110), secrets-never-baked rule (SRC-111) | §9 |
| SRC-112–SRC-131 | Prompt section (SRC-112–SRC-113), prompt structure (SRC-114–SRC-115), all 9 prompt requirements (SRC-116–SRC-124), prompt ownership (SRC-125–SRC-126), version control rule (SRC-127), review rule (SRC-128), SHA-256 hash (SRC-129), iteration timeline (SRC-130), formal owner (SRC-131) | §5 |
| SRC-132–SRC-141 | Output experience header (SRC-132), portal (SRC-133–SRC-134), rendered export (SRC-135–SRC-136), email HTML (SRC-137), Slack MD (SRC-138), static site (SRC-139), future distribution (SRC-140), URL enforcement (SRC-141) | §3.7, §3.9 |
| SRC-142–SRC-148 | Reliability section header (SRC-143), retries (SRC-144), idempotency (SRC-145), alerting (SRC-146), manual override (SRC-147), Twitter degradation (SRC-148) | §3.4, §3.8 |
| SRC-149–SRC-150 | Quality monitoring — structlog run metrics, post-launch review triggers | §10 |

#### Explicit inline coverage for section-header and sub-bullet SRC-* IDs

The following SRC-* IDs correspond to section headers, introductory framing lines, or sub-bullets in the requirements document. They are covered by the sections identified above and are explicitly acknowledged here to satisfy AC-001 completeness:

| SRC-ID | Requirements Text | Covered By |
|--------|-------------------|------------|
| SRC-002 | "1. Overview" section header | §1 System Overview |
| SRC-005 | "2. Functional Requirements" section header | §3 (agents) + §1 |
| SRC-015 | Curation agent runs at least once each day to sift candidates | §3.6 (cadence matrix, CurationAgent.run) |
| SRC-051 | "3.1 High-Level Flow" section header | §7 Agent Data Flow |
| SRC-058 | "What this means in practice" — provider-agnostic design sub-intro | §3.2 design principle |
| SRC-079 | Phase 2 serverless container narrative | §8.2 |
| SRC-081 | Agent runs 3× per week, completes in 1–5 min, fully stateless | §8.2 container design principles |
| SRC-082 | Pay only for execution time — idle cost ~$0 | §8.2 |
| SRC-083 | No infrastructure to manage | §8.2 |
| SRC-084 | Native cron-style triggers with retry/DLQ | §3.8 retry + §8.2 |
| SRC-087 | "4.3 Equivalent Stacks Across Clouds" section header | §8.2 cloud equivalents table |
| SRC-088 | Pick cloud based on org provisioning — all three equally suitable | §8.2 |
| SRC-089 | Cloud equivalents table (GCP/AWS/Azure) | §8.2 |
| SRC-096 | CI/CD pipeline is same regardless of cloud target | §9 |
| SRC-097 | "5.1 Stages" section header | §9 pipeline stages |
| SRC-103 | "5.2 Pipeline Choice" section header | §9 GitHub Actions default |
| SRC-106 | "Required at runtime" secrets sub-header | §9 secrets table |
| SRC-110 | Cloud deployment credentials (workload identity preferred) | §9 secrets table |
| SRC-114 | "6.1 Prompt Structure" section header | §5.2 required sections |
| SRC-125 | "6.2 Prompt Ownership" section header | §5.1 ownership |
| SRC-126 | Prompt ownership shared; treat prompts like code | §5.1 |
| SRC-143 | "8.1 Reliability" section header | §3.8 retry + §10 |

> **Coverage check (updated):** All 150 SRC-* entries (SRC-001–SRC-150) defined in `docs/requirements/spec.md` are represented in the architecture sections and explicit inline table above. No gaps. AC-001 satisfied for this document.

---

*This architecture document is the authoritative design reference for all implementation stages (SLICE-002 through SLICE-006). Re-read `docs/requirements/requirements.md` before editing code (VAL-002). Every implementation artifact must carry SRC-* traceability (VAL-001). Coverage of SRC-001–SRC-150 must be verified before any slice is declared complete (AC-001).*
