# Working Brief — AI News Curation Agent

> **Generated / Last Verified:** 2026-05-10  
> **Status:** Active reference for all implementation stages  
> **Source-of-Truth Order:** `requirements.md` ▶ `spec.md` ▶ `implementation-plan.md` ▶ `backlog.md`  
> **VAL-002 compliance:** Produced after reading all four planning artifacts in full (direct file reads, not summaries).  
> **Coverage:** All 150 source requirements (SRC-001–SRC-150) are mapped and accounted for.

---

## 1. Goal Confirmation

Build a **multi-agent AI News Curation platform** that autonomously sources, curates, and renders AI-industry news digests on **daily, weekly, monthly, and annual cadences**. The system delivers two non-negotiable outputs (SRC-004):

1. **Web portal** — well-designed browser UI for browsing curated digests by timeframe and agent configuration; theme visualization; export downloads.
2. **Structured export files** — Markdown, HTML, and JSON per curation run; downloadable from the portal; usable in email, Slack/Teams, or static sites without code changes.

**Core design constraints (non-negotiable):**

| Constraint | Detail | Source |
|------------|--------|--------|
| Provider-agnostic LLM layer | Default **OpenAI** (OpenAI Agents SDK); swap provider without pipeline changes | SRC-055–SRC-061 |
| Twitter/X integration | Via **tweepy**, bearer-token auth, signal-only role; graceful degradation if unavailable | SRC-062–SRC-070, SRC-148 |
| Per-agent YAML configs | Runtime behavior without code changes; secrets always in env vars, never YAML | SRC-071–SRC-073 |
| Serverless containers | GCP Cloud Run / AWS App Runner / Azure Container Apps | SRC-075–SRC-086 |
| Working links enforced | Every curated item must have a retrievable URL; no URL = dropped at renderer (non-negotiable) | SRC-049, SRC-141 |

_All 150 source requirements (SRC-001–SRC-150) are addressed by this system._

---

## 2. Source File Verification and Consistency Audit

### 2.1 File Inventory

| File | Lines | Status | Role |
|------|-------|--------|------|
| `docs/requirements/requirements.md` | 200 | ✅ Present, non-empty | **Canonical source of truth.** Full product specification: functional requirements, architecture, deployment, prompt design, and operations. Wins on any conflict with other files. |
| `docs/requirements/spec.md` | 221 | ✅ Present, non-empty | **Implementation specification.** Maps all 200 source lines to `SRC-001`–`SRC-150` (150 entries confirmed). Defines VAL-001, VAL-002, AC-001. Defers to `requirements.md` on conflict. |
| `docs/requirements/implementation-plan.md` | 26 | ✅ Present, non-empty | **Delivery slices.** Six sequential slices (SLICE-001–SLICE-006) from spec refinement through coverage review. Each cites all SRC-* IDs and declares exit criteria. |
| `docs/requirements/backlog.md` | 21 | ✅ Present, non-empty | **Prioritized backlog.** Five P0/P1 items sequenced from spec refinement → first slice → validation → remaining workflows → ops/docs. Each cites all SRC-* IDs. |

### 2.2 Consistency Checks

| Check | Result | Detail |
|-------|--------|--------|
| All four files present and non-empty | ✅ Pass | Verified by filesystem inspection |
| `spec.md` source count = 150 unique SRC-* entries | ✅ Pass | `grep -o "\| SRC-[0-9]*" spec.md \| sort -u \| wc -l` → 150 |
| Highest SRC entry is SRC-150 | ✅ Pass | Last entry: SRC-150 (Quality Monitoring, source line 201) |
| `implementation-plan.md` defines exactly 6 slices | ✅ Pass | SLICE-001 through SLICE-006 confirmed |
| `backlog.md` defines exactly 5 items (3×P0, 2×P1) | ✅ Pass | Sequencing matches SLICE order |
| No conflicting instructions between files | ✅ Pass | `requirements.md` is root; all derivatives consistent and subordinate |
| No orphaned or duplicate SRC-* IDs | ✅ Pass | Sequential SRC-001 through SRC-150, no gaps or repeats |
| Default LLM provider: OpenAI | ✅ Consistent | requirements.md line 76 = SRC-057; confirmed across all four files |
| Document store recommendation | ✅ Acceptable | requirements.md says "document store perhaps" (SRC-053); TinyDB is an implementation-level choice consistent with the intent |
| Secrets model | ✅ Consistent | requirements.md, spec.md, and backlog all agree: secrets in env vars only, never YAML (SRC-073) |

**Consistency verdict: ✅ All four files are present, mutually non-contradictory, and internally consistent. `requirements.md` governs.**

---

## 3. Functional Areas — Definitions and Backlog Mapping

The system is organized into **eight functional areas**. Every area traces to one or more backlog items and a specific set of SRC-* source requirements.

---

### 3.1 Sourcing Agent

**Backlog item(s):** "Implement first end-to-end product slice" (P0) · "Complete remaining core workflows" (P1)  
**Implementation slice:** SLICE-003  
**Primary SRC-* IDs:** SRC-006–SRC-013, SRC-033–SRC-053, SRC-062–SRC-070, SRC-148

**Purpose:** Fetch candidate articles from the web and Twitter/X within a configurable lookback window. Store exactly once (deduplication). This agent **only sources — curation happens later** (SRC-013).

