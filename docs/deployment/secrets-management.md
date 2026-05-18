# Secrets Management Guide

> **Requirement traceability**: SRC-073 (secrets from env vars only), SRC-097 (pipeline stages),
> SRC-100 (push to registry), SRC-101 (deploy), SRC-102 (smoke test), SRC-104 (GitHub Actions),
> SRC-110 (Workload Identity Federation preferred), SRC-111 (secrets manager at runtime).

---

## Principles

1. **Secrets never enter the image.** The `Dockerfile` contains no secrets. All sensitive values
   are injected at container-start time via environment variables or a cloud secrets manager.

2. **Workload Identity Federation (WIF) is preferred** over long-lived service account keys
   for all cloud authentication. WIF eliminates the need to store cloud credentials as GitHub
   repository secrets. See per-cloud instructions below.

3. **CI uses synthetic dummy values.** The `.github/workflows/ci.yml` workflow sets
   `OPENAI_API_KEY=sk-ci-test-not-real` and similar placeholders. Real keys are only ever
   present in protected GitHub Environments (`production`, `staging`).

4. **Runtime secrets are never logged.** The `RuntimeSecrets` Pydantic model reads from
   environment variables at startup and is never serialised to disk, logs, or output files.

---

## Secret Inventory

| Secret name              | Required | Where used                                       | SRC trace |
|--------------------------|----------|--------------------------------------------------|-----------|
| `OPENAI_API_KEY`         | Yes      | LLM calls (OpenAI provider)                      | SRC-055   |
| `ANTHROPIC_API_KEY`      | Optional | LLM calls (Anthropic provider)                   | SRC-056   |
| `GOOGLE_API_KEY`         | Optional | LLM calls (Google Gemini provider)               | SRC-056   |
| `TWITTER_BEARER_TOKEN`   | Optional | Twitter/X v2 API read access                     | SRC-063   |
| `WEB_SEARCH_API_KEY`     | Optional | Brave / Tavily search fallback                   | SRC-060   |
| `GCP_WIF_PROVIDER`       | GCP only | Workload Identity pool provider resource name    | SRC-110   |
| `GCP_SERVICE_ACCOUNT`    | GCP only | GCP service account email for WIF                | SRC-110   |
| `GCP_PROJECT_ID`         | GCP only | GCP project ID for Cloud Run deploy              | SRC-101   |
| `AWS_IAM_ROLE_ARN`       | AWS only | IAM role ARN for OIDC federation                 | SRC-110   |
| `AWS_REGION`             | AWS only | AWS region for ECR / App Runner                  | SRC-101   |
| `AWS_APP_RUNNER_ARN`     | AWS only | App Runner service ARN for deploy                | SRC-101   |
| `AZURE_CLIENT_ID`        | Azure    | Azure app registration client ID                 | SRC-110   |
| `AZURE_TENANT_ID`        | Azure    | Azure tenant ID                                  | SRC-110   |
| `AZURE_SUBSCRIPTION_ID`  | Azure    | Azure subscription ID                            | SRC-110   |
| `AZURE_RG`               | Azure    | Azure resource group name                        | SRC-101   |
| `ACR_NAME`               | Azure    | Azure Container Registry name                    | SRC-100   |
| `SERVICE_URL`            | Optional | Deployed service URL for live health check       | SRC-102   |
| `SLACK_WEBHOOK_URL`      | Optional | Slack incoming webhook for failure alerts        | SRC-146   |

---

## GitHub Actions Setup

### 1. Create GitHub Environments

1. Go to **Settings → Environments → New environment**.
2. Create `production` and `staging` environments.
3. Add **required reviewers** for `production` to gate deployments.
4. Set **Deployment branches** to `main` only.

### 2. Add Secrets to the Environment

For each secret in the inventory above, add it to the appropriate GitHub Environment
(**Settings → Environments → production → Environment secrets → Add secret**).

> **Never** add `OPENAI_API_KEY` or `TWITTER_BEARER_TOKEN` as repository-level secrets —
> they belong only in protected environments where branch restrictions apply.

### 3. Reference Secrets in Workflows

