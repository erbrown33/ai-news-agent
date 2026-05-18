"""
llm/google_client.py — Google (Gemini / Vertex AI) concrete LLM client.

Uses the Google AI Python SDK (``google-generativeai``) or the Google Agent
Development Kit (``google-adk``) for:
  • Gemini 1.5 / 2.0 / 2.5 models
  • Grounding with Google Search (native tool, preferred when available)
  • Automatic retry — exponential backoff via ``with_retry`` (SRC-144)
  • parse_structured — identical Markdown + ```json``` block extraction (SRC-061)

Design constraints (SRC-056):
  No Google-specific types leak to the pipeline above this layer.
  All errors are normalised to LLMError.
  Output parsing is based on plain-text JSON-block extraction only. (SRC-061)

Install: ``pip install 'ai-news-agent[google]'``

Traces: SRC-027 (LLM scoring), SRC-055–SRC-056 (provider-agnostic extensibility),
        SRC-057 (OpenAI default — this is an alternative provider),
        SRC-059 (plain prompts — no provider-specific formatting tricks),
        SRC-060 (native Google Search grounding or injected AbstractSearchTool fallback),
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

# Models that support Google Search grounding natively
_GROUNDING_MODELS = frozenset(
    {
        "gemini-1.5-pro",
        "gemini-1.5-flash",
        "gemini-2.0-flash",
        "gemini-2.0-flash-lite",
        "gemini-2.5-pro",
        "gemini-2.5-flash",
        "gemini-2.5-flash-preview-05-20",
        "gemini-2.5-pro-preview-05-06",
    }
)

# Default generation config
_DEFAULT_MAX_OUTPUT_TOKENS: int = 8_192


class GoogleLLMClient(AbstractLLMClient):
    """
    Concrete LLM client wrapping the Google Generative AI Python SDK.

    Search routing (SRC-060):
      • If the model supports grounding AND no external search_tool is injected,
        uses the Google Search grounding tool (native, preferred).
      • Otherwise, delegates to the injected AbstractSearchTool (Brave / Tavily).

    Extended thinking is not yet available in the GA Gemini API; the ``thinking``
    kwarg is accepted but silently treated as a prompt enhancement hint.

    parse_structured uses the shared ``_parse_structured_impl`` function —
    provider-independent JSON-block extraction. (SRC-061)

    Traces: SRC-027, SRC-055–SRC-056, SRC-059–SRC-061, SRC-144, SRC-150
    """

    def __init__(
        self,
        api_key: str,
        search_tool: AbstractSearchTool | None = None,
        use_grounding: bool = True,
    ) -> None:
        """
        Args:
            api_key:        Google AI API key or Vertex AI credentials path.
            search_tool:    Fallback search tool (Brave / Tavily). Used when the
                            model does not support grounding or ``use_grounding=False``.
            use_grounding:  If True and the model supports it, activate Google Search
                            grounding (native preferred — SRC-060).
        """
        try:
            import google.generativeai as genai  # type: ignore[import-untyped]

            genai.configure(api_key=api_key)
            self._genai = genai
        except ImportError as exc:
            raise ImportError(
                "google-generativeai is required for GoogleLLMClient. "
                "Install with: pip install 'ai-news-agent[google]'"
            ) from exc

        self._api_key = api_key
        self._search_tool = search_tool
        self._use_grounding = use_grounding
        self._last_token_usage: int = 0  # SRC-150

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
        Send a generation request via the Google Generative AI SDK.

        Messages are converted to Gemini's ``contents`` format. System instructions
        are passed via ``system_instruction`` when a single system message is present.

        The ``thinking=True`` kwarg is accepted for API compatibility but is
        currently translated into a prompt-level hint for Gemini (no dedicated
        reasoning mode in the GA API). (SRC-032 — annual deep synthesis)

        Provider structured-output is NOT used. (SRC-061)

        Traces: SRC-059 (plain prompts), SRC-061 (caller parses output),
                SRC-150 (token usage)
        """
        thinking: bool = kwargs.pop("thinking", False)

        log.debug(
            "google_complete",
            model=model,
            n_messages=len(messages),
            thinking=thinking,
        )

        # Use the stored genai reference (set in __init__, mockable in tests)
        genai = self._genai

        # Separate system instruction from conversation
        system_instruction: str | None = None
        contents: list[dict[str, Any]] = []

        for msg in messages:
            role = msg.get("role", "user")
            content = msg.get("content", "")
            if role == "system":
                # Gemini uses system_instruction at model-init level; we accumulate
                # and pass the last/only system message.
                system_instruction = content
            elif role == "assistant":
                contents.append({"role": "model", "parts": [{"text": content}]})
            else:
                parts_text = content
                if thinking:
                    # Prepend a thinking hint for deep synthesis runs
                    parts_text = (
                        "[You are performing deep research synthesis. Think step by step, "
                        "cross-reference sources, and provide well-reasoned analysis.]\n\n"
                        + content
                    )
                contents.append({"role": "user", "parts": [{"text": parts_text}]})

        # Ensure at least one user message
        if not contents:
            contents = [{"role": "user", "content": "Please proceed with the instructions above."}]

        # Build generation config
        gen_config = genai.GenerationConfig(
            temperature=temperature,
            max_output_tokens=_DEFAULT_MAX_OUTPUT_TOKENS,
        )

        # Grounding tool (SRC-060 — native preferred when available)
        tools: list[Any] = []
        model_lower = model.lower()
        supports_grounding = any(m in model_lower for m in _GROUNDING_MODELS)
        if self._use_grounding and supports_grounding and self._search_tool is None:
            try:
                tools = [genai.Tool(google_search_retrieval=genai.GoogleSearchRetrieval())]
                log.debug("google_grounding_enabled", model=model)
            except Exception:  # noqa: BLE001
                tools = []  # grounding may not be available on all API tiers

        try:
            model_obj = genai.GenerativeModel(
                model_name=model,
                system_instruction=system_instruction,
                tools=tools if tools else None,
                generation_config=gen_config,
            )
            response = model_obj.generate_content(contents)
        except Exception as exc:  # noqa: BLE001
            raise LLMError(f"Google Generative AI error: {exc}", cause=exc) from exc

        # Extract text
        text = ""
        try:
            text = response.text
        except Exception:  # noqa: BLE001
            # response.text may raise if the model returned non-text parts
            for candidate in getattr(response, "candidates", []):
                for part in getattr(candidate.content, "parts", []):
                    part_text = getattr(part, "text", None)
                    if part_text:
                        text += part_text

        # Token usage (SRC-150)
        usage = getattr(response, "usage_metadata", None)
        if usage:
            input_t = getattr(usage, "prompt_token_count", 0) or 0
            output_t = getattr(usage, "candidates_token_count", 0) or 0
            self._last_token_usage = input_t + output_t
            log.debug(
                "google_token_usage",
                model=model,
                input_tokens=input_t,
                output_tokens=output_t,
                total_tokens=self._last_token_usage,
            )

        return text

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
        Execute a web search.

        Uses the injected ``AbstractSearchTool`` (Brave / Tavily).
        Google grounding is activated at model generation time (in ``complete()``)
        rather than as a separate search step, so this method is used when an
        explicit search is required by the sourcing agent outside of generation.

        Traces: SRC-060 (abstract tool use), SRC-121 (search budget)
        """
        if self._search_tool is None:
            raise LLMError(
                "GoogleLLMClient.search() requires an injected search_tool. "
                "Configure WEB_SEARCH_API_KEY + WEB_SEARCH_PROVIDER in env vars."
            )

        effective_n = n_results * 3 if budget_hint == "deep" else n_results
        log.debug(
            "google_search",
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
        Parse the Google Generative AI response into a typed Pydantic schema.

        Uses the shared ``_parse_structured_impl`` — provider-independent. (SRC-061)

        Traces: SRC-061 (never depends on provider schema-enforcement)
        """
        return _parse_structured_impl(raw, schema_cls)

    # ------------------------------------------------------------------
    # Monitoring helpers (SRC-150)
    # ------------------------------------------------------------------

    @property
    def last_token_usage(self) -> int:
        """Total tokens consumed by the most recent ``complete()`` call. (SRC-150)"""
        return self._last_token_usage