**Key behaviors:**
- **Daily lookback:** 00:00–23:59 UTC; runnable multiple times per day — adds new candidates only, never duplicates (SRC-008–SRC-012)
- **Article storage schema per record:** `url_hash` (dedup key), `url`, `headline`, `abstract`, `source_name`, `pub_date`, `fetched_at`, `tier`, `source_class` (`web`|`twitter`), `agent_id`, optional `twitter_handle` + `tweet_url` (SRC-011)
- **Web fetch methods (configurable):** LLM-native web search, Brave Search API, Tavily API (SRC-053, SRC-060)
- **Twitter/X fetch:** tweepy v2 API, bearer token from env var `TWITTER_BEARER_TOKEN`; filter out pure replies and RTs without comment; hydrate linked URLs (SRC-063–SRC-069)
- **Twitter role:** signal and lead generation — not primary news, unless the tweet itself is the announcement (SRC-047)
- **Source tier hierarchy** (determines curation scoring weight):
  - **Tier 1a:** User-configured custom sources — optional (SRC-017)
  - **Tier 1b:** Business press — Reuters, Bloomberg, WSJ, FT, The Economist, Axios (SRC-018)
  - **Tier 2:** Tech/AI blogs — YCombinator, Netflix Tech Blog, Anthropic Blog, OpenAI Blog, HuggingFace Blog, Towards AI (SRC-019)
  - **Tier 3:** Tech business press — The Information, Stratechery, Platformer, TechCrunch, The Verge, MIT Tech Review, Wired, FastCompany (SRC-020)
  - **Tier 4:** Policy/research — Brookings, RAND, Stanford HAI, AI Now Institute, government press releases (SRC-021)
- **Default Twitter influencer handles:** @karpathy, @sama, @demishassabis, @DarioAmodei, @ylecun, @AndrewYNg, @fchollet, @drfeifei, @emilymbender — all configurable without code changes (SRC-036–SRC-046)
- **Graceful degradation:** If Twitter API is unavailable, sourcing continues from web sources alone; digest notes that influencer signal was unavailable for this run (SRC-148)
- **Deduplication:** Primary key = normalized URL (SHA-256 hash); secondary signal = headline similarity (Levenshtein ≥ 0.85 → flag for review) (SRC-012)

---

### 3.2 Curation Agent

**Backlog item(s):** "Implement first end-to-end product slice" (P0) · "Complete remaining core workflows" (P1)  
**Implementation slice:** SLICE-003 → SLICE-004  
**Primary SRC-* IDs:** SRC-014–SRC-032, SRC-047–SRC-049, SRC-054, SRC-070, SRC-112–SRC-131

**Purpose:** Intelligently score and prioritize candidate articles for each lookback window using LLM reasoning against a configurable curation prompt. Output ranked digests with "why it matters" summaries.

**Key behaviors:**
- Runs at the start of each lookback period; re-runnable on user demand for any period (SRC-028)
- **LLM-powered scoring** against curation prompt; default scoring criteria:
  - ✅ **Business impact** — changes how companies create value, compete, or operate (SRC-023)
  - ✅ **Workforce/societal impact** — changes how people work, learn, or live (SRC-024)
  - ✅ **Strategic/policy impact** — legislation, lawsuits, geopolitical AI moves, safety incidents (SRC-025)
  - ❌ **Disqualifier (default only):** technical depth — tutorials, architecture papers, benchmark deep-dives, framework comparisons, code walkthroughs (SRC-026)
- **All prioritization via LLM** — intelligent, not rule-based (SRC-027)
- **Four cadence modes** (SRC-028–SRC-032):
  - **Daily** — top N articles from previous day; headline + source + link + "why it matters" (2–3 sentences)
  - **Weekly** — Sunday–Saturday; identify 2–3 themes; forward-looking outlook; links to top articles
  - **Monthly** — first–last of month; bigger-picture themes; anticipated news; top articles; research-grade LLM configurable
  - **Annual** — Jan 1; top 10 articles of year; top 10 predictions grounded in observed trends with reasoning shown; high-reasoning/research model preferred
- **Curated item schema** (SRC-048):
  - `headline`, `source_name`, `url` (required; drop if missing — SRC-049), `pub_date` (ISO-8601), `why_it_matters` (2–3 sentences), `impact_tags`, `tier`, `cross_refs`, `twitter_handle` (null if web), `tweet_url` (null if web), `prompt_version` (SHA-256 of prompt file)
- **Hard rule:** Items without a retrievable working URL are **dropped** — not truncated or flagged, dropped entirely (SRC-049, SRC-141)
- **Prompt versioning:** each digest output records the SHA-256 hash of the prompt file used for regression tracing (SRC-129)
- **Research LLM:** configurable per cadence via YAML `cadence_overrides`; monthly and annual should use higher-reasoning model (e.g., o3 with extended thinking) (SRC-054, SRC-032)

---

### 3.3 Rendering Agent

**Backlog item(s):** "Implement first end-to-end product slice" (P0) · "Add validation and test coverage" (P0)  
**Implementation slice:** SLICE-003 → SLICE-004  
**Primary SRC-* IDs:** SRC-004, SRC-006, SRC-048–SRC-049, SRC-120, SRC-132, SRC-135–SRC-141, SRC-145

**Purpose:** Transform curation output into three downloadable export formats for every run.

**Key behaviors:**
- **Output formats:** Markdown (Slack/Teams paste-ready), HTML (email-paste-ready), JSON (machine-readable/archive-ready) — all three produced for every curation run (SRC-004, SRC-136)
- **Output path pattern:** `outputs/{agent_id}/{YYYY-MM-DD}-{cadence}.{ext}` (SRC-145)
- **Date-stamped filenames** make re-runs idempotent — overwrites cleanly (SRC-145)
- **URL enforcement at render time:** renderer independently drops any item missing a working link — final safety check after curation (SRC-141)
- All rendered files are made available for download via the portal
- Output schema and naming convention designed to accept a future thin distribution layer with zero core changes (SRC-140)
- No automated distribution in this release — files written to disk; user pastes/syncs manually (SRC-004)

