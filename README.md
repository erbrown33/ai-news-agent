# AI News Curation Agent

> **Multi-agent platform** for autonomously sourcing, curating, and rendering AI-industry
> news digests on **daily, weekly, monthly, and annual cadences.**

[![CI](https://github.com/erbrown33/wm-ai-news-agent-2/actions/workflows/ci.yml/badge.svg)](https://github.com/erbrown33/wm-ai-news-agent-2/actions/workflows/ci.yml)
[![Python 3.12](https://img.shields.io/badge/python-3.12-blue.svg)](https://python.org)
[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)

---

## What This System Does

The AI News Curation Agent automatically:

1. **Sources** candidate articles from web search tools (OpenAI native / Brave / Tavily)
   and Twitter/X influencer feeds via tweepy — storing each article exactly once per
   lookback window (SRC-008–SRC-012).

2. **Curates** candidates using an LLM against a configurable prompt — scoring for business
   impact, workforce/societal impact, and strategic/policy impact; excluding pure technical
   depth articles by default (SRC-023–SRC-026).

3. **Renders** each digest in three export formats — **Markdown** (Slack/Teams paste-ready),
   **HTML** (email-ready), **JSON** (archive/machine-readable) — for every run (SRC-004).

4. **Serves** a **web portal** (FastAPI + Jinja2) for browsing digests by cadence and agent,
   with theme visualization and export downloads (SRC-133–SRC-134).

**Two non-negotiable deliverables (SRC-004):**
- Well-designed web portal for viewing curated digests and downloading exports.
- Structured export files (MD/HTML/JSON) for every curation run.

---

## Quick-Start: Run Locally in 5 Minutes

### 1. Install

```bash
# Clone and set up virtual environment
git clone https://github.com/erbrown33/wm-ai-news-agent-2.git
cd wm-ai-news-agent-2
python -m venv .venv
source .venv/bin/activate     # Windows: .venv\Scripts\activate

# Install package + dev dependencies
pip install -e ".[dev]"
```

### 2. Configure secrets

```bash
cp .env.example .env
# Edit .env — fill in your OPENAI_API_KEY (required)
# TWITTER_BEARER_TOKEN is optional; agent degrades gracefully without it (SRC-148)
```

See [`.env.example`](.env.example) for all available variables and
[`docs/user-guide.md`](docs/user-guide.md) for detailed walkthrough.

### 3. Run the full pipeline

```bash
# Load secrets from .env
set -a
source .env
set +a

# Run: sourcing → curation → rendering for today's daily digest
ai-news-run --cadence daily --agent configs/default-agent.yaml

# View outputs
ls outputs/default/
# → 2026-05-12-daily.md   (Slack/email paste-ready)
# → 2026-05-12-daily.html (email-client paste-ready)
# → 2026-05-12-daily.json (machine-readable)
```

### 4. Start the web portal

```bash
source .env && ai-news-portal
# → http://localhost:8080
```

### 5. Dry-run (no real API calls — for testing)

```bash
SMOKE_TEST_MOCK_LLM=1 SMOKE_TEST_MOCK_TWITTER=1 \
ai-news-run --cadence daily --dry-run --skip-sourcing
```

> **Full user guide:** [`docs/user-guide.md`](docs/user-guide.md) — step-by-step
> instructions for configuring agents, authoring prompts, running stages locally,
> launching the portal, and deploying to a serverless container.

---

## Documentation Index

### Planning Artifacts (read before changing anything)

> **VAL-002:** Every implementation stage must read these files before editing code.

| Document | Purpose |
|----------|---------|
| [`docs/requirements/requirements.md`](docs/requirements/requirements.md) | **Canonical source of truth.** All 150 SRC-* requirements. Wins all conflicts. |
| [`docs/requirements/spec.md`](docs/requirements/spec.md) | Traceable specification mapping all SRC-* IDs to validation rules and acceptance checks. |
| [`docs/requirements/implementation-plan.md`](docs/requirements/implementation-plan.md) | Six delivery slices (SLICE-001–SLICE-006) with exit criteria. |
| [`docs/requirements/backlog.md`](docs/requirements/backlog.md) | Prioritized backlog (3×P0, 2×P1) with SRC-* traceability. |

### Architecture and Design

| Document | Purpose |
|----------|---------|
| [`docs/architecture.md`](docs/architecture.md) | Full system architecture: module designs, data models, YAML schemas, end-to-end flow, deployment. |
| [`docs/working-brief.md`](docs/working-brief.md) | Active working reference synthesizing all four planning artifacts. |
| [`docs/config/config-reference.md`](docs/config/config-reference.md) | Per-agent YAML configuration reference — every field, default, and example. |

### User Guide

| Document | Purpose |
|----------|---------|
| [`docs/user-guide.md`](docs/user-guide.md) | **Start here for day-to-day use.** Configuring agents, authoring prompts, running stages, launching the portal, deploying. |

### Operations and Deployment

| Document | Purpose |
|----------|---------|
| [`docs/deployment/deployment-guide.md`](docs/deployment/deployment-guide.md) | **Full deployment guide** — GCP Cloud Run, AWS App Runner (+ Lambda caveat), Azure Container Apps. |
| [`docs/deployment/secrets-management.md`](docs/deployment/secrets-management.md) | Secrets management — Workload Identity Federation, cloud secrets managers, CI handling. |
| [`docs/operations/reliability.md`](docs/operations/reliability.md) | Reliability practices — retries, idempotency, manual override, Twitter degradation. |
| [`docs/operations/quality-monitoring.md`](docs/operations/quality-monitoring.md) | Quality monitoring playbook — 4–6 week review checklist, per-run metrics, prompt iteration. |

---

## Architecture

```
Scheduler (APScheduler / Cloud Scheduler / EventBridge / Logic Apps)
  └── for each enabled agent config (configs/scheduler.yaml):
        ├── Sourcing Agent  → [TinyDB store: outputs/{agent_id}/store.json]
        │     ├── Web fetcher (OpenAI native / Brave / Tavily)
        │     └── Twitter/X client (tweepy) — graceful degradation (SRC-148)
        └── Curation Agent → Rendering Agent → outputs/{agent_id}/
              ├── PromptBuilder (ISO dates + Twitter signal + SHA-256 hash)
              └── LLM client (OpenAI default; provider-agnostic factory)

Web Portal (FastAPI + Jinja2)
  ├── GET /                   → digest list by cadence + agent
  ├── GET /digest/{...}       → cadence-specific themed view
  ├── GET /download/{...}     → download MD/HTML/JSON
  └── POST /api/trigger       → manual override trigger (SRC-147)
```

Full architecture: [`docs/architecture.md`](docs/architecture.md)

---

## Secrets

Secrets are **always** sourced from environment variables or a cloud secrets manager.
**Never** placed in YAML config files. **Never** baked into container images. (SRC-073, SRC-111)

| Secret | Env Var | Required | Notes |
|--------|---------|----------|-------|
| OpenAI API Key | `OPENAI_API_KEY` | Yes | Default LLM provider (SRC-107) |
| Twitter/X Bearer Token | `TWITTER_BEARER_TOKEN` | Recommended | Basic tier minimum; agent degrades gracefully without it (SRC-065, SRC-148) |
| Web Search API Key | `WEB_SEARCH_API_KEY` | Optional | Brave or Tavily fallback (SRC-109) |
| Web Search Provider | `WEB_SEARCH_PROVIDER` | Optional | `"brave"` or `"tavily"` (SRC-060) |
| Scheduler API Key | `SCHEDULER_API_KEY` | Optional | Protects `POST /api/trigger` (SRC-147) |
| Anthropic API Key | `ANTHROPIC_API_KEY` | Optional | Alternative LLM provider (SRC-055) |

See [`docs/deployment/secrets-management.md`](docs/deployment/secrets-management.md) for
Workload Identity Federation setup (GCP/AWS/Azure).

---

## Configuration

All runtime behavior is controlled by YAML files in `configs/`. No code changes needed to:

| Change | File | Source |
|--------|------|--------|
| Add/remove/reweight Twitter influencer handles | `configs/default-agent.yaml` | SRC-046 |
| Change source tier lists | `configs/default-agent.yaml` | SRC-016–SRC-021 |
| Switch LLM provider or model | `configs/default-agent.yaml` | SRC-057 |
| Configure per-cadence research model | `configs/default-agent.yaml` | SRC-054 |
| Run multiple agents with different themes | `configs/scheduler.yaml` | SRC-072 |
| Adjust cron schedule | `configs/scheduler.yaml` | SRC-052 |
| Change retry policy | `configs/scheduler.yaml` | SRC-144 |

**Key config files:**

| File | Purpose |
|------|---------|
| `configs/scheduler.yaml` | Root scheduler: trigger times, retry policy, agent registry |
| `configs/default-agent.yaml` | Default agent: business + society AI curation |
| `configs/example-technical-agent.yaml` | Example alternate-theme agent (disabled by default) |
| `configs/example-minimal-agent.yaml` | Minimal starting point for a new agent |
| `prompts/daily.md` | Daily curation prompt template (SRC-115–SRC-123) |
| `prompts/weekly.md` | Weekly curation prompt template |
| `prompts/monthly.md` | Monthly prompt (research LLM configurable) |
| `prompts/annual.md` | Annual prompt: top-10 + 10 predictions (SRC-124) |

Full config reference: [`docs/config/config-reference.md`](docs/config/config-reference.md)

---

## All Run Commands

### Pipeline entry points

```bash
# Full pipeline: sourcing → curation → rendering
ai-news-run --cadence daily --agent configs/default-agent.yaml
ai-news-run --cadence weekly --agent configs/default-agent.yaml
ai-news-run --cadence monthly --agent configs/default-agent.yaml
ai-news-run --cadence annual --agent configs/default-agent.yaml

# Dry-run (writes to /tmp, zero production writes — SRC-102)
ai-news-run --cadence daily --dry-run

# Historical backfill with explicit window (SRC-028, SRC-147)
ai-news-run --cadence weekly \
  --window-start 2026-05-04 \
  --window-end 2026-05-10

# Skip sourcing (re-curate from existing article store)
ai-news-run --cadence daily --skip-sourcing
```

### Individual agent entry points

```bash
# Sourcing only
ai-news-source --agent configs/default-agent.yaml

# Curation only (requires sourcing to have run first)
ai-news-curate --agent configs/default-agent.yaml --cadence daily
ai-news-curate --agent configs/default-agent.yaml --cadence weekly

# Rendering only (requires curation JSON input)
ai-news-render --input outputs/default/2026-05-12-daily.json
```

### Scheduler

```bash
# Start background scheduler (reads configs/scheduler.yaml)
ai-news-schedule

# Manual trigger override — useful for backfills (SRC-147)
ai-news-schedule --trigger-agent default --job curation --cadence daily

# One-shot serverless trigger (for cloud scheduler invocations)
ai-news-oneshot --job curation --cadence daily --agent default
```

### Web portal

```bash
ai-news-portal
# → http://localhost:8080
```

### Prompt hash management (SRC-129)

```bash
# Verify prompt manifest matches current prompt files (runs in CI)
ai-news-prompt-hashes --verify

# Update manifest after editing a prompt file
ai-news-prompt-hashes --save
```

---

## Development Commands

### Run tests

```bash
# All unit tests (mocked LLM + Twitter — no real API calls)
pytest tests/unit/ -v

# Integration smoke tests
pytest tests/integration/ -v -m integration

# CI smoke assertions
pytest tests/ci/ -v

# With coverage (≥85% required in CI)
pytest tests/unit/ \
  --cov=src/ai_news_agent \
  --cov-report=term-missing \
  --cov-fail-under=85

# Run with real env vars (makes real API calls — use sparingly)
source .env && pytest tests/ -v -m "not slow"
```

### Lint

```bash
# Check (used in CI — must pass before merge)
ruff check src/ tests/

# Auto-fix
ruff check --fix src/ tests/

# Format check
ruff format --check src/ tests/

# Auto-format
ruff format src/ tests/
```

### Type check

```bash
mypy src/
```

### Build Docker image

```bash
docker build -t ai-news-agent .
```

### Run via Docker (local)

```bash
docker run --rm \
  -e OPENAI_API_KEY="$OPENAI_API_KEY" \
  -e TWITTER_BEARER_TOKEN="$TWITTER_BEARER_TOKEN" \
  -p 8080:8080 \
  -v "$(pwd)/outputs:/app/outputs" \
  -v "$(pwd)/configs:/app/configs" \
  ai-news-agent
```

### Validate config files

```bash
# Validate per-agent YAML
python -m ai_news_agent.config.schema --validate configs/default-agent.yaml --summary

# Validate scheduler YAML
python -m ai_news_agent.config.schema \
  --validate configs/scheduler.yaml \
  --type scheduler \
  --summary
```

### Convenience dry-run script

```bash
./scripts/dry_run.sh
```

---

## Output Files

All digest outputs are written to `outputs/{agent_id}/` with date-stamped names (SRC-145):

```
outputs/
└── default/
    ├── 2026-05-12-daily.md        ← Slack/Teams paste-ready
    ├── 2026-05-12-daily.html      ← Email-client paste-ready
    ├── 2026-05-12-daily.json      ← Machine-readable archive
    ├── 2026-05-12-weekly.md       ← Written on Monday (Sun–Sat window)
    ├── 2026-05-01-monthly.md      ← Written on 1st (prior month)
    ├── 2026-01-01-annual.md       ← Written Jan 1 (prior year + predictions)
    └── store.json                 ← TinyDB article store (internal)
```

Re-runs overwrite cleanly — idempotent by date (SRC-145). All three formats are
available for download from the web portal.

**Distribution (manual — no automation in v1, SRC-004):**
- Email: paste the `.html` file into a mail client
- Slack/Teams: paste the `.md` directly
- Static site: sync `outputs/` to GitHub Pages, S3+CDN, etc.

---

## Deployment

The system is designed as a **serverless container** (SRC-080–SRC-086):

| Component | GCP | AWS | Azure |
|-----------|-----|-----|-------|
| Scheduler | Cloud Scheduler | EventBridge | Logic Apps |
| Compute | Cloud Run | **App Runner**† | Container Apps |
| Storage | Cloud Storage | S3 | Blob Storage |
| Secrets | Secret Manager | Secrets Manager | Key Vault |
| Logs | Cloud Logging | CloudWatch | Application Insights |

> **†AWS Lambda note (SRC-090):** Lambda's 15-minute hard timeout is fine for daily/weekly/monthly
> runs. However, the `annual` cadence (extended-thinking synthesis) can take 5–10 minutes and
> the 15-min cap leaves no headroom. **Use App Runner or Fargate for any deployment that
> includes the annual cadence.**

**CI/CD:** GitHub Actions (`.github/workflows/`) —
lint → typecheck → tests → prompt-hashes → docker-build → smoke → deploy.

Full deployment guide: [`docs/deployment/deployment-guide.md`](docs/deployment/deployment-guide.md)

---

## Prompt Versioning

Every curation run records the SHA-256 hash of the prompt file used. This enables
quality regression tracing — if output quality degrades, identify exactly which prompt
change caused it. (SRC-129)

**Prompts are treated like code (SRC-126–SRC-128):**
- Versioned in git alongside source code
- Changes require PR review (at least one reviewer beyond the author)
- Hash manifest (`prompts/prompt_hashes.json`) updated with every change
- CI blocks merge if manifest doesn't match current prompt files

---

## Monitoring

Every run logs all §8.2 monitoring fields (SRC-150):

| Field | Description |
|-------|-------------|
| `items_considered` | Articles fetched and scored by LLM |
| `items_included` | Articles selected for the digest |
| `items_by_tier` | Breakdown by tier (1a/1b/2/3/4) |
| `items_by_source_class` | Breakdown: web vs Twitter-originated |
| `token_usage` | Total tokens consumed by LLM |
| `llm_provider` + `llm_model` | Provider and model used |
| `prompt_version` | SHA-256 of prompt file (SRC-129) |
| `twitter_signal_available` | Whether Twitter API was available |
| `tweet_api_call_count` | Twitter API calls made |

Quality monitoring playbook (4–6 week review, source dominance, slip-through, Twitter value):
[`docs/operations/quality-monitoring.md`](docs/operations/quality-monitoring.md)

---

## Source Coverage

All 150 source requirements (SRC-001–SRC-150) are addressed. Traceability map:

| Functional Area | Primary SRC-* IDs |
|-----------------|-------------------|
| Sourcing Agent | SRC-006–SRC-013, SRC-033–SRC-070 |
| Curation Agent | SRC-014–SRC-032, SRC-047–SRC-049, SRC-054, SRC-112–SRC-131 |
| Rendering Agent | SRC-004, SRC-135–SRC-141, SRC-145 |
| Web Portal | SRC-004, SRC-133–SRC-134, SRC-136 |
| Scheduler | SRC-028–SRC-032, SRC-052, SRC-072, SRC-144, SRC-147–SRC-148 |
| Configuration System | SRC-071–SRC-073, SRC-105–SRC-111 |
| LLM Abstraction Layer | SRC-055–SRC-061, SRC-107, SRC-109 |
| Operations / Deployment | SRC-074–SRC-111, SRC-142–SRC-150 |

Full AC-001 coverage review: [`docs/architecture.md`](docs/architecture.md) §12.

---

## Implementation Status

| Slice | Scope | Status |
|-------|-------|--------|
| SLICE-001 | Spec refinement + SRC-* traceability | ✅ Complete |
| SLICE-002 | Project scaffolding | ✅ Complete |
| SLICE-003 | Core domain implementation | ✅ Complete |
| SLICE-004 | User-facing surfaces (portal) | ✅ Complete |
| SLICE-005 | Ops layer (scheduler, CI/CD, monitoring, docs) | ✅ Complete |
| SLICE-006 | Coverage review (AC-001) | ✅ Complete |

---

## Contributing

1. **Read** `docs/requirements/requirements.md` before changing any implementation (VAL-002).
2. Every artifact must carry **SRC-* traceability** in docstrings or comments (VAL-001).
3. Run `ruff check src/ tests/` and `pytest tests/unit/` before committing.
4. **Prompt changes** require at least one reviewer beyond the author (SRC-128).
5. After any prompt change, run `ai-news-prompt-hashes --save` to update the manifest (SRC-129).

---

## License

MIT — see [LICENSE](LICENSE).
