"""
llm/factory.py — Factory functions for LLM client and search tool instantiation.

``get_llm_client()`` is the single entry point for the entire pipeline.
Pipeline code NEVER instantiates provider clients directly — always uses this
factory. Adding a new provider requires only:
  1. A new concrete ``*_client.py`` implementing ``AbstractLLMClient``.
  2. A new ``case`` branch in ``get_llm_client()``.
  3. No other pipeline changes needed. (SRC-056)

Search tool priority (SRC-060):
  1. Native OpenAI search  — when provider=openai and no WEB_SEARCH_PROVIDER override
  2. Tavily               — when WEB_SEARCH_API_KEY set and WEB_SEARCH_PROVIDER=tavily
  3. Brave                — when WEB_SEARCH_API_KEY set and WEB_SEARCH_PROVIDER=brave
  4. Google native grounding — when provider=google (wired inside GoogleLLMClient)
  5. ConfigError          — if no search tool is resolvable

Smoke-test / CI mock mode (SRC-102):
  When ``SMOKE_TEST_MOCK_LLM=1`` is set in the environment the factory returns a
  lightweight ``_SmokeMockLLMClient`` that never makes real API calls.  This allows
  the container smoke step in ci.yml to run ``ai-news-run --dry-run`` without
  real API keys or spend.  The mock always returns a deterministic valid digest
  JSON so all §8.2 monitoring-field assertions pass.

Traces: SRC-055 (provider-agnostic), SRC-056 (swap without pipeline changes),
        SRC-057 (OpenAI as default), SRC-060 (search tool selection),
        SRC-102 (smoke-test mock), SRC-109 (WEB_SEARCH_API_KEY)
"""

from __future__ import annotations

import json
import os
from typing import TYPE_CHECKING, Any

import openai

from ai_news_agent.config.loader import ConfigError
from ai_news_agent.llm.search_tools import (
    AbstractSearchTool,
    BraveSearchTool,
    NativeOpenAISearchTool,
    TavilySearchTool,
)

if TYPE_CHECKING:
    from ai_news_agent.config.models import LLMConfig, RuntimeSecrets
    from ai_news_agent.llm.base import AbstractLLMClient


# ---------------------------------------------------------------------------
# Smoke-test mock client (SMOKE_TEST_MOCK_LLM=1 — SRC-102)
# ---------------------------------------------------------------------------


def _make_smoke_mock_response() -> str:
    """Return a deterministic JSON digest response for CI smoke tests (SRC-102)."""
    payload = {
        "items": [
            {
                "headline": "Smoke Test: AI Pipeline Validated",
                "source_name": "CI Runner",
                "url": "https://example.com/smoke-test-article",
                "pub_date": "2026-01-01",
                "why_it_matters": (
                    "This synthetic item confirms the end-to-end pipeline produces "
                    "valid output with all required fields populated. "
                    "Used exclusively in CI dry-run smoke tests. (SRC-102)"
                ),
                "impact_tags": ["business_impact"],
                "tier": "1b",
                "cross_refs": [],
                "twitter_handle": None,
                "tweet_url": None,
            }
        ],
        "themes": ["Pipeline validation", "CI smoke testing"],
        "outlook": "Smoke test passed — pipeline is healthy.",
        "predictions": [],
    }
    return f"```json\n{json.dumps(payload)}\n```"