---

### 3.4 Web Portal

**Backlog item(s):** "Complete remaining core workflows" (P1) · "Complete docs, operations, and deployment readiness" (P1)  
**Implementation slice:** SLICE-004  
**Primary SRC-* IDs:** SRC-004, SRC-133–SRC-134, SRC-136

**Purpose:** Browser UI for reading curated digests, switching agent configurations, and downloading export files. No auth and no portal-driven config changes in v1.

**Key behaviors:**
- **Daily view:** article card list — headline, source badge, link, "why it matters" blurb, impact tags
- **Weekly/monthly/annual view:** theme-centric layout; visual exploration (e.g., word/tag cloud, model provider filter, topic tags); links to elevated articles; forward-looking section for weekly; predictions section for annual
- **Agent config switcher:** display output from different agent configs side-by-side (display-only in v1) (SRC-134)
- **Download buttons:** Markdown, HTML, JSON per run (SRC-136)
- Responsive, well-designed UI
- No authentication required in v1 (SRC-134)
- No configuration changes through portal in v1 — future enhancement noted explicitly in SRC-072

**Stack:** FastAPI + Jinja2 templates + minimal JS (tag cloud, provider filter, download triggers)

---

### 3.5 Scheduler

**Backlog item(s):** "Complete remaining core workflows" (P1) · "Complete docs, operations, and deployment readiness" (P1)  
**Implementation slice:** SLICE-005  
**Primary SRC-* IDs:** SRC-028–SRC-032, SRC-052, SRC-072, SRC-144, SRC-147–SRC-148

**Purpose:** Trigger sourcing and curation agents on the correct cadence across all registered agent configurations. Handle retry, recovery, and manual override.

**Key behaviors:**
- **Sourcing trigger:** daily at 00:00 UTC for all enabled agents (SRC-052, SRC-009)
- **Curation triggers** (SRC-028–SRC-032):
  - Daily: 00:05 UTC each day (runs against prior-day candidates)
  - Weekly: 01:00 UTC every Sunday (covers Sun–Sat window)
  - Monthly: 02:00 UTC 1st of each month
  - Annual: 03:00 UTC January 1st
- Each **agent configuration is a separate, independently schedulable unit**; scheduler reads `configs/scheduler.yaml` and iterates over all enabled agent configs (SRC-072)
- **Retry policy:** 3 retries with exponential backoff (`30s → 60s → 120s`); all major cloud schedulers support this natively (SRC-144)
- **Manual override endpoint:** authenticated `POST /api/trigger` for on-demand runs, backfills, or misfire recovery (SRC-147)
- **Twitter graceful degradation propagation:** if Twitter API is unavailable, agent continues with web sources alone; appends note to digest (SRC-148)

**Implementation:** APScheduler; reads `configs/scheduler.yaml` at startup

---

### 3.6 Configuration System

**Backlog item(s):** "Refine traceable product specification" (P0) · "Complete docs, operations, and deployment readiness" (P1)  
**Implementation slice:** SLICE-002 → SLICE-005  
**Primary SRC-* IDs:** SRC-017, SRC-034, SRC-036–SRC-046, SRC-053–SRC-054, SRC-057, SRC-071–SRC-073, SRC-105–SRC-111

**Purpose:** YAML-based, per-agent runtime configuration controlling all behavior without code changes. Secrets always in environment variables — never in YAML files, never committed.

**Key behaviors:**
- **Per-agent config file** (e.g., `configs/default-agent.yaml`) controls:
  - `agent_id` — unique identifier
  - `llm.provider` + `llm.model` (and `cadence_overrides` for per-cadence research model)
  - `curation_prompt` — path into `prompts/`
  - `sources` — tier_1b through tier_4 plus `custom` list (Tier 1a); each tier configurable as a list of domain strings
  - `twitter.handles` — list with optional `weight` per handle (add/remove/weight without code changes — SRC-046)
  - `limits` — `daily_top_n`, `weekly_top_n`, `monthly_top_n`, `annual_top_n`
  - `output_dir` — base path for rendered files
- **Root scheduler config** (`configs/scheduler.yaml`) references all agent config files; each agent is enabled/disabled independently
- **Multiple agent instances** run simultaneously with different configs (different themes, audiences, curation prompts, LLM models) (SRC-072)
- **Secrets** (`OPENAI_API_KEY`/`LLM_API_KEY`, `TWITTER_BEARER_TOKEN`, `WEB_SEARCH_API_KEY`, cloud credentials) — **env vars or cloud secrets manager ONLY. Never committed. Never in YAML.** (SRC-073, SRC-111)
- Validation: Pydantic v2 models + `pydantic-settings`; startup fails with clear error if required secrets are missing
- **Future:** portal-driven configuration (explicitly out of scope for v1 per SRC-072)

---

### 3.7 LLM Abstraction Layer

**Backlog item(s):** "Implement first end-to-end product slice" (P0)  
**Implementation slice:** SLICE-003  
**Primary SRC-* IDs:** SRC-027, SRC-053–SRC-061, SRC-107, SRC-109, SRC-112–SRC-113, SRC-121

**Purpose:** Provider-agnostic interface allowing the LLM provider to be swapped without any change to the surrounding pipeline.

