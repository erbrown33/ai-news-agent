# Product Specification

## Objective

Deliver the product described in `docs/requirements/requirements.md` using spec-driven development. This file is the canonical implementation specification and must remain traceable to the source requirements inventory below.

## Source Of Truth

- Source requirements: `docs/requirements/requirements.md`
- Every functional requirement, validation rule, acceptance check, and implementation slice must map back to one or more `SRC-*` entries.
- If this file and `docs/requirements/requirements.md` conflict, `docs/requirements/requirements.md` wins until this specification is explicitly updated.

## Non-Goals

- Do not implement capabilities not traceable to the source requirements inventory.
- Do not add distribution, deployment, provider, or integration behavior beyond the source requirements without updating this specification first.

## Functional Requirements

| ID | Requirement | Priority | Source |
|----|-------------|----------|--------|
| REQ-001 | Preserve and satisfy the complete source requirements inventory. | P0 | All `SRC-*` |
| REQ-002 | Maintain a requirements-to-implementation trace through the backlog and implementation plan. | P0 | All `SRC-*` |

## User / System Flows

| Flow | Summary | Source |
|------|---------|--------|
| FLOW-001 | Execute the product workflows described by the source requirements. | All `SRC-*` |

## Data Model

| Entity / Object | Fields | Rules | Source |
|-----------------|--------|-------|--------|
| SourceRequirement | `id`, `line`, `text` | Each non-empty source line receives a stable `SRC-*` identifier. | All `SRC-*` |

## API / Integration Contracts

| Contract | Direction | Request / Input | Response / Output | Source |
|----------|-----------|-----------------|-------------------|--------|
| Source requirements file | Inbound | `docs/requirements/requirements.md` | Traceable spec, implementation plan, and backlog | All `SRC-*` |

## Validation Rules

| ID | Rule | Applies To | Expected Behavior | Source |
|----|------|------------|-------------------|--------|
| VAL-001 | Every implementation slice references source requirement IDs. | Backlog / Plan | Missing traceability fails review. | All `SRC-*` |
| VAL-002 | Agents must read `docs/requirements/requirements.md` before changing implementation behavior. | Pipeline execution | Stage output must not rely only on summarized DOT prompts. | All `SRC-*` |

## Acceptance Checks

| ID | Source Requirement | Check | Verification Method |
|----|--------------------|-------|---------------------|
| AC-001 | All `SRC-*` | Requirements coverage is reviewed against this source inventory before implementation is considered complete. | Manual / Code review |

## Implementation Slices

See `implementation-plan.md` and `backlog.md`. Each slice must list source requirement IDs.

## Source Requirements Inventory