class _SmokeMockLLMClient:
    """
    Lightweight mock LLM client for CI container smoke tests (SRC-102).

    Activated when ``SMOKE_TEST_MOCK_LLM=1`` is set.  Returns deterministic
    responses that satisfy all §8.2 monitoring-field assertions without making
    any real API calls.

    Implements the same interface as ``AbstractLLMClient`` but without the
    abstract base class dependency (avoids circular imports in the factory).

    Traces: SRC-056 (provider-agnostic), SRC-102 (dry-run smoke test)
    """

    provider: str = "smoke_mock"
    model: str = "smoke-mock-model"

    def complete(
        self,
        messages: list[dict[str, str]],
        model: str,
        temperature: float = 0.2,
        **kwargs: Any,
    ) -> str:
        return _make_smoke_mock_response()

    def search(
        self,
        query: str,
        n_results: int = 10,
        budget_hint: str = "normal",
    ) -> list[Any]:
        from ai_news_agent.llm.base import SearchResult

        return [
            SearchResult(
                url="https://example.com/smoke-search-result",
                title="Smoke Test Search Result",
                snippet="Synthetic search result for CI smoke testing. (SRC-102)",
                source="example.com",
            )
        ]

    def parse_structured(self, raw: str, schema_cls: type) -> Any:
        """Parse the smoke mock JSON block."""
        import re

        json_block_re = re.compile(r"```json\s*(.*?)```", re.DOTALL | re.IGNORECASE)
        match = json_block_re.search(raw)
        json_str = match.group(1).strip() if match else raw.strip()
        try:
            data = json.loads(json_str)
        except json.JSONDecodeError:
            start = json_str.find("{")
            end = json_str.rfind("}") + 1
            data = json.loads(json_str[start:end])
        return schema_cls.model_validate(data)


# ---------------------------------------------------------------------------
# Search tool factory
# ---------------------------------------------------------------------------


def get_search_tool(
    llm_cfg: LLMConfig,
    secrets: RuntimeSecrets,
) -> AbstractSearchTool:
    """
    Resolve and return the appropriate :class:`AbstractSearchTool`.

    Priority (SRC-060):
    1. If ``provider == "openai"`` and no explicit ``WEB_SEARCH_PROVIDER`` override →
       ``NativeOpenAISearchTool`` (OpenAI's built-in web search via Responses API).
    2. If ``WEB_SEARCH_API_KEY`` is set:
       a. ``WEB_SEARCH_PROVIDER == "tavily"`` (or unset, with key present) → TavilySearchTool
       b. ``WEB_SEARCH_PROVIDER == "brave"`` → BraveSearchTool
    3. ``provider == "google"`` → returns ``None`` (Google grounding is wired inside
       GoogleLLMClient; no separate search tool needed for ``complete()``). However, a
       BraveSearchTool / TavilySearchTool is still returned for explicit ``search()`` calls.
    4. Raises :class:`ConfigError` if no search tool is available.

    Args:
        llm_cfg: LLM configuration block from the agent YAML.
        secrets: Runtime secrets (from env vars only — SRC-073).

    Raises:
        ConfigError: If no search tool can be resolved.

    Traces: SRC-060 (abstract tool use), SRC-109 (WEB_SEARCH_API_KEY)
    """
    provider = llm_cfg.provider
    web_key = secrets.web_search_api_key
    web_provider_override = (secrets.web_search_provider or "").lower().strip()

    # Explicit WEB_SEARCH_PROVIDER override always wins
    if web_provider_override == "tavily" and web_key:
        return TavilySearchTool(api_key=web_key)

    if web_provider_override == "brave" and web_key:
        return BraveSearchTool(api_key=web_key)

    if web_provider_override == "native" or (not web_provider_override and provider == "openai"):
        # Native OpenAI search — the client itself is created in get_llm_client();
        # we create a temporary client here solely to pass into NativeOpenAISearchTool.
        # The factory creates a fresh client so we don't share state between search
        # and completion calls.
        client = openai.OpenAI(api_key=secrets.openai_api_key)
        return NativeOpenAISearchTool(client=client)

    # No override: fall back to any available external key
    if web_key:
        # Prefer Tavily if a key is present but no explicit provider specified
        # (Tavily is optimised for LLM-oriented search workloads)
        return TavilySearchTool(api_key=web_key)

    # Google provider: grounding is wired inside GoogleLLMClient.complete().
    # For explicit .search() calls from the sourcing agent, a fallback tool is
    # required — raise ConfigError if none is configured.
    if provider == "google":
        raise ConfigError(
            "GoogleLLMClient requires WEB_SEARCH_API_KEY + WEB_SEARCH_PROVIDER "
            "(tavily or brave) for explicit search() calls. "
            "Google Search grounding is used automatically inside complete()."
        )

    raise ConfigError(
        "No search tool available. Options:\n"
        "  1. Use provider=openai (native search via OpenAI Responses API)\n"
        "  2. Set WEB_SEARCH_API_KEY + WEB_SEARCH_PROVIDER=tavily\n"
        "  3. Set WEB_SEARCH_API_KEY + WEB_SEARCH_PROVIDER=brave\n"
        "(SRC-060, SRC-109)"
    )