```yaml
# In deploy.yml — environment scopes the secrets automatically
jobs:
  push-and-deploy:
    environment: production          # ← secrets from this environment are available
    steps:
      - name: Deploy
        env:
          OPENAI_API_KEY: ${{ secrets.OPENAI_API_KEY }}
          TWITTER_BEARER_TOKEN: ${{ secrets.TWITTER_BEARER_TOKEN }}
```

---

## Workload Identity Federation (Preferred)

WIF lets GitHub Actions authenticate to cloud providers **without any long-lived credential**.
The GitHub OIDC token is exchanged for a short-lived cloud access token at runtime.

### GCP — Workload Identity Federation

**One-time setup** (run once per GCP project):

```bash
PROJECT_ID="your-gcp-project"
POOL_NAME="github-actions-pool"
PROVIDER_NAME="github-provider"
SA_EMAIL="ai-news-agent-deploy@${PROJECT_ID}.iam.gserviceaccount.com"
REPO="your-org/your-repo"

# 1. Create a Workload Identity Pool
gcloud iam workload-identity-pools create "${POOL_NAME}" \
  --project="${PROJECT_ID}" \
  --location="global" \
  --display-name="GitHub Actions Pool"

# 2. Create a provider inside the pool
gcloud iam workload-identity-pools providers create-oidc "${PROVIDER_NAME}" \
  --project="${PROJECT_ID}" \
  --location="global" \
  --workload-identity-pool="${POOL_NAME}" \
  --display-name="GitHub Actions Provider" \
  --attribute-mapping="google.subject=assertion.sub,attribute.repository=assertion.repository" \
  --issuer-uri="https://token.actions.githubusercontent.com"

# 3. Create (or use existing) a service account
gcloud iam service-accounts create ai-news-agent-deploy \
  --project="${PROJECT_ID}" \
  --display-name="AI News Agent CI/CD"

# 4. Grant the service account permissions
gcloud projects add-iam-policy-binding "${PROJECT_ID}" \
  --member="serviceAccount:${SA_EMAIL}" \
  --role="roles/run.developer"

gcloud projects add-iam-policy-binding "${PROJECT_ID}" \
  --member="serviceAccount:${SA_EMAIL}" \
  --role="roles/artifactregistry.writer"

# 5. Allow GitHub Actions OIDC to impersonate the service account
POOL_RESOURCE="projects/$(gcloud projects describe ${PROJECT_ID} --format='value(projectNumber)')/locations/global/workloadIdentityPools/${POOL_NAME}"
gcloud iam service-accounts add-iam-policy-binding "${SA_EMAIL}" \
  --project="${PROJECT_ID}" \
  --role="roles/iam.workloadIdentityUser" \
  --member="principalSet://iam.googleapis.com/${POOL_RESOURCE}/attribute.repository/${REPO}"

# 6. Record the provider resource name for the GitHub secret
gcloud iam workload-identity-pools providers describe "${PROVIDER_NAME}" \
  --project="${PROJECT_ID}" \
  --location="global" \
  --workload-identity-pool="${POOL_NAME}" \
  --format="value(name)"
```

**GitHub secrets to add** (Environment: `production`):
- `GCP_WIF_PROVIDER` — output of the last command above
- `GCP_SERVICE_ACCOUNT` — `ai-news-agent-deploy@<PROJECT_ID>.iam.gserviceaccount.com`
- `GCP_PROJECT_ID` — your GCP project ID

**Uncomment in `deploy.yml`**:
```yaml
- name: Authenticate to GCP via Workload Identity Federation
  uses: google-github-actions/auth@v2
  with:
    workload_identity_provider: ${{ secrets.GCP_WIF_PROVIDER }}
    service_account: ${{ secrets.GCP_SERVICE_ACCOUNT }}

- name: Configure Docker for GCP Artifact Registry
  run: gcloud auth configure-docker ${{ env.REGISTRY }} --quiet
```

Set repository variables:
- `REGISTRY_URL` → `us-docker.pkg.dev`
- `IMAGE_PATH` → `<PROJECT_ID>/<ARTIFACT_REGISTRY_REPO>/ai-news-agent`

---

### AWS — OIDC Federation (no long-lived keys)

**One-time setup**:

