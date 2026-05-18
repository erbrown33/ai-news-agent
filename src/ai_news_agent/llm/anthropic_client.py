"""
llm/anthropic_client.py — Anthropic concrete LLM client.

Uses the Anthropic Python SDK (``anthropic>=0.27``) for:
  • Messages API — all Claude models (claude-3-7-sonnet, claude-3-5-haiku, etc.)
  • Extended thinking — claude-3-7-sonnet with ``thinking`` budget (SRC-032)
  • Search fallback  — delegates to injected AbstractSearchTool (Brave or Tavily)
  • Automatic retry  — exponential backoff via ``with_retry`` decorator (SRC-144)
  • parse_structured — identical Markdown + ```json``` block extraction (SRC-061)

Design constraints (SRC-056):
  No Anthropic-specific types leak to the pipeline above this layer.
  All errors are normalised to LLMError.
  Tool use is via the injected AbstractSearchTool — no Anthropic tool_use feature.

Install: ``pip install 'ai-news-agent[anthropic]'``

Traces: SRC-027 (LLM scoring), SRC-032/SRC-054 (extended thinking / research model),
        SRC-055–SRC-056 (provider-agnostic extensibility),
        SRC-059 (plain prompts — no Anthropic-specific formatting),
        SRC-060 (abstract tool use — Brave/Tavily fallback for search),
        SRC-061 (output parsing from plain text),
        SRC-144 (retry/backoff), SRC-150 (token usage logging)
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, TypeVar

import structlog

from ai_news_agent.llm.base import AbstractLLMClient, SearchResult
from ai_news_agent.llm.openai_client import _parse_structured_impl
from ai_news_agent.llm.retry import LLMError, with_retry

if TYPE_CHECKING:
    from ai_news_agent.llm.search_tools import AbstractSearchTool

log = structlog.get_logger(__name__)
T = TypeVar("T")

# Models that support extended thinking (streaming + thinking budget)
_THINKING_MODELS = frozenset(
    {
        "claude-3-7-sonnet-20250219",
        "claude-3-7-sonnet-latest",
    }
)

# Maximum tokens for extended thinking budget (used when thinking=True)
_THINKING_BUDGET_TOKENS: int = 10_000

# Default max output tokens — generous for curation tasks
_DEFAULT_MAX_TOKENS: int = 8_192


class AnthropicLLMClient(AbstractLLMClient):
    """
    Concrete LLM client wrapping the Anthropic Python SDK.

    Search is always delegated to the injected ``AbstractSearchTool`` because
    Anthropic does not provide a native web search tool in the same way OpenAI does.
    Typically BraveSearchTool or TavilySearchTool is injected.

    Extended thinking:
      ``thinking=True`` → enables Anthropic extended thinking with a token budget
      of ``_THINKING_BUDGET_TOKENS``. Only works for claude-3-7-sonnet models.
      Annual curation runs benefit from this for deep cross-year synthesis (SRC-032).

    parse_structured uses the identical ``_parse_structured_impl`` function shared
    with OpenAILLMClient — provider-independent JSON-block extraction. (SRC-061)

    Traces: SRC-027, SRC-032, SRC-054, SRC-055–SRC-056, SRC-059–SRC-061,
            SRC-144, SRC-150
    """

    def __init__(
        self,
        api_key: str,
        search_tool: AbstractSearchTool | None = None,
    ) -> None:
        """
        Args:
            api_key:     Anthropic API key (from ``ANTHROPIC_API_KEY`` env var).
            search_tool: Injected search tool (BraveSearchTool or TavilySearchTool).
                         Required for any run that calls ``search()``.
        """
        try:
            import anthropic  # type: ignore[import-untyped]

            self._client = anthropic.Anthropic(api_key=api_key)
            self._anthropic = anthropic  # keep module reference for error type checks
        except ImportError as exc:
            raise ImportError(
                "anthropic is required for AnthropicLLMClient. "
                "Install with: pip install 'ai-news-agent[anthropic]'"
            ) from exc

        self._search_tool = search_tool
        self._last_token_usage: int = 0  # updated after each complete() call (SRC-150)

    # ------------------------------------------------------------------
    # AbstractLLMClient — complete()
    # ------------------------------------------------------------------

    @with_retry(max_retries=3, backoff_base=30.0)
    def complete(
        self,
        messages: list[dict[str, str]],
        model: str,
        temperature: float = 0.2,
        **kwargs: Any,
    ) -> str:
        """
        Send a completion request via the Anthropic Messages API.

        The ``thinking=True`` kwarg enables extended thinking for supported models
        (claude-3-7-sonnet-*). For unsupported models it is silently ignored.

        System messages are extracted and passed via the ``system`` parameter
        (the Anthropic Messages API requires this). (SRC-059 — plain prompts)

        Provider structured-output / tool_use are NOT used. The caller uses
        ``parse_structured()`` on the returned string. (SRC-061)

        Traces: SRC-059, SRC-061, SRC-032/SRC-054, SRC-150
        """
        thinking: bool = kwargs.pop("thinking", False)

        log.debug(
            "anthropic_complete",
            model=model,
            n_messages=len(messages),
            thinking=thinking,
        )

        # Separate system messages from conversation messages (Anthropic API requirement)
        system_parts: list[str] = []
        conv_messages: list[dict[str, Any]] = []
        for msg in messages:
            role = msg.get("role", "user")
            content = msg.get("content", "")
            if role == "system":
                system_parts.append(content)
            else:
                conv_messages.append({"role": role, "content": content})

        # If no user messages exist (system-only), add a minimal user turn
        if not conv_messages:
            conv_messages = [{"role": "user", "content": "Please proceed with the instructions above."}]

        system_text = "\n\n".join(system_parts) if system_parts else None

        # Build request kwargs
        req_kwargs: dict[str, Any] = {
            "model": model,
            "max_tokens": _DEFAULT_MAX_TOKENS,
            "messages": conv_messages,
        }
        if system_text:
            req_kwargs["system"] = system_text

        # Extended thinking (SRC-032, SRC-054)
        use_thinking = thinking and model in _THINKING_MODELS
        if use_thinking:
            req_kwargs["thinking"] = {
                "type": "enabled",
                "budget_tokens": _THINKING_BUDGET_TOKENS,
            }
            # Extended thinking requires temperature=1 on supported models
            req_kwargs["temperature"] = 1
            log.debug("anthropic_extended_thinking_enabled", model=model)
        else:
            req_kwargs["temperature"] = temperature

        try:
            resp = self._client.messages.create(**req_kwargs)
        except Exception as exc:  # noqa: BLE001
            # Normalise to LLMError — no Anthropic types leak up (SRC-056)
            raise LLMError(f"Anthropic Messages API error: {exc}", cause=exc) from exc

        # Extract text from response content blocks
        text_parts: list[str] = []
        for block in resp.content:
            block_type = getattr(block, "type", "")
            if block_type == "text":
                text_parts.append(getattr(block, "text", ""))
            # Thinking blocks are internal — we skip them; only text blocks go to caller

        # Token usage (SRC-150)
        if resp.usage:
            input_t = getattr(resp.usage, "input_tokens", 0) or 0
            output_t = getattr(resp.usage, "output_tokens", 0) or 0
            self._last_token_usage = input_t + output_t
            log.debug(
                "anthropic_token_usage",
                model=model,
                input_tokens=input_t,
                output_tokens=output_t,
                total_tokens=self._last_token_usage,
            )

        return "\n".join(text_parts)

    # ------------------------------------------------------------------
    # AbstractLLMClient — search()
    # ------------------------------------------------------------------

    def search(
        self,
        query: str,
        n_results: int = 10,
        budget_hint: str = "normal",
    ) -> list[SearchResult]:
        """
        Delegate to the injected ``AbstractSearchTool`` (Brave or Tavily).

        Anthropic does not provide a native hosted web-search tool so we always
        use the fallback path. (SRC-060)

        ``budget_hint="deep"`` → 3× results for monthly/annual cadences. (SRC-121)

        Traces: SRC-060 (abstract tool use), SRC-121 (search budget)
        """
        if self._search_tool is None:
            raise LLMError(
                "AnthropicLLMClient.search() requires an injected search_tool. "
                "Configure WEB_SEARCH_API_KEY + WEB_SEARCH_PROVIDER in env vars."
            )

        effective_n = n_results * 3 if budget_hint == "deep" else n_results
        log.debug(
            "anthropic_search",
            query=query[:80],
            n=effective_n,
            budget=budget_hint,
        )
        return self._search_tool.search(query, effective_n)

    # ------------------------------------------------------------------
    # AbstractLLMClient — parse_structured()
    # ------------------------------------------------------------------

    def parse_structured(self, raw: str, schema_cls: type[T]) -> T:
        """
        Parse the Anthropic response into a typed Pydantic schema.

        Uses the identical ``_parse_structured_impl`` function as
        ``OpenAILLMClient`` — the output parsing contract is fully
        provider-independent. (SRC-061)

        Traces: SRC-061 (never depends on provider schema-enforcement)
        """
        return _parse_structured_impl(raw, schema_cls)

    # ------------------------------------------------------------------
    # Monitoring helpers (SRC-150)
    # ------------------------------------------------------------------

    @property
    def last_token_usage(self) -> int:
        """
        Total tokens consumed by the most recent ``complete()`` call. (SRC-150)
        """
        return self._last_token_usage
