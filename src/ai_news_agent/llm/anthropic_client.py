"""
llm/anthropic_client.py — Anthropic concrete LLM client.

Uses the Anthropic Python SDK (``anthropic>=0.27``) for:
  • Messages API — all Claude models, from Claude 3.x through Sonnet 5 / Opus 5 / Fable
  • Extended thinking — adaptive on Opus/Sonnet 4.6 and newer; legacy ``budget_tokens``
    on 3.7 / 4.0–4.5 / Haiku 4.5 (SRC-032)
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

# ---------------------------------------------------------------------------
# Model capability families (matched by model-id prefix)
#
# The Messages API request shape differs by model generation:
#   • Sampling params (temperature/top_p/top_k) are accepted up to Opus 4.6 /
#     Sonnet 4.6 / Haiku 4.5, and rejected (HTTP 400) by Opus 4.7+, Sonnet 5+,
#     Fable, and Mythos.
#   • Thinking: pre-4.6 models take {"type": "enabled", "budget_tokens": N};
#     Opus 4.6 / Sonnet 4.6 and every newer model take {"type": "adaptive"}
#     (budget_tokens is deprecated on 4.6 and rejected on 4.7+ / 5.x).
#   • Claude 3.5 and older have no thinking support at all.
# Unknown (future) models are treated as the newest generation: no sampling
# params, adaptive thinking.
# ---------------------------------------------------------------------------
_NO_THINKING_PREFIXES: tuple[str, ...] = (
    "claude-3-5",
    "claude-3-haiku",
    "claude-3-opus",
    "claude-3-sonnet",
)
_BUDGET_THINKING_PREFIXES: tuple[str, ...] = (
    "claude-3-7",
    "claude-haiku-4",
    "claude-sonnet-4-0",
    "claude-sonnet-4-2",  # claude-sonnet-4-20250514
    "claude-sonnet-4-5",
    "claude-opus-4-0",
    "claude-opus-4-1",
    "claude-opus-4-2",  # claude-opus-4-20250514
    "claude-opus-4-5",
)
_SAMPLING_PREFIXES: tuple[str, ...] = (
    "claude-3",
    "claude-haiku-4",
    "claude-sonnet-4",  # all Sonnet 4.x (incl. 4.6)
    "claude-opus-4-0",
    "claude-opus-4-1",
    "claude-opus-4-2",
    "claude-opus-4-5",
    "claude-opus-4-6",
)


def _accepts_sampling_params(model: str) -> bool:
    """True if the model accepts ``temperature`` (rejected from Opus 4.7 / Sonnet 5 on)."""
    return model.startswith(_SAMPLING_PREFIXES)


def _thinking_style(model: str) -> str:
    """Return ``"none"``, ``"budget"`` (legacy budget_tokens), or ``"adaptive"``."""
    if model.startswith(_NO_THINKING_PREFIXES):
        return "none"
    if model.startswith(_BUDGET_THINKING_PREFIXES):
        return "budget"
    return "adaptive"


# Maximum tokens for the legacy extended thinking budget (budget-style models only)
_THINKING_BUDGET_TOKENS: int = 10_000

# Default max output tokens — generous for curation tasks
_DEFAULT_MAX_TOKENS: int = 8_192

# max_tokens when budget thinking is enabled must exceed budget_tokens (API requirement)
_THINKING_MAX_TOKENS: int = _THINKING_BUDGET_TOKENS + 4_096

# Adaptive-thinking models share max_tokens between thinking and the answer (and
# may think even when not asked), so give them more room. Kept at 16k so a
# non-streaming request stays well inside the SDK's HTTP timeout.
_ADAPTIVE_MAX_TOKENS: int = 16_000


class AnthropicLLMClient(AbstractLLMClient):
    """
    Concrete LLM client wrapping the Anthropic Python SDK.

    Search is always delegated to the injected ``AbstractSearchTool`` because
    Anthropic does not provide a native web search tool in the same way OpenAI does.
    Typically BraveSearchTool or TavilySearchTool is injected.

    Extended thinking:
      ``thinking=True`` → adaptive thinking on Opus/Sonnet 4.6 and newer; a
      ``_THINKING_BUDGET_TOKENS`` budget on older thinking-capable models; ignored on 3.5-era
      models. Sampling params are omitted for models that reject them (Opus 4.7+, Sonnet 5+).
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

        The ``thinking=True`` kwarg enables extended thinking in the form the model
        supports (adaptive or budget); for models without thinking it is ignored.
        ``temperature`` is sent only to models that accept it.

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
            conv_messages = [
                {"role": "user", "content": "Please proceed with the instructions above."}
            ]

        system_text = "\n\n".join(system_parts) if system_parts else None

        # Build request kwargs
        req_kwargs: dict[str, Any] = {
            "model": model,
            "max_tokens": _DEFAULT_MAX_TOKENS,
            "messages": conv_messages,
        }
        if system_text:
            req_kwargs["system"] = system_text

        # Extended thinking (SRC-032, SRC-054) — request shape depends on model generation
        style = _thinking_style(model)
        if style == "adaptive":
            # Newer models think adaptively (several do so even when not asked), and
            # thinking tokens count toward max_tokens.
            req_kwargs["max_tokens"] = _ADAPTIVE_MAX_TOKENS
            if thinking:
                req_kwargs["thinking"] = {"type": "adaptive"}
                log.debug("anthropic_adaptive_thinking_enabled", model=model)
        elif style == "budget" and thinking:
            req_kwargs["thinking"] = {
                "type": "enabled",
                "budget_tokens": _THINKING_BUDGET_TOKENS,
            }
            # max_tokens must exceed budget_tokens (Anthropic API requirement)
            req_kwargs["max_tokens"] = _THINKING_MAX_TOKENS
            log.debug("anthropic_extended_thinking_enabled", model=model)

        # Sampling params: legacy budget thinking requires temperature=1; Opus 4.7+,
        # Sonnet 5+, and Fable reject temperature entirely.
        if _accepts_sampling_params(model):
            req_kwargs["temperature"] = 1 if "thinking" in req_kwargs else temperature

        try:
            resp = self._client.messages.create(**req_kwargs)
        except Exception as exc:  # noqa: BLE001
            # Normalise to LLMError — no Anthropic types leak up (SRC-056)
            raise LLMError(f"Anthropic Messages API error: {exc}", cause=exc) from exc

        stop_reason = getattr(resp, "stop_reason", None)
        if stop_reason == "refusal":
            raise LLMError(f"Anthropic model {model} declined the request (stop_reason=refusal)")
        if stop_reason == "max_tokens":
            log.warning("anthropic_max_tokens_reached", model=model, max_tokens=req_kwargs["max_tokens"])

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