```bash
# 1. Create OIDC identity provider in IAM (one per AWS account)
aws iam create-open-id-connect-provider \
  --url https://token.actions.githubusercontent.com \
  --client-id-list sts.amazonaws.com \
  --thumbprint-list 6938fd4d98bab03faadb97b34396831e3780aea1

# 2. Create an IAM role with a trust policy for your repo
cat > trust-policy.json <<EOF
{
  "Version": "2012-10-17",
  "Statement": [{
    "Effect": "Allow",
    "Principal": {
      "Federated": "arn:aws:iam::<ACCOUNT_ID>:oidc-provider/token.actions.githubusercontent.com"
    },
    "Action": "sts:AssumeRoleWithWebIdentity",
    "Condition": {
      "StringEquals": {
        "token.actions.githubusercontent.com:aud": "sts.amazonaws.com",
        "token.actions.githubusercontent.com:sub": "repo:<ORG>/<REPO>:ref:refs/heads/main"
      }
    }
  }]
}
EOF

aws iam create-role \
  --role-name ai-news-agent-github-actions \
  --assume-role-policy-document file://trust-policy.json

# 3. Attach required policies
aws iam attach-role-policy \
  --role-name ai-news-agent-github-actions \
  --policy-arn arn:aws:iam::aws:policy/AmazonEC2ContainerRegistryPowerUser

aws iam attach-role-policy \
  --role-name ai-news-agent-github-actions \
  --policy-arn arn:aws:iam::aws:policy/AWSAppRunnerFullAccess
```

**GitHub secrets to add** (Environment: `production`):
- `AWS_IAM_ROLE_ARN` → `arn:aws:iam::<ACCOUNT_ID>:role/ai-news-agent-github-actions`

**GitHub variables to add**:
- `REGISTRY_URL` → `<ACCOUNT_ID>.dkr.ecr.<REGION>.amazonaws.com`
- `IMAGE_PATH` → `ai-news-agent`
- `AWS_REGION` → `us-east-1` (or your region)

**Uncomment in `deploy.yml`**:
```yaml
- name: Authenticate to AWS via OIDC
  uses: aws-actions/configure-aws-credentials@v4
  with:
    role-to-assume: ${{ secrets.AWS_IAM_ROLE_ARN }}
    aws-region: ${{ vars.AWS_REGION || 'us-east-1' }}

- name: Log in to Amazon ECR
  uses: aws-actions/amazon-ecr-login@v2
```

> **Note on Lambda timeout**: The `annual` curation run can take 5–10 minutes due to extended
> LLM reasoning. AWS Lambda's 15-minute hard timeout is safe in most cases but tight. Prefer
> **App Runner** or **Fargate** for the annual cadence. (SRC-086)

---

### Azure — Federated Credential (no service principal secret)

**One-time setup**:

```bash
# 1. Create app registration
az ad app create --display-name "ai-news-agent-github-actions"
APP_ID=$(az ad app list --display-name "ai-news-agent-github-actions" --query "[0].appId" -o tsv)

# 2. Create service principal
az ad sp create --id "${APP_ID}"

# 3. Add federated credential
az ad app federated-credential create \
  --id "${APP_ID}" \
  --parameters '{
    "name": "github-actions-main",
    "issuer": "https://token.actions.githubusercontent.com",
    "subject": "repo:<ORG>/<REPO>:ref:refs/heads/main",
    "audiences": ["api://AzureADTokenExchange"]
  }'

# 4. Assign role to service principal
az role assignment create \
  --assignee "${APP_ID}" \
  --role "AcrPush" \
  --scope "/subscriptions/<SUBSCRIPTION_ID>/resourceGroups/<RG>/providers/Microsoft.ContainerRegistry/registries/<ACR_NAME>"

az role assignment create \
  --assignee "${APP_ID}" \
  --role "Contributor" \
  --scope "/subscriptions/<SUBSCRIPTION_ID>/resourceGroups/<RG>/providers/Microsoft.App/containerApps/ai-news-agent"
```

**GitHub secrets to add** (Environment: `production`):
- `AZURE_CLIENT_ID` → `${APP_ID}`
- `AZURE_TENANT_ID` → output of `az account show --query tenantId -o tsv`
- `AZURE_SUBSCRIPTION_ID` → output of `az account show --query id -o tsv`

