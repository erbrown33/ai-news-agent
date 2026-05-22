"""
rendering/impact_tags.py — single source of truth for impact-tag display metadata.

Each curation prompt vocabulary (default agent, developer agent, future custom
agents) emits its own set of ``impact_tag`` string values on every curated item.
The portal templates, the standalone HTML export, and the Markdown export all
need to turn those raw strings into a colored pill with an emoji and a label.

Before this module existed, every renderer hardcoded the default agent's three
tags, so any custom-prompt tag (e.g. ``tooling_impact``) rendered as plain text
without styling and was missing from the filter bar.

To add support for a new agent vocabulary, add its tags to ``IMPACT_TAGS``.
Unknown tags fall back to a neutral pill with a title-cased label.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class TagDisplay:
    """Display metadata for a single impact tag."""

    label: str
    emoji: str
    color: str
    bg: str
    border: str


IMPACT_TAGS: dict[str, TagDisplay] = {
    # Default agent vocabulary (business / society lens)
    "business_impact": TagDisplay("Business", "💼", "#1d4ed8", "#dbeafe", "#93c5fd"),
    "workforce_impact": TagDisplay("Workforce", "👥", "#15803d", "#dcfce7", "#86efac"),
    "policy_impact": TagDisplay("Policy", "⚖️", "#be185d", "#fce7f3", "#f9a8d4"),
    # Developer / builder vocabulary
    "tooling_impact": TagDisplay("Tooling", "🛠️", "#0e7490", "#cffafe", "#67e8f9"),
    "model_impact": TagDisplay("Model", "🧠", "#7c3aed", "#ede9fe", "#c4b5fd"),
    "infra_impact": TagDisplay("Infra", "⚙️", "#b45309", "#fef3c7", "#fcd34d"),
    "security_impact": TagDisplay("Security", "🔒", "#b91c1c", "#fee2e2", "#fca5a5"),
    "practice_impact": TagDisplay("Practice", "📐", "#0369a1", "#e0f2fe", "#7dd3fc"),
}

_FALLBACK_EMOJI = "📌"
_FALLBACK_COLOR = "#475569"
_FALLBACK_BG = "#f1f5f9"
_FALLBACK_BORDER = "#cbd5e1"


def display_for(tag: str) -> TagDisplay:
    """
    Return display metadata for a tag, falling back to neutral styling
    with a title-cased label for unknown tag strings.
    """
    known = IMPACT_TAGS.get(tag)
    if known is not None:
        return known
    stripped = tag.removesuffix("_impact").replace("_", " ").strip()
    label = stripped.title() if stripped else tag
    return TagDisplay(
        label=label,
        emoji=_FALLBACK_EMOJI,
        color=_FALLBACK_COLOR,
        bg=_FALLBACK_BG,
        border=_FALLBACK_BORDER,
    )
