"""
curation — Curation Agent: score, rank, and summarize candidates via LLM.

Public API:
- ``CurationAgent``     — main orchestrator (SRC-014–SRC-032)
- ``CurationRunResult`` — complete run output, passed to Rendering Agent
- ``PromptBuilder``     — builds fully-parameterised prompts (SRC-115–SRC-124)
- ``Scorer``            — LLM-powered scoring + URL enforcement (SRC-027, SRC-049)
- ``ScorerResult``      — items + themes + outlook + predictions + token_usage

Traces: SRC-014–SRC-032 (curation agent), SRC-112–SRC-131 (prompt design, versioning)
"""

from ai_news_agent.curation.agent import CurationAgent, CurationRunResult
from ai_news_agent.curation.prompt_builder import PromptBuilder
from ai_news_agent.curation.scorer import Scorer, ScorerResult

__all__ = [
    "CurationAgent",
    "CurationRunResult",
    "PromptBuilder",
    "Scorer",
    "ScorerResult",
]