**Key behaviors:**
- **Default provider:** OpenAI via OpenAI Agents SDK (SRC-057)
- **Abstract interface** (`AbstractLLMClient`) wraps: `complete()`, `search()`, `parse_structured()` — pipeline depends only on this interface (SRC-056)
- **Prompts:** written in plain natural language — no provider-specific formatting; any frontier model should produce correct output with at most minor tuning (SRC-059)
- **Tool use described abstractly:** native web search used if provider supports it; fallback to Brave/Tavily adapter matching the same `AbstractSearchTool` interface (SRC-060)
- **Output parsing:** based on Markdown + JSON block embedded in LLM response — no dependency on provider structured-output features; provider schema enforcement is an optional enhancement only within the concrete implementation (SRC-061)
- **Research/high-thinking model:** configurable per cadence; monthly and annual can specify a different, higher-capability model with extended thinking (SRC-054, SRC-032)
- **Prompt directory:** `prompts/` — versioned alongside code; each output records prompt file SHA-256 hash (SRC-113, SRC-129)
- **Factory pattern:** `get_llm_client(config)` returns the correct concrete client; pipeline never instantiates providers directly (SRC-057)

**Class hierarchy:**
```
AbstractLLMClient  (llm/base.py)
  ├── OpenAILLMClient    ← DEFAULT (llm/openai_client.py)
  └── AnthropicLLMClient ← STUB (llm/anthropic_client.py)

AbstractSearchTool (llm/search_tools.py)
  ├── NativeOpenAISearchTool
  ├── BraveSearchTool
  └── TavilySearchTool

get_llm_client(cfg) → AbstractLLMClient   (llm/factory.py)
```

---

### 3.8 Operations / Deployment

**Backlog item(s):** "Complete docs, operations, and deployment readiness" (P1)  
**Implementation slice:** SLICE-005 → SLICE-006  
**Primary SRC-* IDs:** SRC-074–SRC-111, SRC-126–SRC-129, SRC-142–SRC-150

**Purpose:** Containerized, cloud-deployable, observable, CI/CD-ready system with structured quality monitoring per run.

**Key behaviors:**
- **Phase 1 (local dev):** developer machine, manual or local cron trigger; iterate on prompt quality (SRC-076–SRC-077)
- **Phase 2 (production):** serverless containers — same image runs locally, in CI, and in production (SRC-085)
- **CI/CD pipeline stages** (SRC-097–SRC-102):
  1. Lint: `ruff check src/ tests/`
  2. Test: `pytest` with mocked LLM and Twitter calls
  3. Build: `docker build`
  4. Push: to registry (main branch only)
  5. Deploy: cloud-specific CLI command
  6. Smoke test: `--dry-run` mode → writes to scratch location; verifies non-empty output, all required fields, no items missing URLs
- **Quality monitoring logged per run** (SRC-150):
  - Items considered / items included
  - Items by tier (1a / 1b / 2 / 3 / 4)
  - Items by source class (web vs Twitter-originated)
  - Total token usage
  - LLM provider + model name
  - Prompt file SHA-256 hash (SRC-129)
  - Twitter API call count (0 if degraded)
- **Idempotency:** date-stamped output filenames allow clean re-run overwrites (SRC-145)
- **Alerting:** cloud-native logging alert on any non-2xx response from the worker (SRC-146)
- **Dry-run mode:** produces digest but writes to scratch location only — used in CI smoke test and manual validation (SRC-102)
- **AWS Lambda note:** 15-min hard timeout is fine for daily/weekly/monthly; annual synthesis requires App Runner or Fargate (SRC-090)
- **GitHub Actions** is the default CI platform; replaceable with Cloud Build, CodePipeline, or Azure Pipelines (SRC-103–SRC-104)

---

## 4. Backlog Items → Functional Area Cross-Reference

| Priority | Backlog Item | SLICE(s) | Functional Areas Touched | Exit Criteria |
|----------|-------------|----------|--------------------------|---------------|
| **P0** | Refine traceable product specification | SLICE-001 | All eight areas (spec coverage, traceability scaffold) | `spec.md` has requirement IDs, validation rules, and acceptance checks mapped to SRC-* |
| **P0** | Implement first end-to-end product slice | SLICE-002 + SLICE-003 | Config, LLM Abstraction, Sourcing, Curation, Rendering | Project runs locally; unit tests cover core behavior |
| **P0** | Add validation and test coverage | SLICE-003 | All areas (test harness, mocking strategy, acceptance checks) | Automated tests or manual checks cover acceptance criteria |
| **P1** | Complete remaining core workflows | SLICE-004 | Curation (all cadences), Rendering (all formats), Portal, Scheduler | All P0/P1 requirements represented in code or documented checks |
| **P1** | Complete docs, operations, and deployment readiness | SLICE-005 + SLICE-006 | Ops, Config, Scheduler, CI/CD, Portal downloads, coverage review | README, config, secrets, and run/test commands are current; no P0/P1 gaps remain |

---

## 5. Backlog Item → SRC-* Mapping Summary

Every backlog item cites all 150 SRC-* IDs. The table below maps each item to the **primary** SRC-* clusters it directly addresses:

| Backlog Item | Primary SRC-* Clusters |
|--------------|------------------------|
| Refine traceable specification | SRC-001–SRC-006 (overview, agent decomposition), all others for traceability scaffold |
| First end-to-end slice | SRC-006–SRC-073 (agent core: sourcing, curation, rendering, config, LLM layer) |
| Validation and test coverage | SRC-098 (test suite), SRC-049/141 (URL enforcement test), SRC-012 (dedup test), SRC-061 (output parsing test) |
| Remaining core workflows | SRC-028–SRC-032 (all cadences), SRC-133–SRC-134 (portal), SRC-052 (scheduler), SRC-144/147/148 (reliability) |
| Docs, ops, and deployment | SRC-074–SRC-111 (deployment), SRC-126–SRC-131 (prompt ownership), SRC-142–SRC-150 (reliability + monitoring) |

