# Configuration Reference — AI News Curation Agent

> **Requirement traces:** SRC-034 (user-configurable sources), SRC-046 (configurable handles),
> SRC-054 (research LLM), SRC-057 (provider selection), SRC-071–SRC-073 (config system),
> SRC-105–SRC-111 (secrets from env vars)
>
> **Schema files:** `configs/agent-config.schema.json`, `configs/scheduler.schema.json`
>
> **Validator CLI:** `python -m ai_news_agent.config.schema --validate <file>`

---

## Table of Contents

1. [Overview — Multi-Agent Config Design](#1-overview--multi-agent-config-design)
2. [Secrets Policy](#2-secrets-policy)
3. [Per-Agent Configuration (`agent-id.yaml`)](#3-per-agent-configuration-agent-idyaml)
   - 3.1 [agent_id](#31-agent_id)
   - 3.2 [llm](#32-llm)
   - 3.3 [curation_prompt](#33-curation_prompt)
   - 3.4 [sources](#34-sources)
   - 3.5 [twitter](#35-twitter)
   - 3.6 [limits](#36-limits)
   - 3.7 [output_dir](#37-output_dir)
   - 3.8 [store_backend](#38-store_backend)
4. [Scheduler Configuration (`scheduler.yaml`)](#4-scheduler-configuration-scheduleryaml)
   - 4.1 [scheduler (retry policy)](#41-scheduler-retry-policy)
   - 4.2 [api (manual override)](#42-api-manual-override)
   - 4.3 [triggers (cron schedule)](#43-triggers-cron-schedule)
   - 4.4 [agents (registry)](#44-agents-registry)
5. [Adding a New Agent](#5-adding-a-new-agent)
6. [JSON Schema Validation](#6-json-schema-validation)
7. [IDE Integration](#7-ide-integration)
8. [Environment Variables Reference](#8-environment-variables-reference)
9. [Common Recipes](#9-common-recipes)
10. [Requirement Traceability](#10-requirement-traceability)

---

## 1. Overview — Multi-Agent Config Design

The system supports **multiple simultaneous agent configurations**, each with its own:

- Curation theme (via different prompt files)
- LLM provider and model selection
- Source tier lists
- Twitter influencer handles and weights
- Top-N limits per cadence
- Output directory

All agents are discovered by the scheduler at startup from `configs/scheduler.yaml` —
**no code changes are needed** to add, remove, enable, or disable agents (SRC-072).

### Config file hierarchy

```
configs/
├── scheduler.yaml              ← Root: retry, cron, API, agent registry
├── default-agent.yaml          ← Default agent (business + society AI)
├── example-technical-agent.yaml ← Example: technical AI developments
├── example-policy-agent.yaml   ← Example: policy + governance
├── agent-config.schema.json    ← JSON Schema for per-agent YAMLs
└── scheduler.schema.json       ← JSON Schema for scheduler.yaml
```

Each `*-agent.yaml` is an independent configuration unit that maps to one
set of curation runs (sourcing → curation → rendering for all 4 cadences).

---

## 2. Secrets Policy

**Non-negotiable rule (SRC-073):** Secrets must NEVER appear in YAML config files.

| What | Where |
|------|-------|
| `OPENAI_API_KEY` | Env var / secrets manager only |
| `TWITTER_BEARER_TOKEN` | Env var / secrets manager only |
| `WEB_SEARCH_API_KEY` | Env var / secrets manager only |
| `ANTHROPIC_API_KEY` | Env var / secrets manager only |
| `SCHEDULER_API_KEY` | Env var / secrets manager only |

The loader (`config/loader.py`) scans all non-comment YAML lines for common
secret patterns (OpenAI `sk-` prefix, Anthropic key shapes, etc.) and raises
a `ConfigError` if any are found. This is a defence-in-depth check.

```yaml
# ✗ WRONG — never do this
llm:
  api_key: sk-proj-abc123   # ← ConfigError raised at startup

# ✓ CORRECT — reference env var name in comments only
# Required env vars: OPENAI_API_KEY, TWITTER_BEARER_TOKEN
llm:
  provider: openai
  model: gpt-4o
```

---

## 3. Per-Agent Configuration (`agent-id.yaml`)

### 3.1 `agent_id`

**Required.** Unique identifier for this agent instance.

```yaml
agent_id: default
```

- Used as the storage namespace and output directory component.
- Must match the `id` field in `scheduler.yaml` agents registry.
- Allowed characters: letters, digits, hyphens, underscores (`^[a-zA-Z0-9_-]+$`).
- Maximum length: 64 characters.

**Traces:** SRC-072 (agent identity), SRC-145 (output dir namespace)

---

### 3.2 `llm`

LLM provider and model configuration.

```yaml
llm:
  provider: openai            # "openai" | "anthropic"
  model: gpt-4o               # Default for daily + weekly curation

  cadence_overrides:          # Optional per-cadence model overrides
    monthly:
      model: o3               # Research-grade for monthly synthesis
      thinking: false
    annual:
      model: o3
      thinking: true          # Extended thinking for annual predictions
```

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `provider` | string | `"openai"` | LLM provider. `"openai"` or `"anthropic"` (SRC-057) |
| `model` | string | `"gpt-4o"` | Model name for daily/weekly (SRC-057) |
| `cadence_overrides` | map | `{}` | Per-cadence overrides; keys: `daily`, `weekly`, `monthly`, `annual` (SRC-054) |
| `cadence_overrides.*.model` | string | — | Model to use for this cadence |
| `cadence_overrides.*.thinking` | boolean | `false` | Enable extended reasoning mode (SRC-032) |

**Provider swap without code changes (SRC-056):**
```yaml
# Switch to Anthropic — only this field changes
llm:
  provider: anthropic
  model: claude-3-7-sonnet-20250219
```

**Monthly + annual research model (SRC-054, SRC-032):**
Monthly and annual runs benefit from more capable, reasoning-heavy models.
The spec explicitly calls for "a current model with higher thinking and research mode activated"
for annual synthesis (SRC-032).

**Traces:** SRC-054, SRC-055–SRC-061 (provider-agnostic layer)

---

### 3.3 `curation_prompt`

Path to the curation prompt directory or a specific prompt file.

```yaml
curation_prompt: prompts/daily.md
```

- At runtime the curation agent selects the cadence-specific file:
  `prompts/daily.md`, `prompts/weekly.md`, `prompts/monthly.md`, `prompts/annual.md`.
- The **SHA-256 hash** of the selected prompt file is recorded in every digest output
  to enable quality regression tracing (SRC-129).
- Prompts are provider-agnostic — plain natural language, no provider-specific formatting (SRC-059).
- Prompts live in version control alongside the code (SRC-127).

**To customise curation direction:** edit the prompt files in `prompts/` rather than
this field. The prompt is the most important quality lever (SRC-112).

**Traces:** SRC-113, SRC-127, SRC-129

---

### 3.4 `sources`

Configurable source tier lists. (SRC-016–SRC-021, SRC-034)

```yaml
sources:
  custom:   []                    # Tier 1a — user-specified priority sources
  tier_1b:
    - reuters.com
    - bloomberg.com
    - wsj.com
  tier_2:
    - openai.com
    - anthropic.com
    - huggingface.co
  tier_3:
    - techcrunch.com
    - theverge.com
  tier_4:
    - brookings.edu
    - rand.org
```

| Tier | Label | Examples | Source |
|------|-------|----------|--------|
| `custom` | Tier 1a — user-specified | Your blog, internal wiki, custom sources | SRC-017 |
| `tier_1b` | Tier 1b — business press | reuters.com, bloomberg.com, wsj.com, ft.com, economist.com | SRC-018 |
| `tier_2` | Tier 2 — AI company blogs | openai.com, anthropic.com, huggingface.co | SRC-019 |
| `tier_3` | Tier 3 — tech business press | techcrunch.com, theverge.com, wired.com | SRC-020 |
| `tier_4` | Tier 4 — policy + research | brookings.edu, rand.org, hai.stanford.edu | SRC-021 |

All fields are optional lists of domain strings. The sourcing agent uses these
as priority-weighted allowlists when ranking web search results. Higher tiers
get higher curation scoring weight (SRC-022).

**Technical agent example — emphasising research sources:**
```yaml
sources:
  custom:
    - arxiv.org
    - paperswithcode.com
  tier_1b:
    - nature.com
    - science.org
  tier_2:
    - openai.com
    - anthropic.com
    - deepmind.google
```

**Traces:** SRC-016–SRC-021, SRC-022, SRC-034

---

### 3.5 `twitter`

Twitter/X influencer monitoring configuration.

```yaml
twitter:
  enabled: true
  handles:
    - { handle: karpathy,     weight: 1.0 }   # Andrej Karpathy
    - { handle: sama,         weight: 1.0 }   # Sam Altman
    - { handle: ylecun,       weight: 1.5 }   # Yann LeCun — boosted weight
    - { handle: emilymbender, weight: 2.0 }   # Emily Bender — specialist focus
```

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `enabled` | boolean | `true` | Set `false` to skip Twitter sourcing for this agent (SRC-148) |
| `handles` | list | `[]` | Influencer handles to monitor |
| `handles[].handle` | string | — | Twitter handle **without** `@` prefix |
| `handles[].weight` | float | `1.0` | Signal weight (0.0 < weight ≤ 10.0). Higher = more prominent in LLM prompt |

**Default 9 handles (SRC-037–SRC-045):**
`@karpathy`, `@sama`, `@demishassabis`, `@DarioAmodei`, `@ylecun`,
`@AndrewYNg`, `@fchollet`, `@drfeifei`, `@emilymbender`

**Add, remove, or re-weight handles without code changes (SRC-046).**

**Twitter = signal, not primary news (SRC-047):**
Tweets are passed to the LLM as a labeled "Influencer Signal" context section.
The prompt explicitly instructs the model to use tweets as leads for web search,
not as direct citations — unless the tweet itself is the news
(e.g., an executive announcement on X before press coverage exists).

**Graceful degradation (SRC-148):**
If Twitter is unavailable or `enabled: false`, the digest is still produced
from web sources alone, with a note in the digest.

**Traces:** SRC-036–SRC-047, SRC-064, SRC-148

---

### 3.6 `limits`

Top-N article limits per cadence.

```yaml
limits:
  daily_top_n:   10    # Articles for daily digest
  weekly_top_n:  7     # Articles for weekly digest
  monthly_top_n: 10    # Articles for monthly digest
  annual_top_n:  10    # Articles + predictions for annual digest
```

| Field | Type | Default | Valid Range | Description |
|-------|------|---------|-------------|-------------|
| `daily_top_n` | integer | `10` | 1–50 | Max articles for daily curation (SRC-029) |
| `weekly_top_n` | integer | `7` | 1–50 | Max articles for weekly curation (SRC-030) |
| `monthly_top_n` | integer | `10` | 1–50 | Max articles for monthly curation (SRC-031) |
| `annual_top_n` | integer | `10` | 1–20 | Top articles + 10 predictions for annual (SRC-032) |

These are **soft targets** — the LLM may return fewer items if quality thresholds
are not met or insufficient candidates are available.

**Traces:** SRC-029–SRC-032

---

### 3.7 `output_dir`

Base directory where digest files are written.

```yaml
output_dir: outputs/{agent_id}    # → resolved to "outputs/default"
```

The `{agent_id}` placeholder is resolved to the agent's `agent_id` at load time.
Each agent has an isolated output namespace so multiple agents don't overwrite each other.

**Output file naming (SRC-145):**
```
outputs/{agent_id}/YYYY-MM-DD-daily.md
outputs/{agent_id}/YYYY-MM-DD-daily.html
outputs/{agent_id}/YYYY-MM-DD-daily.json
outputs/{agent_id}/YYYY-MM-DD-weekly.md
...
```

Re-runs overwrite cleanly (idempotent by date — SRC-145).

**Traces:** SRC-072, SRC-145

---

### 3.8 `store_backend`

Article store backend. Controls where fetched articles, tweet signals, and digest records are persisted for deduplication and curation window queries.

```yaml
store_backend: tinydb    # default — zero-infrastructure JSON file
# store_backend: sqlite  # production-grade — indexed SQLite database
```

| Value | File | Best for |
|-------|------|----------|
| `tinydb` *(default)* | `outputs/{agent_id}/store.json` | Local dev, low article volumes (<50 k records) |
| `sqlite` | `outputs/{agent_id}/store.db` | Production, higher volumes, concurrent reads |

**Switching from TinyDB to SQLite:**
Set `store_backend: sqlite` in your agent YAML. On the next run, if `store.json` already exists in the output directory and the SQLite store is empty, all existing articles, tweet signals, and digest records are automatically imported before the pipeline runs. The original `store.json` is kept as a backup.

**Traces:** SRC-053, SRC-072, SRC-076, SRC-085

---

## 4. Scheduler Configuration (`scheduler.yaml`)

### 4.1 `scheduler` (retry policy)

```yaml
scheduler:
  max_retries: 3                   # Retry attempts on failure (SRC-144)
  retry_backoff_base_seconds: 30   # Exponential: 30s → 60s → 120s
```

| Field | Default | Description |
|-------|---------|-------------|
| `max_retries` | `3` | Retry attempts after first failure (0–10) |
| `retry_backoff_base_seconds` | `30` | Base sleep interval in seconds (1–300) |

**Retry schedule (SRC-144):**
Attempt 1 (immediate) → wait 30s → attempt 2 → wait 60s → attempt 3 → wait 120s → attempt 4 (final).

**Traces:** SRC-144

---

### 4.2 `api` (manual override)

```yaml
api:
  enabled: true
  host: "0.0.0.0"
  port: 8081
```

Exposes `POST /api/trigger` for on-demand job execution — useful for backfills
and when the schedule misfires (SRC-147).

**Traces:** SRC-147

---

### 4.3 `triggers` (cron schedule)

```yaml
triggers:
  sourcing_daily:   "0 0 * * *"     # 00:00 UTC daily
  curation_daily:   "5 0 * * *"     # 00:05 UTC daily
  curation_weekly:  "0 1 * * 0"     # 01:00 UTC Sunday
  curation_monthly: "0 2 1 * *"     # 02:00 UTC 1st of month
  curation_annual:  "0 3 1 1 *"     # 03:00 UTC January 1st
```

Five-field cron format: `minute hour day_of_month month day_of_week`

All times are **UTC**. The sourcing job runs at 00:00 and curation follows at 00:05
to ensure candidates are available.

**Traces:** SRC-009, SRC-028–SRC-032, SRC-052

---

### 4.4 `agents` (registry)

```yaml
agents:
  - id: default
    config: configs/default-agent.yaml
    enabled: true
    description: "Default business + society AI news curation"

  - id: technical
    config: configs/example-technical-agent.yaml
    enabled: false
    description: "Technical AI developments (disabled by default)"

  - id: policy
    config: configs/example-policy-agent.yaml
    enabled: false
    description: "AI policy, governance, and safety"
```

| Field | Required | Description |
|-------|----------|-------------|
| `id` | Yes | Unique agent ID — must match `agent_id` in the YAML file |
| `config` | Yes | Path to the per-agent YAML (relative to the working directory) |
| `enabled` | No (default: `true`) | Set `false` to skip at startup without removing the entry |
| `description` | No | Human-readable description (shown in portal agent switcher — SRC-134) |

**Per-agent job count:** Each enabled agent gets **5 scheduled jobs** (sourcing + 4 curation cadences).
With 2 enabled agents, the scheduler runs 10 jobs.

**Traces:** SRC-072

---

## 5. Adding a New Agent

**Step 1 — Create the agent YAML:**

```bash
cp configs/default-agent.yaml configs/my-new-agent.yaml
```

Edit `agent_id`, `llm`, `sources`, `twitter`, `limits`, and `output_dir` as needed.

**Step 2 — Add to the scheduler registry:**

```yaml
# In configs/scheduler.yaml:
agents:
  - id: my-new-agent
    config: configs/my-new-agent.yaml
    enabled: true
    description: "My specialised curation theme"
```

**Step 3 — Validate:**

```bash
python -m ai_news_agent.config.schema --validate configs/my-new-agent.yaml --summary
python -m ai_news_agent.config.schema --validate configs/scheduler.yaml --type scheduler
```

**Step 4 — Test locally:**

```bash
ai-news-schedule --trigger-agent my-new-agent --job sourcing
ai-news-schedule --trigger-agent my-new-agent --job curation --cadence daily
```

No code changes required for any of the above.

**Traces:** SRC-072

---

## 6. JSON Schema Validation

JSON Schema files are generated from the Pydantic models and exported to:

- `configs/agent-config.schema.json` — validates per-agent YAML files
- `configs/scheduler.schema.json` — validates `scheduler.yaml`

**Regenerate schemas** (after model changes):

```bash
python -m ai_news_agent.config.schema --export-dir configs/
```

**Validate a config file:**

```bash
# Agent YAML
python -m ai_news_agent.config.schema --validate configs/default-agent.yaml
python -m ai_news_agent.config.schema --validate configs/default-agent.yaml --summary

# Scheduler YAML
python -m ai_news_agent.config.schema --validate configs/scheduler.yaml --type scheduler --summary

# Print the JSON Schema itself
python -m ai_news_agent.config.schema --validate configs/default-agent.yaml --json-schema
```

**Pre-commit hook** (`pre-commit` config):

```yaml
# .pre-commit-config.yaml
- repo: local
  hooks:
    - id: validate-agent-configs
      name: Validate agent YAML configs
      language: python
      entry: python -m ai_news_agent.config.schema --validate
      files: ^configs/.*-agent\.yaml$
```

**Traces:** SRC-071, SRC-072

---

## 7. IDE Integration

### VS Code — YAML plugin

Install the [Red Hat YAML extension](https://marketplace.visualstudio.com/items?itemName=redhat.vscode-yaml)
and add to `.vscode/settings.json`:

```json
{
  "yaml.schemas": {
    "configs/agent-config.schema.json": [
      "configs/*-agent.yaml",
      "configs/default-agent.yaml"
    ],
    "configs/scheduler.schema.json": [
      "configs/scheduler.yaml"
    ]
  }
}
```

This enables inline autocompletion, hover documentation, and error highlighting
for all config files.

### JetBrains IDEs

In File → Settings → Languages & Frameworks → Schemas and DTDs → JSON Schema Mappings:
- Add `configs/agent-config.schema.json` mapped to pattern `configs/*-agent.yaml`
- Add `configs/scheduler.schema.json` mapped to `configs/scheduler.yaml`

---

## 8. Environment Variables Reference

| Variable | Required | Description | Source |
|----------|----------|-------------|--------|
| `OPENAI_API_KEY` | Required (OpenAI) | OpenAI API key | SRC-107 |
| `TWITTER_BEARER_TOKEN` | Required | Twitter/X v2 bearer token | SRC-108 |
| `WEB_SEARCH_API_KEY` | Optional | Brave or Tavily API key | SRC-109 |
| `WEB_SEARCH_PROVIDER` | Optional | `"native"` \| `"brave"` \| `"tavily"` | SRC-060 |
| `ANTHROPIC_API_KEY` | Required (Anthropic) | Anthropic API key | SRC-055 |
| `SCHEDULER_API_KEY` | Optional | Bearer token for `POST /api/trigger` | SRC-147 |

**Local development:** copy `.env.example` to `.env` and fill in values:

```bash
cp .env.example .env
# Edit .env — never commit it
```

**Production:** use your cloud's secrets manager:
- GCP: Secret Manager → Cloud Run env vars
- AWS: Secrets Manager → Lambda / App Runner env vars
- Azure: Key Vault → Container Apps env vars

**Traces:** SRC-073, SRC-105–SRC-111

---

## 9. Common Recipes

### Disable Twitter for a single agent

```yaml
# In the agent YAML:
twitter:
  enabled: false
```

The sourcing agent falls back to web-only, logging a note (SRC-148).

### Add a custom priority source

```yaml
sources:
  custom:
    - myblog.example.com       # Tier 1a — checked first
    - internal-wiki.corp.com
```

### Boost a specific influencer handle

```yaml
twitter:
  handles:
    - { handle: karpathy, weight: 3.0 }   # 3x more prominent in LLM prompt
    - { handle: sama,     weight: 1.0 }
```

### Use Anthropic for monthly/annual, OpenAI for daily/weekly

```yaml
llm:
  provider: openai
  model: gpt-4o
  cadence_overrides:
    monthly:
      model: claude-3-7-sonnet-20250219
    annual:
      model: claude-3-7-sonnet-20250219
      thinking: true
```

> Note: mixed-provider per-cadence is a future enhancement.
> Currently `provider` applies to the base model; cadence overrides use the same provider.

### Minimal valid agent config

```yaml
agent_id: minimal
```

All other fields have sensible defaults. This works but produces a generic,
unconfigured agent — useful for quick testing.

---

## 10. Requirement Traceability

| Config Field | Source Requirements |
|---|---|
| `agent_id` | SRC-072, SRC-145 |
| `llm.provider` | SRC-055–SRC-057 |
| `llm.model` | SRC-057 |
| `llm.cadence_overrides` | SRC-032, SRC-054 |
| `curation_prompt` | SRC-113, SRC-127, SRC-129 |
| `sources.custom` | SRC-017, SRC-034 |
| `sources.tier_1b` | SRC-018 |
| `sources.tier_2` | SRC-019 |
| `sources.tier_3` | SRC-020 |
| `sources.tier_4` | SRC-021 |
| `twitter.enabled` | SRC-047, SRC-148 |
| `twitter.handles` | SRC-036–SRC-046 |
| `twitter.handles[].weight` | SRC-046 |
| `limits.daily_top_n` | SRC-029 |
| `limits.weekly_top_n` | SRC-030 |
| `limits.monthly_top_n` | SRC-031 |
| `limits.annual_top_n` | SRC-032 |
| `output_dir` | SRC-136, SRC-145 |
| `store_backend` | SRC-053, SRC-072, SRC-076, SRC-085 |
| Secrets in env vars only | SRC-073, SRC-105–SRC-111 |
| `scheduler.max_retries` | SRC-144 |
| `scheduler.retry_backoff_base_seconds` | SRC-144 |
| `api.*` | SRC-147 |
| `triggers.*` | SRC-009, SRC-028–SRC-032, SRC-052 |
| `agents[]` | SRC-072 |
| JSON Schema validation | SRC-071 |
