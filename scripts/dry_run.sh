#!/usr/bin/env bash
# scripts/dry_run.sh — End-to-end dry-run of the full pipeline.
#
# Runs sourcing → curation → rendering in dry-run mode so rendered files
# are written to a scratch directory instead of the production output tree.
# No real LLM or Twitter API calls are made unless real API keys are present
# in the environment.
#
# Usage
# ─────
#   # Default: daily cadence, default agent config, auto scratch dir
#   ./scripts/dry_run.sh
#
#   # Weekly curation dry-run
#   ./scripts/dry_run.sh --cadence weekly
#
#   # Explicit scratch directory (preserved after the run for inspection)
#   ./scripts/dry_run.sh --cadence daily --scratch-dir /tmp/ai-news-debug
#
#   # Skip sourcing — curate from existing store candidates
#   ./scripts/dry_run.sh --cadence daily --skip-sourcing
#
#   # Full annual run with custom agent config
#   ./scripts/dry_run.sh \
#       --cadence annual \
#       --agent configs/default-agent.yaml \
#       --scratch-dir /tmp/ai-news-annual-dry-run
#
# Environment variables (see .env.example for full list)
# ──────────────────────────────────────────────────────
#   OPENAI_API_KEY         Required for real LLM calls; mock responses used in CI.
#   TWITTER_BEARER_TOKEN   Optional; Twitter features gracefully disabled when absent.
#   WEB_SEARCH_API_KEY     Optional; web search falls back to LLM-native when absent.
#
# Exit codes
# ──────────
#   0  Pipeline completed successfully
#   1  Pipeline failed — check stderr / structured logs
#   2  Configuration error
#
# Traces: SRC-076 (local dev Phase 1), SRC-102 (CI smoke test dry-run mode),
#         SRC-147 (manual trigger), SRC-150 (§8.2 monitoring summary printed)

set -euo pipefail

# ── Locate project root ────────────────────────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
cd "${PROJECT_ROOT}"

# ── Default parameters ─────────────────────────────────────────────────────
CADENCE="${CADENCE:-daily}"
AGENT_CONFIG="${AGENT_CONFIG:-configs/default-agent.yaml}"
PROMPTS_DIR="${PROMPTS_DIR:-prompts}"
SCRATCH_DIR="${SCRATCH_DIR:-}"
SKIP_SOURCING="${SKIP_SOURCING:-false}"
EXTRA_ARGS=""

# ── Parse CLI flags ────────────────────────────────────────────────────────
while [[ $# -gt 0 ]]; do
    case "$1" in
        --cadence)      CADENCE="$2"; shift 2 ;;
        --agent)        AGENT_CONFIG="$2"; shift 2 ;;
        --prompts-dir)  PROMPTS_DIR="$2"; shift 2 ;;
        --scratch-dir)  SCRATCH_DIR="$2"; shift 2 ;;
        --skip-sourcing) SKIP_SOURCING="true"; shift ;;
        --window-start) EXTRA_ARGS="${EXTRA_ARGS} --window-start $2"; shift 2 ;;
        --window-end)   EXTRA_ARGS="${EXTRA_ARGS} --window-end $2"; shift 2 ;;
        --twitter-available) EXTRA_ARGS="${EXTRA_ARGS} --twitter-available $2"; shift 2 ;;
        -h|--help)
            echo "Usage: $0 [--cadence CADENCE] [--agent PATH] [--scratch-dir DIR] [--skip-sourcing]"
            echo "       $0 [--window-start YYYY-MM-DD --window-end YYYY-MM-DD]"
            exit 0
            ;;
        *)
            echo "Unknown flag: $1" >&2
            exit 2
            ;;
    esac
done

# ── Load .env if present ───────────────────────────────────────────────────
if [[ -f "${PROJECT_ROOT}/.env" ]]; then
    echo "[dry_run.sh] Loading .env ..." >&2
    set -a
    # shellcheck disable=SC1091
    source "${PROJECT_ROOT}/.env"
    set +a
fi

# ── Build command ──────────────────────────────────────────────────────────
CMD=(
    python -m ai_news_agent.pipeline
    --cadence "${CADENCE}"
    --agent   "${AGENT_CONFIG}"
    --prompts-dir "${PROMPTS_DIR}"
    --dry-run
)

if [[ -n "${SCRATCH_DIR}" ]]; then
    CMD+=(--scratch-dir "${SCRATCH_DIR}")
fi

if [[ "${SKIP_SOURCING}" == "true" ]]; then
    CMD+=(--skip-sourcing)
fi

# Append any extra args collected above
if [[ -n "${EXTRA_ARGS}" ]]; then
    # Split EXTRA_ARGS safely
    # shellcheck disable=SC2086
    CMD+=($EXTRA_ARGS)
fi

# ── Print configuration ────────────────────────────────────────────────────
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━" >&2
echo " AI News Pipeline — DRY RUN" >&2
echo " Cadence:       ${CADENCE}" >&2
echo " Agent config:  ${AGENT_CONFIG}" >&2
echo " Prompts dir:   ${PROMPTS_DIR}" >&2
echo " Scratch dir:   ${SCRATCH_DIR:-<auto temp dir>}" >&2
echo " Skip sourcing: ${SKIP_SOURCING}" >&2
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━" >&2

# ── Execute ────────────────────────────────────────────────────────────────
exec "${CMD[@]}"