---

## 6. Implementation Slice Sequencing

```
SLICE-001  Spec refinement + SRC-* traceability
           Exit: spec.md has requirement IDs, validation rules, acceptance checks mapped to SRC-*
               ↓
SLICE-002  Project scaffolding: pyproject.toml, configs/, prompts/, Dockerfile,
           .env.example, .github/workflows/, dev commands, documented install/run/test
           Exit: project runs locally with documented commands
               ↓
SLICE-003  Core domain: LLM abstraction layer, sourcing agent, document store (TinyDB),
           curation agent (all 4 cadences), rendering agent (MD/HTML/JSON),
           unit tests with mocked LLM + Twitter calls
           Exit: unit tests cover core behavior; source traceability updated in every module
               ↓
SLICE-004  User-facing surfaces: web portal (daily/weekly/monthly/annual views,
           theme visualization, agent switcher, export downloads), all four
           cadence curation outputs end-to-end verified
           Exit: functional checks cover primary workflows and required outputs
               ↓
SLICE-005  Ops layer: APScheduler (all triggers + retry + manual override),
           GitHub Actions CI/CD pipeline, quality monitoring per run (structlog),
           deployment docs, secrets guide, README
           Exit: README, config, secrets, and run/test commands are current
               ↓
SLICE-006  Coverage review: every SRC-001–SRC-150 addressed in code or documented checks;
           no P0/P1 gaps remain; AC-001 satisfied
           Exit: no unaddressed P0/P1 source requirements remain
```

**Dependency rule:** No slice begins before its predecessor's exit criteria are met.

---

## 7. Key Design Decisions

| Decision | Rationale | Source IDs |
|----------|-----------|------------|
| Document store (TinyDB) for article candidates | Articles are JSON-like objects; no relational joins needed; natural fit for dedup-by-URL; SRC-053 suggests "document store perhaps"; zero infrastructure for Phase 1 local dev | SRC-053 |
| tweepy for Twitter/X integration | Best-maintained Python client; supports v1.1 + v2 endpoints | SRC-063 |
| Twitter = signal, not primary source | Prevents unverified tweet citation; grounds output in primary web reporting; tweet can be news if the tweet itself is the announcement | SRC-047, SRC-070 |
| Prompts in `prompts/` directory, versioned by SHA-256 hash | Enables quality regression tracing; prompts treated as code requiring review | SRC-113, SRC-127–SRC-129 |
| ISO-date injection into all prompts | Prevents temporal ambiguity ("last week") in LLM context | SRC-116 |
| URL enforcement at renderer (hard drop, not flag) | Non-negotiable reader trust requirement — every item must have a working link; enforced redundantly at both curation and rendering | SRC-049, SRC-141 |
| Provider-agnostic LLM abstraction layer | Swap providers without pipeline changes; OpenAI is the default; pipeline never calls SDK directly | SRC-055–SRC-057 |
| Use provider SDK within concrete implementation | OpenAI Agents SDK (etc.) used inside concrete client; abstracted from everything upstream | SRC-057 |
| Serverless containers for deployment | Pay-per-execution, zero idle cost, cloud-agnostic, same image locally and in production | SRC-080–SRC-086 |
| Secrets in env vars only, never YAML | Security posture; cloud secrets manager at runtime; never committed, never baked into image | SRC-073, SRC-111 |
| Multiple agent configs, each independently schedulable | Different teams/themes/audiences can run simultaneously without code changes | SRC-072 |
| Annual run uses high-reasoning/research model | Synthesis quality required for year-in-review and 10 predictions grounded in observed trends; extended thinking enabled | SRC-032, SRC-054 |
| GitHub Actions as default CI/CD | Simplest hosted option; drop-in replacement with Cloud Build / CodePipeline / Azure Pipelines | SRC-103–SRC-104 |
| Twitter graceful degradation | Twitter is signal, not a hard dependency; digest still produced from web sources; run noted as degraded | SRC-148 |
| APScheduler for local + containerized scheduling | Cron-compatible, Python-native, cloud-scheduler-replaceable; reads scheduler.yaml at startup | SRC-052, SRC-144 |

---

## 8. Prompt Architecture Summary

All prompts live in `prompts/` and are **version-controlled alongside code** (SRC-127). Changes require code review with at least one reviewer beyond the author (SRC-128). Each prompt is plain natural language — no provider-specific formatting tricks (SRC-059).

**Prompt files:** `prompts/daily.md`, `prompts/weekly.md`, `prompts/monthly.md`, `prompts/annual.md`

Every prompt template **must** include all 9 requirements (SRC-115–SRC-124):

| # | Requirement | Source |
|---|-------------|--------|
| 1 | Inject **concrete ISO date ranges** (e.g., `2026-05-01` to `2026-05-07`) — never "last week" | SRC-116 |
| 2 | State **disqualifiers explicitly** — list each excluded category (tutorials, arch papers, benchmarks, framework comparisons, code walkthroughs) | SRC-117 |
| 3 | State **inclusion criteria explicitly** — with examples for each impact category (business, workforce/societal, strategic/policy) | SRC-118 |
| 4 | Include a **labeled Twitter influencer signal section** — clearly marked as context/lead-gen only; not primary citation unless the tweet is the news | SRC-119 |
| 5 | Constrain **output format strictly** — Markdown + embedded JSON metadata block (or pure JSON); enables deterministic renderer parsing | SRC-120 |
| 6 | Set **search budget** appropriate to cadence — more for monthly/annual | SRC-121 |
| 7 | Require **"why it matters" justification** (2–3 sentences) for every included item | SRC-122 |
| 8 | Require **working source URL** for every item — items failing URL retrieval are dropped | SRC-123 |
| 9 | **Annual only:** additionally instruct model to identify themes/inflection points across the year and produce a 10-prediction section grounded in observed trends with reasoning shown | SRC-124 |

