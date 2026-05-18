# Quality Monitoring Playbook — AI News Curation Agent

> **Requirement traces:** SRC-149–SRC-150 (§8.2 quality monitoring), SRC-129 (prompt versioning),
> SRC-047 (Twitter as signal, not primary source), SRC-026 (disqualifier: technical depth),
> SRC-023–SRC-025 (business/workforce/policy impact scoring)
>
> **SLICE:** SLICE-005 — Operational, deployment, and documentation requirements
>
> **Source-of-Truth Order:** `requirements.md` §8.2 ▶ `spec.md` ▶ this document

---

## Table of Contents

1. [Quality Monitoring Philosophy](#1-quality-monitoring-philosophy)
2. [Per-Run Monitoring Fields](#2-per-run-monitoring-fields)
3. [4–6 Week Review Checklist](#3-46-week-review-checklist)
   - 3.1 [Source Dominance Review](#31-source-dominance-review)
   - 3.2 [Disqualified-Content Slip-Through Review](#32-disqualified-content-slip-through-review)
   - 3.3 [Twitter Signal Value Review](#33-twitter-signal-value-review)
   - 3.4 [Prompt Version and Regression Review](#34-prompt-version-and-regression-review)
   - 3.5 [Tier Distribution Review](#35-tier-distribution-review)
   - 3.6 [Token Usage and Cost Review](#36-token-usage-and-cost-review)
4. [Accessing the Monitoring Data](#4-accessing-the-monitoring-data)
5. [Quality Signals and Thresholds](#5-quality-signals-and-thresholds)
6. [Prompt Iteration Process](#6-prompt-iteration-process)
7. [Ongoing Quarterly Review](#7-ongoing-quarterly-review)
8. [Monitoring Log Structure Reference](#8-monitoring-log-structure-reference)
9. [Requirement Traceability](#9-requirement-traceability)

---

## 1. Quality Monitoring Philosophy

> **SRC-149–SRC-150:** After 4–6 weeks of operation, review whether the same sources keep
> dominating (a sign of overweighting), whether disqualified-content slip-through is occurring,
> and whether Twitter signal is actually adding value or just creating noise.

The agent produces structured monitoring data on every run. This playbook tells you:

1. **What to log** — the per-run monitoring fields in every JSON digest output
2. **When to review** — the 4–6 week review schedule after going live
3. **What to look for** — specific signals, thresholds, and corrective actions
4. **How to iterate** — the prompt change process for quality improvement

Quality is dominated by prompt quality (SRC-112). Monitoring data tells you whether
the prompts are working as intended or whether drift, overweighting, or slip-through
is occurring.

---

## 2. Per-Run Monitoring Fields

> **SRC-150:** The agent should log, for each run: number of items considered, number
> included, items by tier, items by source class (web vs Twitter-originated), total token
> usage, LLM provider + model + prompt version, and Twitter API call counts.

Every digest JSON output (`outputs/{agent_id}/YYYY-MM-DD-{cadence}.json`) contains a
`metadata` block with all required monitoring fields:

```json
{
  "schema_version": "1.0",
  "metadata": {
    "agent_id": "default",
    "cadence": "daily",
    "run_date": "2026-05-11",
    "window_start": "2026-05-10T00:00:00Z",
    "window_end": "2026-05-10T23:59:59Z",

    // Prompt versioning (SRC-129) — enables quality regression tracing
    "prompt_version": "sha256:a3b2c1d4e5f6...",

    // LLM configuration (SRC-150)
    "llm_provider": "openai",
    "llm_model": "gpt-4o",

    // Candidate pool metrics (SRC-150)
    "items_considered": 47,
    "items_included": 8,

    // Tier distribution (SRC-150) — key quality signal
    "items_by_tier": {
      "tier_1a": 0,
      "tier_1b": 3,
      "tier_2": 2,
      "tier_3": 2,
      "tier_4": 1
    },

    // Source class (SRC-150) — web vs Twitter-originated
    "items_by_source_class": {
      "web": 7,
      "twitter": 1
    },

    // Token usage (SRC-150) — cost monitoring
    "token_usage": 18500,

    // Twitter signal status (SRC-148, SRC-150)
    "twitter_signal_available": true,
    "tweet_api_call_count": 9,
    "twitter_degradation_reason": null
  },
  "items": [ ... ],
  "prompt_version": "sha256:a3b2c1d4e5f6...",
  "urls": [ ... ]
}
```

The structured log emitted at run completion mirrors these fields:

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
  "prompt_version": "sha256:a3b2c1d4e5f6...",
  "twitter_signal_available": true,
  "tweet_api_call_count": 9,
  "duration_seconds": 127.4,
  "exit_status": "success"
}
```

---

## 3. 4–6 Week Review Checklist

> **SRC-150:** After 4–6 weeks of operation, review whether the same sources keep dominating,
> whether disqualified-content slip-through is occurring, and whether Twitter signal is
> actually adding value or just creating noise.

Run this review at the **4-week mark** for your first assessment, then again at **6 weeks**
to compare trends. After that, move to quarterly reviews (see §7).

---

### 3.1 Source Dominance Review

**Question:** Are the same sources appearing in every digest?

**What to look for:** If Reuters, TechCrunch, or any single domain appears in >50% of
daily digests over the 4-week period, that source is being overweighted relative to its
true importance in the AI news landscape.

**Data to collect:**

```bash
# Extract source domains from all daily JSON outputs (last 30 days)
for f in outputs/default/2026-*-daily.json; do
  jq -r '.items[].source_name' "$f"
done | sort | uniq -c | sort -rn | head -20
```

**Expected healthy distribution (approximate):**

| Source tier | Expected share of included items |
|-------------|----------------------------------|
| Tier 1a (custom) | 0–20% (depends on config) |
| Tier 1b (business press) | 30–50% |
| Tier 2 (AI company blogs) | 20–35% |
| Tier 3 (tech business press) | 15–30% |
| Tier 4 (policy/research) | 5–15% |

**Red flags:**
- Any single domain > 25% of items over 30 days
- Tier 1b alone > 60% of all items (overweights business press)
- Tier 4 (policy/research) consistently at 0% (policy stories may be under-sourced)

**Corrective actions:**

| Problem | Action |
|---------|--------|
| Single source dominates | Add competing sources to the same tier; check if the prompt is too permissive for that domain |
| Tier 4 always empty | Verify sourcing is fetching from Brookings, RAND, Stanford HAI; check web search queries |
| Tier 1a sources not appearing | Check that custom domains are correctly listed in `sources.custom` in agent YAML |

---

### 3.2 Disqualified-Content Slip-Through Review

**Question:** Are technical articles passing the filter when they shouldn't be?

> **SRC-026:** Disqualifier (by default): technical depth — articles whose primary content is
> implementation tutorials, framework comparisons, code walkthroughs, model architecture papers,
> or benchmark deep-dives.

**Manual spot-check process:**

Review the past 4 weeks of daily digests. For each included item, ask:
- Is this primarily a business/workforce/policy story?
- Would an AI-curious business executive find this directly relevant to their decisions?
- Is the primary content tutorials, architecture papers, or benchmarks?

**Common slip-through categories:**

| Category | Example | Should it be included? |
|----------|---------|------------------------|
| Model architecture paper | "New attention mechanism improves transformer efficiency by 23%" | ❌ No — technical depth |
| Benchmark result | "GPT-5 scores 94.7% on HumanEval" | ❌ No — no business impact |
| Framework tutorial | "How to fine-tune LLaMA 3 in 5 steps" | ❌ No — implementation tutorial |
| Enterprise deployment | "Salesforce AI Studio reaches 10M users" | ✅ Yes — business impact |
| Regulatory action | "EU AI Act fines issued for first time" | ✅ Yes — policy impact |
| Research with workforce angle | "Deloitte: 40% of white-collar tasks automatable by 2028" | ✅ Yes — societal impact |

**If slip-through is occurring:**

1. Identify the pattern — which disqualifier category is slipping through?
2. Update the relevant prompt file to make the disqualifier more explicit:

```bash
# Edit the daily prompt to add more specific examples
# Example: add "papers measuring model performance on coding benchmarks" to disqualifiers
vim prompts/daily.md

# Update the prompt hash manifest after any change
ai-news-prompt-hashes --save

# Verify the change is intentional and well-reasoned
git diff prompts/daily.md
```

3. Run curation with `--dry-run` to verify the new prompt changes behavior.
4. Submit as a PR — prompt changes require at least one reviewer (SRC-128).

---

### 3.3 Twitter Signal Value Review

**Question:** Is Twitter (influencer signal) actually adding value, or is it just noise?

> **SRC-047:** Twitter content is treated as signal and commentary, not as primary news.
> A tweet alone rarely warrants inclusion.

**Data to collect:**

```bash
# What fraction of included items originated from Twitter signal?
for f in outputs/default/2026-*-daily.json; do
  jq '.metadata.items_by_source_class' "$f"
done

# Which handles are contributing the most leads?
for f in outputs/default/2026-*-daily.json; do
  jq -r '.items[] | select(.twitter_handle != null) | .twitter_handle' "$f"
done | sort | uniq -c | sort -rn
```

**Healthy Twitter signal pattern:**
- `items_by_source_class.twitter` typically 0–2 items per daily digest (≤25%)
- Twitter-sourced items should be "executive announcement on X before press coverage exists"
  type stories — not just rephrasing of web articles
- Most items should have `twitter_handle: null` (sourced from web, not Twitter)

**Red flags:**
- Twitter-sourced items > 40% of included items (over-relying on Twitter signal)
- 0 Twitter-sourced items for 4+ consecutive weeks with `twitter_signal_available: true` (signal not being used)
- Same handle appearing in >50% of Twitter-sourced items (single-source dependency)

**Corrective actions:**

| Problem | Action |
|---------|--------|
| Twitter items > 40% | Review prompt — ensure it says "use tweets as lead generation, not direct citation unless tweet is the news" (SRC-119) |
| Signal always 0% despite availability | Check handle list; consider adding more active handles; verify tweet substantivity filter isn't too aggressive |
| Single handle dominates | Consider reducing weight for that handle in YAML; add more handles for balance (SRC-046) |
| `twitter_signal_available` consistently false | Check `TWITTER_BEARER_TOKEN` validity; check API tier (SRC-065); check degradation reason in logs |

**Adjusting Twitter handle weights (SRC-046):**

```yaml
# configs/default-agent.yaml
twitter:
  handles:
    # Reduce weight for a handle that's generating noise
    - { handle: ylecun,       weight: 0.5 }   # Was 1.0; academic focus — less signal for business angle
    # Increase weight for a handle consistently surfacing good leads
    - { handle: sama,         weight: 2.0 }   # Executive announcements often newsworthy
    # Add a new handle
    - { handle: gdb,          weight: 1.0 }   # Greg Brockman
```

No code changes needed — update YAML and restart the scheduler.

---

### 3.4 Prompt Version and Regression Review

**Question:** Did a prompt change cause output quality to improve or degrade?

> **SRC-129:** Each digest output records the prompt version (file hash) used to produce it,
> so quality regressions can be traced.

**Data to collect:**

```bash
# List all prompt versions used over the last 30 days
for f in outputs/default/2026-*-daily.json; do
  echo "$(basename $f .json): $(jq -r '.metadata.prompt_version' "$f")"
done

# Verify prompt hash manifest is current
ai-news-prompt-hashes --verify

# Show all tracked prompt versions
cat prompts/prompt_hashes.json
```

**Regression detection process:**

1. **Baseline:** Record subjective quality scores for the first 2 weeks of operation
   (0–5 scale: relevance, no slip-through, diversity, "why it matters" quality)

2. **After any prompt change:** Compare quality scores for the 2 weeks before and after
   using the `prompt_version` field to identify exactly which runs used which prompt version

3. **Roll back:** If a prompt change degrades quality, revert the prompt file and update
   the hash manifest:

   ```bash
   git checkout main -- prompts/daily.md
   ai-news-prompt-hashes --save
   git add prompts/prompt_hashes.json
   git commit -m "revert: roll back daily prompt to prior version"
   ```

**Prompt change requirements (SRC-126–SRC-128):**

| Rule | Enforcement |
|------|-------------|
| Prompts treated like code | Versioned in git alongside source code |
| Changes require review | At least one reviewer beyond the author |
| Hash manifest updated | `ai-news-prompt-hashes --save` before committing |
| CI blocks unreviewed changes | `ai-news-prompt-hashes --verify` in CI pipeline |

---

### 3.5 Tier Distribution Review

**Question:** Is the tier distribution matching curation intent?

**Data to collect:**

```bash
# Aggregate tier distribution over 30 days
python3 - <<'EOF'
import json, pathlib, collections

outputs = pathlib.Path("outputs/default")
total = collections.Counter()
runs = 0

for f in sorted(outputs.glob("2026-*-daily.json")):
    d = json.loads(f.read_text())
    tiers = d.get("metadata", {}).get("items_by_tier", {})
    for tier, count in tiers.items():
        total[tier] += count
    runs += 1

print(f"Runs analyzed: {runs}")
for tier, count in sorted(total.items()):
    pct = 100 * count / sum(total.values())
    print(f"  {tier}: {count} items ({pct:.1f}%)")
EOF
```

**Expected healthy tier distribution (daily digest, default agent):**

| Tier | Items | Comment |
|------|-------|---------|
| Tier 1a (custom) | 0 | No custom sources configured by default |
| Tier 1b (business press) | 30–50% | Reuters, Bloomberg, WSJ — high signal-to-noise |
| Tier 2 (AI company blogs) | 20–30% | OpenAI, Anthropic, Google blogs — primary AI announcements |
| Tier 3 (tech business press) | 15–25% | TechCrunch, Wired, MIT Tech Review |
| Tier 4 (policy/research) | 5–15% | Brookings, RAND, Stanford HAI — low volume, high value |

**Red flags:**
- Tier 2 (AI company blogs) > 50% — may be capturing marketing content, not news
- Tier 4 consistently 0% — policy stories are being missed; check sourcing and prompt
- Any tier consistently 0% — check sourcing configuration for that tier's domains

---

### 3.6 Token Usage and Cost Review

**Question:** Is token usage within expected cost ranges?

**Data to collect:**

```bash
# Token usage by cadence over 30 days
for cadence in daily weekly monthly annual; do
  echo "=== ${cadence} ==="
  for f in outputs/default/2026-*-${cadence}.json; do
    [ -f "$f" ] && jq -r '.metadata.token_usage' "$f"
  done | awk '{sum+=$1; count++} END {if(count>0) printf "  avg: %d / max: %d / runs: %d\n", sum/count, max, count}'
done
```

**Expected token usage ranges:**

| Cadence | Expected tokens | Approx OpenAI cost |
|---------|----------------|-------------------|
| Daily (gpt-4o) | 10,000–25,000 | $0.05–0.25 |
| Weekly (gpt-4o) | 20,000–50,000 | $0.10–0.50 |
| Monthly (o3) | 50,000–150,000 | $5–15 |
| Annual (o3 + extended thinking) | 100,000–500,000 | $20–50 |

**Red flags:**
- Daily consistently > 50,000 tokens — article set is too large; reduce `daily_top_n` or tighten sourcing window
- Monthly / annual < 10,000 tokens — sourcing may not have found enough candidates; check sourcing configuration
- Token usage spiked after a specific date — correlate with `prompt_version` changes

**Corrective actions:**

| Problem | Action |
|---------|--------|
| Daily token cost too high | Reduce `limits.daily_top_n` in agent YAML |
| Annual token cost excessive | Confirm annual is using o3 (correct) vs gpt-4o (misconfigured) |
| Monthly token cost varies wildly | Check whether web search is returning consistent article counts |

---

## 4. Accessing the Monitoring Data

### Option 1 — Web portal (recommended)

The web portal displays summary information per digest. For detailed monitoring data,
click **Download JSON** on any digest to inspect the full `metadata` block.

```bash
# Start the portal
ai-news-portal
# → http://localhost:8080
```

### Option 2 — Direct JSON inspection

```bash
# Latest daily digest metadata
cat outputs/default/$(date +%Y-%m-%d)-daily.json | python3 -m json.tool | grep -A 20 '"metadata"'

# All metadata fields for the current month
for f in outputs/default/$(date +%Y-%m)-*.json; do
  echo "=== $(basename $f) ==="
  jq '.metadata | {items_considered, items_included, items_by_tier, items_by_source_class, token_usage, twitter_signal_available}' "$f"
done
```

### Option 3 — Cloud logging queries

#### GCP — Cloud Logging

```bash
gcloud logging read \
  'resource.type="cloud_run_revision" AND jsonPayload.event="curation_run_complete"' \
  --limit=50 \
  --format=json | python3 -m json.tool
```

#### AWS — CloudWatch Logs Insights

```sql
-- Query: average items_considered per cadence over 30 days
fields @timestamp, cadence, items_considered, items_included, token_usage
| filter event = "curation_run_complete"
| stats avg(items_considered) as avg_considered,
        avg(items_included) as avg_included,
        avg(token_usage) as avg_tokens
    by cadence
| sort cadence asc
```

#### Azure — Application Insights

```kusto
// KQL: run quality metrics over time
customEvents
| where name == "curation_run_complete"
| where timestamp > ago(30d)
| extend
    cadence = tostring(customDimensions.cadence),
    items_included = toint(customDimensions.items_included),
    token_usage = toint(customDimensions.token_usage)
| summarize
    avg_items = avg(items_included),
    avg_tokens = avg(token_usage)
    by cadence, bin(timestamp, 1d)
| render timechart
```

---

## 5. Quality Signals and Thresholds

Summary of all key quality signals, their healthy ranges, and corrective actions:

| Signal | Data source | Healthy range | Red flag | Corrective action |
|--------|-------------|---------------|----------|-------------------|
| Items considered per run | `metadata.items_considered` | 20–100 | <10: sourcing broken; >200: too broad | Check sourcing config; tighten window |
| Items included per run | `metadata.items_included` | 5–15 (daily) | <3: too restrictive; >20: too permissive | Adjust prompt inclusion criteria |
| Include rate (included/considered) | Computed | 10–30% | <5%: very restrictive; >50%: too permissive | Tune prompt scoring criteria |
| Tier 1b share | `metadata.items_by_tier` | 30–50% | >60%: business press overweight | Add more Tier 2/3 sources; tune prompt |
| Tier 4 share | `metadata.items_by_tier` | 5–15% | 0%: policy stories missed | Verify Tier 4 sources in config and sourcing |
| Twitter share | `metadata.items_by_source_class` | 0–25% | >40%: over-relying on Twitter | Strengthen prompt: tweets = leads, not citations |
| Twitter availability | `metadata.twitter_signal_available` | true (if token set) | false for >3 days: investigate | Check token validity; check API tier |
| Token usage (daily) | `metadata.token_usage` | 10k–25k | >50k: reduce article set size | Lower `daily_top_n`; tighten sourcing |
| Unique prompt versions | `metadata.prompt_version` | Stable (1–2 per quarter) | High churn: too many unreviewed changes | Enforce PR review for prompt changes |
| Single source dominance | Derived from `items[]` | No single domain >25% | >25%: overweighting | Add competing sources; check sourcing |

---

## 6. Prompt Iteration Process

> **SRC-130:** The first 4–6 weeks of operation will produce most of the prompt iteration;
> beyond that, expect quarterly tuning.

The first 4–6 weeks are the highest-value prompt iteration period. Most of the quality
improvements will happen in this window. Beyond that, expect quarterly tuning.

### Iteration process

```
1. Identify issue
   └─ From 4-week review, quality spot-check, or user feedback

2. Hypothesize prompt change
   └─ Be specific: "Add X to disqualifier list"
   └─ Reference SRC-* IDs for the requirement being addressed

3. Edit the prompt file
   └─ vim prompts/daily.md
   └─ All 9 requirements (SRC-115–SRC-124) must remain satisfied

4. Update hash manifest
   └─ ai-news-prompt-hashes --save

5. Test with dry-run
   └─ ai-news-run --cadence daily --dry-run
   └─ Review output quality manually

6. Submit PR (at least one reviewer — SRC-128)
   └─ Title: "prompt(daily): add benchmark articles to disqualifier"
   └─ Description: SRC-* traces, before/after examples, rationale

7. Merge and deploy
   └─ CI verifies hash manifest (SRC-129)
   └─ Monitor for 1 week post-change
```

### Prompt change log

Maintain a brief change log in each prompt file's header:

```markdown
<!-- prompts/daily.md — Change log
2026-05-11  Added "model architecture papers" to disqualifier (SRC-026 enforcement)
2026-06-15  Expanded business impact examples to include fintech + healthcare AI
2026-09-01  Reduced search budget from 15 to 10 (cost optimization)
-->
```

---

## 7. Ongoing Quarterly Review

After the initial 4–6 week intensive review, shift to quarterly quality checks.

### Quarterly checklist (every 3 months)

- [ ] **Source freshness** — Are the configured tier_1b–tier_4 domain lists still the right sources? New AI publications may have emerged; old ones may have declined in quality.
- [ ] **Twitter handle list** — Are all 9 default handles still active and relevant? (SRC-037–SRC-045) Have any gone quiet or shifted focus?
- [ ] **Model currency** — Is the configured LLM still the best choice for each cadence? (SRC-054) New models may offer better quality at lower cost.
- [ ] **Prompt performance** — Are business, workforce, and policy impact stories appropriately balanced? (SRC-023–SRC-025)
- [ ] **Annual predictions** — For the annual review (Jan 1): did the prior-year predictions prove accurate? This informs the next annual prompt tuning.
- [ ] **Cost trends** — Is token usage per cadence trending up or down? Are there cost optimization opportunities?
- [ ] **Prompt ownership** — Has a formal prompt owner been designated? (SRC-131) If not, is informal ownership working?

### Annual review (January, post-annual-digest)

After the annual digest is produced on January 1st:

1. **Read the annual digest** — Does it capture the year's most important AI developments accurately?
2. **Check the predictions** — How did last year's predictions fare? Grade them 1–10 on accuracy.
3. **Review prediction reasoning** — Were the "grounded in observed trends" justifications sound? (SRC-124)
4. **Update the annual prompt** if predictions were systematically off in a direction:
   - Too optimistic → add balancing language about execution risk
   - Too technical → reinforce business/society impact framing
   - Too US-centric → add geographic diversity instruction
5. Run `ai-news-prompt-hashes --save` and submit PR (SRC-128).

---

## 8. Monitoring Log Structure Reference

All logs use structlog JSON format. Key event types:

| Event | When | Key fields |
|-------|------|------------|
| `sourcing_run_start` | Start of sourcing | `agent_id`, `cadence`, `window_start`, `window_end` |
| `sourcing_run_complete` | End of sourcing | `items_fetched`, `items_new`, `items_duplicate`, `tweet_api_calls` |
| `curation_run_start` | Start of curation | `agent_id`, `cadence`, `llm_model`, `items_in_window` |
| `curation_run_complete` | End of curation | All §8.2 fields (SRC-150) |
| `rendering_run_complete` | End of rendering | `output_files`, `items_dropped_no_url` |
| `twitter_degraded` | Twitter failure | `reason` (DegradationReason), `api_status_code` |
| `llm_retry` | LLM call retry | `attempt`, `delay_seconds`, `error_type` |
| `pipeline_complete` | End of full pipeline | `exit_status`, `duration_seconds` |

### Structured log query examples

```bash
# Find all runs where Twitter was degraded
grep '"event":"twitter_degraded"' /var/log/ai-news-agent.log | \
  python3 -c "import sys,json; [print(json.dumps(json.loads(l),indent=2)) for l in sys.stdin]"

# Find all runs with low item count (possible sourcing issue)
grep '"event":"curation_run_complete"' /var/log/ai-news-agent.log | \
  python3 -c "
import sys, json
for line in sys.stdin:
  d = json.loads(line)
  if d.get('items_included', 99) < 3:
    print(f\"{d['run_date']} {d['cadence']}: {d['items_included']} items included\")
"

# Token usage trend (last 30 days, daily only)
grep '"event":"curation_run_complete"' /var/log/ai-news-agent.log | \
  python3 -c "
import sys, json
rows = [json.loads(l) for l in sys.stdin]
daily = [(r['run_date'], r['token_usage']) for r in rows if r.get('cadence')=='daily']
for date, tokens in sorted(daily)[-30:]:
    print(f'{date}: {tokens:,} tokens')
"
```

---

## 9. Requirement Traceability

| Requirement | Implementation |
|-------------|----------------|
| SRC-023 — Business impact scoring | §3.2 (slip-through: business impact examples) |
| SRC-024 — Workforce/societal impact | §3.2 (slip-through: societal examples) |
| SRC-025 — Strategic/policy impact | §3.2 (slip-through: policy examples) |
| SRC-026 — Technical depth disqualifier | §3.2 (slip-through review) |
| SRC-047 — Twitter = signal, not primary | §3.3 (Twitter value review) |
| SRC-112 — Output quality dominated by prompt | §6 (prompt iteration process) |
| SRC-126–SRC-128 — Prompt ownership + review | §6 (PR process, at-least-one-reviewer) |
| SRC-129 — Prompt version tracking | §3.4 (regression review) |
| SRC-130 — 4–6 weeks prompt iteration | §3 (review checklist), §6 |
| SRC-131 — Formal owner designation | §7 (quarterly checklist) |
| SRC-149 — §8.2 Quality monitoring | Entire document |
| SRC-150 — Per-run logging fields | §2 (monitoring fields), §8 (log structure) |

---

*Traces: SRC-023–SRC-026, SRC-047, SRC-112, SRC-126–SRC-131, SRC-149–SRC-150*
