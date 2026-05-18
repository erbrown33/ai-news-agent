# tests/ci/ — CI smoke-assertion tests.
#
# These tests run the pipeline in dry-run mode with mocked LLM/Twitter and
# assert the exact same contract that the container smoke step in ci.yml
# checks after ``ai-news-run --dry-run``.  Running them in Python gives fast
# feedback before the Docker build step completes.
#
# Traces: SRC-097 (pipeline stages), SRC-098 (pytest), SRC-102 (smoke test)