# ---------------------------------------------------------------------------
# LLM client factory
# ---------------------------------------------------------------------------


def get_llm_client(
    llm_cfg: LLMConfig,
    secrets: RuntimeSecrets,
) -> AbstractLLMClient:
    """
    Return the correct concrete :class:`AbstractLLMClient` for ``llm_cfg.provider``.

    Pipeline code NEVER instantiates provider clients directly — always calls
    this factory. Adding a new provider requires only a new ``case`` here and a
    new ``*_client.py`` module. (SRC-056)

    **Smoke-test mode** (SRC-102): when ``SMOKE_TEST_MOCK_LLM=1`` is set in the
    environment, a ``_SmokeMockLLMClient`` is returned instead of any real client.
    This allows the CI container smoke step to run without real API keys.

    Raises :class:`ConfigError` for:
      - Unknown provider strings.
      - Missing required API keys for the selected provider.

    Args:
        llm_cfg: LLM configuration block from the per-agent YAML.
        secrets: Runtime secrets (env vars only — SRC-073, SRC-111).

    Returns:
        Concrete ``AbstractLLMClient`` instance wired with the appropriate search tool.

    Traces: SRC-055 (provider-agnostic), SRC-056 (swap without pipeline changes),
            SRC-057 (OpenAI as default), SRC-060 (search tool wiring),
            SRC-102 (smoke-test mock bypass)
    """
    # ── CI smoke-test bypass (SRC-102) ────────────────────────────────────
    if os.environ.get("SMOKE_TEST_MOCK_LLM") == "1":
        return _SmokeMockLLMClient()  # type: ignore[return-value]

    match llm_cfg.provider:
        case "openai":
            if not secrets.openai_api_key:
                raise ConfigError(
                    "provider=openai requires OPENAI_API_KEY environment variable. (SRC-107)"
                )
            from ai_news_agent.llm.openai_client import OpenAILLMClient

            search_tool = get_search_tool(llm_cfg, secrets)
            return OpenAILLMClient(
                api_key=secrets.openai_api_key,
                search_tool=search_tool,
            )

        case "anthropic":
            api_key = secrets.anthropic_api_key or ""
            if not api_key:
                raise ConfigError(
                    "provider=anthropic requires ANTHROPIC_API_KEY environment variable. (SRC-055)"
                )
            from ai_news_agent.llm.anthropic_client import AnthropicLLMClient

            # Anthropic has no native search tool; always uses injected AbstractSearchTool
            try:
                search_tool = get_search_tool(llm_cfg, secrets)
            except ConfigError:
                # Anthropic without a search tool: sourcing search() will fail if called,
                # but curation complete() + parse_structured() still work.
                search_tool = None  # type: ignore[assignment]

            return AnthropicLLMClient(
                api_key=api_key,
                search_tool=search_tool,
            )

        case "google":
            api_key = secrets.google_api_key or ""
            if not api_key:
                raise ConfigError(
                    "provider=google requires GOOGLE_API_KEY environment variable. (SRC-055)"
                )
            from ai_news_agent.llm.google_client import GoogleLLMClient

            # Google grounding is wired inside GoogleLLMClient.complete();
            # we try to get an explicit search tool for the sourcing agent's
            # search() calls — but it's optional (grounding may cover use cases).
            try:
                search_tool = get_search_tool(llm_cfg, secrets)
            except ConfigError:
                search_tool = None  # type: ignore[assignment]

            return GoogleLLMClient(
                api_key=api_key,
                search_tool=search_tool,
                use_grounding=True,
            )

        case _:
            raise ConfigError(
                f"Unknown LLM provider: {llm_cfg.provider!r}. "
                f"Supported providers: 'openai', 'anthropic', 'google'. (SRC-055)"
            )
