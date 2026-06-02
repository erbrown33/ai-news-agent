# User Guide — AI News Curation Agent

> **Requirement traces:** SRC-001–SRC-004 (system overview), SRC-006–SRC-013 (sourcing),
> SRC-014–SRC-032 (curation cadences), SRC-046 (configurable handles), SRC-052 (scheduler),
> SRC-057 (LLM provider), SRC-071–SRC-073 (config system), SRC-112–SRC-131 (prompts),
> SRC-133–SRC-136 (web portal), SRC-145 (idempotent outputs), SRC-147 (manual trigger)
>
> **SLICE:** SLICE-005 — Operational, deployment, and documentation requirements
>
> **Source-of-Truth Order:** `docs/requirements/requirements.md` ▶ `docs/requirements/spec.md`
> ▶ [`docs/architecture.md`](architecture.md) ▶ this document

---

## Table of Contents

1. [What This System Does](#1-what-this-system-does)
2. [Installation](#2-installation)
3. [Secrets and Environment Variables](#3-secrets-and-environment-variables)
4. [Configuring Agents](#4-configuring-agents)
   - 4.1 [The config file hierarchy](#41-the-config-file-hierarchy)
   - 4.2 [Per-agent YAML: every field explained](#42-per-agent-yaml-every-field-explained)
   - 4.3 [The scheduler config](#43-the-scheduler-config)
   - 4.4 [Adding a new agent](#44-adding-a-new-agent)
   - 4.5 [Validating your config](#45-validating-your-config)
5. [Authoring and Versioning Curation Prompts](#5-authoring-and-versioning-curation-prompts)
   - 5.1 [Prompt file locations and cadences](#51-prompt-file-locations-and-cadences)
   - 5.2 [Template placeholders](#52-template-placeholders)
   - 5.3 [Inclusion and exclusion criteria](#53-inclusion-and-exclusion-criteria)
   - 5.4 [Output structure requirements](#54-output-structure-requirements)
   - 5.5 [Versioning and SHA-256 hashes](#55-versioning-and-sha-256-hashes)
   - 5.6 [Prompt change process](#56-prompt-change-process)
6. [Running the Pipeline Locally](#6-running-the-pipeline-locally)
   - 6.1 [Full pipeline: sourcing → curation → rendering](#61-full-pipeline-sourcing--curation--rendering)
   - 6.2 [Running stages individually](#62-running-stages-individually)
   - 6.3 [Dry-run mode](#63-dry-run-mode)
   - 6.4 [Historical backfill and re-runs](#64-historical-backfill-and-re-runs)
   - 6.5 [The background scheduler](#65-the-background-scheduler)
   - 6.6 [Output files](#66-output-files)
7. [Launching the Web Portal](#7-launching-the-web-portal)
   - 7.1 [Start the portal](#71-start-the-portal)
   - 7.2 [Portal features](#72-portal-features)
   - 7.3 [Health and API endpoints](#73-health-and-api-endpoints)
8. [Deploying to a Serverless Container Platform](#8-deploying-to-a-serverless-container-platform)
   - 8.1 [Build and test the container locally](#81-build-and-test-the-container-locally)
   - 8.2 [GCP Cloud Run + Cloud Scheduler](#82-gcp-cloud-run--cloud-scheduler)
   - 8.3 [AWS App Runner + EventBridge](#83-aws-app-runner--eventbridge)
   - 8.4 [Azure Container Apps + Logic Apps](#84-azure-container-apps--logic-apps)
   - 8.5 [CI/CD pipeline overview](#85-cicd-pipeline-overview)
9. [Monitoring and Quality](#9-monitoring-and-quality)
10. [Troubleshooting](#10-troubleshooting)
11. [Requirement Traceability](#11-requirement-traceability)

---

## 1. What This System Does

The AI News Curation Agent is a **multi-agent platform** (SRC-006) that autonomously:

1. **Sources** candidate news articles from web blogs, news sites, and Twitter/X influencer
   feeds (SRC-007–SRC-013, SRC-033–SRC-049). Each article is stored exactly once per lookback
   window — no duplicates even across multiple sourcing runs (SRC-012).

2. **Curates** those candidates using an LLM against a configurable prompt, scoring each article
   for **business impact**, **workforce/societal impact**, and **strategic/policy impact**
   (SRC-023–SRC-026). The curation agent runs for four distinct cadence windows:
   - **Daily** — top articles from the prior 24 hours (SRC-009, SRC-029)
   - **Weekly** — themed synthesis of Sun–Sat, with a "what to watch" section (SRC-030)
   - **Monthly** — bigger-picture themes and anticipated news for the coming month (SRC-031)
   - **Annual** — top-10 articles of the year + 10 falsifiable predictions for next year (SRC-032)

3. **Renders** each curated digest in three export formats (SRC-004, SRC-135–SRC-141):
   - **Markdown** (`.md`) — Slack/Teams paste-ready
   - **HTML** (`.html`) — email-client paste-ready
   - **JSON** (`.json`) — machine-readable archive with all monitoring metadata

4. **Serves a web portal** (FastAPI + Jinja2) at `http://localhost:8080` where you can browse
   digests by cadence, view theme visualizations, filter by impact category, and download
   all three export formats (SRC-133–SRC-134).

### Two non-negotiable deliverables (SRC-004)

- A well-designed web portal for viewing AI news summaries.
- Structured digest export files for every curation run.

Every other feature in this system exists to produce these two outputs reliably and with
high curation quality.

---

## 2. Installation

### Prerequisites

- **Python 3.12+** (`python --version` to check)
- **pip** (comes with Python)
- **Docker** — optional for container testing, required for cloud deployment

### Install from source

```bash
# 1. Clone the repository
git clone https://github.com/erbrown33/ai-news-agent.git
cd ai-news-agent

# 2. Create an isolated virtual environment (strongly recommended)
python -m venv .venv
source .venv/bin/activate        # macOS/Linux
# .venv\Scripts\activate         # Windows

# 3. Install the package in editable mode, including all dev dependencies
pip install -e ".[dev]"
```

This registers all CLI entry points (`ai-news-run`, `ai-news-portal`, etc.) in your PATH.

### Optional provider extras

```bash
# Anthropic (Claude) LLM provider
pip install -e ".[anthropic]"

# Google Gemini/Vertex AI LLM provider
pip install -e ".[google]"

# Tavily web search fallback
pip install -e ".[tavily]"
```

### Verify the installation

```bash
ai-news-run --help        # Should print full help text
ai-news-portal --help     # Should print portal help
pytest tests/unit/ -q     # Should show 1600+ tests passing
```

---

## 3. Secrets and Environment Variables

**Non-negotiable security rule (SRC-073, SRC-111):** Secrets must **never** appear in YAML
config files, Dockerfiles, or logs. They are always sourced from environment variables or
a cloud secrets manager at runtime.

### Set up local secrets

```bash
# Create your local .env file from the template
cp .env.example .env

# Open .env and fill in real values:
#   OPENAI_API_KEY=sk-...          ← Required for all LLM calls
#   TWITTER_BEARER_TOKEN=AAA...    ← Recommended; agent degrades gracefully without it
```

> **Never commit `.env` with real values.** The `.gitignore` excludes `.env` automatically.

### Load secrets for a session

```bash
source .env     # Loads all variables into the current shell session
```

### Complete secret inventory

| Env Var | Required | Purpose | SRC |
|---------|----------|---------|-----|
| `OPENAI_API_KEY` | **Yes** | LLM calls (OpenAI — default provider) | SRC-107 |
| `TWITTER_BEARER_TOKEN` | Recommended | Twitter/X v2 API read access; Basic tier minimum | SRC-065, SRC-148 |
| `ANTHROPIC_API_KEY` | Optional | LLM calls when `llm.provider: anthropic` | SRC-055–SRC-056 |
| `GOOGLE_API_KEY` | Optional | LLM calls when `llm.provider: google` | SRC-055–SRC-056 |
| `WEB_SEARCH_API_KEY` | Optional | Brave or Tavily API key — required for web sourcing search | SRC-109 |
| `WEB_SEARCH_PROVIDER` | Optional | `"brave"` or `"tavily"` — defaults to `"tavily"` when key is set | SRC-060 |
| `SCHEDULER_API_KEY` | Optional | Protects `POST /api/trigger` — generate with `python -c "import secrets; print(secrets.token_hex(32))"` | SRC-147 |

For cloud deployment secrets (GCP WIF, AWS OIDC, Azure managed identity), see
[`docs/deployment/secrets-management.md`](deployment/secrets-management.md).

---

## 4. Configuring Agents

All runtime behavior is controlled by YAML files. **No code changes are needed** to change
curation themes, swap LLM providers, adjust source tier lists, modify influencer handles, or
add entirely new agents. (SRC-071–SRC-072)

### 4.1 The config file hierarchy

```
configs/
├── scheduler.yaml               ← Root: cron schedule, retry, API, agent registry
├── default-agent.yaml           ← Default agent: business + society AI focus
├── example-technical-agent.yaml ← Example: technical AI developments (disabled)
├── example-minimal-agent.yaml   ← Minimal starting template for new agents
├── agent-config.schema.json     ← JSON Schema for IDE validation (SRC-073)
└── scheduler.schema.json        ← JSON Schema for scheduler.yaml
```

Each `*-agent.yaml` is an **independent configuration unit** — its own curation theme,
LLM config, source lists, Twitter handles, and output directory. The scheduler discovers
all enabled agents from `scheduler.yaml` at startup.

### 4.2 Per-agent YAML: every field explained

Here is the complete annotated `default-agent.yaml` as a reference:

```yaml
# Unique identifier — becomes the output directory name and storage namespace.
# Must match the `id:` in scheduler.yaml. Allowed: letters, digits, hyphens, underscores.
# (SRC-072, SRC-145)
agent_id: default

# ── LLM configuration (SRC-057) ────────────────────────────────────────────
llm:
  provider: openai               # "openai" | "anthropic" | "google" (SRC-057)
  model: gpt-4o                  # Model for daily + weekly curation

  # Per-cadence overrides — monthly and annual benefit from research-grade models (SRC-054)
  cadence_overrides:
    monthly:
      model: o3
      thinking: false
    annual:
      model: o3
      thinking: true             # Extended thinking for annual predictions (SRC-032)

# Path to the prompt directory. The curation agent selects the cadence-specific
# file (daily.md / weekly.md / monthly.md / annual.md) automatically. (SRC-113)
# The SHA-256 hash of the selected prompt is embedded in every digest output. (SRC-129)
curation_prompt: prompts/daily.md

# ── Source tier configuration (SRC-016–SRC-021, SRC-034) ───────────────────
sources:
  # Tier 1a — Your own priority sources (optional). Highest curation weight.
  # Examples: internal wikis, company blog, niche domain expertise sites.
  custom: []                     # Add domain strings, e.g. ["myblog.com"] (SRC-017)

  # Tier 1b — Popular business press (SRC-018)
  tier_1b:
    - reuters.com
    - bloomberg.com
    - wsj.com
    - ft.com
    - economist.com
    - axios.com

  # Tier 2 — Top tech and AI company blogs (SRC-019)
  tier_2:
    - news.ycombinator.com
    - anthropic.com
    - openai.com
    - huggingface.co
    - towardsai.net

  # Tier 3 — Tech business press (SRC-020)
  tier_3:
    - techcrunch.com
    - theverge.com
    - technologyreview.com
    - wired.com
    - stratechery.com
    - platformer.news

  # Tier 4 — Policy and research (SRC-021)
  tier_4:
    - brookings.edu
    - rand.org
    - hai.stanford.edu
    - ainowresearch.org

# ── Twitter/X influencer configuration (SRC-036–SRC-046, SRC-063) ──────────
# Twitter = signal and commentary, NOT primary news (SRC-047).
# Tweets surface leads for web search — they do not become direct citations
# unless the tweet itself IS the news (e.g., an executive announcement on X
# before press coverage exists).
# Library: tweepy ≥ 4.14 via TWITTER_BEARER_TOKEN bearer-token auth (SRC-063).
#
# Add, remove, or re-weight handles without code changes (SRC-046).
# Bearer token degradation: if TWITTER_BEARER_TOKEN is absent or the API fails,
# the digest is still produced from web sources only (SRC-148).
twitter:
  enabled: true
  handles:
    - { handle: karpathy,      weight: 1.0 }   # Andrej Karpathy
    - { handle: sama,          weight: 1.0 }   # Sam Altman
    - { handle: demishassabis, weight: 1.0 }   # Demis Hassabis
    - { handle: DarioAmodei,   weight: 1.0 }   # Dario Amodei
    - { handle: ylecun,        weight: 1.0 }   # Yann LeCun
    - { handle: AndrewYNg,     weight: 1.0 }   # Andrew Ng
    - { handle: fchollet,      weight: 1.0 }   # François Chollet
    - { handle: drfeifei,      weight: 1.0 }   # Fei-Fei Li
    - { handle: emilymbender,  weight: 1.0 }   # Emily M. Bender

# ── Top-N article limits per cadence (SRC-029–SRC-032) ─────────────────────
limits:
  daily_top_n:   10    # Articles in daily digest
  weekly_top_n:  7     # Articles in weekly digest (plus themes + outlook)
  monthly_top_n: 10    # Articles in monthly digest (plus themes + anticipated news)
  annual_top_n:  10    # Top 10 articles + 10 predictions in annual digest

# Output directory — {agent_id} resolved to "default" at runtime (SRC-145)
output_dir: outputs/{agent_id}
```

**Full field documentation:** [`docs/config/config-reference.md`](config/config-reference.md)

#### Switching LLM providers (SRC-057)

Change only the `llm:` block — nothing else in the pipeline changes:

```yaml
# Switch to Anthropic
llm:
  provider: anthropic
  model: claude-3-7-sonnet-20250219
  cadence_overrides:
    annual:
      model: claude-3-7-sonnet-20250219
      thinking: true             # Extended thinking via Anthropic

# Switch to Google Gemini
llm:
  provider: google
  model: gemini-2.0-flash
```

Remember to set the corresponding secret (`ANTHROPIC_API_KEY` or `GOOGLE_API_KEY`).

#### Adjusting influencer weights (SRC-046)

Higher `weight` makes an influencer's tweets more prominent in the curation prompt signal
section. The default weight is `1.0`. Range: `0.0 < weight ≤ 10.0`.

```yaml
twitter:
  handles:
    # Boost policy-focused voices for a policy-themed agent
    - { handle: emilymbender, weight: 3.0 }
    - { handle: drfeifei,     weight: 2.0 }
    - { handle: karpathy,     weight: 1.0 }
    # Remove handles you don't want by simply omitting them
```

### 4.3 The scheduler config

`configs/scheduler.yaml` is the root configuration the scheduler reads on startup:

```yaml
# Retry policy — all retries use exponential backoff (SRC-144)
scheduler:
  max_retries: 3                   # 3 retries after initial failure
  retry_backoff_base_seconds: 30   # 30s → 60s → 120s

# Manual override API (SRC-147)
api:
  enabled: true
  host: "0.0.0.0"
  port: 8081

# Cron triggers — all UTC (SRC-052, SRC-009, SRC-028–SRC-032)
triggers:
  sourcing_daily:   "0 0 * * *"   # 00:00 UTC daily
  curation_daily:   "5 0 * * *"   # 00:05 UTC daily (after sourcing finishes)
  curation_weekly:  "0 1 * * 0"   # 01:00 UTC Sunday (Sun–Sat window)
  curation_monthly: "0 2 1 * *"   # 02:00 UTC 1st of month (prior month)
  curation_annual:  "0 3 1 1 *"   # 03:00 UTC January 1st (prior year)

# Agent registry (SRC-072)
agents:
  - id: default
    config: configs/default-agent.yaml
    enabled: true

  - id: technical
    config: configs/example-technical-agent.yaml
    enabled: false    # Set to true to activate
```

The scheduler registers **5 jobs per enabled agent** at startup:
`sourcing_daily`, `curation_daily`, `curation_weekly`, `curation_monthly`, `curation_annual`.

### 4.4 Adding a new agent

To spin up a second curation theme (e.g., AI policy) without code changes (SRC-072):

**Step 1 — Copy the minimal template:**

```bash
cp configs/example-minimal-agent.yaml configs/my-policy-agent.yaml
```

**Step 2 — Edit the YAML:**

```yaml
agent_id: policy                   # Must be unique; becomes output dir name

llm:
  provider: openai
  model: gpt-4o
  cadence_overrides:
    monthly:
      model: o3
    annual:
      model: o3
      thinking: true

curation_prompt: prompts/daily.md  # Cadence file selected automatically at runtime

sources:
  custom:
    - ftc.gov
    - nist.gov
  tier_1b:
    - reuters.com
    - ft.com
  tier_4:
    - brookings.edu
    - rand.org
    - hai.stanford.edu

twitter:
  enabled: true
  handles:
    - { handle: emilymbender, weight: 2.0 }
    - { handle: drfeifei,     weight: 1.5 }
    - { handle: ylecun,       weight: 1.0 }

limits:
  daily_top_n:   8
  weekly_top_n:  5
  monthly_top_n: 8
  annual_top_n:  10

output_dir: outputs/{agent_id}     # Resolves to outputs/policy/
```

**Step 3 — Register in the scheduler:**

```yaml
# configs/scheduler.yaml — add to the agents: list
agents:
  - id: default
    config: configs/default-agent.yaml
    enabled: true

  - id: policy
    config: configs/my-policy-agent.yaml
    enabled: true         # ← activate it
    description: "AI policy, governance, and safety curation"
```

**Step 4 — Validate and restart:**

```bash
python -m ai_news_agent.config.schema \
  --validate configs/my-policy-agent.yaml --summary

# Restart the scheduler to pick up the new agent
ai-news-schedule
```

The curation theme is primarily controlled by the prompt files in `prompts/` — see §5.

### 4.5 Validating your config

```bash
# Validate a per-agent YAML (checks all field types and required values)
python -m ai_news_agent.config.schema \
  --validate configs/default-agent.yaml --summary

# Validate the scheduler YAML
python -m ai_news_agent.config.schema \
  --validate configs/scheduler.yaml --type scheduler --summary

# Pre-commit hook (runs automatically on git commit if configured)
# See .pre-commit-config.yaml for setup
```

The loader also raises a `ConfigError` at startup if any YAML value matches a known
secret pattern (e.g., `sk-`, `AAA...`). This is a defence-in-depth check against
accidentally committing secrets in config files.

**IDE schema validation:**
The JSON Schema files `configs/agent-config.schema.json` and `configs/scheduler.schema.json`
provide live validation and autocomplete in VS Code (configured automatically via
`.vscode/settings.json`).

---

## 5. Authoring and Versioning Curation Prompts

> **The prompt is the single most important quality lever in the system (SRC-112).**
> Everything else — LLM selection, source tiers, Twitter signal — serves the prompt.

### 5.1 Prompt file locations and cadences

```
prompts/
├── daily.md       ← Daily curation (top N articles from prior 24h)   (SRC-115–SRC-123)
├── weekly.md      ← Weekly synthesis (themes + top N + week outlook) (SRC-030)
├── monthly.md     ← Monthly synthesis (big-picture + anticipated news) (SRC-031, SRC-054)
├── annual.md      ← Annual year-in-review + 10 predictions           (SRC-032, SRC-124)
└── prompt_hashes.json  ← SHA-256 manifest (CI-enforced)              (SRC-129)
```

The curation agent automatically selects the cadence-appropriate file. The `curation_prompt`
field in the agent YAML is just a path root — the cadence suffix is appended at runtime.

All four prompt files are **provider-agnostic plain natural language** (SRC-059). They work
with OpenAI, Anthropic, and Google models without provider-specific formatting tricks.
Prompts are versioned in git alongside the source code (SRC-127) and require PR review before
any change is merged (SRC-128).

### 5.2 Template placeholders

PromptBuilder injects the following placeholders at runtime. Do not change the placeholder
names — they are matched exactly by the builder (SRC-116, SRC-119–SRC-121):

| Placeholder | Description | Cadences |
|-------------|-------------|----------|
| `{{window_start_iso}}` | ISO-8601 UTC start of the lookback window | All |
| `{{window_end_iso}}` | ISO-8601 UTC end of the lookback window | All |
| `{{top_n}}` | Maximum articles to return (from `limits.*_top_n`) | All |
| `{{tier_1a_articles}}` | Formatted article list from Tier 1a sources | All |
| `{{tier_1b_articles}}` | Formatted article list from Tier 1b sources | All |
| `{{tier_2_articles}}` | Formatted article list from Tier 2 sources | All |
| `{{tier_3_articles}}` | Formatted article list from Tier 3 sources | All |
| `{{tier_4_articles}}` | Formatted article list from Tier 4 sources | All |
| `{{twitter_signal_section}}` | Formatted influencer tweets (or "API unavailable" note) | All |
| `{{search_budget_directive}}` | Search budget instruction (5/10/25/40 for daily/weekly/monthly/annual) | All |
| `{{year}}` | The year being reviewed (e.g. `2025`) | Annual only |
| `{{year_plus_1}}` | The upcoming year (e.g. `2026`) | Annual only |

**Critical rule (SRC-116):** Never use relative time phrases (`"yesterday"`, `"last week"`,
`"recently"`) in prompts. Always write full ISO dates or use the `{{window_*_iso}}`
placeholders. The LLM cannot reliably infer "yesterday" at curation time.

### 5.3 Inclusion and exclusion criteria

The default prompts implement the business-and-society-impact curation direction
from SRC-003, SRC-023–SRC-026 (SRC-118: explicit inclusion criteria with examples;
SRC-117: explicit disqualifier list). The three inclusion criteria are:

**Business impact (SRC-023):**
- New enterprise AI capabilities reaching production
- Market consolidation (acquisitions, mergers) with announced terms
- Regulatory rulings that shift competitive dynamics
- Significant enterprise adoption with named customers or revenue data

**Workforce/societal impact (SRC-024):**
- Credible job displacement or productivity studies with methodology
- Announced layoffs/hiring shifts attributed to AI
- Education system or curriculum changes in response to AI
- Accessibility breakthroughs for a named population

**Strategic/policy impact (SRC-025):**
- National AI legislation: introduced, passed, or signed
- Executive orders with enforcement teeth
- Major lawsuits with precedent implications
- Export controls, trade restrictions, sanctions on AI hardware/software

**Default disqualifier — technical depth (SRC-026):**
- Implementation tutorials, how-to content
- Model architecture papers (unless the business/policy impact was independently significant)
- Benchmark comparisons and leaderboard updates
- Framework comparisons, library benchmarks
- Code releases unless triggering documented large-scale enterprise adoption
- Academic preprints focused on technical methodology

> **To change the curation direction:** Edit the inclusion/exclusion criteria sections
> in the relevant prompt files under `prompts/`. The curation theme is what makes agents
> differ from one another — not the YAML config.

### 5.4 Output structure requirements

Every prompt instructs the LLM to produce a **two-part response** (SRC-120):

**Part 1 — Markdown narrative paragraph (3–5 sentences):**
A brief editorial framing summarizing the period's most significant theme or standout story.
Displayed on the portal as the digest's headline context.

**Part 2 — Structured JSON block:**

```json
{
  "items": [
    {
      "headline": "Full article headline exactly as published",
      "source_name": "Publication name",
      "url": "https://full-working-url-to-primary-source",
      "pub_date": "YYYY-MM-DD",
      "why_it_matters": "2–3 sentence explanation of significance. Start with consequence.",
      "impact_tags": ["business_impact"],
      "tier": "1b",
      "cross_refs": [],
      "twitter_handle": null,
      "tweet_url": null
    }
  ],
  "themes": ["Theme label"],
  "outlook": "Forward-looking paragraph (weekly/monthly/annual only)",
  "predictions": []
}
```

**Mandatory per-item rules (SRC-122–SRC-123):**
- `why_it_matters` — exactly 2–3 sentences, mandatory for every item, starts with consequence not headline
- `url` — must be a complete, working HTTPS URL. **Items without a verified URL are dropped.** This rule is enforced at two additional layers: the Scorer (post-LLM) and the renderers (SRC-049, SRC-141).

**Annual-only additions (SRC-124):**
The annual prompt requires a `predictions` array of 10 objects, each with:
- `prediction`: The statement
- `reasoning`: Evidence from the year reviewed
- `failure_condition`: How to know if the prediction was wrong
- `note`: Optional context

### 5.5 Versioning and SHA-256 hashes

Every prompt file is hashed at runtime. The hash is embedded in every digest output:

```json
{
  "prompt_version": "sha256:694abeb491da06f898daf33531e21d03f10e59907dca63c8caf2dad222ab4470"
}
```

This enables quality regression tracing — if curation quality degrades, check the prompt
version in the JSON output to identify which prompt change caused it. (SRC-129)

The `prompts/prompt_hashes.json` manifest is the CI-enforced source of truth:

```bash
# Verify the manifest matches current prompt files (must pass in CI)
ai-news-prompt-hashes --verify

# After editing any prompt file, update the manifest:
ai-news-prompt-hashes --save

# Check which files changed:
git diff prompts/
```

> **CI gate:** `ai-news-prompt-hashes --verify` runs as a mandatory CI stage. Merging with
> a stale or mismatched manifest will fail CI.

### 5.6 Prompt change process

Prompts are treated as code (SRC-126–SRC-128):

1. **Edit** the relevant file under `prompts/`.
2. **Test locally** with a dry-run: `ai-news-run --cadence daily --dry-run`
3. **Update the hash manifest**: `ai-news-prompt-hashes --save`
4. **Open a pull request.** Prompt changes require **at least one reviewer beyond the author** (SRC-128).
5. **After merge**, verify the new hash is in the JSON outputs of the next real run.

---

## 6. Running the Pipeline Locally

### 6.1 Full pipeline: sourcing → curation → rendering

The `ai-news-run` command runs all three stages in sequence:

```bash
# Ensure secrets are loaded
source .env

# Run the full pipeline for a specific cadence
ai-news-run --cadence daily --agent configs/default-agent.yaml
ai-news-run --cadence weekly --agent configs/default-agent.yaml
ai-news-run --cadence monthly --agent configs/default-agent.yaml
ai-news-run --cadence annual --agent configs/default-agent.yaml

# With explicit prompts directory (default: ./prompts)
ai-news-run --cadence daily --agent configs/default-agent.yaml --prompts-dir prompts
```

After a successful run you will find:

```
outputs/
└── default/
    ├── 2026-05-12-daily.md      ← Paste into Slack or Teams
    ├── 2026-05-12-daily.html    ← Paste into Gmail, Outlook
    ├── 2026-05-12-daily.json    ← Machine-readable; contains all monitoring metadata
    └── store.json               ← Article store (TinyDB default; use store.db with store_backend: sqlite)
```

### 6.2 Running stages individually

Each stage has its own CLI entry point. This is useful when iterating on curation prompts
without re-fetching articles, or when debugging a rendering issue:

```bash
# Stage 1 — Sourcing only
# Fetches and stores candidate articles for the current lookback window
ai-news-source --agent configs/default-agent.yaml

# Stage 2 — Curation only
# Reads the article store, calls LLM, produces a JSON digest file
# (Requires sourcing to have run first for the relevant window)
ai-news-curate --agent configs/default-agent.yaml --cadence daily
ai-news-curate --agent configs/default-agent.yaml --cadence weekly
ai-news-curate --agent configs/default-agent.yaml --cadence monthly
ai-news-curate --agent configs/default-agent.yaml --cadence annual

# Stage 3 — Rendering only
# Converts a JSON digest file into .md and .html outputs
# (Requires curation to have produced a JSON file first)
ai-news-render --input outputs/default/2026-05-12-daily.json
```

**Typical prompt iteration workflow:**

```bash
# 1. Source articles once (takes real API calls and stores to TinyDB)
ai-news-source --agent configs/default-agent.yaml

# 2. Edit the prompt: prompts/daily.md
# ...

# 3. Re-run curation only (no new sourcing — fast, cheap)
ai-news-curate --agent configs/default-agent.yaml --cadence daily

# 4. Check the output quality
cat outputs/default/$(date +%Y-%m-%d)-daily.md

# 5. Repeat steps 2–4 until satisfied

# 6. Update the hash manifest before committing
ai-news-prompt-hashes --save
```

### 6.3 Dry-run mode

Dry-run mode writes all outputs to a temporary directory and makes **zero production writes**
to the article store. It is designed for CI testing and safe local experimentation (SRC-102).

```bash
# Dry-run with real API calls (requires secrets)
source .env
ai-news-run --cadence daily --dry-run

# Dry-run with mock LLM (no API keys needed — for CI and offline testing)
SMOKE_TEST_MOCK_LLM=1 SMOKE_TEST_MOCK_TWITTER=1 \
  ai-news-run --cadence daily --dry-run

# Skip sourcing and re-curate from existing store (dry-run mode)
ai-news-run --cadence daily --dry-run --skip-sourcing

# Convenience script (sets mock flags automatically)
./scripts/dry_run.sh
```

In dry-run mode:
- Outputs go to a temporary directory (e.g., `/tmp/ai-news-dry-run-XXXX/`)
- The `PipelineRunResult.dry_run` flag is set to `True`
- No writes to `outputs/default/` or the article store (`store.json` / `store.db`)
- The structured log at run end marks `"dry_run": true`

### 6.4 Historical backfill and re-runs

To curate articles for a specific historical window (e.g., to backfill a missed daily digest
or re-run with an improved prompt), use `--window-start` and `--window-end` (SRC-028, SRC-147):

```bash
# Backfill a specific day
ai-news-run \
  --cadence daily \
  --agent configs/default-agent.yaml \
  --window-start 2026-05-10 \
  --window-end 2026-05-10

# Backfill a week with an explicit window
ai-news-run \
  --cadence weekly \
  --agent configs/default-agent.yaml \
  --window-start 2026-05-04 \
  --window-end 2026-05-10

# Re-run curation only for a specific window (skip sourcing)
ai-news-run \
  --cadence daily \
  --skip-sourcing \
  --window-start 2026-05-10 \
  --window-end 2026-05-10
```

**Note:** `--window-start` and `--window-end` must **both** be provided or **neither** (default
auto-computes the current window). Providing only one of them raises an error.

**Re-runs are idempotent (SRC-145):** Output filenames are date-stamped, so re-running for the
same date simply overwrites the previous output files — no duplicates, no stale state.

### 6.5 The background scheduler

The background scheduler reads `configs/scheduler.yaml` and runs all enabled agents on their
configured cron schedules. Use this for continuous local operation or as a long-running service:

```bash
# Start the scheduler (runs until interrupted with Ctrl+C)
source .env && ai-news-schedule

# The scheduler logs all job registrations at startup:
# → Registered job: default-sourcing-daily (0 0 * * *)
# → Registered job: default-curation-daily (5 0 * * *)
# → Registered job: default-curation-weekly (0 1 * * 0)
# → Registered job: default-curation-monthly (0 2 1 * *)
# → Registered job: default-curation-annual (0 3 1 1 *)

# Manually trigger a job without waiting for the cron time (SRC-147)
ai-news-schedule --trigger-agent default --job curation --cadence daily

# One-shot invocation — runs a single job and exits (for serverless triggers)
ai-news-oneshot --job curation --cadence daily --agent default
```

**Scheduled job times (all UTC):**

| Job | Schedule | Notes |
|-----|----------|-------|
| Daily sourcing | `0 0 * * *` | 00:00 UTC every day |
| Daily curation | `5 0 * * *` | 00:05 UTC — runs 5 min after sourcing |
| Weekly curation | `0 1 * * 0` | 01:00 UTC Sunday; covers Sun–Sat |
| Monthly curation | `0 2 1 * *` | 02:00 UTC 1st of month; covers prior month |
| Annual curation | `0 3 1 1 *` | 03:00 UTC January 1st; covers prior year |

### 6.6 Output files

All digest outputs follow the naming convention `{YYYY-MM-DD}-{cadence}.{ext}` (SRC-145):

```
outputs/
└── default/
    ├── 2026-05-12-daily.md        ← Today's daily digest (Markdown)
    ├── 2026-05-12-daily.html      ← Today's daily digest (HTML)
    ├── 2026-05-12-daily.json      ← Today's daily digest (JSON with full metadata)
    ├── 2026-05-12-weekly.md       ← Weekly digest (written Monday, covers Sun–Sat)
    ├── 2026-05-01-monthly.md      ← Monthly digest (written 1st, covers prior month)
    ├── 2026-01-01-annual.md       ← Annual digest (written Jan 1, covers prior year)
    └── store.json                 ← Article store (TinyDB default; use store.db with store_backend: sqlite)
```

**Re-runs overwrite cleanly.** The date in the filename ensures idempotency — running the
daily curation twice on the same day simply overwrites the output files.

**JSON metadata structure** (every `.json` file contains):

```json
{
  "schema_version": "1.0",
  "metadata": {
    "agent_id": "default",
    "cadence": "daily",
    "run_date": "2026-05-12",
    "window_start": "2026-05-11T00:00:00Z",
    "window_end": "2026-05-11T23:59:59Z",
    "prompt_version": "sha256:694abeb...",
    "llm_provider": "openai",
    "llm_model": "gpt-4o",
    "items_considered": 47,
    "items_included": 8,
    "items_by_tier": {"tier_1b": 3, "tier_2": 2, "tier_3": 2, "tier_4": 1},
    "items_by_source_class": {"web": 7, "twitter": 1},
    "token_usage": 18500,
    "twitter_signal_available": true,
    "tweet_api_call_count": 9
  },
  "items": [ ... ],
  "prompt_version": "sha256:694abeb...",
  "urls": [ ... ]
}
```

**Distribution (manual — no automation in v1):**
- Email: paste `.html` file content into a mail client (Gmail, Outlook)
- Slack/Teams: paste `.md` content directly into a channel
- Static site: sync the `outputs/` directory to GitHub Pages, S3+CloudFront, etc.

---

## 7. Launching the Web Portal

The web portal provides a browser-based view of all digests with cadence tabs, theme
visualization, impact filtering, and download links for all three export formats.

### 7.1 Start the portal

```bash
source .env
ai-news-portal

# Default: http://localhost:8080
# Override host/port:
ai-news-portal --host 0.0.0.0 --port 9090

# Or via uvicorn directly (useful for development with auto-reload):
uvicorn ai_news_agent.portal.app:app --reload --port 8080
```

The portal scans `outputs/` at startup and on each request for new digest files. No restart
is needed to pick up new digests.

### 7.2 Portal features

**Home page (`GET /`):**
- Lists all available digests organized by cadence tab (daily / weekly / monthly / annual)
- Cadence tab color palettes (distinct visual identity per cadence)
- Tab selection persists across page reloads via `sessionStorage`

**Digest view (`GET /digest/{agent_id}/{cadence}/{date}`):**
- **Daily view:** Article cards with headline, source, publication date, "why it matters"
  summary, impact tags, and source tier color-coded left border
- **Weekly view:** Theme section, top article cards, week outlook paragraph
- **Monthly view:** Big-picture themes, article cards, anticipated news for next month
- **Annual view:** Top 10 articles with circular rank badges (gold/silver/bronze for top 3),
  10 predictions with reasoning and failure conditions

**Filtering:**
- Single-select impact filter pills: **All / Business / Workforce / Policy** (SRC-023–SRC-026)
- Word cloud visualization of theme terms (linear font-size scaling 0.78em–1.55em)
- MutationObserver-based ARIA state sync for accessibility

**Export downloads (`GET /download/{agent_id}/{cadence}/{date}/{format}`):**
- Download any digest as `.md`, `.html`, or `.json`
- Links appear in the digest view header

**Multiple agents:**
An agent switcher in the navigation bar lets you browse digests from different agent
configurations (e.g., `default` vs. `technical`) without changing URLs.

### 7.3 Health and API endpoints

```bash
# Health check — returns agent list, digest counts, scheduler status
GET /api/health
# Response: { "status": "ok", "agents": [...], "total_digests": 42, "scheduler": {...} }

# Manual trigger — on-demand pipeline run (SRC-147)
# Requires SCHEDULER_API_KEY in the Authorization header
POST /api/trigger
Content-Type: application/json
Authorization: Bearer <SCHEDULER_API_KEY>

{
  "job": "curation",
  "cadence": "daily",
  "agent": "default"
}

# Job status — check status of a running or completed job
GET /api/status/{job_id}
```

---

## 8. Deploying to a Serverless Container Platform

The system is designed as a **serverless container** (SRC-080–SRC-086). The same Docker image
runs on your laptop, in CI, and in production — no code changes between environments.

Full deployment instructions with copy-paste commands:
[`docs/deployment/deployment-guide.md`](deployment/deployment-guide.md)

### 8.1 Build and test the container locally

The same multi-stage Docker image runs on a developer laptop, in CI, and in production —
**no code changes between environments** (SRC-085). The Dockerfile uses a builder stage
(dependencies) and a runtime stage (application only) to keep the image lean.

```bash
# Build the multi-stage image (SRC-085)
docker build -t ai-news-agent .

# Run locally with secrets injected at runtime (never baked in — SRC-111)
docker run --rm \
  -e OPENAI_API_KEY="$OPENAI_API_KEY" \
  -e TWITTER_BEARER_TOKEN="$TWITTER_BEARER_TOKEN" \
  -p 8080:8080 \
  -v "$(pwd)/outputs:/app/outputs" \
  -v "$(pwd)/configs:/app/configs" \
  ai-news-agent

# Smoke test inside the container (zero-cost: mock LLM + mock Twitter)
docker run --rm \
  -e SMOKE_TEST_MOCK_LLM=1 \
  -e SMOKE_TEST_MOCK_TWITTER=1 \
  -e OPENAI_API_KEY=sk-test-not-real \
  -e TWITTER_BEARER_TOKEN=test-not-real \
  ai-news-agent \
  ai-news-run --cadence daily --dry-run
```

### 8.2 GCP Cloud Run + Cloud Scheduler

GCP is the recommended starting point if you already have a GCP project. (SRC-088–SRC-089)

```bash
export PROJECT_ID="your-gcp-project-id"
export REGION="us-central1"
export SA_EMAIL="ai-news-runner@${PROJECT_ID}.iam.gserviceaccount.com"

# 1. Build and push the image
IMAGE="us-docker.pkg.dev/${PROJECT_ID}/ai-news-agent/ai-news-agent"
docker build -t "${IMAGE}:latest" .
docker push "${IMAGE}:latest"

# 2. Deploy to Cloud Run (SRC-101)
gcloud run deploy ai-news-agent \
  --image "${IMAGE}:latest" \
  --region "${REGION}" \
  --service-account "${SA_EMAIL}" \
  --set-secrets "OPENAI_API_KEY=OPENAI_API_KEY:latest,TWITTER_BEARER_TOKEN=TWITTER_BEARER_TOKEN:latest" \
  --max-instances 3 \
  --min-instances 0 \
  --memory 1Gi \
  --timeout 900 \
  --no-allow-unauthenticated

# 3. Create the daily curation scheduler job (SRC-052, SRC-084)
SERVICE_URL=$(gcloud run services describe ai-news-agent \
  --region="${REGION}" --format="value(status.url)")

gcloud scheduler jobs create http ai-news-curation-daily \
  --schedule="5 0 * * *" \
  --time-zone="UTC" \
  --uri="${SERVICE_URL}/api/oneshot" \
  --message-body='{"job":"curation","cadence":"daily","agent":"default"}' \
  --max-retry-attempts=3 \
  --min-backoff-duration="30s" \
  --max-backoff-duration="120s"
```

**Annual cadence note (SRC-090):** For the annual synthesis, increase Cloud Run timeout:
`--timeout 3600` (1 hour). Annual runs using extended-thinking models typically take 5–10 minutes.

Full GCP guide: [`docs/deployment/deployment-guide.md`](deployment/deployment-guide.md) §4.

### 8.3 AWS App Runner + EventBridge

```bash
export AWS_REGION="us-east-1"
export AWS_ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
export ECR_REGISTRY="${AWS_ACCOUNT_ID}.dkr.ecr.${AWS_REGION}.amazonaws.com"

# Build and push to ECR
aws ecr get-login-password --region "${AWS_REGION}" | \
  docker login --username AWS --password-stdin "${ECR_REGISTRY}"

docker build -t "${ECR_REGISTRY}/ai-news-agent:latest" .
docker push "${ECR_REGISTRY}/ai-news-agent:latest"
```

> **Lambda caveat (SRC-090):** Lambda's 15-minute hard timeout is fine for daily, weekly, and
> monthly curation. For annual synthesis (extended-thinking model, 5–10 minutes), Lambda leaves
> no headroom. **Use App Runner or Fargate for annual cadence deployments.**

Full AWS guide: [`docs/deployment/deployment-guide.md`](deployment/deployment-guide.md) §5.

### 8.4 Azure Container Apps + Logic Apps

Full Azure guide: [`docs/deployment/deployment-guide.md`](deployment/deployment-guide.md) §6.

### 8.5 CI/CD pipeline overview

GitHub Actions runs on every push and pull request:

```
Stage 1: lint          → ruff check + ruff format --check                  (SRC-098)
Stage 2: typecheck     → mypy src/                                         (advisory)
Stage 3: test          → pytest tests/unit/ + tests/integration/ + tests/ci/  (SRC-098)
Stage 4: prompt-hashes → ai-news-prompt-hashes --verify                   (SRC-129)
Stage 5: docker-build  → docker buildx build + import test                (SRC-099)
Stage 6: smoke-docker  → dry-run pipeline inside container, assert fields  (SRC-102)
```

On push to `main`, `deploy.yml` additionally runs:
```
Stage 7: push         → push image to registry (GCP/AWS/Azure)            (SRC-100)
Stage 8: deploy       → deploy to Cloud Run / App Runner / Container Apps  (SRC-101)
Stage 9: smoke-live   → health check on deployed service URL               (SRC-102)
```

**Secrets in CI (SRC-073, SRC-111):**
- CI jobs use **synthetic dummy values** (`OPENAI_API_KEY=sk-ci-test-not-real`)
- Real secrets are **only** in protected GitHub Environments (`production`, `staging`)
- Container smoke tests use `SMOKE_TEST_MOCK_LLM=1` — zero real API calls in CI

See [`docs/deployment/secrets-management.md`](deployment/secrets-management.md) for
Workload Identity Federation (WIF) setup on GCP, AWS, and Azure.

---

## 9. Monitoring and Quality

### Per-run monitoring

Every run emits a structured `curation_run_complete` log event and embeds all monitoring
fields in the JSON digest output (SRC-150):

```json
{
  "event": "curation_run_complete",
  "agent_id": "default",
  "cadence": "daily",
  "items_considered": 47,
  "items_included": 8,
  "items_by_tier": {"tier_1b": 3, "tier_2": 2, "tier_3": 2, "tier_4": 1},
  "items_by_source_class": {"web": 7, "twitter": 1},
  "token_usage": 18500,
  "llm_provider": "openai",
  "llm_model": "gpt-4o",
  "prompt_version": "sha256:694abeb...",
  "twitter_signal_available": true,
  "tweet_api_call_count": 9,
  "duration_seconds": 127.4
}
```

### Reliability mechanisms (SRC-142–SRC-148)

| Mechanism | What it protects against |
|-----------|--------------------------|
| Retry (3 attempts, 30s→60s→120s backoff) | Transient LLM/network failures (SRC-144) |
| Idempotent date-stamped filenames | Duplicate or inconsistent re-runs (SRC-145) |
| Manual override endpoint | Missed schedules and backfills (SRC-147) |
| Twitter graceful degradation | Twitter API outages blocking digests (SRC-148) |
| Failure alerting (Slack webhook) | Silent failures going unnoticed (SRC-146) |

Full reliability guide: [`docs/operations/reliability.md`](operations/reliability.md)

### Quality review (SRC-149–SRC-150)

After 4–6 weeks of operation, run the quality review checklist:

```bash
# Check source dominance — are the same domains appearing in >50% of digests?
for f in outputs/default/*-daily.json; do
  jq -r '.items[].source_name' "$f"
done | sort | uniq -c | sort -rn | head -20

# Check tier distribution — is Tier 4 (policy/research) consistently at 0%?
for f in outputs/default/*-daily.json; do
  jq -r '.metadata.items_by_tier' "$f"
done
```

Full quality monitoring playbook:
[`docs/operations/quality-monitoring.md`](operations/quality-monitoring.md)

---

## 10. Troubleshooting

### "No articles sourced for window"

**Cause:** Web search returned no results, or all results were filtered as duplicates.
**Fix:**
1. Check that `OPENAI_API_KEY` is valid and has active quota
2. Verify `WEB_SEARCH_API_KEY` is set and `WEB_SEARCH_PROVIDER` is `"brave"` or `"tavily"`
3. Check that the lookback window dates are correct (run with `--window-start` / `--window-end`)
4. Look at the sourcing log: `structlog` output includes `items_fetched` per source tier

### "Twitter API unavailable — continuing with web sources only"

**Cause:** `TWITTER_BEARER_TOKEN` is unset, expired, or the API returned an error.
**Effect:** The digest is still produced from web sources alone, with a note in the digest
and `twitter_signal_available: false` in the JSON metadata (SRC-148).
**Fix:** Check your Twitter API Basic tier access at developer.twitter.com and verify
the bearer token value in `.env`.

### "ConfigError: potential secret found in YAML"

**Cause:** A value in your agent YAML matches a known secret pattern (e.g., starts with `sk-`).
**Fix:** Remove the secret from the YAML file and put it in `.env` as an environment variable.
The config loader scans for secrets as a defence-in-depth check.

### "prompt_hashes --verify failed"

**Cause:** A prompt file was edited without updating the hash manifest.
**Fix:** Run `ai-news-prompt-hashes --save` to regenerate the manifest, then commit both
the changed prompt file and the updated `prompts/prompt_hashes.json`.

### "Items without URLs were dropped"

**Cause:** The LLM returned items without verifiable working URLs. This is expected and by design.
The URL requirement is non-negotiable (SRC-049, SRC-123, SRC-141) — items without confirmed,
accessible URLs are dropped at three layers: the LLM prompt instructions, the Scorer
(post-LLM), and the renderers.
**Fix:** If too many items are being dropped, check your prompt's URL verification instructions
or adjust the search budget to give the LLM more lookups.

### "Annual curation timed out" (cloud deployment)

**Cause:** Extended-thinking models can take 5–10 minutes for annual synthesis.
**Fix:** See [§8.2](#82-gcp-cloud-run--cloud-scheduler) — increase the Cloud Run timeout
to `--timeout 3600`. For AWS Lambda, switch to App Runner for the annual cadence (SRC-090).

### Tests failing after changes

```bash
# Verify lint passes
ruff check src/ tests/

# Verify prompt hashes are up to date
ai-news-prompt-hashes --verify

# Run the unit test suite
pytest tests/unit/ -v

# Check for type errors
mypy src/
```

---

## 11. Requirement Traceability

This user guide covers the following source requirements from
[`docs/requirements/requirements.md`](requirements/requirements.md):

| SRC-* | Requirement | Section in this guide |
|-------|-------------|----------------------|
| SRC-001–SRC-004 | System overview, cadences, output formats | §1 |
| SRC-006–SRC-013 | Three-agent architecture, sourcing behavior | §1, §6.2 |
| SRC-014–SRC-032 | Curation agent, four cadences, top-N limits | §1, §4.2, §6.1–6.5 |
| SRC-016–SRC-021 | Five source tiers (1a custom, 1b business press, 2 AI blogs, 3 tech press, 4 policy) | §4.2 |
| SRC-034, SRC-046 | Configurable source tiers and Twitter handles | §4.2 |
| SRC-036–SRC-045 | Default 9 Twitter influencer handles | §4.2 |
| SRC-047–SRC-049 | Twitter as signal; URL enforcement | §5.4 |
| SRC-052 | Cron scheduler; five trigger types | §6.5 |
| SRC-054–SRC-061 | Research LLM for monthly/annual; provider-agnostic | §4.2 |
| SRC-062–SRC-070 | Twitter/X integration via tweepy (SRC-063: tweepy ≥ 4.14, bearer-token auth) | §4.2 |
| SRC-071–SRC-073 | Config system; secrets from env vars only | §3, §4 |
| SRC-080–SRC-086 | Serverless container deployment (SRC-085: same image local/CI/prod) | §8 |
| SRC-090 | Lambda timeout caveat for annual cadence | §8.3 |
| SRC-097–SRC-104 | CI/CD pipeline stages | §8.5 |
| SRC-105–SRC-111 | Secrets from env vars; never in YAML | §3 |
| SRC-112–SRC-131 | Curation prompts; SRC-117 disqualifiers; SRC-118 inclusion criteria; SRC-127 versioned in git; SRC-128 PR review; SHA-256 hash | §5 |
| SRC-133–SRC-136 | Web portal; cadence views; export downloads | §7 |
| SRC-135–SRC-141 | Rendering; URL enforcement; three formats | §6.6 |
| SRC-142–SRC-148 | Reliability: retry, idempotency, degradation | §9 |
| SRC-145 | Date-stamped idempotent filenames | §6.4, §6.6 |
| SRC-147 | Manual trigger override | §6.4, §7.3 |
| SRC-148 | Twitter graceful degradation | §10 |
| SRC-149–SRC-150 | Quality monitoring; per-run metrics | §9 |

**Related documentation:**

| Document | Purpose |
|----------|---------|
| [`docs/architecture.md`](architecture.md) | Complete system architecture — canonical reference for all design decisions |
| [`docs/config/config-reference.md`](config/config-reference.md) | Full config field reference with defaults and examples |
| [`docs/deployment/deployment-guide.md`](deployment/deployment-guide.md) | Step-by-step cloud deployment: GCP, AWS, Azure |
| [`docs/deployment/secrets-management.md`](deployment/secrets-management.md) | Workload Identity Federation, cloud secrets managers |
| [`docs/operations/reliability.md`](operations/reliability.md) | Retry policy, idempotency, manual override, Twitter degradation |
| [`docs/operations/quality-monitoring.md`](operations/quality-monitoring.md) | 4–6 week quality review playbook; per-run monitoring fields |
| [`docs/requirements/requirements.md`](requirements/requirements.md) | **Canonical source of truth** — all 150 SRC-* requirements |
| [`docs/requirements/spec.md`](requirements/spec.md) | Product specification with AC-001 acceptance criteria |
