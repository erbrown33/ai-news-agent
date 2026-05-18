# Deployment Guide — AI News Curation Agent

> **Requirement traces:** SRC-074–SRC-111 (deployment), SRC-144 (retries), SRC-145 (idempotency),
> SRC-146 (failure alerting), SRC-147 (manual override), SRC-148 (Twitter degradation),
> SRC-085 (same image local/CI/prod), SRC-090 (Lambda 15-min concern for annual)
>
> **SLICE:** SLICE-005 — Operational, deployment, and documentation requirements
>
> **Source-of-Truth Order:** `requirements.md` ▶ `spec.md` ▶ this document

---

## Table of Contents

1. [Deployment Philosophy](#1-deployment-philosophy)
2. [Phase 1 — Local Development](#2-phase-1--local-development)
3. [Phase 2 — Serverless Containers](#3-phase-2--serverless-containers)
4. [GCP — Cloud Run + Cloud Scheduler](#4-gcp--cloud-run--cloud-scheduler)
5. [AWS — App Runner + EventBridge (and Lambda caveat)](#5-aws--app-runner--eventbridge-and-lambda-caveat)
6. [Azure — Container Apps + Logic Apps](#6-azure--container-apps--logic-apps)
7. [Container Image Reference](#7-container-image-reference)
8. [Cloud Scheduler Configuration](#8-cloud-scheduler-configuration)
9. [Runtime Secrets Injection](#9-runtime-secrets-injection)
10. [Output Storage](#10-output-storage)
11. [Health and Monitoring Endpoints](#11-health-and-monitoring-endpoints)
12. [CI/CD Pipeline Overview](#12-cicd-pipeline-overview)
13. [Cost Estimate](#13-cost-estimate)
14. [Requirement Traceability](#14-requirement-traceability)

---

## 1. Deployment Philosophy

> **SRC-080–SRC-086:** The agent workload is a textbook serverless-container shape — short-lived,
> fully stateless, infrequent, and container-packaged. The same Docker image runs on a developer
> laptop, in CI, and in production. No code changes when switching clouds.

### Why Serverless Containers?

| Property | Detail | Source |
|----------|--------|--------|
| Execution frequency | ≤5 jobs/day across all cadences | SRC-009, SRC-028–SRC-032 |
| Run duration | 1–10 minutes per run | SRC-081 |
| State | Fully stateless (state is in output files + TinyDB) | SRC-082 |
| Idle cost | Effectively $0 — pay only for execution seconds | SRC-082 |
| Infrastructure management | Zero VMs, zero Kubernetes, zero patching | SRC-083 |
| Trigger | Native cron-style scheduling with built-in retry | SRC-084 |
| Observability | Logs ship to the cloud's native stack automatically | SRC-086 |

### Recommended Deployment Path

```
Phase 1 (weeks 1–3)   →  Local dev: iterate on prompts and output quality
Phase 2 (onwards)     →  Serverless containers on whichever cloud you already have
```

Pick the cloud based on what your organisation already has provisioned (SRC-088).
All three options (GCP, AWS, Azure) are equally suitable for this workload.

---

## 2. Phase 1 — Local Development

> **SRC-076–SRC-077:** Run on a developer machine, triggered manually or via local cron.
> This is the fastest path to a digest you actually trust.

### Prerequisites

- Python 3.12+
- Docker (optional, for container testing)
- API keys (see [Secrets](#9-runtime-secrets-injection))

### Setup

```bash
# 1. Clone (or download) the repository
git clone https://github.com/erbrown33/wm-ai-news-agent-2.git
cd wm-ai-news-agent-2

# 2. Create a virtual environment
python -m venv .venv
source .venv/bin/activate      # Windows: .venv\Scripts\activate

# 3. Install the package and dev dependencies
pip install -e ".[dev]"

# 4. Configure secrets — never commit real values (SRC-073)
cp .env.example .env
# Edit .env with real values:
#   OPENAI_API_KEY=sk-...
#   TWITTER_BEARER_TOKEN=...
```

### Run the full pipeline manually

```bash
# Load secrets from .env
source .env

# Option A: run the full pipeline (sourcing → curation → rendering)
ai-news-run \
  --cadence daily \
  --agent configs/default-agent.yaml \
  --prompts-dir prompts

# Option B: dry-run (writes to /tmp, no production writes)
ai-news-run \
  --cadence daily \
  --agent configs/default-agent.yaml \
  --prompts-dir prompts \
  --dry-run

# Option C: run each stage individually
ai-news-source --agent configs/default-agent.yaml
ai-news-curate --agent configs/default-agent.yaml --cadence daily
ai-news-render --input outputs/default/2026-05-11-daily.json
```

### Start the background scheduler

```bash
# Reads configs/scheduler.yaml and starts all enabled agent cron jobs
source .env && ai-news-schedule

# Manual trigger override (useful for backfills — SRC-147)
ai-news-schedule --trigger-agent default --job curation --cadence weekly
```

### Start the web portal

```bash
source .env && ai-news-portal
# → http://localhost:8080
```

### Verify output

After a successful run, outputs land at:

```
outputs/
└── default/
    ├── 2026-05-11-daily.md      ← Slack/Teams paste-ready
    ├── 2026-05-11-daily.html    ← Email-client paste-ready
    ├── 2026-05-11-daily.json    ← Machine-readable / archive
    └── store.json               ← TinyDB article store (internal)
```

Re-runs overwrite cleanly — filenames are idempotent by date (SRC-145).

---

## 3. Phase 2 — Serverless Containers

Once your prompts produce reliable output, deploy to the cloud. The same Docker image
runs in all three phases: local development → CI → production (SRC-085).

### Container image

```bash
# Build locally
docker build -t ai-news-agent .

# Test locally with secrets injected at runtime (never baked in — SRC-111)
docker run --rm \
  --env OPENAI_API_KEY="$OPENAI_API_KEY" \
  --env TWITTER_BEARER_TOKEN="$TWITTER_BEARER_TOKEN" \
  --volume "$(pwd)/outputs:/app/outputs" \
  --volume "$(pwd)/configs:/app/configs" \
  -p 8080:8080 \
  ai-news-agent
```

### Architecture per cloud

| Component | GCP | AWS | Azure |
|-----------|-----|-----|-------|
| Compute | Cloud Run | **App Runner** (or Lambda†) | Container Apps |
| Scheduler | Cloud Scheduler | EventBridge Scheduler | Logic Apps / Timer trigger |
| Secrets | Secret Manager | Secrets Manager | Key Vault |
| Storage | Cloud Storage | S3 | Blob Storage |
| Logs | Cloud Logging | CloudWatch | Application Insights |

> **†AWS Lambda note (SRC-090):** Lambda's 15-minute hard timeout is fine for daily, weekly, and
> monthly runs. However, the `annual` curation synthesizes a full year of data and can take
> 5–10 minutes with extended-thinking models. The 15-minute cap is technically feasible but
> leaves no headroom. **Prefer App Runner or Fargate for any deployment that runs the annual
> cadence.** See [§5](#5-aws--app-runner--eventbridge-and-lambda-caveat) for details.

---

## 4. GCP — Cloud Run + Cloud Scheduler

> **SRC-088–SRC-089:** GCP is the recommended starting point if you have an existing GCP project.

### 4.1 Prerequisites

```bash
# Tools required
gcloud --version   # >= 450.0.0
docker --version   # >= 24.0

# Authenticate
gcloud auth login
gcloud auth configure-docker us-docker.pkg.dev

# Set project
export PROJECT_ID="your-gcp-project-id"
export REGION="us-central1"
gcloud config set project "${PROJECT_ID}"
```

### 4.2 One-time infrastructure setup

```bash
# 1. Enable required APIs
gcloud services enable \
  run.googleapis.com \
  cloudscheduler.googleapis.com \
  secretmanager.googleapis.com \
  artifactregistry.googleapis.com \
  iam.googleapis.com

# 2. Create Artifact Registry repository
gcloud artifacts repositories create ai-news-agent \
  --repository-format=docker \
  --location="${REGION}" \
  --description="AI News Agent container images"

# 3. Create a service account for the Cloud Run job
gcloud iam service-accounts create ai-news-runner \
  --display-name="AI News Agent runner"

SA_EMAIL="ai-news-runner@${PROJECT_ID}.iam.gserviceaccount.com"

# 4. Grant Secret Manager access (runtime secrets injection)
gcloud projects add-iam-policy-binding "${PROJECT_ID}" \
  --member="serviceAccount:${SA_EMAIL}" \
  --role="roles/secretmanager.secretAccessor"

# 5. Store runtime secrets in Secret Manager (never baked into the image — SRC-111)
echo -n "${OPENAI_API_KEY}" | \
  gcloud secrets create OPENAI_API_KEY \
    --project="${PROJECT_ID}" \
    --data-file=-

echo -n "${TWITTER_BEARER_TOKEN}" | \
  gcloud secrets create TWITTER_BEARER_TOKEN \
    --project="${PROJECT_ID}" \
    --data-file=-
```

### 4.3 Build and push the container image

```bash
IMAGE="us-docker.pkg.dev/${PROJECT_ID}/ai-news-agent/ai-news-agent"

docker build -t "${IMAGE}:latest" .
docker push "${IMAGE}:latest"
```

### 4.4 Deploy to Cloud Run

```bash
# Deploy the container as a Cloud Run service (SRC-101)
gcloud run deploy ai-news-agent \
  --image "${IMAGE}:latest" \
  --region "${REGION}" \
  --project "${PROJECT_ID}" \
  --service-account "${SA_EMAIL}" \
  --set-secrets "OPENAI_API_KEY=OPENAI_API_KEY:latest,TWITTER_BEARER_TOKEN=TWITTER_BEARER_TOKEN:latest" \
  --set-env-vars "AGENT_CONFIG_DIR=/app/configs,PROMPTS_DIR=/app/prompts" \
  --max-instances 3 \
  --min-instances 0 \
  --memory 1Gi \
  --cpu 1 \
  --timeout 900 \
  --no-allow-unauthenticated

# Note: --timeout 900 = 15 minutes for daily/weekly/monthly
# For annual: increase to --timeout 3600 (1 hour) if using Cloud Run
```

### 4.5 Configure Cloud Scheduler triggers

Cloud Scheduler sends authenticated HTTP requests to the Cloud Run service. (SRC-052, SRC-084)

```bash
# Get the Cloud Run service URL
SERVICE_URL=$(gcloud run services describe ai-news-agent \
  --region="${REGION}" \
  --format="value(status.url)")

# Create service account for Cloud Scheduler to invoke Cloud Run
gcloud iam service-accounts create ai-news-invoker \
  --display-name="AI News Agent scheduler invoker"

INVOKER_EMAIL="ai-news-invoker@${PROJECT_ID}.iam.gserviceaccount.com"

gcloud run services add-iam-policy-binding ai-news-agent \
  --member="serviceAccount:${INVOKER_EMAIL}" \
  --role="roles/run.invoker" \
  --region="${REGION}"

# ── Daily sourcing: 00:00 UTC every day (SRC-009) ──────────────────────────
gcloud scheduler jobs create http ai-news-sourcing-daily \
  --location="${REGION}" \
  --schedule="0 0 * * *" \
  --time-zone="UTC" \
  --uri="${SERVICE_URL}/api/oneshot" \
  --message-body='{"job":"sourcing","cadence":"daily","agent":"default"}' \
  --oidc-service-account-email="${INVOKER_EMAIL}" \
  --attempt-deadline="30m" \
  --max-retry-attempts=3 \
  --min-backoff-duration="30s" \
  --max-backoff-duration="120s"

# ── Daily curation: 00:05 UTC every day (SRC-029) ─────────────────────────
gcloud scheduler jobs create http ai-news-curation-daily \
  --location="${REGION}" \
  --schedule="5 0 * * *" \
  --time-zone="UTC" \
  --uri="${SERVICE_URL}/api/oneshot" \
  --message-body='{"job":"curation","cadence":"daily","agent":"default"}' \
  --oidc-service-account-email="${INVOKER_EMAIL}" \
  --attempt-deadline="30m" \
  --max-retry-attempts=3 \
  --min-backoff-duration="30s" \
  --max-backoff-duration="120s"

# ── Weekly curation: 01:00 UTC Sunday (SRC-030) ────────────────────────────
gcloud scheduler jobs create http ai-news-curation-weekly \
  --location="${REGION}" \
  --schedule="0 1 * * 0" \
  --time-zone="UTC" \
  --uri="${SERVICE_URL}/api/oneshot" \
  --message-body='{"job":"curation","cadence":"weekly","agent":"default"}' \
  --oidc-service-account-email="${INVOKER_EMAIL}" \
  --attempt-deadline="30m" \
  --max-retry-attempts=3 \
  --min-backoff-duration="30s" \
  --max-backoff-duration="120s"

# ── Monthly curation: 02:00 UTC 1st of month (SRC-031) ────────────────────
gcloud scheduler jobs create http ai-news-curation-monthly \
  --location="${REGION}" \
  --schedule="0 2 1 * *" \
  --time-zone="UTC" \
  --uri="${SERVICE_URL}/api/oneshot" \
  --message-body='{"job":"curation","cadence":"monthly","agent":"default"}' \
  --oidc-service-account-email="${INVOKER_EMAIL}" \
  --attempt-deadline="30m" \
  --max-retry-attempts=3 \
  --min-backoff-duration="30s" \
  --max-backoff-duration="120s"

# ── Annual curation: 03:00 UTC January 1st (SRC-032) ─────────────────────
# NOTE: increase --attempt-deadline for annual synthesis (SRC-090)
gcloud scheduler jobs create http ai-news-curation-annual \
  --location="${REGION}" \
  --schedule="0 3 1 1 *" \
  --time-zone="UTC" \
  --uri="${SERVICE_URL}/api/oneshot" \
  --message-body='{"job":"curation","cadence":"annual","agent":"default"}' \
  --oidc-service-account-email="${INVOKER_EMAIL}" \
  --attempt-deadline="60m" \
  --max-retry-attempts=3 \
  --min-backoff-duration="30s" \
  --max-backoff-duration="120s"
```

> **Retry policy (SRC-144):** All Cloud Scheduler jobs above are configured with
> `--max-retry-attempts=3` and exponential backoff `30s → 60s → 120s`.
> This matches the application-level retry configuration in `configs/scheduler.yaml`.

### 4.6 Manual override (on-demand trigger)

```bash
# Trigger a run on-demand — useful for backfills or misfire recovery (SRC-147)
gcloud scheduler jobs run ai-news-curation-daily \
  --location="${REGION}"

# Or directly via curl (requires authentication)
curl -X POST "${SERVICE_URL}/api/trigger" \
  -H "Authorization: Bearer $(gcloud auth print-identity-token)" \
  -H "Content-Type: application/json" \
  -d '{"job":"curation","cadence":"daily","agent":"default"}'
```

### 4.7 Output storage with Cloud Storage

```bash
# Create a bucket for persistent output storage
gsutil mb -l "${REGION}" "gs://${PROJECT_ID}-ai-news-outputs"

# Mount via Cloud Run volume mount (or sync after each run)
# Add to Cloud Run deploy:
#   --add-volume name=outputs,type=cloud-storage,bucket=${PROJECT_ID}-ai-news-outputs \
#   --add-volume-mount volume=outputs,mount-path=/app/outputs
```

---

## 5. AWS — App Runner + EventBridge (and Lambda Caveat)

> **SRC-090:** AWS Lambda's 15-minute hard timeout is fine for daily/weekly/monthly runs but
> tight for the annual synthesis run. **App Runner is the recommended compute for any
> deployment that includes the annual cadence.** Lambda is acceptable for daily/weekly/monthly
> only if annual is run via App Runner or Fargate.

### 5.1 The Lambda Timeout Problem for Annual Runs

The `annual` cadence uses an extended-thinking model (e.g., `o3` with research mode) to synthesize
a full year of articles and produce 10 falsifiable predictions (SRC-032). This typically takes
**5–10 minutes** depending on article volume and model reasoning depth.

| Cadence | Typical duration | Lambda 15-min cap | Safe? |
|---------|-----------------|-------------------|-------|
| Daily | 1–3 min | ✅ Fine | Yes |
| Weekly | 2–4 min | ✅ Fine | Yes |
| Monthly | 3–6 min | ✅ Fine with headroom | Yes |
| Annual | 5–10 min | ⚠️ Tight | Marginal — prefer App Runner |

**Recommendation:** Use **App Runner** for all cadences. If you must use Lambda for cost reasons,
configure it for daily/weekly/monthly and use a separate App Runner service for the annual run.

### 5.2 Prerequisites

```bash
# Tools required
aws --version       # >= 2.15
docker --version    # >= 24.0

# Authenticate
aws configure       # Or use environment variables / instance roles

export AWS_REGION="us-east-1"
export AWS_ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
```

### 5.3 One-time infrastructure setup

```bash
# 1. Create ECR repository
aws ecr create-repository \
  --repository-name ai-news-agent \
  --region "${AWS_REGION}"

ECR_REGISTRY="${AWS_ACCOUNT_ID}.dkr.ecr.${AWS_REGION}.amazonaws.com"

# 2. Store runtime secrets (never baked into the image — SRC-111)
aws secretsmanager create-secret \
  --name OPENAI_API_KEY \
  --secret-string "${OPENAI_API_KEY}"

aws secretsmanager create-secret \
  --name TWITTER_BEARER_TOKEN \
  --secret-string "${TWITTER_BEARER_TOKEN}"

# 3. Create IAM role for App Runner to pull secrets
cat > trust-policy.json <<'EOF'
{
  "Version": "2012-10-17",
  "Statement": [{
    "Effect": "Allow",
    "Principal": { "Service": "tasks.apprunner.amazonaws.com" },
    "Action": "sts:AssumeRole"
  }]
}
EOF

aws iam create-role \
  --role-name ai-news-agent-runner \
  --assume-role-policy-document file://trust-policy.json

aws iam attach-role-policy \
  --role-name ai-news-agent-runner \
  --policy-arn arn:aws:iam::aws:policy/SecretsManagerReadWrite

aws iam attach-role-policy \
  --role-name ai-news-agent-runner \
  --policy-arn arn:aws:iam::aws:policy/AmazonEC2ContainerRegistryReadOnly

RUNNER_ROLE_ARN=$(aws iam get-role \
  --role-name ai-news-agent-runner \
  --query Role.Arn --output text)
```

### 5.4 Build and push to ECR

```bash
# Authenticate Docker with ECR
aws ecr get-login-password --region "${AWS_REGION}" | \
  docker login --username AWS \
    --password-stdin "${ECR_REGISTRY}"

# Build and push (SRC-099–SRC-100)
docker build -t "${ECR_REGISTRY}/ai-news-agent:latest" .
docker push "${ECR_REGISTRY}/ai-news-agent:latest"
```

### 5.5 Deploy to AWS App Runner (recommended — avoids Lambda timeout)

```bash
# Create App Runner service (SRC-101)
# App Runner has no hard timeout — safe for annual curation (SRC-090)
aws apprunner create-service \
  --service-name ai-news-agent \
  --source-configuration "{
    \"ImageRepository\": {
      \"ImageIdentifier\": \"${ECR_REGISTRY}/ai-news-agent:latest\",
      \"ImageRepositoryType\": \"ECR\",
      \"ImageConfiguration\": {
        \"Port\": \"8080\",
        \"RuntimeEnvironmentVariables\": {
          \"AGENT_CONFIG_DIR\": \"/app/configs\",
          \"PROMPTS_DIR\": \"/app/prompts\"
        },
        \"RuntimeEnvironmentSecrets\": {
          \"OPENAI_API_KEY\": \"arn:aws:secretsmanager:${AWS_REGION}:${AWS_ACCOUNT_ID}:secret:OPENAI_API_KEY\",
          \"TWITTER_BEARER_TOKEN\": \"arn:aws:secretsmanager:${AWS_REGION}:${AWS_ACCOUNT_ID}:secret:TWITTER_BEARER_TOKEN\"
        }
      }
    },
    \"AuthenticationConfiguration\": {
      \"AccessRoleArn\": \"${RUNNER_ROLE_ARN}\"
    },
    \"AutoDeploymentsEnabled\": false
  }" \
  --instance-configuration "{
    \"Cpu\": \"1 vCPU\",
    \"Memory\": \"2 GB\"
  }"

SERVICE_URL=$(aws apprunner describe-service \
  --service-arn "$(aws apprunner list-services --query 'ServiceSummaryList[?ServiceName==`ai-news-agent`].ServiceArn' --output text)" \
  --query Service.ServiceUrl --output text)

echo "App Runner URL: https://${SERVICE_URL}"
```

### 5.6 Configure EventBridge Scheduler

```bash
# Create EventBridge execution role
aws iam create-role \
  --role-name ai-news-eventbridge-scheduler \
  --assume-role-policy-document '{
    "Version": "2012-10-17",
    "Statement": [{
      "Effect": "Allow",
      "Principal": { "Service": "scheduler.amazonaws.com" },
      "Action": "sts:AssumeRole"
    }]
  }'

# Allow scheduler to invoke App Runner (via HTTP target)
# EventBridge Scheduler supports Universal Targets — configure per your org setup

# ── Daily sourcing: 00:00 UTC (SRC-009) ────────────────────────────────────
aws scheduler create-schedule \
  --name ai-news-sourcing-daily \
  --schedule-expression "cron(0 0 * * ? *)" \
  --schedule-expression-timezone "UTC" \
  --flexible-time-window '{"Mode": "OFF"}' \
  --target '{
    "Arn": "arn:aws:scheduler:::aws-sdk:apprunner:startDeployment",
    "RoleArn": "arn:aws:iam::'${AWS_ACCOUNT_ID}':role/ai-news-eventbridge-scheduler",
    "Input": "{\"job\":\"sourcing\",\"cadence\":\"daily\",\"agent\":\"default\"}"
  }' \
  --retry-policy '{"MaximumRetryAttempts": 3, "MaximumEventAgeInSeconds": 300}'

# ── Daily curation: 00:05 UTC (SRC-029) ───────────────────────────────────
aws scheduler create-schedule \
  --name ai-news-curation-daily \
  --schedule-expression "cron(5 0 * * ? *)" \
  --schedule-expression-timezone "UTC" \
  --flexible-time-window '{"Mode": "OFF"}' \
  --target '{
    "Arn": "arn:aws:scheduler:::aws-sdk:apprunner:startDeployment",
    "RoleArn": "arn:aws:iam::'${AWS_ACCOUNT_ID}':role/ai-news-eventbridge-scheduler",
    "Input": "{\"job\":\"curation\",\"cadence\":\"daily\",\"agent\":\"default\"}"
  }' \
  --retry-policy '{"MaximumRetryAttempts": 3, "MaximumEventAgeInSeconds": 300}'

# ── Weekly curation: 01:00 UTC Sunday (SRC-030) ────────────────────────────
aws scheduler create-schedule \
  --name ai-news-curation-weekly \
  --schedule-expression "cron(0 1 ? * 1 *)" \
  --schedule-expression-timezone "UTC" \
  --flexible-time-window '{"Mode": "OFF"}' \
  --target '{
    "Arn": "arn:aws:scheduler:::aws-sdk:apprunner:startDeployment",
    "RoleArn": "arn:aws:iam::'${AWS_ACCOUNT_ID}':role/ai-news-eventbridge-scheduler",
    "Input": "{\"job\":\"curation\",\"cadence\":\"weekly\",\"agent\":\"default\"}"
  }' \
  --retry-policy '{"MaximumRetryAttempts": 3, "MaximumEventAgeInSeconds": 600}'

# ── Monthly curation: 02:00 UTC 1st of month (SRC-031) ────────────────────
aws scheduler create-schedule \
  --name ai-news-curation-monthly \
  --schedule-expression "cron(0 2 1 * ? *)" \
  --schedule-expression-timezone "UTC" \
  --flexible-time-window '{"Mode": "OFF"}' \
  --target '{
    "Arn": "arn:aws:scheduler:::aws-sdk:apprunner:startDeployment",
    "RoleArn": "arn:aws:iam::'${AWS_ACCOUNT_ID}':role/ai-news-eventbridge-scheduler",
    "Input": "{\"job\":\"curation\",\"cadence\":\"monthly\",\"agent\":\"default\"}"
  }' \
  --retry-policy '{"MaximumRetryAttempts": 3, "MaximumEventAgeInSeconds": 900}'

# ── Annual curation: 03:00 UTC January 1st (SRC-032) ─────────────────────
# NOTE: annual uses App Runner (not Lambda) to avoid the 15-min timeout (SRC-090)
aws scheduler create-schedule \
  --name ai-news-curation-annual \
  --schedule-expression "cron(0 3 1 1 ? *)" \
  --schedule-expression-timezone "UTC" \
  --flexible-time-window '{"Mode": "OFF"}' \
  --target '{
    "Arn": "arn:aws:scheduler:::aws-sdk:apprunner:startDeployment",
    "RoleArn": "arn:aws:iam::'${AWS_ACCOUNT_ID}':role/ai-news-eventbridge-scheduler",
    "Input": "{\"job\":\"curation\",\"cadence\":\"annual\",\"agent\":\"default\"}"
  }' \
  --retry-policy '{"MaximumRetryAttempts": 3, "MaximumEventAgeInSeconds": 3600}'
```

### 5.7 Manual override (AWS)

```bash
# Force-run a specific cadence on demand (SRC-147)
# Via the portal's /api/trigger endpoint:
curl -X POST "https://${SERVICE_URL}/api/trigger" \
  -H "Authorization: Bearer ${SCHEDULER_API_KEY}" \
  -H "Content-Type: application/json" \
  -d '{"job":"curation","cadence":"daily","agent":"default"}'

# Or re-invoke App Runner directly
aws apprunner start-deployment \
  --service-arn "${SERVICE_ARN}"
```

### 5.8 Output storage with S3

```bash
# Create S3 bucket for digest outputs
aws s3 mb "s3://${AWS_ACCOUNT_ID}-ai-news-outputs" \
  --region "${AWS_REGION}"

# After each run, sync outputs to S3
aws s3 sync outputs/ "s3://${AWS_ACCOUNT_ID}-ai-news-outputs/" \
  --exclude "store.json"     # TinyDB store is agent-local; outputs are portable
```

---

## 6. Azure — Container Apps + Logic Apps

> **SRC-089:** Azure Container Apps is the recommended Azure compute option, with Logic Apps
> providing the cron-style trigger (or Azure Container App Jobs for direct schedule execution).

### 6.1 Prerequisites

```bash
# Tools required
az --version       # >= 2.59
docker --version   # >= 24.0

# Authenticate
az login
az account set --subscription "your-subscription-id"

export SUBSCRIPTION_ID=$(az account show --query id -o tsv)
export RG="ai-news-agent-rg"
export LOCATION="eastus"
export ACR_NAME="ainewsagentacr"     # Must be globally unique
export KEYVAULT_NAME="ai-news-kv"   # Must be globally unique
```

### 6.2 One-time infrastructure setup

```bash
# 1. Create resource group
az group create --name "${RG}" --location "${LOCATION}"

# 2. Create Azure Container Registry (ACR)
az acr create \
  --resource-group "${RG}" \
  --name "${ACR_NAME}" \
  --sku Basic \
  --admin-enabled false

# 3. Create Key Vault for runtime secrets (never baked into image — SRC-111)
az keyvault create \
  --resource-group "${RG}" \
  --name "${KEYVAULT_NAME}" \
  --location "${LOCATION}" \
  --sku standard

# 4. Store secrets
az keyvault secret set \
  --vault-name "${KEYVAULT_NAME}" \
  --name "OPENAI-API-KEY" \
  --value "${OPENAI_API_KEY}"

az keyvault secret set \
  --vault-name "${KEYVAULT_NAME}" \
  --name "TWITTER-BEARER-TOKEN" \
  --value "${TWITTER_BEARER_TOKEN}"

# 5. Create a managed identity for the Container App to access Key Vault
az identity create \
  --resource-group "${RG}" \
  --name ai-news-agent-identity

IDENTITY_ID=$(az identity show \
  --resource-group "${RG}" \
  --name ai-news-agent-identity \
  --query id -o tsv)

IDENTITY_PRINCIPAL=$(az identity show \
  --resource-group "${RG}" \
  --name ai-news-agent-identity \
  --query principalId -o tsv)

# Grant the managed identity access to Key Vault secrets
az keyvault set-policy \
  --name "${KEYVAULT_NAME}" \
  --object-id "${IDENTITY_PRINCIPAL}" \
  --secret-permissions get list
```

### 6.3 Build and push to ACR

```bash
# Log in to ACR
az acr login --name "${ACR_NAME}"

ACR_REGISTRY="${ACR_NAME}.azurecr.io"

# Build and push (SRC-099–SRC-100)
docker build -t "${ACR_REGISTRY}/ai-news-agent:latest" .
docker push "${ACR_REGISTRY}/ai-news-agent:latest"
```

### 6.4 Deploy to Azure Container Apps

```bash
# Create Container Apps environment
az containerapp env create \
  --resource-group "${RG}" \
  --name ai-news-agent-env \
  --location "${LOCATION}"

# Deploy the Container App (SRC-101)
az containerapp create \
  --resource-group "${RG}" \
  --environment ai-news-agent-env \
  --name ai-news-agent \
  --image "${ACR_REGISTRY}/ai-news-agent:latest" \
  --registry-server "${ACR_REGISTRY}" \
  --user-assigned "${IDENTITY_ID}" \
  --target-port 8080 \
  --ingress external \
  --min-replicas 0 \
  --max-replicas 3 \
  --cpu 1.0 \
  --memory 2.0Gi \
  --secrets \
    "openai-key=keyvaultref:https://${KEYVAULT_NAME}.vault.azure.net/secrets/OPENAI-API-KEY,identityref:${IDENTITY_ID}" \
    "twitter-token=keyvaultref:https://${KEYVAULT_NAME}.vault.azure.net/secrets/TWITTER-BEARER-TOKEN,identityref:${IDENTITY_ID}" \
  --env-vars \
    "OPENAI_API_KEY=secretref:openai-key" \
    "TWITTER_BEARER_TOKEN=secretref:twitter-token" \
    "AGENT_CONFIG_DIR=/app/configs" \
    "PROMPTS_DIR=/app/prompts"

SERVICE_URL=$(az containerapp show \
  --resource-group "${RG}" \
  --name ai-news-agent \
  --query properties.configuration.ingress.fqdn -o tsv)

echo "Container App URL: https://${SERVICE_URL}"
```

### 6.5 Configure Logic Apps triggers

Azure Logic Apps provides cron-style HTTP triggers to the Container App. (SRC-052)

```bash
# Alternatively, use Azure Container App Jobs for direct scheduling:
# az containerapp job create — supports cron expressions natively

# ── Daily sourcing job: 00:00 UTC (SRC-009) ───────────────────────────────
az containerapp job create \
  --resource-group "${RG}" \
  --environment ai-news-agent-env \
  --name ai-news-sourcing-daily \
  --image "${ACR_REGISTRY}/ai-news-agent:latest" \
  --registry-server "${ACR_REGISTRY}" \
  --user-assigned "${IDENTITY_ID}" \
  --cron-expression "0 0 * * *" \
  --replica-completion-count 1 \
  --replica-timeout 1800 \
  --parallelism 1 \
  --command "ai-news-oneshot" \
  --args "--job=sourcing" "--cadence=daily" "--agent=configs/default-agent.yaml" \
  --secrets \
    "openai-key=keyvaultref:https://${KEYVAULT_NAME}.vault.azure.net/secrets/OPENAI-API-KEY,identityref:${IDENTITY_ID}" \
    "twitter-token=keyvaultref:https://${KEYVAULT_NAME}.vault.azure.net/secrets/TWITTER-BEARER-TOKEN,identityref:${IDENTITY_ID}" \
  --env-vars \
    "OPENAI_API_KEY=secretref:openai-key" \
    "TWITTER_BEARER_TOKEN=secretref:twitter-token"

# ── Daily curation: 00:05 UTC (SRC-029) ───────────────────────────────────
az containerapp job create \
  --resource-group "${RG}" \
  --environment ai-news-agent-env \
  --name ai-news-curation-daily \
  --image "${ACR_REGISTRY}/ai-news-agent:latest" \
  --registry-server "${ACR_REGISTRY}" \
  --user-assigned "${IDENTITY_ID}" \
  --cron-expression "5 0 * * *" \
  --replica-completion-count 1 \
  --replica-timeout 1800 \
  --command "ai-news-oneshot" \
  --args "--job=curation" "--cadence=daily" "--agent=configs/default-agent.yaml"
  # (add --secrets / --env-vars same as above)

# ── Weekly: 01:00 UTC Sunday (SRC-030), Monthly: 02:00 UTC 1st (SRC-031),
# ── Annual: 03:00 UTC Jan 1 (SRC-032) — follow same pattern above
# Azure Container App Jobs do NOT have a 15-minute limit, so annual is safe.
```

### 6.6 Manual override (Azure)

```bash
# Trigger a job on-demand (SRC-147)
az containerapp job start \
  --resource-group "${RG}" \
  --name ai-news-curation-daily

# Or via the portal endpoint
curl -X POST "https://${SERVICE_URL}/api/trigger" \
  -H "Authorization: Bearer ${SCHEDULER_API_KEY}" \
  -H "Content-Type: application/json" \
  -d '{"job":"curation","cadence":"weekly","agent":"default"}'
```

---

## 7. Container Image Reference

The `Dockerfile` uses a multi-stage build to keep the production image lean.
The same image runs locally, in CI, and in production (SRC-085).

### Key facts

| Property | Value |
|----------|-------|
| Base image | `python:3.12-slim` |
| Python version | 3.12 |
| Working directory | `/app` |
| Exposed port | `8080` |
| Default command | `ai-news-portal` (web portal) |
| Config directory | `/app/configs` |
| Prompts directory | `/app/prompts` |
| Outputs directory | `/app/outputs` (mountable volume) |

### Running individual jobs in the container

```bash
# Web portal (default)
docker run --rm -p 8080:8080 \
  -e OPENAI_API_KEY="..." \
  -e TWITTER_BEARER_TOKEN="..." \
  ai-news-agent

# End-to-end pipeline run (sourcing → curation → rendering)
docker run --rm \
  -e OPENAI_API_KEY="..." \
  -e TWITTER_BEARER_TOKEN="..." \
  -v "$(pwd)/outputs:/app/outputs" \
  ai-news-agent \
  ai-news-run --cadence daily --agent configs/default-agent.yaml

# One-shot serverless trigger (for cloud scheduler invocations)
docker run --rm \
  -e OPENAI_API_KEY="..." \
  -e TWITTER_BEARER_TOKEN="..." \
  -v "$(pwd)/outputs:/app/outputs" \
  ai-news-agent \
  ai-news-oneshot --job curation --cadence weekly --agent default

# Dry-run (no production writes — used in CI smoke test, SRC-102)
docker run --rm \
  -e OPENAI_API_KEY="..." \
  -e TWITTER_BEARER_TOKEN="..." \
  -e SMOKE_TEST_MOCK_LLM="1" \
  -v /tmp/smoke-out:/smoke-out \
  ai-news-agent \
  ai-news-run --cadence daily --dry-run --scratch-dir /smoke-out
```

---

## 8. Cloud Scheduler Configuration

### Cron schedule reference

| Job | Cron expression | Cadence | Source |
|-----|----------------|---------|--------|
| Sourcing (all agents) | `0 0 * * *` | Daily at 00:00 UTC | SRC-009 |
| Curation — daily | `5 0 * * *` | Daily at 00:05 UTC | SRC-029 |
| Curation — weekly | `0 1 * * 0` | Sunday at 01:00 UTC | SRC-030 |
| Curation — monthly | `0 2 1 * *` | 1st of month, 02:00 UTC | SRC-031 |
| Curation — annual | `0 3 1 1 *` | January 1st, 03:00 UTC | SRC-032 |

### Retry policy (SRC-144)

All scheduler jobs must be configured with **3 retry attempts** and **exponential backoff**:

| Attempt | Delay |
|---------|-------|
| Initial failure | Immediate retry |
| After 1st retry failure | Wait 30s |
| After 2nd retry failure | Wait 60s |
| After 3rd retry failure | Wait 120s → alert |

This matches the application-level retry in `configs/scheduler.yaml`:
```yaml
scheduler:
  max_retries: 3
  retry_backoff_base_seconds: 30
```

All major cloud schedulers support native retry with exponential backoff:
- **GCP Cloud Scheduler**: `--max-retry-attempts=3 --min-backoff-duration=30s --max-backoff-duration=120s`
- **AWS EventBridge**: `--retry-policy '{"MaximumRetryAttempts": 3}'`
- **Azure Container App Jobs**: configured via `--replica-timeout` and Logic Apps retry policies

---

## 9. Runtime Secrets Injection

> **SRC-073, SRC-111:** Secrets are NEVER baked into the container image. They are ALWAYS
> injected at container-start time from the cloud's secrets manager.

| Secret | Env var | Required | Cloud source |
|--------|---------|----------|--------------|
| OpenAI API key | `OPENAI_API_KEY` | Yes | GCP Secret Manager / AWS Secrets Manager / Azure Key Vault |
| Twitter bearer token | `TWITTER_BEARER_TOKEN` | Recommended | Same |
| Web search API key | `WEB_SEARCH_API_KEY` | Optional | Same |
| Scheduler API key | `SCHEDULER_API_KEY` | Optional | Same |

Full secrets management guide: [`docs/deployment/secrets-management.md`](secrets-management.md)

---

## 10. Output Storage

### Local development

Outputs are written to `outputs/{agent_id}/` relative to the working directory.
They are gitignored. Re-runs overwrite cleanly (idempotent by date — SRC-145).

### Production — persistent storage

Mount a cloud storage volume to `/app/outputs` in the container, or sync outputs
after each run:

```bash
# GCP — after each Cloud Run invocation, sync outputs to Cloud Storage
gsutil rsync -r outputs/ gs://your-project-ai-news-outputs/

# AWS — sync to S3
aws s3 sync outputs/ s3://your-account-ai-news-outputs/

# Azure — sync to Blob Storage
az storage blob sync \
  --source outputs/ \
  --container ai-news-outputs \
  --account-name yourstorageaccount
```

The web portal reads from the local `outputs/` directory. For production,
configure the portal container to mount the cloud storage volume.

---

## 11. Health and Monitoring Endpoints

| Endpoint | Method | Description | Source |
|----------|--------|-------------|--------|
| `/api/health` | GET | Health check; returns agents list, total digests, scheduler status | SRC-146 |
| `/api/trigger` | POST | On-demand run trigger (requires `SCHEDULER_API_KEY` if set) | SRC-147 |
| `/api/status` | GET | Scheduler job status; next run times | SRC-147 |

### Health check response schema

```json
{
  "status": "ok",
  "agents": ["default"],
  "total_digests": 42,
  "scheduler": {
    "running": true,
    "jobs": [
      {"id": "sourcing-daily-default", "next_run": "2026-05-12T00:00:00Z"},
      {"id": "curation-daily-default", "next_run": "2026-05-12T00:05:00Z"}
    ]
  }
}
```

### Failure alerting (SRC-146)

Configure a cloud-native log alert to fire on any non-2xx response from the worker.
Pipe to your incident channel of choice (Slack, PagerDuty, email):

```bash
# GCP — create a log-based alert
gcloud logging metrics create ai-news-agent-errors \
  --description="Non-2xx responses from AI News Agent" \
  --log-filter='resource.type="cloud_run_revision" AND httpRequest.status>=400'

# Then create an alerting policy on the metric in Cloud Monitoring
```

See [deploy.yml](.github/workflows/deploy.yml) for a Slack notification stub that can be
activated by setting `SLACK_WEBHOOK_URL` in GitHub Environment secrets.

---

## 12. CI/CD Pipeline Overview

The CI/CD pipeline is the same regardless of cloud target. Replace the deploy step
with the appropriate cloud CLI command. (SRC-096)

```
push to branch
     │
     ▼
┌─────────────────────────────────────────────────────────────────────┐
│  Stage 1: Lint  (SRC-098)                                           │
│    ruff check src/ tests/                                           │
│    ruff format --check src/ tests/                                  │
└────────────────────────┬────────────────────────────────────────────┘
                         │
                         ▼
┌─────────────────────────────────────────────────────────────────────┐
│  Stage 2: Type-check  (advisory)                                    │
│    mypy src/                                                        │
└────────────────────────┬────────────────────────────────────────────┘
                         │
                         ▼
┌─────────────────────────────────────────────────────────────────────┐
│  Stage 3: Tests  (SRC-098)                                          │
│    pytest tests/unit/  (mocked LLM + Twitter — no real API calls)  │
│    pytest tests/integration/                                        │
│    pytest tests/ci/    (smoke contract assertions)                  │
│    Coverage ≥ 85%                                                   │
└────────────────────────┬────────────────────────────────────────────┘
                         │
                         ▼
┌─────────────────────────────────────────────────────────────────────┐
│  Stage 4: Prompt hash verification  (SRC-129)                       │
│    ai-news-prompt-hashes --verify                                   │
│    Blocks merge if prompts changed without updating manifest        │
└────────────────────────┬────────────────────────────────────────────┘
                         │
                         ▼
┌─────────────────────────────────────────────────────────────────────┐
│  Stage 5: Docker build  (SRC-099)                                   │
│    docker buildx build (no push in CI)                              │
│    Container import + entry-point checks                            │
└────────────────────────┬────────────────────────────────────────────┘
                         │
                         ▼
┌─────────────────────────────────────────────────────────────────────┐
│  Stage 6: Container smoke test  (SRC-102)                           │
│    Run ai-news-run --dry-run inside the built container             │
│    SMOKE_TEST_MOCK_LLM=1 (zero API cost)                            │
│    Assert: non-empty MD/HTML/JSON, all §8.2 monitoring fields,      │
│            prompt_version sha256:..., zero items without URL,       │
│            filename YYYY-MM-DD-{cadence}.*                          │
└────────────────────────┬────────────────────────────────────────────┘
                         │ (main branch only)
                         ▼
┌─────────────────────────────────────────────────────────────────────┐
│  Deploy  (SRC-100–SRC-101)                                          │
│    docker push to registry                                          │
│    Cloud-specific deploy command (Cloud Run / App Runner / ACA)     │
│    Post-deploy smoke test                                           │
└─────────────────────────────────────────────────────────────────────┘
```

Full CI workflow: [`.github/workflows/ci.yml`](.github/workflows/ci.yml)
Full deploy workflow: [`.github/workflows/deploy.yml`](.github/workflows/deploy.yml)

---

## 13. Cost Estimate

> **SRC-082:** Pay only for execution time — idle cost is effectively $0.

Assumptions: 1 agent, default model (gpt-4o for daily/weekly, o3 for monthly/annual),
all four cadences active, 30-day month.

| Cloud | Compute cost (est.) | LLM cost (est.) | Total (est.) |
|-------|--------------------:|----------------:|-------------:|
| GCP Cloud Run | $2–5/month | $30–80/month | **$35–85/month** |
| AWS App Runner | $3–7/month | $30–80/month | **$35–90/month** |
| Azure Container Apps | $2–6/month | $30–80/month | **$35–90/month** |

LLM cost is the dominant variable. Annual synthesis with o3 + extended thinking can cost
$20–50 per run. Daily digests with gpt-4o cost $1–3/run.

---

## 14. Requirement Traceability

| Requirement | Implementation |
|-------------|----------------|
| SRC-074 — Deployment recommendation | §1 (philosophy), §3 (phase summary) |
| SRC-075 — Start local, move to serverless | §2 (Phase 1), §3 (Phase 2) |
| SRC-076–SRC-077 — Local dev | §2 (local setup and run commands) |
| SRC-078–SRC-079 — Serverless phase | §3–§6 (GCP/AWS/Azure) |
| SRC-080–SRC-086 — Serverless container rationale | §1 (rationale table) |
| SRC-085 — Same image local/CI/prod | §7 (container reference), §12 (CI pipeline) |
| SRC-087–SRC-089 — Equivalent stacks | §4–§6 (per-cloud sections) |
| SRC-090 — Lambda 15-min timeout | §3 (table note), §5 (dedicated section) |
| SRC-091–SRC-094 — Why not alternatives | §1 (serverless rationale) |
| SRC-095–SRC-104 — CI/CD pipeline | §12 (pipeline overview) |
| SRC-144 — Retry policy | §8 (scheduler retry table) |
| SRC-145 — Idempotent filenames | §2 (output layout note), §10 (output storage) |
| SRC-146 — Failure alerting | §11 (health endpoints) |
| SRC-147 — Manual override | §4.6, §5.7, §6.6 (per-cloud override) |

---

*Traces: SRC-074–SRC-111, SRC-144–SRC-148*
