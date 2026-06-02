"""
llm/openai_client.py — OpenAI concrete LLM client (DEFAULT provider).

Uses the OpenAI Python SDK (``openai>=1.30``) for:
  • Chat Completions API  — all models (gpt-4o, gpt-4o-mini, etc.)
  • Responses API         — models with tool use (web_search_preview) and
                            extended reasoning (o3/o1 series with reasoning_effort)
  • Automatic retry       — exponential backoff via ``with_retry`` decorator (SRC-144)
  • parse_structured      — Markdown + ```json``` block extraction (SRC-061)

Design constraints (SRC-056):
  No provider-specific types leak to the pipeline above this layer.
  All errors are normalised to LLMError.
  parse_structured is based on plain-text JSON-block extraction only.

Traces: SRC-027 (LLM scoring), SRC-032/SRC-054 (extended reasoning for annual),
        SRC-057 (OpenAI as default provider), SRC-059 (plain prompts),
        SRC-060 (native/fallback search), SRC-061 (output parsing),
        SRC-144 (retry/backoff), SRC-150 (token usage logging)
"""

from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING, Any, TypeVar

import openai
import structlog

from ai_news_agent.llm.base import AbstractLLMClient, SearchResult
from ai_news_agent.llm.retry import LLMError, with_retry

if TYPE_CHECKING:
    from ai_news_agent.llm.search_tools import AbstractSearchTool

log = structlog.get_logger(__name__)
T = TypeVar("T")

# ---------------------------------------------------------------------------
# Regex helpers for output parsing (SRC-061)
# ---------------------------------------------------------------------------

# Match the FIRST ```json ... ``` fenced block (greedy is intentionally avoided)
_JSON_BLOCK_RE = re.compile(r"```json\s*(.*?)```", re.DOTALL | re.IGNORECASE)

# Models that use the Responses API (vs Chat Completions API)
# o-series models support reasoning_effort; GPT-4o-mini / gpt-4o use Chat Completions
_RESPONSES_API_MODELS = frozenset(
    {
        "o1",
        "o1-mini",
        "o1-preview",
        "o3",
        "o3-mini",
        "o4-mini",
    }
)


def _uses_responses_api(model: str) -> bool:
    """Return True if ``model`` should be called via the Responses API."""
    model_lower = model.lower()
    return any(model_lower == m or model_lower.startswith(m + "-") for m in _RESPONSES_API_MODELS)


# ---------------------------------------------------------------------------
# OpenAILLMClient
# ---------------------------------------------------------------------------


