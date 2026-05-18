"""
llm/base.py — AbstractLLMClient interface and SearchResult dataclass.
Traces: SRC-056 (provider-agnostic), SRC-059 (plain language prompts),
        SRC-060 (abstract tool use), SRC-061 (output parsing contract)
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, TypeVar

T = TypeVar("T")


@dataclass
class SearchResult:
    """
    Normalised search result — same shape regardless of provider or search tool.
    Traces: SRC-060 (abstract tool use — uniform interface across all search providers)
    """

    url: str
    title: str
    snippet: str
    source: str  # domain name (e.g. "reuters.com")


class AbstractLLMClient(ABC):
    """
    Provider-agnostic LLM client interface.

    All pipeline code (sourcing, curation, rendering, scheduler) depends **only**
    on this interface — never on concrete subclasses.  Provider-specific
    authentication, calling conventions, and optional structured-output features
    are fully encapsulated in the concrete implementation.

    Design invariants (SRC-056):
    - Prompts are plain natural language — no provider-specific formatting (SRC-059).
    - Tool use is described abstractly; native tools used if available, otherwise
      the injected AbstractSearchTool fallback is used (SRC-060).
    - Output parsing is based on Markdown + embedded JSON block — NOT on
      provider schema-enforcement features (SRC-061).

    Traces: SRC-055–SRC-061
    """

    @abstractmethod
    def complete(
        self,
        messages: list[dict[str, str]],
        model: str,
        temperature: float = 0.2,
        **kwargs: Any,
    ) -> str:
        """
        Send a chat-completion request.
        Returns the full assistant text content as a plain string.

        Provider structured-output is NEVER relied on by the pipeline — it may
        be used as an optional enhancement inside the concrete implementation only.
        The caller uses ``parse_structured()`` on the returned string.

        Traces: SRC-059 (plain prompts), SRC-061 (caller parses output)

        Args:
            messages:    OpenAI-style message list, e.g.
                         ``[{"role": "user", "content": "..."}]``.
            model:       Model identifier string, e.g. ``"gpt-4o"``.
            temperature: Sampling temperature (default 0.2 for deterministic curation).
            **kwargs:    Provider-specific extras.  ``thinking=True`` enables extended
                         thinking for o3 annual runs (SRC-032).
        """

    @abstractmethod
    def search(
        self,
        query: str,
        n_results: int = 10,
        budget_hint: str = "normal",
    ) -> list[SearchResult]:
        """
        Execute a web search.

        Uses the provider's native search tool if available; otherwise delegates
        to the injected :class:`AbstractSearchTool` fallback.

        Args:
            query:       Plain text search query.
            n_results:   Desired number of results.
            budget_hint: ``"normal"`` for daily/weekly; ``"deep"`` for
                         monthly/annual (more results, more searches — SRC-121).

        Traces: SRC-060 (abstract tool use), SRC-121 (search budget per cadence)
        """

    @abstractmethod
    def parse_structured(self, raw: str, schema_cls: type[T]) -> T:
        """
        Parse the LLM's plain-text response into a typed Pydantic schema.

        Algorithm (SRC-061):
        1. Find the first ```json ... ``` fenced block via regex.
        2. ``json.loads()`` the extracted block.
        3. ``schema_cls.model_validate(parsed_dict)``.
        4. On failure: lenient fallback — strip leading/trailing prose and retry.

        NEVER depends on provider schema-enforcement features. (SRC-061)

        Traces: SRC-061 (deterministic output parsing from plain text)
        """