**GitHub variables to add**:
- `REGISTRY_URL` → `<ACR_NAME>.azurecr.io`
- `IMAGE_PATH` → `ai-news-agent`
- `AZURE_RG` → your resource group name
- `ACR_NAME` → your ACR name

---

## Runtime Secrets in Containers

At container start-time, the cloud injects secrets from its secrets manager:

### GCP — Secret Manager

```bash
# Store secrets
echo -n "sk-..." | gcloud secrets create OPENAI_API_KEY --data-file=-
echo -n "Bearer ..." | gcloud secrets create TWITTER_BEARER_TOKEN --data-file=-

# Reference in Cloud Run
gcloud run deploy ai-news-agent \
  --image us-docker.pkg.dev/PROJECT/REPO/ai-news-agent:SHA \
  --update-secrets "OPENAI_API_KEY=OPENAI_API_KEY:latest,TWITTER_BEARER_TOKEN=TWITTER_BEARER_TOKEN:latest"
```

### AWS — Secrets Manager

```bash
# Store secrets
aws secretsmanager create-secret --name OPENAI_API_KEY --secret-string "sk-..."
aws secretsmanager create-secret --name TWITTER_BEARER_TOKEN --secret-string "Bearer ..."

# Reference via App Runner environment variables (pulled at task start)
# Or inject via ECS task definition secrets:
# "secrets": [{"name": "OPENAI_API_KEY", "valueFrom": "arn:aws:secretsmanager:..."}]
```

### Azure — Key Vault

```bash
# Store secrets
az keyvault secret set --vault-name MY_VAULT --name OPENAI-API-KEY --value "sk-..."
az keyvault secret set --vault-name MY_VAULT --name TWITTER-BEARER-TOKEN --value "Bearer ..."

# Reference in Container Apps
az containerapp secret set \
  --name ai-news-agent \
  --resource-group MY_RG \
  --secrets "openai-key=keyvaultref:https://MY_VAULT.vault.azure.net/secrets/OPENAI-API-KEY,identityref:/subscriptions/.../managedidentities/..."
```

---

## Local Development

Copy `.env.example` to `.env` and fill in your own keys:

```bash
cp .env.example .env
# Edit .env — never commit this file
```

The `.gitignore` already excludes `.env`. The `RuntimeSecrets` model reads from environment
variables — it will never read from a file path.

```bash
# Load env and run a dry-run daily digest
source .env && ai-news-run --cadence daily --dry-run
```

---

## Fallback: Long-Lived Keys (Not Recommended)

If WIF is not available in your org, you can use long-lived service account keys / access keys
as a temporary fallback. **Rotate them every 90 days minimum** and add them as GitHub Environment
secrets (never repository secrets).

```yaml
# deploy.yml — service account key fallback (GCP)
- name: Authenticate to GCP (key fallback — prefer WIF)
  uses: google-github-actions/auth@v2
  with:
    credentials_json: ${{ secrets.GCP_SA_KEY_JSON }}
```

> ⚠️ Long-lived keys are a security risk if the repository is compromised. Migrate to WIF as
> soon as your org allows. See [GitHub's guide](https://docs.github.com/en/actions/security-for-github-actions/security-hardening-your-deployments/about-security-hardening-with-openid-connect)
> for details.

---

## Verification Checklist

Before going to production, verify:

- [ ] `OPENAI_API_KEY` added to GitHub `production` environment (not repository-level)
- [ ] `TWITTER_BEARER_TOKEN` added to GitHub `production` environment
- [ ] WIF configured and tested (or long-lived key rotation policy documented)
- [ ] Cloud secrets manager holds runtime secrets (not baked into image)
- [ ] `.env` excluded from git (confirmed in `.gitignore`)
- [ ] `ci.yml` uses only synthetic dummy secrets — no real keys visible in logs
- [ ] `docker inspect` on the built image confirms no secret env vars are set
- [ ] Smoke test (`ai-news-run --dry-run`) passes with `SMOKE_TEST_MOCK_LLM=1`

---

*Traces: SRC-073, SRC-097, SRC-100, SRC-101, SRC-102, SRC-104, SRC-110, SRC-111, SRC-146*