Each digest output records the SHA-256 hash of the prompt file used (SRC-129), enabling quality regression tracing.

---

## 9. Data Flow Diagram

```
┌──────────────────────────────────────────────────────────────────┐
│  Scheduler  (configs/scheduler.yaml → all enabled agent configs)  │
│  Sourcing   daily   00:00 UTC  (SRC-009, SRC-052)                 │
│  Curation   daily   00:05 UTC  (prior-day window)                 │
│  Curation   weekly  01:00 UTC Sunday  (Sun–Sat window)            │
│  Curation   monthly 02:00 UTC 1st     (full month window)         │
│  Curation   annual  03:00 UTC Jan 1   (full prior year)           │
│  3 retries + exponential backoff  (SRC-144)                       │
│  POST /api/trigger  manual override  (SRC-147)                    │
└─────────────────────┬────────────────────────────────────────────┘
                      │ sourcing trigger (per agent config)
                      ▼
┌─────────────────────────────────────────────────────────────────┐
│                Sourcing Agent                                    │
│  SRC-006–SRC-013, SRC-033–SRC-053, SRC-062–SRC-070              │
│  • Twitter/X via tweepy → TweetSignal[] (SRC-063–069)           │
│    └─ graceful degrade if unavailable (SRC-148)                  │
│  • Web search (LLM-native / Brave / Tavily) → ArticleRecord[]   │
│  • Normalize URL → dedup check → insert_if_new (SRC-012)        │
│  • Classify tier (1a/1b/2/3/4) (SRC-016–021)                    │
│  • Log SourcingRunResult (SRC-150)                               │
└─────────────────────┬───────────────────────────────────────────┘
                      │ candidate ArticleRecords + TweetSignals
                      ▼
┌─────────────────────────────────────────────────────────────────┐
│           Document Store (TinyDB)  (SRC-053)                    │
│  outputs/{agent_id}/store.json — one file per agent             │
│  Shared read/write by sourcing and curation agents              │
└─────────────────────┬───────────────────────────────────────────┘
                      │ curation trigger (cadence boundary)
                      ▼
┌─────────────────────────────────────────────────────────────────┐
│                Curation Agent                                    │
│  SRC-014–SRC-032, SRC-047–049, SRC-112–131                      │
│  1. store.get_window(agent_id, window) → articles + signals      │
│  2. Select model: cadence_overrides or default (SRC-054)         │
│  3. PromptBuilder.build() → prompt + sha256_hash (SRC-116–124)  │
│  4. llm.complete(prompt, model) → raw response (SRC-027)         │
│  5. llm.parse_structured(raw) → list[CuratedItem] (SRC-061)     │
│  6. Scorer.rank(items, top_n) → drop no-URL items (SRC-049)     │
│  7. Return CurationRunResult + DigestMetadata (SRC-150)          │
└─────────────────────┬───────────────────────────────────────────┘
                      │ CurationRunResult (structured digest)
                      ▼
┌─────────────────────────────────────────────────────────────────┐
│                Rendering Agent                                   │
│  SRC-004, SRC-135–141, SRC-145                                   │
│  1. Drop items with url=None/empty (SRC-141 — final enforcement) │
│  2. outputs/{agent_id}/{YYYY-MM-DD}-{cadence}.md  (SRC-138)     │
│  3. outputs/{agent_id}/{YYYY-MM-DD}-{cadence}.html (SRC-137)    │
│  4. outputs/{agent_id}/{YYYY-MM-DD}-{cadence}.json (SRC-140)    │
│  Date-stamped = idempotent re-runs (SRC-145)                     │
└─────────────────────┬───────────────────────────────────────────┘
                      │ rendered files on disk
                      ▼
┌─────────────────────────────────────────────────────────────────┐
│                   Web Portal                                     │
│  SRC-004, SRC-133–134, SRC-136                                   │
│  FastAPI + Jinja2                                                │
│  GET /  → digest list by agent + cadence                        │
│  GET /digest/{agent}/{date}/{cadence} → cadence-specific view   │
│    daily   → article cards + why-it-matters + impact tags       │
│    weekly  → theme sections + top articles + outlook            │
│    monthly → big-picture themes + anticipated news              │
│    annual  → top 10 + predictions + year-in-review              │
│  GET /download/{agent}/{date}/{cadence}/{fmt} → serve file      │
│  Tag cloud + provider filter (JS)  (SRC-134)                    │
│  Agent config switcher (display-only v1)  (SRC-134)             │
│  No authentication in v1  (SRC-134)                             │
└─────────────────────────────────────────────────────────────────┘
```

---

## 10. Repository Layout (Canonical)

