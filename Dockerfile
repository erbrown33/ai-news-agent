# Dockerfile — AI News Curation Agent
# Multi-stage build: same image runs locally, in CI, and in production.
# Traces: SRC-085 (container-based), SRC-099 (docker build), SRC-074–SRC-086 (serverless)
#
# SECRETS NOTE (SRC-073, SRC-111):
#   All secrets (OPENAI_API_KEY, TWITTER_BEARER_TOKEN, WEB_SEARCH_API_KEY) are injected
#   at runtime via environment variables or cloud secrets manager.
#   NEVER bake secrets into this image.

# ---------------------------------------------------------------------------
# Stage 1: builder — install dependencies
# ---------------------------------------------------------------------------
FROM python:3.12-slim AS builder

WORKDIR /build

# Install build tools
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Copy dependency manifest first for layer caching.
# README.md and LICENSE are required by hatchling because pyproject.toml
# declares `readme` and `license-files`.
COPY pyproject.toml README.md LICENSE ./
COPY src/ ./src/

# Install the package and its core dependencies into /install
RUN pip install --upgrade pip \
    && pip install --prefix=/install --no-cache-dir ".[tavily]"

# ---------------------------------------------------------------------------
# Stage 2: runtime — minimal image
# ---------------------------------------------------------------------------
FROM python:3.12-slim AS runtime

WORKDIR /app

# Security: run as non-root
RUN useradd --create-home --shell /bin/bash appuser

# Copy installed packages from builder
COPY --from=builder /install /usr/local

# Copy application source and runtime assets
COPY src/          ./src/
COPY configs/      ./configs/
COPY prompts/      ./prompts/

# Create writable output directory (SRC-145 — date-stamped outputs)
RUN mkdir -p /app/outputs && chown appuser:appuser /app/outputs

USER appuser

# Expose portal port (SRC-133)
EXPOSE 8080

# Default CMD: run the web portal (override in cloud scheduler for agent runs)
# SRC-076–SRC-079: local dev runs portal; scheduler triggers sourcing/curation
CMD ["python", "-m", "uvicorn", "ai_news_agent.portal.app:app", \
     "--host", "0.0.0.0", "--port", "8080", "--workers", "1"]

# ---------------------------------------------------------------------------
# Labels
# ---------------------------------------------------------------------------
LABEL org.opencontainers.image.title="AI News Curation Agent"
LABEL org.opencontainers.image.description="Multi-agent AI News platform: sourcing, curation, rendering, portal"
LABEL org.opencontainers.image.source="https://github.com/erbrown33/wm-ai-news-agent-2"