class OpenAILLMClient(AbstractLLMClient):
    """
    Concrete LLM client wrapping the OpenAI Python SDK.

    Routing logic:
    ┌─ model is o-series? ──────┐
    │  YES → Responses API      │  (supports reasoning_effort / tool use)
    │  NO  → Chat Completions   │  (all other models)
    └───────────────────────────┘

    Search routing (SRC-060):
      Delegates to injected BraveSearchTool or TavilySearchTool.
      search_tool=None is accepted; search() raises LLMError if called without one.

    parse_structured uses regex-based JSON-block extraction — never provider
    schema-enforcement (SRC-061).

    Traces: SRC-027, SRC-032, SRC-054, SRC-057, SRC-059, SRC-060, SRC-061,
            SRC-144, SRC-150
    """

    def __init__(self, api_key: str, search_tool: AbstractSearchTool | None = None) -> None:
        """
        Args:
            api_key:     OpenAI API key (from ``OPENAI_API_KEY`` env var — SRC-107).
            search_tool: Injected BraveSearchTool or TavilySearchTool. Optional —
                         if None, search() raises LLMError; complete() still works.
        """
        self._client = openai.OpenAI(api_key=api_key)
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
        Send a chat-completion request and return the assistant text as a plain string.

        Routing:
          • o-series (o1, o3, o4-mini…) → Responses API with ``reasoning_effort``
          • All other models             → Chat Completions API

        The ``thinking=True`` kwarg is translated to ``reasoning_effort="high"`` for
        o3/o1 runs (SRC-032, SRC-054).  It is silently ignored for non-o-series models.

        Provider structured-output is NEVER enabled here — the caller uses
        ``parse_structured()`` on the returned string. (SRC-061)

        Traces: SRC-059 (plain prompts), SRC-061 (caller parses output),
                SRC-032/SRC-054 (extended thinking), SRC-150 (token logging)
        """
        thinking: bool = kwargs.pop("thinking", False)

        log.debug(
            "openai_complete",
            model=model,
            n_messages=len(messages),
            thinking=thinking,
        )

        if _uses_responses_api(model):
            return self._complete_responses(messages, model, thinking=thinking)
        return self._complete_chat(messages, model, temperature=temperature)

    def _complete_chat(
        self,
        messages: list[dict[str, str]],
        model: str,
        temperature: float,
    ) -> str:
        """Chat Completions API path — gpt-4o, gpt-4o-mini, etc."""
        try:
            resp = self._client.chat.completions.create(
                model=model,
                messages=messages,  # type: ignore[arg-type]
                temperature=temperature,
            )
        except openai.OpenAIError as exc:  # type: ignore[attr-defined]
            raise LLMError(f"OpenAI Chat Completions error: {exc}", cause=exc) from exc

        content = resp.choices[0].message.content or ""
        if resp.usage:
            self._last_token_usage = resp.usage.total_tokens
            log.debug(
                "openai_token_usage",
                model=model,
                prompt_tokens=resp.usage.prompt_tokens,
                completion_tokens=resp.usage.completion_tokens,
                total_tokens=resp.usage.total_tokens,
            )
        return content

    def _complete_responses(
        self,
        messages: list[dict[str, str]],
        model: str,
        thinking: bool,
    ) -> str:
        """
        Responses API path — o1, o3, o4-mini, etc.

        The Responses API takes a single ``input`` string (or structured list) rather
        than separate system/user messages. We concatenate the messages, preserving
        role labels so the model can distinguish system instructions from user content.

        ``thinking=True`` → ``reasoning_effort="high"`` (SRC-032)
        ``thinking=False`` → ``reasoning_effort="medium"`` (balanced quality/cost)

        Traces: SRC-032 (annual high-reasoning model), SRC-054 (configurable per-cadence)
        """
        reasoning_effort = "high" if thinking else "medium"

        # Convert OpenAI-style message list to a single prompt string
        # The Responses API also accepts structured input; using plain text is
        # provider-agnostic and works for all supported models. (SRC-059)
        parts: list[str] = []
        for msg in messages:
            role = msg.get("role", "user")
            content = msg.get("content", "")
            if role == "system":
                parts.append(f"[SYSTEM INSTRUCTIONS]\n{content}")
            elif role == "assistant":
                parts.append(f"[ASSISTANT]\n{content}")
            else:
                parts.append(f"[USER]\n{content}")
        combined_input = "\n\n".join(parts)

        try:
            resp = self._client.responses.create(
                model=model,
                input=combined_input,
                reasoning={"effort": reasoning_effort},
            )
        except openai.OpenAIError as exc:  # type: ignore[attr-defined]
            raise LLMError(f"OpenAI Responses API error: {exc}", cause=exc) from exc

        # Extract text content from the response
        text = ""
        for item in resp.output or []:
            if hasattr(item, "content"):
                for block in item.content or []:
                    block_text = getattr(block, "text", None)
                    if block_text:
                        text += block_text
            # Fallback: item may be a plain text item
            elif hasattr(item, "text"):
                text += item.text or ""

        if resp.usage:
            # Responses API usage may report input_tokens + output_tokens
            total = getattr(resp.usage, "total_tokens", None)
            if total is None:
                input_t = getattr(resp.usage, "input_tokens", 0) or 0
                output_t = getattr(resp.usage, "output_tokens", 0) or 0
                total = input_t + output_t
            self._last_token_usage = total
            log.debug(
                "openai_responses_token_usage",
                model=model,
                total_tokens=total,
                reasoning_effort=reasoning_effort,
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
        Execute a web search via the injected ``AbstractSearchTool``.

        ``budget_hint="deep"`` → 3× results requested for monthly/annual cadences (SRC-121).
        Always invoked through the ``AbstractSearchTool`` interface. (SRC-060)

        Traces: SRC-060 (abstract tool use), SRC-121 (search budget per cadence)
        """
        if self._search_tool is None:
            from ai_news_agent.llm.retry import LLMError

            raise LLMError(
                "OpenAILLMClient.search() requires an injected search_tool. "
                "Configure WEB_SEARCH_API_KEY + WEB_SEARCH_PROVIDER in env vars."
            )
        effective_n = n_results * 3 if budget_hint == "deep" else n_results
        log.debug(
            "openai_search",
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
        Parse the LLM's plain-text response into a typed Pydantic schema.

        Algorithm (SRC-061):
        1. Find the first ```json ... ``` fenced block via ``_JSON_BLOCK_RE``.
        2. ``json.loads()`` the extracted block.
        3. ``schema_cls.model_validate(parsed_dict)``.
        4. Lenient fallback — if no fenced block, scan for the first ``{`` / last ``}``
           and attempt to parse that substring.

        NEVER depends on OpenAI structured-output / response-format features. (SRC-061)

        Traces: SRC-061 (deterministic output parsing from plain text)
        """
        return _parse_structured_impl(raw, schema_cls)

    # ------------------------------------------------------------------
    # Monitoring helpers (SRC-150)
    # ------------------------------------------------------------------

    @property
    def last_token_usage(self) -> int:
        """
        Total tokens consumed by the most recent ``complete()`` call.
        Exposed so ``CurationAgent`` can populate ``DigestMetadata.token_usage``. (SRC-150)
        """
        return self._last_token_usage


# ---------------------------------------------------------------------------
# Shared parse_structured implementation
# ---------------------------------------------------------------------------
# Factored out so AnthropicLLMClient and GoogleLLMClient can reuse the identical
# algorithm without inheritance coupling. (SRC-061)


def _parse_structured_impl(raw: str, schema_cls: type[T]) -> T:  # noqa: UP047
    """
    Extract and validate the JSON block embedded in an LLM response.

    This function is the single canonical implementation of the output-parsing
    contract (SRC-061) shared by all provider clients.

    Steps:
    1. Search for a ```json ... ``` fenced block (case-insensitive).
    2. ``json.loads()`` the content of the first matching block.
    3. Validate against ``schema_cls`` via Pydantic ``model_validate()``.
    4. Fallback A: if no fenced block, try parsing the whole ``raw`` string.
    5. Fallback B: scan for first ``{`` … last ``}`` and retry.

    Raises:
        ``json.JSONDecodeError`` / ``pydantic.ValidationError`` if all attempts fail.

    Traces: SRC-061 (Markdown + JSON block contract, not provider schema-enforcement)
    """
    match = _JSON_BLOCK_RE.search(raw)
    json_str = match.group(1).strip() if match else raw.strip()

    def _try_parse(s: str) -> T:
        data = json.loads(s)
        return schema_cls.model_validate(data)  # type: ignore[return-value]

    # Attempt 1: exact extraction
    try:
        return _try_parse(json_str)
    except (json.JSONDecodeError, Exception):  # noqa: BLE001
        pass

    # Attempt 2: strip prose before/after JSON object boundaries
    start = json_str.find("{")
    end = json_str.rfind("}") + 1
    if start >= 0 and end > start:
        try:
            return _try_parse(json_str[start:end])
        except (json.JSONDecodeError, Exception):  # noqa: BLE001
            pass

    # Attempt 3: maybe the response is a JSON array wrapping a single object
    arr_start = json_str.find("[")
    arr_end = json_str.rfind("]") + 1
    if arr_start >= 0 and arr_end > arr_start:
        try:
            candidate = json_str[arr_start:arr_end]
            items = json.loads(candidate)
            if isinstance(items, list) and len(items) == 1:
                return schema_cls.model_validate(items[0])  # type: ignore[return-value]
        except (json.JSONDecodeError, Exception):  # noqa: BLE001
            pass

    # All attempts failed — log and re-raise with helpful context
    log.warning(
        "parse_structured_failed",
        schema=schema_cls.__name__,
        raw_length=len(raw),
        raw_preview=raw[:200],
    )
    # Final attempt: raise a clear error
    data = json.loads(json_str)  # will raise JSONDecodeError with context
    return schema_cls.model_validate(data)  # type: ignore[return-value]