```
ai-news-agent/
├── pyproject.toml                        # PEP 517/518 build + ruff, pytest, mypy config
├── Dockerfile                            # Multi-stage; same image local → CI → prod (SRC-085)
├── .dockerignore
├── .env.example                          # Env-var documentation — never commit real values (SRC-073)
├── .github/
│   └── workflows/
│       ├── ci.yml                        # Lint + test + build (SRC-097–SRC-099)
│       └── deploy.yml                    # Push + deploy (main only) (SRC-100–SRC-102)
├── configs/
│   ├── scheduler.yaml                    # Root scheduler; references all agent configs (SRC-072)
│   ├── default-agent.yaml                # Default agent: business + society AI focus
│   └── example-technical-agent.yaml      # Example alternate-theme agent (disabled by default)
├── prompts/
│   ├── daily.md                          # Daily curation prompt template (SRC-113, SRC-127)
│   ├── weekly.md                         # Weekly curation prompt template
│   ├── monthly.md                        # Monthly curation prompt template
│   └── annual.md                         # Annual prompt — includes predictions section (SRC-124)
├── src/
│   └── ai_news_agent/
│       ├── __init__.py
│       ├── config/                       # Config loading + validation (SRC-071–073)
│       │   ├── models.py                 # Pydantic v2 config models
│       │   └── loader.py                 # YAML loader; env-var secret injection
│       ├── llm/                          # Provider-agnostic LLM abstraction (SRC-055–061)
│       │   ├── base.py                   # AbstractLLMClient + SearchResult dataclass
│       │   ├── openai_client.py          # OpenAI Agents SDK implementation (default)
│       │   ├── anthropic_client.py       # Anthropic stub (future)
│       │   ├── search_tools.py           # AbstractSearchTool + Brave/Tavily adapters
│       │   └── factory.py               # get_llm_client() factory
│       ├── storage/                      # Document store abstraction (SRC-053)
│       │   ├── base.py                   # AbstractArticleStore interface
│       │   ├── tinydb_store.py           # TinyDB concrete implementation (default)
│       │   └── models.py                 # ArticleRecord + TweetSignal dataclasses
│       ├── twitter/                      # Twitter/X integration (SRC-062–070)
│       │   └── client.py                 # tweepy wrapper; filter logic; graceful degradation
│       ├── sourcing/                     # Sourcing Agent (SRC-006–013, SRC-033–049)
│       │   ├── agent.py                  # SourcingAgent orchestrator + cli_main
│       │   ├── web_fetcher.py            # Web search strategies (LLM-native / Brave / Tavily)
│       │   └── twitter_fetcher.py        # Thin wrapper: calls twitter.client, normalizes output
│       ├── curation/                     # Curation Agent (SRC-014–032, SRC-112–131)
│       │   ├── agent.py                  # CurationAgent orchestrator + cli_main
│       │   ├── prompt_builder.py         # Injects ISO dates, Twitter signal, criteria
│       │   └── scorer.py                 # LLM scoring + tier-weighted ranking + URL-drop
│       ├── rendering/                    # Rendering Agent (SRC-004, SRC-135–141)
│       │   ├── agent.py                  # RenderingAgent orchestrator + cli_main
│       │   ├── markdown_renderer.py      # Renders .md (Slack/email paste-ready)
│       │   ├── html_renderer.py          # Renders .html (email-client paste-ready)
│       │   └── json_renderer.py          # Renders .json (machine-readable archive)
│       ├── scheduler/                    # APScheduler orchestration (SRC-052, SRC-072, SRC-144)
│       │   └── runner.py                 # Cadence jobs; reads scheduler.yaml; cli_main
│       └── portal/                       # Web portal (SRC-004, SRC-133–134)
│           ├── app.py                    # FastAPI application factory + cli_main
│           ├── routes.py                 # HTTP route handlers
│           ├── templates/                # Jinja2 HTML templates
│           │   ├── base.html             # Layout, nav, agent switcher
│           │   ├── index.html            # Landing: digest list by cadence + agent
│           │   ├── daily.html            # Article card list + why-it-matters
│           │   ├── weekly.html           # Theme-centric + top articles + outlook
│           │   ├── monthly.html          # Big-picture themes + anticipated news
│           │   └── annual.html           # Top 10 + predictions + tag cloud
│           └── static/
│               ├── css/app.css           # Tailwind-compiled or minimal hand-crafted CSS
│               └── js/app.js             # Theme filter, word cloud, download triggers
├── tests/
│   ├── conftest.py                       # Shared fixtures; LLM + Twitter mocks (SRC-098)
│   ├── unit/
│   │   ├── test_config.py                # Config loading, schema validation, secret injection
│   │   ├── test_llm_base.py              # AbstractLLMClient contract tests
│   │   ├── test_sourcing.py              # SourcingAgent: fetch, dedup, store
│   │   ├── test_curation.py              # CurationAgent: prompt build, scoring, URL drop
│   │   ├── test_rendering.py             # All three renderers; URL-drop enforcement
│   │   ├── test_storage.py               # TinyDB store: insert, dedup, lookback query
│   │   ├── test_scheduler.py             # Cadence trigger logic; retry backoff
│   │   └── test_twitter.py               # tweepy wrapper; filter logic; degradation path
│   └── integration/
│       └── test_pipeline_smoke.py        # Dry-run end-to-end; no real LLM/Twitter calls
└── outputs/                              # Runtime output directory (gitignored)
    └── .gitkeep
```

---

## 11. Secrets Reference

| Secret | Env Var Name | Required | Used By | Source IDs |
|--------|-------------|----------|---------|------------|
| LLM API key (OpenAI default) | `OPENAI_API_KEY` | Yes | LLM abstraction layer | SRC-107 |
| Twitter/X bearer token | `TWITTER_BEARER_TOKEN` | Yes | Sourcing agent (tweepy) | SRC-064, SRC-108 |
| Web search API key | `WEB_SEARCH_API_KEY` | Optional | Sourcing agent (Brave/Tavily fallback) | SRC-109 |
| Web search provider | `WEB_SEARCH_PROVIDER` | Optional | Sourcing agent (selects adapter) | SRC-060 |
| Cloud deploy credentials | _(workload identity federation preferred)_ | CI only | CI/CD pipeline | SRC-110 |