| Source ID | Line | Source Text |
|-----------|------|-------------|
| SRC-001 | 1 | AI News Curation Agent — Build Specification |
| SRC-002 | 2 | 1. Overview |
| SRC-003 | 3 | An automated agent that curates AI news on daily, weekly, monthly, and annual cadences. Curation can be configured and honed by the user - by default should focus on AI news from a business and society impact perspective. |
| SRC-004 | 4 | Output: 1) Well-designed web portal to view AI news and summaries and 2) Structured digest files (Markdown, HTML, and JSON) rendered and exportable for various runs. #2 formats are designed to be easy to copy into email, paste into Slack/Teams, or post to a static site — but the agent itself does not handle distribution in this release. |
| SRC-005 | 6 | 2. Functional Requirements |
| SRC-006 | 7 | Break system into agents that are specialized in their specific functional area. The following functional areas should be considered: 1. Sourcing - retrieving candidate news articles from various configurable sources - primarily web blogs, news sources, and Twitter/X influencers. 2. Curation - prioritize candidates based on curation direction configured by user. Summarize in various timeframes accordingly. 3. Rendering - render timeframe summaries in various exportable formats. |
| SRC-007 | 8 | 2.1 Sourcing |
| SRC-008 | 9 | Sourcing will pull articles that exist within the lookback window - all candidate stories/posts should be stored (only once) for that lookback window (daily, weekly, monthly). Sourcing runs can be run more than once within that look back window - refreshing with new candidate articles that may have been missed in the previous run but no duplicates. Look back windows will be: |
| SRC-009 | 10 | -Daily - 00:00 UTC - 23:59 UTC (run by default at the start of each day) |
| SRC-010 | 11 | Can be run multiple times per day to continue to source articles. |
| SRC-011 | 12 | Articles should be stored with titles/abstracts/urls and other unique identifiers (source, date, etc.) |
| SRC-012 | 13 | Sourcing should not store the same candidate twice, but can add new ones for each run filtered by the lookback period (beginning of the previous day) |
| SRC-013 | 14 | Job of this agent is stricly to source - curation happens later. |
| SRC-014 | 16 | 2.2 Curation |
| SRC-015 | 17 | Curation agent runs at least once each day - it's job is to sift through candidate sources for the lookback period. They should be prioritized to a configurable number of sources for each period. |
| SRC-016 | 19 | Sources will be broken into tiers: |
| SRC-017 | 20 | Tier 1a - any configured source by the user (optional for user) |
| SRC-018 | 21 | Tier 1b — Popular Business press: Reuters, Bloomberg, WSJ, FT, The Economist, Axios, etc. |
| SRC-019 | 22 | Tier 2 - Top tech and business blogs: ycombinator, Netflix Tech Blog, Anthropic blog, Open AI Blog, Hugging Face blog, Towards AI, etc. |
| SRC-020 | 23 | Tier 3 — Tech business press: The Information, Stratechery, Platformer, TechCrunch, The Verge, MIT Tech Review, Wired, FastCompany, etc. |
| SRC-021 | 24 | Tier 4 — Policy/research: Brookings, RAND, Stanford HAI, AI Now Institute, government press releases, etc |
| SRC-022 | 26 | Each candidate article is scored against it's tier as well as qualitative elements configurable by the user. By default the following qualitative prompt will be considered: |
| SRC-023 | 28 | Business impact — Does this change how companies create value, compete, or operate? (e.g., new enterprise capabilities, market consolidation, regulatory shifts affecting commerce) |
| SRC-024 | 29 | Workforce/societal impact — Does this change how people work, learn, or live? (e.g., job displacement studies, education shifts, accessibility breakthroughs) |
| SRC-025 | 30 | Strategic/policy impact — Does this change the rules of the game? (e.g., legislation, major lawsuits, geopolitical AI moves, safety incidents with regulatory consequences) |
| SRC-026 | 31 | Disqualifier (by default only) — technical depth — Articles whose primary content is implementation tutorials, framework comparisons, code walkthroughs, model architecture papers, or benchmark deep-dives. These are filtered out for the default agent configuration if the user does not supply a curation prompt. |
| SRC-027 | 33 | All prioritization should be executed through an appropriate LLM interaction for intelligent priortization based on curation prompt. |
| SRC-028 | 35 | Curation runs for each lookback window at the beginning of the following window (e.g. Monday first thing for Sunday curation, Feb 1 for Jan curation). Curation can be rerun for the associated lookback window at user request. |
| SRC-029 | 36 | -Daily - pull candidates from the previous day - select the top number of configured articles based on curation - output should include the arcticle title, source, and link to the full content along with a summary of why it mattered for the daily window. |
| SRC-030 | 37 | -Weekly -  candidates from Sunday through Saturday (run by default on Sunday every week). Read through the source content and identify a few themes for the week. Think about what we should be looking forward to within the curation themes for the upcoming week. Link to the top few articles of the week and provide an intelligent weekly summary. |
| SRC-031 | 38 | -Monthly - candidates from the first of the month through the end of the last day on the month. Similar to the weekly run, but now we're thinking through bigger picture monthly themes and anticiplated news for the current month related to curation themes. |
| SRC-032 | 39 | -Annual - runs Jan 1 every year. This time we're going to provide the top 10 articles of the year and provide our big top 10 predictions for the upcoming year based on the curation prompt. This should punchy and impactful but well researched and justified with well argued and explained summaries for each and appropriate links where readers want to follow up. Predictions must be grounded in observed trends from the year reviewed, with reasoning shown. For annual consider using a current model with higher thinking an research mode activated. |
| SRC-033 | 41 | 2.3 Source Coverage |
| SRC-034 | 43 | See tiers from 2.2 for guidance. Users can configure sources as well as mentioned (provide a good config method/format) |
| SRC-035 | 45 | Twitter/X sources (via Twitter API) |
| SRC-036 | 46 | A configurable list of AI influencer accounts whose posts are scanned for newsworthy items, commentary, and signal. Default list: |
| SRC-037 | 48 | @karpathy (Andrej Karpathy) |
| SRC-038 | 49 | @sama (Sam Altman) |
| SRC-039 | 50 | @demishassabis (Demis Hassabis) |
| SRC-040 | 51 | @DarioAmodei (Dario Amodei) |
| SRC-041 | 52 | @ylecun (Yann LeCun) |
| SRC-042 | 53 | @AndrewYNg (Andrew Ng) |
| SRC-043 | 54 | @fchollet (François Chollet) |
| SRC-044 | 55 | @drfeifei (Fei-Fei Li) |
| SRC-045 | 56 | @emilymbender (Emily M. Bender) |
| SRC-046 | 58 | The influencer list is configurable — handles can be added, removed, or weighted without code changes. |
| SRC-047 | 59 | Twitter content is treated as signal and commentary, not as primary news. A tweet alone rarely warrants inclusion; tweets surface what's worth investigating, and the agent then uses web search to find primary reporting on those topics. The exception: when an influencer tweet is the news (e.g., an executive announcing something on X before press coverage exists). |
| SRC-048 | 62 | Each curated item contains: headline, source name + URL, publication date, 2–3 sentence "why it matters" summary, impact category tag(s), source tier, and optional cross-references to related items. Twitter-sourced items additionally include the originating handle and tweet URL. |
| SRC-049 | 63 | Every recommended item must have a working link to its primary source. This is non-negotiable — readers need to verify and read further. |
| SRC-050 | 65 | 3. Architecture |
| SRC-051 | 66 | 3.1 High-Level Flow |
| SRC-052 | 68 | Scheduler (cron-like) - initiates sourcing agent each day, initiates curation agent based on lookback windows |
| SRC-053 | 70 | Sourcing Agent - should have various configurable methods to fetch (e.g. basic LLM model, web search tools, providers like Tavily or Brave), sources data in appropriate local storage. Configurable, but think about good defaul store that would work for this - document store perhaps given the data? |
| SRC-054 | 72 | Curator Agent - configurable LLMs. consider configuration for a research LLM for monthly and annual lookback windows. Use appropriate storage configuration but guessing same one as sourcing agent can be used. |
| SRC-055 | 74 | 3.2 Provider-Agnostic Design |
| SRC-056 | 75 | The agent is built so the LLM provider can be swapped without changing the surrounding pipeline. |
| SRC-057 | 76 | Use an appropriate abstration layer around LLM/agent implementations. If relevant, consider using the choosen provider's SDK (e.g. Anthropic Agent SDK, OpenAI Agent SDK, Google ADK). By default let's use OpenAI. |
| SRC-058 | 78 | What this means in practice: |
| SRC-059 | 80 | Prompts are written in plain natural language with no provider-specific formatting tricks. Anything that works in one frontier model should work in another with at most minor tuning. |
| SRC-060 | 81 | Tool use is described abstractly. If the chosen provider has a native web search tool, the implementation uses it; if not, the implementation falls back to a generic search API (e.g., Brave, Tavily) wrapped to match the same interface. |
| SRC-061 | 82 | Output parsing is based on a structured format the model is instructed to produce (Markdown headings + JSON block, or strict JSON), not on provider-specific structured-output features. If a provider offers schema-enforced output and you want to use it, that's an optional enhancement inside the concrete implementation — the pipeline doesn't depend on it. |
| SRC-062 | 84 | 3.3 Twitter/X Integration |
| SRC-063 | 85 | Library: tweepy is the most widely used and best-maintained Python client for the Twitter/X API. It supports both v1.1 and v2 endpoints. |
| SRC-064 | 86 | Auth: The Twitter/X API requires a bearer token (and optionally OAuth 2.0 user context for higher rate limits). The bearer token is provided via the TWITTER_BEARER_TOKEN environment variable, sourced from a secrets manager at runtime — never committed. |
| SRC-065 | 87 | Access tier consideration: The Twitter/X API has tiered access (Free, Basic, Pro, Enterprise) with significant differences in rate limits and historical search depth. The Free tier is restrictive enough that Basic is likely the practical minimum for this use case. Confirm current pricing and tier capabilities before committing. |
| SRC-066 | 89 | What we fetch: |
| SRC-067 | 91 | For each handle in the configured list, fetch tweets from the lookback window. |
| SRC-068 | 92 | Filter to substantive posts: skip pure replies, retweets without comment, and very short tweets unless they contain a link. |
| SRC-069 | 93 | Hydrate any linked URLs (the URL itself is what feeds back into web fetching for primary reporting). |
| SRC-070 | 95 | How influencer tweets feed curation: Tweets are passed to the LLM as a separate input section labeled "Influencer signal — for context and lead generation, not direct citation unless the tweet itself is the news." The prompt instructs the model to use these as hints about what to investigate, then ground its picks in primary reporting found via web search. |
| SRC-071 | 97 | 3.5 Configuration |
| SRC-072 | 98 | A single config.yaml (or equivalent) controls runtime behavior without code changes. Consider the appropriate format for the requirements and document configuration. Note that in future we would like to configure via web portal potentially. In addition, should be able to spin up multiple agent configurations with different curation prompts/themes and different source and LLM configurations, so there should be configuration per agent, and the scheduler should be aware of them all as it will have to engage with them accordingly. |
| SRC-073 | 100 | Secrets stay in environment variables / secrets manager, not in config.yaml. |
| SRC-074 | 102 | 4. Deployment Recommendation |
| SRC-075 | 103 | 4.1 Recommendation: Start Local, Move to Serverless Containers |
| SRC-076 | 104 | Phase 1 — Local development and validation (weeks 1–3) |
| SRC-077 | 105 | Run the agent on a developer machine, triggered manually or via local cron. Iterate on prompts and output quality. This is by far the fastest path to a digest you actually trust. |
| SRC-078 | 106 | Phase 2 — Serverless containers in production |
| SRC-079 | 107 | Once prompts produce reliable output, deploy to a serverless container platform on whichever cloud you already have access to. The workload is identical across clouds because the agent runs as a container. |
| SRC-080 | 108 | 4.2 Why This Workload Fits Serverless Containers |
| SRC-081 | 109 | The agent runs three times a week at most, each run completes in 1–5 minutes, and is fully stateless. That is the textbook serverless container shape: |
| SRC-082 | 111 | Pay only for execution time — idle cost is effectively $0. |
| SRC-083 | 112 | No infrastructure to manage — no VMs, no Kubernetes, no patching. |
| SRC-084 | 113 | Native cron-style triggers with built-in retry and dead-letter handling. |
| SRC-085 | 114 | Container-based — the same image runs locally, in CI, and in production. |
| SRC-086 | 115 | Logs and traces ship to the cloud's native observability stack automatically. |
| SRC-087 | 117 | 4.3 Equivalent Stacks Across Clouds |
| SRC-088 | 118 | Pick based on what your org already has provisioned. All three options are equally suitable for this workload. |
| SRC-089 | 119 | ComponentGCPAWSAzureSchedulerCloud SchedulerEventBridge SchedulerLogic Apps / Timer-triggerComputeCloud RunLambda or App RunnerContainer AppsStorageCloud StorageS3Blob StorageSecretsSecret ManagerSecrets ManagerKey VaultLogsCloud LoggingCloudWatchApplication Insights |
| SRC-090 | 120 | Note on AWS Lambda specifically: The 15-minute hard timeout is fine for weekly/monthly runs but tight for annual synthesis. App Runner or Fargate avoids this concern. |
| SRC-091 | 121 | 4.4 Why Not the Alternatives |
| SRC-092 | 123 | Always-on VM — Wastes money. The agent is idle >99% of the time. |
| SRC-093 | 124 | Kubernetes — Massive operational overhead for one cron job. Reach for this only if you already have a cluster and want to consolidate. |
| SRC-094 | 125 | GitHub Actions on a schedule — Workable as a "Phase 1.5" if you want hosted scheduling without committing to a cloud project. Acceptable for weekly cadence; less ideal for the annual run because of timeout caps and weaker observability. |
| SRC-095 | 128 | 5. Build Pipeline (CI/CD) |
| SRC-096 | 129 | The pipeline is the same regardless of cloud target. Replace the deploy step with the appropriate cloud CLI command. |
| SRC-097 | 130 | 5.1 Stages |
| SRC-098 | 132 | Lint & test — ruff check, pytest (unit tests on parsing/rendering logic; mock the LLM and Twitter calls). |
| SRC-099 | 133 | Build container — docker build against the project Dockerfile. |
| SRC-100 | 134 | Push to registry — Artifact Registry / ECR / ACR. |
| SRC-101 | 135 | Deploy — cloud-specific deploy command, pointing the scheduler at the new revision. |
| SRC-102 | 136 | Smoke test — Trigger a "dry-run" mode that produces a digest but writes only to a scratch location. Verify it returns 200 with non-empty output and all required fields populated. |
| SRC-103 | 138 | 5.2 Pipeline Choice |
| SRC-104 | 139 | GitHub Actions is the simpler default. Cloud-native pipelines (Cloud Build, CodePipeline, Azure Pipelines) are equally fine if your org standardizes on them. |
| SRC-105 | 140 | 5.3 Secrets |
| SRC-106 | 141 | Required at runtime: |
| SRC-107 | 143 | LLM_API_KEY (provider-specific name; e.g., one per supported provider) |
| SRC-108 | 144 | TWITTER_BEARER_TOKEN |
| SRC-109 | 145 | Optional: WEB_SEARCH_API_KEY if using a non-native search provider (Brave, Tavily, SerpAPI) |
| SRC-110 | 146 | Cloud deployment credentials (workload identity federation preferred over long-lived keys) |
| SRC-111 | 148 | All secrets pulled from the cloud's secrets manager at runtime — never baked into the image. |
| SRC-112 | 150 | 6. The Curation Prompt (Most Important Component) |
| SRC-113 | 151 | Output quality is dominated by prompt quality. The prompt is provider-agnostic — written in plain natural language — and lives in versioned files under prompts/. |
| SRC-114 | 152 | 6.1 Prompt Structure |
| SRC-115 | 153 | The prompt should: |
| SRC-116 | 155 | Specify the timeframe explicitly with concrete ISO dates, not relative phrases. Compute "last week" as actual dates and inject them. |
| SRC-117 | 156 | Spell out the disqualifier explicitly — list examples of technical content to exclude (implementation tutorials, model architecture papers, benchmark deep-dives, framework comparisons, coding examples). |
| SRC-118 | 157 | Spell out the inclusion criteria with examples — what business impact, workforce impact, and policy impact look like. |
| SRC-119 | 158 | Provide the influencer Twitter signal as a labeled context section — clearly distinct from primary news, with instructions for how to use it (lead generation, not direct citation unless the tweet itself is the news). |
| SRC-120 | 159 | Constrain the output format — request a strict structured format (Markdown with a JSON metadata block, or pure JSON) so the renderer can parse it deterministically. |
| SRC-121 | 160 | Specify search budget appropriate to the mode (more for monthly and annual). |
| SRC-122 | 161 | Require a "why it matters" justification for every included item — this both forces better curation and gives the reader the rationale. |
| SRC-123 | 162 | Require working links — every claim must reference a source URL. Items without retrievable URLs are dropped. |
| SRC-124 | 163 | For annual mode only — additionally instruct the model to identify themes/inflection points across the year and to produce a predictions section grounded in observed trends, with reasoning shown. |
| SRC-125 | 165 | 6.2 Prompt Ownership |
| SRC-126 | 166 | Prompt ownership is shared between you and the AI leaders involved at launch. Treat prompts like code: |
| SRC-127 | 168 | Live in version control alongside the rest of the codebase. |
| SRC-128 | 169 | Changes go through review — at least one reviewer beyond the author. |
| SRC-129 | 170 | Each digest output records the prompt version (file hash) used to produce it, so quality regressions can be traced. |
| SRC-130 | 171 | The first 4–6 weeks of operation will produce most of the prompt iteration; beyond that, expect quarterly tuning. |
| SRC-131 | 173 | A more formal owner can be designated later once usage patterns settle. |
| SRC-132 | 175 | 7. Output Experience |
| SRC-133 | 177 | 7.1 AI News Portal |
| SRC-134 | 178 | A well-designed web portal should be included in the distribution. This portal in it's first iteration should be able to view various curation outputs. Daily experience should be focused on the curated articles with links, while weekly, monthly, and annual interaction should be more creatively focused on releant themes with links to the "elevated" articles for that window. Consider visually helpful waays to show themes and or filter articles by theme (e.g. word cloud, model providers, etc.). The portal should be able to choose different agent configurations (that would obviously be associated with different curation prompts). But for the first release, we don't have to support authentication or configuration through the portal. |
| SRC-135 | 180 | 7.2 Rendered Export |
| SRC-136 | 181 | This release does not include automated distribution. The agent writes three formats — Markdown, HTML, JSON — to a configurable output directory. Downstream distribution is left to the consumer, but each of this should be rendered for each curator run and available to download via the portal. |
| SRC-137 | 183 | Email: Paste the HTML file into a mail client, or use a separate tool/script to send it. |
| SRC-138 | 184 | Slack/Teams: Paste the Markdown directly, or use a separate webhook integration. |
| SRC-139 | 185 | Static site / archive: Sync the output directory to a static host (GitHub Pages, S3 + CDN, etc.). |
| SRC-140 | 187 | The output formats and naming convention are designed so that a thin distribution layer can be added later without changes to the core agent. |
| SRC-141 | 188 | Every recommended news item includes a working link to its primary source in all three output formats. This is enforced at the renderer level — items missing a URL are dropped from the final output. |
| SRC-142 | 190 | 8. Operational Concerns |
| SRC-143 | 191 | 8.1 Reliability |
| SRC-144 | 193 | Scheduler retries: Configure 3 retries with exponential backoff. All major cloud schedulers do this natively. |
| SRC-145 | 194 | Idempotency: The output filename includes the date, so re-runs overwrite cleanly. |
| SRC-146 | 195 | Failure alerting: Cloud-native logging alert on any non-2xx response from the worker. Pipe to your incident channel of choice. |
| SRC-147 | 196 | Manual override: The worker accepts an authenticated request to trigger a run on demand — useful for backfills or when the schedule misfires. |
| SRC-148 | 197 | Twitter API failure handling: If the Twitter API is unavailable, the agent should still produce a digest from web sources alone, with a note that influencer signal was unavailable for this run. Twitter is signal, not a hard dependency. |
| SRC-149 | 200 | 8.2 Quality Monitoring |
| SRC-150 | 201 | The agent should log, for each run: number of items considered, number included, items by tier, items by source class (web vs Twitter-originated), total token usage, LLM provider + model + prompt version, and Twitter API call counts. After 4–6 weeks of operation, review whether the same sources keep dominating (a sign of overweighting), whether disqualified-content slip-through is occurring, and whether Twitter signal is actually adding value or just creating noise. |

## Assumptions

- Initial traceability is line-based so the package can be created deterministically without additional LLM calls.

## Open Questions

- Which source requirements should be split or consolidated into more granular functional requirements before implementation starts?
