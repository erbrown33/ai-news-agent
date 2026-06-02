"""
llm — Provider-agnostic LLM abstraction layer.

Public API (pipeline code depends ONLY on these symbols):
  AbstractLLMClient  — base interface all providers implement
  AbstractSearchTool — base interface all search tools implement
  SearchResult       — normalised search result dataclass
  get_llm_client()   — factory: returns correct client for provider config
  get_search_tool()  — factory: returns correct search tool for provider config
  LLMError           — normalised error raised after retries exhausted

Provider clients (accessed via factory; never imported directly by pipeline):
  OpenAILLMClient    — default provider (SRC-057)
  AnthropicLLMClient — Anthropic Claude (SRC-055–SRC-056)
  GoogleLLMClient    — Google Gemini / Vertex AI (SRC-055–SRC-056)

Search tools (accessed via factory; never imported directly by pipeline):
  NativeOpenAISearchTool — OpenAI Responses API web_search (fallback for provider=openai)
  BraveSearchTool        — Brave Search API (WEB_SEARCH_PROVIDER=brave, SRC-060)
  TavilySearchTool       — Tavily Search API (WEB_SEARCH_PROVIDER=tavily, SRC-060; default)

Traces: SRC-055–SRC-061 (provider-agnostic design), SRC-057 (OpenAI default),
        SRC-144 (retry/backoff via LLMError + with_retry)
"""

from ai_news_agent.llm.base import AbstractLLMClient, SearchResult
from ai_news_agent.llm.factory import get_llm_client, get_search_tool
from ai_news_agent.llm.retry import LLMError, with_retry
from ai_news_agent.llm.search_tools import (
    AbstractSearchTool,
    BraveSearchTool,
    NativeOpenAISearchTool,
    TavilySearchTool,
)

__all__ = [
    # Core abstractions (SRC-056, SRC-060)
    "AbstractLLMClient",
    "AbstractSearchTool",
    "SearchResult",
    # Factories (SRC-055–SRC-057)
    "get_llm_client",
    "get_search_tool",
    # Error handling (SRC-144)
    "LLMError",
    "with_retry",
    # Search tool implementations (SRC-060)
    "NativeOpenAISearchTool",
    "BraveSearchTool",
    "TavilySearchTool",
]