**Rule (SRC-073, SRC-111):** All secrets sourced from environment variables or cloud secrets manager at runtime. **Never committed. Never placed in YAML config files. Never baked into container images.**

---

## 12. Deployment Targets (Cloud-Agnostic)

| Component | GCP | AWS | Azure |
|-----------|-----|-----|-------|
| Scheduler | Cloud Scheduler | EventBridge Scheduler | Logic Apps / Timer trigger |
| Compute | Cloud Run | App Runner or Fargate* | Container Apps |
| Storage (outputs) | Cloud Storage (mount or sync) | S3 (sync) | Blob Storage |
| Secrets | Secret Manager | Secrets Manager | Key Vault |
| Logs | Cloud Logging | CloudWatch | Application Insights |

_* AWS Lambda acceptable for daily/weekly/monthly; annual synthesis requires App Runner or Fargate due to 15-min Lambda timeout (SRC-090)_

---

## 13. Out of Scope — v1

| Capability | Notes | Source IDs |
|------------|-------|------------|
| Automated distribution (email send, Slack webhook posting) | Export files written to disk; user pastes/syncs manually | SRC-004, SRC-137–SRC-139 |
| Portal authentication | v1 is read-only and unauthenticated | SRC-134 |
| Portal-driven configuration changes | YAML-only in v1; portal config is future enhancement | SRC-072 |
| Kubernetes deployment | Excessive operational overhead for this workload | SRC-093 |
| Twitter API tier selection | Documented as decision point; Basic tier is likely practical minimum; confirm before committing | SRC-065 |
| Anthropic/Google provider (active) | Stub exists; not activated in v1 | SRC-055–SRC-056 |
| Automated prompt review / formal owner | Prompt iteration expected in first 4–6 weeks; formal owner designation is future | SRC-130–SRC-131 |

---

## 14. Source Requirement Coverage Summary

> **Note:** SRC-* IDs appear in multiple functional areas deliberately — many requirements touch more than one component. Coverage below maps each SRC-* to its **primary** area; all 150 are accounted for.

| Functional Area | Primary SRC-* IDs |
|-----------------|-------------------|
| Sourcing Agent | SRC-006–SRC-013, SRC-033–SRC-070 |
| Curation Agent | SRC-014–SRC-032, SRC-047–SRC-049, SRC-054, SRC-070, SRC-112–SRC-131 |
| Rendering Agent | SRC-004, SRC-006, SRC-048–SRC-049, SRC-120, SRC-132, SRC-135–SRC-141, SRC-145 |
| Web Portal | SRC-004, SRC-133–SRC-134, SRC-136 |
| Scheduler | SRC-028–SRC-032, SRC-052, SRC-072, SRC-144, SRC-147–SRC-148 |
| Configuration System | SRC-017, SRC-034, SRC-036–SRC-046, SRC-053–SRC-054, SRC-057, SRC-071–SRC-073, SRC-105–SRC-111 |
| LLM Abstraction Layer | SRC-027, SRC-053–SRC-061, SRC-107, SRC-109, SRC-112–SRC-113, SRC-121 |
| Operations / Deployment | SRC-001–SRC-005 (overview/nav), SRC-074–SRC-111, SRC-126–SRC-129, SRC-142–SRC-150 |

> **Coverage check:** Cross-referencing all ranges above against all 150 SRC-* entries defined in `spec.md` — **all 150 source requirements are accounted for across functional areas. No gaps detected.**

---

## 15. Traceability Rules (Active for All Implementation Stages)

Per `spec.md` (VAL-001, VAL-002, AC-001):

1. **VAL-001:** Every implementation artifact (code module, test, config, doc, prompt) must reference the SRC-* IDs it satisfies in its docstring or file header comment. Missing traceability fails review.
2. **VAL-002:** Every implementation stage must read `docs/requirements/requirements.md` **before** editing code. This working brief is a synthesized reference — it supplements but does not replace the source document.
3. **AC-001:** Requirements coverage must be reviewed against the full SRC-001–SRC-150 inventory before any increment is considered complete.

---

## 16. Current Project State (2026-05-10)

**Completed artifacts:**
- `docs/requirements/requirements.md` — canonical source (200 lines, 150 SRC-* entries) ✅
- `docs/requirements/spec.md` — full SRC-* inventory, validation rules, acceptance checks ✅
- `docs/requirements/implementation-plan.md` — 6 delivery slices with exit criteria ✅
- `docs/requirements/backlog.md` — 5 prioritized items (3×P0, 2×P1) ✅
- `docs/architecture.md` — module-by-module design, data models, deployment architecture ✅
- `docs/working-brief.md` — this document ✅
- `pyproject.toml` — full dependency manifest, CLI entry points, ruff/pytest/mypy config ✅
- `Dockerfile` — multi-stage; same image local → CI → prod ✅
- `.dockerignore` ✅
- `.env.example` — full secrets reference with SRC-* annotations ✅
- `configs/scheduler.yaml` — root scheduler config with retry policy and agent registry ✅

**Next:** SLICE-002 (project scaffolding — remaining config files, prompts, source package stubs, GitHub Actions workflows) → SLICE-003 (core domain implementation).

---

*This brief is the active working reference for all implementation stages. Re-read `docs/requirements/requirements.md` before editing code (VAL-002). Every implementation artifact must carry SRC-* traceability (VAL-001). This brief maps to SRC-001–SRC-150 in its entirety.*
