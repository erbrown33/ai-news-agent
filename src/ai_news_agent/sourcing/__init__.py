"""
sourcing — Sourcing Agent.
Traces: SRC-006–SRC-013 (sourcing agent), SRC-033–SRC-053 (tiers, dedup, storage)
"""

from ai_news_agent.sourcing.agent import SourcingAgent, SourcingRunResult

__all__ = ["SourcingAgent", "SourcingRunResult"]
