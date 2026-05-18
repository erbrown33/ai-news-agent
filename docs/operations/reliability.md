# Reliability Practices — AI News Curation Agent

> **Requirement traces:** SRC-142–SRC-148 (operational concerns §8.1), SRC-084 (native retry),
> SRC-085 (same image local/CI/prod), SRC-144 (3 retries + exponential backoff),
> SRC-145 (idempotent date-stamped filenames), SRC-146 (failure alerting),
> SRC-147 (manual override), SRC-148 (Twitter API failure handling)
>
> **SLICE:** SLICE-005 — Operational, deployment, and documentation requirements
>
> **Source-of-Truth Order:** `requirements.md` §8.1 ▶ `spec.md` ▶ this document

---

## Table of Contents

1. [Reliability Overview](#1-reliability-overview)
2. [Retry Policy — 3 Retries + Exponential Backoff](#2-retry-policy--3-retries--exponential-backoff)
3. [Idempotency — Date-Stamped Filenames](#3-idempotency--date-stamped-filenames)
4. [Manual Override — On-Demand Triggers](#4-manual-override--on-demand-triggers)
5. [Twitter API Failure Handling — Graceful Degradation](#5-twitter-api-failure-handling--graceful-degradation)
6. [Failure Alerting](#6-failure-alerting)
7. [Window Override — Backfill and Rerun](#7-window-override--backfill-and-rerun)
8. [Dry-Run Mode](#8-dry-run-mode)
9. [Reliability Checklist](#9-reliability-checklist)
10. [Requirement Traceability](#10-requirement-traceability)

---

## 1. Reliability Overview

The AI News Curation Agent is designed to run unattended on a cron schedule. Five distinct
reliability mechanisms ensure digests are produced consistently even when individual components
fail. (SRC-142–SRC-148)

| Mechanism | Protects Against | Source |
|-----------|-----------------|--------|
| Retry with exponential backoff | Transient LLM/network failures | SRC-144 |
| Idempotent date-stamped filenames | Duplicate runs producing inconsistent output | SRC-145 |
| Manual override endpoint | Missed schedules, backfills | SRC-147 |
| Twitter graceful degradation | Twitter API outages not blocking digests | SRC-148 |
| Failure alerting | Silent failures going unnoticed | SRC-146 |

---

## 2. Retry Policy — 3 Retries + Exponential Backoff

> **SRC-144:** Configure 3 retries with exponential backoff. All major cloud schedulers do this natively.

### Application-level retry

The `with_retry` decorator wraps all LLM calls with a 3-attempt exponential backoff:

```python
# src/ai_news_agent/llm/retry.py
# Retry schedule: 30s → 60s → 120s
@with_retry(max_attempts=3, base_delay=30.0)
async def complete(self, prompt: str, ...) -> str:
    ...
```

**Retry schedule:**

| Attempt | Trigger | Wait before retry |
|---------|---------|-------------------|
| 1 (initial) | Request sent | — |
| 2 (first retry) | After failure | 30 seconds |
| 3 (second retry) | After failure | 60 seconds |
| 4 (final) | After failure | 120 seconds → fail + alert |

**What triggers a retry:**
- Rate-limit errors (HTTP 429) from any LLM provider
- Network timeouts and connection resets
- Transient server errors (HTTP 500, 502, 503)

**What does NOT trigger a retry:**
- Authentication errors (HTTP 401, 403) — wrong key, fix the secret
- Malformed request errors (HTTP 400) — prompt or config bug, fail fast
- Context window exceeded — reduce article count or use a larger model

### Scheduler-level retry

Configure retries in `configs/scheduler.yaml`:

```yaml
scheduler:
  max_retries: 3                   # Matches SRC-144
  retry_backoff_base_seconds: 30   # 30s → 60s → 120s
```

All three cloud schedulers support equivalent native retry:

```bash
# GCP Cloud Scheduler (SRC-144)
gcloud scheduler jobs update http ai-news-curation-daily \
  --max-retry-attempts=3 \
  --min-backoff-duration=30s \
  --max-backoff-duration=120s

# AWS EventBridge Scheduler (SRC-144)
aws scheduler update-schedule \
  --name ai-news-curation-daily \
  --retry-policy '{"MaximumRetryAttempts": 3, "MaximumEventAgeInSeconds": 300}'

# Azure Container App Jobs (SRC-144)
# Retry is configured at the job level; jobs retry on non-zero exit codes
az containerapp job update \
  --name ai-news-curation-daily \
  --resource-group "${RG}" \
  --replica-timeout 1800
```

### Verifying retry behaviour in tests

```bash
# Unit tests for retry logic (no real API calls)
pytest tests/unit/ -k "retry" -v

# Expected: 3-attempt sequence with correct delays verified via mocks
```

---

## 3. Idempotency — Date-Stamped Filenames

> **SRC-145:** Idempotency: The output filename includes the date, so re-runs overwrite cleanly.

### Output file naming convention

All digest outputs follow the pattern: `{YYYY-MM-DD}-{cadence}.{ext}`

```
outputs/
└── default/
    ├── 2026-05-11-daily.md
    ├── 2026-05-11-daily.html
    ├── 2026-05-11-daily.json
    ├── 2026-05-11-weekly.md         ← written on Monday, covers Sun–Sat week
    ├── 2026-05-01-monthly.md        ← written on first of month, covers prior month
    └── 2026-01-01-annual.md         ← written on Jan 1, covers prior year
```

### Why this makes re-runs safe

If a run fails partway through, or if you re-run curation with improved prompts,
the output files are simply overwritten with the same filename. No duplicates, no
inconsistent state. The TinyDB article store (sourcing) uses URL deduplication (SHA-256 hash)
to prevent duplicate sourcing as well. (SRC-012)

### Manual re-run for a specific date

```bash
# Re-run daily curation for a specific day (uses same output filename)
ai-news-run \
  --cadence daily \
  --agent configs/default-agent.yaml \
  --window-start 2026-05-10 \
  --window-end 2026-05-10

# Re-run weekly curation with an explicit window
ai-news-run \
  --cadence weekly \
  --agent configs/default-agent.yaml \
  --window-start 2026-05-04 \
  --window-end 2026-05-10
```

Window override requires **both** `--window-start` and `--window-end`. Providing only one
raises a `ConfigError`. The run date in the output filename reflects the actual run date,
while the window dates appear in the JSON metadata.

### DigestRecord idempotency

The `DigestRecord` is persisted using `INSERT OR REPLACE` semantics keyed on
`(agent_id, cadence, run_date)`. Running curation twice on the same day for the same
agent and cadence overwrites the existing digest record. No duplicates in the store.

---

## 4. Manual Override — On-Demand Triggers

> **SRC-147:** The worker accepts an authenticated request to trigger a run on demand —
> useful for backfills or when the schedule misfires.

### HTTP trigger endpoint

```
POST /api/trigger
Authorization: Bearer {SCHEDULER_API_KEY}
Content-Type: application/json

{
  "job": "curation",          # "sourcing" | "curation"
  "cadence": "daily",         # "daily" | "weekly" | "monthly" | "annual"
  "agent": "default",         # agent_id from scheduler.yaml
  "window_start": "2026-05-10",  # optional — override automatic window
  "window_end": "2026-05-10"     # optional — both required or neither
}
```

Authentication is optional — if `SCHEDULER_API_KEY` env var is not set, the endpoint
is open (suitable for internal-only deployments). If set, the `Authorization: Bearer` header
is required.

### CLI trigger (local and remote)

```bash
# Local — trigger directly via CLI (SRC-147)
ai-news-schedule --trigger-agent default --job curation --cadence daily

# Remote — via curl to the deployed service
curl -X POST "https://your-service-url/api/trigger" \
  -H "Authorization: Bearer ${SCHEDULER_API_KEY}" \
  -H "Content-Type: application/json" \
  -d '{"job":"curation","cadence":"daily","agent":"default"}'

# GCP Cloud Scheduler manual trigger
gcloud scheduler jobs run ai-news-curation-daily --location=us-central1

# AWS EventBridge manual invocation
# (trigger via AWS Console or aws scheduler CLI)

# Azure Container App Job manual start
az containerapp job start \
  --resource-group "${RG}" \
  --name ai-news-curation-daily
```

### Backfill scenario

If the scheduler misfired on a Monday (e.g., due to a deploy window) and you need to backfill
the prior-week digest:

```bash
# Step 1: Ensure sourcing ran for the entire prior week
# (If sourcing was missed too, run it first with the explicit window)
ai-news-source \
  --agent configs/default-agent.yaml \
  --window-start 2026-05-04 \
  --window-end 2026-05-10

# Step 2: Run curation for the missed window
ai-news-run \
  --cadence weekly \
  --agent configs/default-agent.yaml \
  --window-start 2026-05-04 \
  --window-end 2026-05-10
```

The output file (`2026-05-11-weekly.md/.html/.json`) is overwritten cleanly.

---

## 5. Twitter API Failure Handling — Graceful Degradation

> **SRC-148:** If the Twitter API is unavailable, the agent should still produce a digest
> from web sources alone, with a note that influencer signal was unavailable for this run.
> Twitter is signal, not a hard dependency.

### Degradation hierarchy

```
Normal operation:
  Twitter API available → fetch influencer tweets → enrich curation prompt

Degraded — API unavailable:
  Error classified → ([], twitter_api_available=False) returned
  Curation proceeds with web sources only
  Digest includes note: "Influencer signal unavailable for this run"
  Monitoring field: twitter_signal_available = false
  tweet_api_call_count = 0

Degraded — bearer token missing:
  TWITTER_BEARER_TOKEN env var not set → skip Twitter entirely at startup
  Logged at INFO level — not an error
```

### Six degradation root causes (DegradationReason)

| Reason | Trigger condition |
|--------|------------------|
| `BEARER_TOKEN_MISSING` | `TWITTER_BEARER_TOKEN` env var not set |
| `TWEEPY_NOT_INSTALLED` | tweepy package not importable |
| `API_RATE_LIMITED` | HTTP 429 from Twitter API |
| `API_FORBIDDEN` | HTTP 403 — insufficient tier access |
| `API_UNAVAILABLE` | HTTP 5xx / network timeout from Twitter |
| `QUIET_WINDOW` | No tweets in the lookback window (API up, results empty) |

**Important distinction:** `API_UNAVAILABLE` (Twitter is down) vs `QUIET_WINDOW` (Twitter is
fine but the window had no qualifying tweets) are tracked separately in the curation prompt
context. The LLM sees different language:
- API down: "Twitter influencer signal was unavailable for this run (API unavailable)"
- Quiet window: "No qualifying tweets were found in the lookback window"

This distinction matters for quality monitoring — a quiet window is normal and expected;
repeated API failures may indicate a tier-access or authentication problem.

### Monitoring fields for Twitter

Every JSON output includes:

```json
{
  "metadata": {
    "twitter_signal_available": false,       // boolean — was Twitter used?
    "tweet_api_call_count": 0,              // integer — API calls made
    "twitter_degradation_reason": "API_UNAVAILABLE"  // null if healthy
  }
}
```

### Configuration options

```yaml
# configs/default-agent.yaml
twitter:
  enabled: true    # Set false to explicitly skip Twitter for this agent
  handles:
    - { handle: karpathy, weight: 1.0 }
    - { handle: sama,     weight: 1.0 }
    # ... (SRC-037–SRC-045: default 9 handles)
```

Setting `enabled: false` is equivalent to a permanent, intentional degradation.
The digest is still produced from web sources. No error is logged — this is a normal
configuration choice. (SRC-046, SRC-148)

### Testing degradation paths

```bash
# Test with no bearer token — verifies graceful degradation
OPENAI_API_KEY="sk-..." \
ai-news-run --cadence daily --agent configs/default-agent.yaml --dry-run
# → Should produce a digest with twitter_signal_available: false
# → Should NOT fail or raise an exception

# Test in CI (always mocked)
pytest tests/unit/test_twitter_integration.py -v
# All 6 degradation reasons are tested
```

---

## 6. Failure Alerting

> **SRC-146:** Failure alerting: Cloud-native logging alert on any non-2xx response from
> the worker. Pipe to your incident channel of choice.

### Structured log on every run

Every curation run emits a `curation_run_complete` structured log entry:

```json
{
  "event": "curation_run_complete",
  "level": "info",
  "agent_id": "default",
  "cadence": "daily",
  "run_date": "2026-05-11",
  "items_considered": 47,
  "items_included": 8,
  "items_by_tier": {"tier_1b": 3, "tier_2": 2, "tier_3": 2, "tier_4": 1},
  "items_by_source_class": {"web": 7, "twitter": 1},
  "token_usage": 18500,
  "llm_provider": "openai",
  "llm_model": "gpt-4o",
  "prompt_version": "sha256:a3b2c1...",
  "twitter_signal_available": true,
  "tweet_api_call_count": 9,
  "duration_seconds": 127.4,
  "exit_status": "success"
}
```

On failure, `exit_status` is `"error"` and an `error` field contains the exception message.

### Cloud-native alert setup

#### GCP — Log-based alert

```bash
# Create a log metric for failed runs
gcloud logging metrics create ai-news-agent-failures \
  --description="AI News Agent run failures" \
  --log-filter='
    resource.type="cloud_run_revision"
    jsonPayload.event="curation_run_complete"
    jsonPayload.exit_status="error"
  '

# Create an alerting policy (via Cloud Monitoring console or API)
# → Notify via email / PagerDuty / Slack webhook (SRC-146)
```

#### AWS — CloudWatch metric filter

```bash
aws logs put-metric-filter \
  --log-group-name "/aws/apprunner/ai-news-agent" \
  --filter-name "run-failures" \
  --filter-pattern '"exit_status":"error"' \
  --metric-transformations \
    metricName=RunFailures,metricNamespace=AINewsAgent,metricValue=1
```

#### Azure — Application Insights alert

```bash
az monitor metrics alert create \
  --name ai-news-agent-failures \
  --resource-group "${RG}" \
  --scopes "/subscriptions/${SUBSCRIPTION_ID}/resourceGroups/${RG}/providers/microsoft.insights/components/ai-news-agent" \
  --condition "count 'customEvents/curation_run_complete' > 0 where exit_status == 'error'" \
  --window-size 5m \
  --evaluation-frequency 1m \
  --action /subscriptions/${SUBSCRIPTION_ID}/resourceGroups/${RG}/providers/microsoft.insights/actionGroups/ai-news-oncall
```

#### Slack notification (deploy.yml stub)

The deploy workflow includes an uncommentable Slack notification block. To activate:

1. Add `SLACK_WEBHOOK_URL` to your GitHub `production` environment secrets.
2. Uncomment the `Notify Slack on failure` step in [`.github/workflows/deploy.yml`](../../.github/workflows/deploy.yml).

### Non-2xx HTTP response alerting

If using HTTP-triggered cloud compute (Cloud Run, App Runner), configure the scheduler
to treat non-2xx responses as failures and trigger the retry + alert chain. All three
major cloud schedulers do this by default.

---

## 7. Window Override — Backfill and Rerun

> **SRC-028:** Curation can be rerun for the associated lookback window at user request.

The pipeline accepts explicit window boundaries to override the automatic computation:

```bash
# Re-run curation for a specific historical window
ai-news-run \
  --cadence monthly \
  --agent configs/default-agent.yaml \
  --window-start 2026-04-01 \
  --window-end 2026-04-30

# Re-run annual for the prior year
ai-news-run \
  --cadence annual \
  --agent configs/default-agent.yaml \
  --window-start 2025-01-01 \
  --window-end 2025-12-31
```

### Rules for window override

| Rule | Detail |
|------|--------|
| Both or neither | `--window-start` and `--window-end` must be provided together or not at all |
| ISO-8601 dates | Format: `YYYY-MM-DD` |
| Start ≤ End | `window_start` must be before or equal to `window_end` |
| Re-runs overwrite | Same filename formula → overwrites prior run cleanly (SRC-145) |
| Sourcing not re-run | Window override applies to curation only; sourcing must have already stored candidates for the window |

### Re-sourcing a historical window

```bash
# If sourcing was missed for a historical window, run it first
ai-news-source \
  --agent configs/default-agent.yaml \
  --since 2026-04-01 \
  --until 2026-04-30

# Then re-run curation over the now-populated window
ai-news-run \
  --cadence monthly \
  --agent configs/default-agent.yaml \
  --window-start 2026-04-01 \
  --window-end 2026-04-30
```

---

## 8. Dry-Run Mode

> **SRC-102:** Smoke test — trigger a "dry-run" mode that produces a digest but writes only
> to a scratch location. Verify it returns 200 with non-empty output and all required fields populated.

Dry-run is used in three contexts:

1. **CI smoke test** — validates pipeline integrity with mocked LLM and Twitter calls (zero API cost).
2. **Developer validation** — test new prompts before committing.
3. **Post-deploy verification** — validate the deployed image produces valid output.

```bash
# Dry-run locally
ai-news-run \
  --cadence daily \
  --agent configs/default-agent.yaml \
  --dry-run
# → Writes to /tmp/ai-news-dry-run-{timestamp}/
# → Outputs are NOT written to outputs/
# → No TinyDB writes

# Dry-run with custom scratch directory
ai-news-run \
  --cadence weekly \
  --agent configs/default-agent.yaml \
  --dry-run \
  --scratch-dir /tmp/my-test/

# Dry-run with mocked LLM (for CI — zero API cost)
SMOKE_TEST_MOCK_LLM=1 SMOKE_TEST_MOCK_TWITTER=1 \
ai-news-run \
  --cadence daily \
  --dry-run \
  --skip-sourcing
```

**What dry-run skips:**
- Writing to `outputs/` directory
- Writing to the TinyDB article store
- Actually calling the LLM (if `SMOKE_TEST_MOCK_LLM=1`)
- Actually calling Twitter (if `SMOKE_TEST_MOCK_TWITTER=1`)

**What dry-run still validates:**
- Config loading and validation
- Prompt building and SHA-256 versioning
- LLM response parsing and structured output
- URL enforcement (items without URLs are dropped)
- Renderer output format correctness
- All §8.2 monitoring fields present in JSON

---

## 9. Reliability Checklist

Use this checklist before going live and after any deployment change.

### Pre-production checklist

- [ ] **Retry policy** configured: 3 retries, 30s/60s/120s backoff in both `scheduler.yaml` and cloud scheduler (SRC-144)
- [ ] **Secrets** stored in cloud secrets manager — NOT in YAML or container image (SRC-073, SRC-111)
- [ ] **Twitter degradation** tested: run once with `TWITTER_BEARER_TOKEN` unset; verify digest still produces (SRC-148)
- [ ] **Dry-run** passes: `ai-news-run --cadence daily --dry-run` exits 0 (SRC-102)
- [ ] **URL enforcement** verified: `twitter_signal_available` is boolean in JSON output (SRC-148)
- [ ] **All monitoring fields** present in JSON metadata (see §8.2 of requirements.md) (SRC-150)
- [ ] **Failure alert** configured: cloud-native log alert on non-2xx / `exit_status: error` (SRC-146)
- [ ] **Manual override** tested: `POST /api/trigger` returns 200 with valid job response (SRC-147)
- [ ] **Idempotency** verified: re-running the same cadence overwrites cleanly (SRC-145)
- [ ] **Annual timeout** addressed: App Runner / Azure ACA / Cloud Run used (not Lambda) for annual cadence (SRC-090)

### Post-incident checklist

- [ ] Identify whether failure was: LLM error, Twitter error, config error, or infrastructure error
- [ ] Check structured log for `exit_status: error` and `error` field for root cause
- [ ] Verify `tweet_api_call_count` — if 0 with `twitter_signal_available: false`, Twitter was degraded
- [ ] Re-run manually with `--window-start` / `--window-end` to backfill (SRC-147)
- [ ] Verify output files were written and are non-empty after successful rerun
- [ ] Update alert thresholds if false-positive alerts triggered

---

## 10. Requirement Traceability

| Requirement | Implementation |
|-------------|----------------|
| SRC-142 — §8 Operational concerns | All sections of this document |
| SRC-143 — §8.1 Reliability | Sections 2–6 |
| SRC-144 — 3 retries + exponential backoff | §2 (retry policy) |
| SRC-145 — Idempotent date-stamped filenames | §3 (idempotency) |
| SRC-146 — Failure alerting | §6 (failure alerting) |
| SRC-147 — Manual override | §4 (manual override) |
| SRC-148 — Twitter failure handling | §5 (graceful degradation) |
| SRC-084 — Native cloud scheduler retry | §2 (scheduler-level retry) |
| SRC-102 — Dry-run mode | §8 (dry-run mode) |
| SRC-028 — Rerun at user request | §7 (window override) |

---

*Traces: SRC-028, SRC-084, SRC-102, SRC-142–SRC-148*
