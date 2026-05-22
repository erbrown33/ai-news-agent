"""
rendering/markdown_renderer.py — Renders curation output as Markdown.

Traces: SRC-004 (Markdown export format), SRC-138 (Slack/Teams paste-ready),
        SRC-141 (URL enforcement — items without valid URL dropped at renderer),
        SRC-145 (date-stamped filename, idempotent re-runs),
        SRC-048 (curated item schema: headline, source+URL, why-it-matters, tags),
        SRC-129 (prompt version in footer),
        SRC-150 (monitoring metadata in footer)
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import structlog

from ai_news_agent.rendering.utils import is_valid_url as _is_valid_url  # noqa: F401

if TYPE_CHECKING:
    from ai_news_agent.curation.agent import CurationRunResult
    from ai_news_agent.storage.models import (
        CuratedItem,
        CurationDiagnostics,
        DigestMetadata,
    )

log = structlog.get_logger(__name__)

# Re-export the shared validator under the legacy private name so existing
# test imports continue to work: ``from ...markdown_renderer import _is_valid_url``
# The canonical import path is ``rendering.utils.is_valid_url``.
# Traces: SRC-049, SRC-141


def _impact_badge(tags: list[str]) -> str:
    """Format impact tags as compact inline badges using the shared registry."""
    from ai_news_agent.rendering.impact_tags import display_for

    parts = [f"{display_for(t).emoji} {display_for(t).label}" for t in tags]
    return " · ".join(parts) if parts else "📌 General"


class MarkdownRenderer:
    """
    Renders a :class:`CurationRunResult` to a Markdown string.

    Output format (SRC-004, SRC-138):
    - Paste-ready into Slack, Teams, or any Markdown-aware email client.
    - Every item includes headline (linked), source + URL, why-it-matters,
      impact tags (SRC-048).
    - Items without a valid http(s):// URL are dropped — final URL enforcement
      (SRC-141, SRC-049). This is the second safety layer after the Scorer.
    - Date-stamped filename pattern: ``{YYYY-MM-DD}-{cadence}.md`` (SRC-145).
    - Footer embeds prompt_version SHA-256 for regression tracing (SRC-129)
      and quality-monitoring fields (SRC-150).

    Cadence-specific sections (SRC-029–SRC-032):
    - **daily**   → article cards only
    - **weekly**  → themes + top articles + looking-ahead outlook (SRC-030)
    - **monthly** → themes + what-to-watch outlook + top articles (SRC-031)
    - **annual**  → year-themes + top-10 articles + 10 predictions (SRC-032, SRC-124)

    Traces: SRC-004, SRC-029–SRC-032, SRC-048, SRC-049, SRC-129,
            SRC-138, SRC-141, SRC-145, SRC-148, SRC-150
    """

    def render(self, result: CurationRunResult) -> str:
        """
        Render the curation result to a Markdown string.

        Args:
            result: :class:`CurationRunResult` from the Curation Agent.

        Returns:
            Markdown string ready to write to disk or paste into Slack/Teams.

        Traces: SRC-004, SRC-048, SRC-141
        """
        meta = result.metadata
        cadence = meta.cadence

        # Final URL enforcement (SRC-141, SRC-049) — second safety layer
        valid_items = [item for item in result.items if _is_valid_url(item.url)]
        dropped = len(result.items) - len(valid_items)
        if dropped > 0:
            log.warning(
                "markdown_renderer_url_drop",
                dropped=dropped,
                cadence=cadence,
                agent_id=meta.agent_id,
            )

        lines: list[str] = []

        # ------------------------------------------------------------------
        # Header (SRC-116 — concrete ISO dates, never relative phrases)
        # ------------------------------------------------------------------
        lines.extend(self._render_header(meta, cadence))

        # ------------------------------------------------------------------
        # Twitter degradation note (SRC-148)
        # ------------------------------------------------------------------
        if result.twitter_degradation_note:
            lines.append(f"> ⚠️ {result.twitter_degradation_note}\n")

        # ------------------------------------------------------------------
        # Sparse-digest diagnostics: explain why this run yielded few/no
        # items so the reader doesn't have to dig into logs.
        # ------------------------------------------------------------------
        if result.diagnostics is not None:
            lines.extend(self._render_diagnostics(result.diagnostics))

        # ------------------------------------------------------------------
        # Cadence-specific body (SRC-029–SRC-032)
        # ------------------------------------------------------------------
        if cadence == "daily":
            lines.extend(self._render_daily(valid_items))
        elif cadence == "weekly":
            lines.extend(self._render_weekly(valid_items, result.themes, result.outlook))
        elif cadence == "monthly":
            lines.extend(self._render_monthly(valid_items, result.themes, result.outlook))
        elif cadence == "annual":
            lines.extend(self._render_annual(valid_items, result.themes, result.predictions))

        # ------------------------------------------------------------------
        # Footer — monitoring metadata (SRC-129, SRC-150)
        # ------------------------------------------------------------------
        lines.extend(self._render_footer(meta))

        return "\n".join(lines)

    # ------------------------------------------------------------------
    # Header
    # ------------------------------------------------------------------

    def _render_header(self, meta: DigestMetadata, cadence: str) -> list[str]:
        """Render the digest header with window dates (SRC-116)."""
        title_map = {
            "daily": "Daily AI News Digest",
            "weekly": "Weekly AI News Digest",
            "monthly": "Monthly AI Intelligence Briefing",
            "annual": "Annual AI Year in Review & Predictions",
        }
        title = title_map.get(cadence, "AI News Digest")
        window = (
            f"{meta.window_start.strftime('%Y-%m-%d')} — {meta.window_end.strftime('%Y-%m-%d')}"
        )

        return [
            f"# {title}",
            f"**Window:** {window}  ",
            f"**Agent:** `{meta.agent_id}`  ",
            f"**Run date:** {meta.run_date}  ",
            f"**Prompt version:** `{meta.prompt_version}`",
            "",
            "---",
            "",
        ]

    # ------------------------------------------------------------------
    # Sparse-digest diagnostics
    # ------------------------------------------------------------------

    def _render_diagnostics(self, diag: CurationDiagnostics) -> list[str]:
        """
        Render the empty/sparse-digest explanation block.

        Surfaced in-document so a reader of the digest can immediately see
        why so few (or no) items were curated — no need to chase down logs.
        """
        lines = [
            "> ℹ️ **Why this digest is sparse**",
            ">",
        ]
        for reason in diag.reasons:
            lines.append(f"> - {reason}")
        lines.extend(
            [
                ">",
                f"> *Articles in store:* {diag.articles_in_store}  ",
                f"> *Candidates in window:* {diag.articles_in_window}"
                + (
                    f" ({', '.join(f'tier {k}={v}' for k, v in sorted(diag.articles_in_window_by_tier.items()))})"
                    if diag.articles_in_window_by_tier
                    else ""
                )
                + "  ",
                f"> *Items dropped (missing URL):* {diag.items_dropped_no_url}  ",
                f"> *Twitter signal available:* {diag.twitter_signal_available}",
                "",
            ]
        )
        return lines

    # ------------------------------------------------------------------
    # Daily (SRC-029)
    # ------------------------------------------------------------------

    def _render_daily(self, items: list[CuratedItem]) -> list[str]:
        """
        Daily format: article cards with headline (linked), source+URL,
        why-it-matters, impact tags, optional Twitter attribution.

        Traces: SRC-029 (daily output), SRC-048 (item schema)
        """
        if not items:
            return ["*No articles met the curation threshold for this period.*", ""]

        lines: list[str] = [f"## Today's Top {len(items)} Stories\n"]
        for i, item in enumerate(items, start=1):
            lines.extend(self._render_item_card(i, item))
        return lines

    # ------------------------------------------------------------------
    # Weekly (SRC-030)
    # ------------------------------------------------------------------

    def _render_weekly(
        self,
        items: list[CuratedItem],
        themes: list[str],
        outlook: str,
    ) -> list[str]:
        """
        Weekly format: themes section + top articles + forward-looking outlook.
        Traces: SRC-030 (weekly output — identify themes, look ahead)
        """
        lines: list[str] = []

        if themes:
            lines.append("## This Week's Themes\n")
            for theme in themes:
                lines.append(f"- {theme}")
            lines.append("")

        lines.append("## Top Stories This Week\n")
        if not items:
            lines.append("*No articles met the curation threshold for this period.*")
            lines.append("")
        else:
            for i, item in enumerate(items, start=1):
                lines.extend(self._render_item_card(i, item))

        if outlook:
            lines.append("## Looking Ahead\n")
            lines.append(outlook)
            lines.append("")

        return lines

    # ------------------------------------------------------------------
    # Monthly (SRC-031)
    # ------------------------------------------------------------------

    def _render_monthly(
        self,
        items: list[CuratedItem],
        themes: list[str],
        outlook: str,
    ) -> list[str]:
        """
        Monthly format: big-picture themes + anticipated news outlook + top articles.

        Ordering: themes → what-to-watch → top stories (bigger picture first,
        then detail — consistent with monthly cadence intent in SRC-031).

        Traces: SRC-031 (monthly output — bigger-picture themes, anticipated news)
        """
        lines: list[str] = []

        if themes:
            lines.append("## Monthly Themes\n")
            for theme in themes:
                lines.append(f"- {theme}")
            lines.append("")

        if outlook:
            lines.append("## What to Watch\n")
            lines.append(outlook)
            lines.append("")

        lines.append("## Top Stories This Month\n")
        if not items:
            lines.append("*No articles met the curation threshold for this period.*")
            lines.append("")
        else:
            for i, item in enumerate(items, start=1):
                lines.extend(self._render_item_card(i, item))

        return lines

    # ------------------------------------------------------------------
    # Annual (SRC-032, SRC-124)
    # ------------------------------------------------------------------

    def _render_annual(
        self,
        items: list[CuratedItem],
        themes: list[str],
        predictions: list[str],
    ) -> list[str]:
        """
        Annual format: year themes + top 10 articles + 10 predictions grounded in trends.

        Traces: SRC-032 (annual output — top 10 articles),
                SRC-124 (predictions grounded in observed trends, reasoning shown)
        """
        lines: list[str] = []

        if themes:
            lines.append("## Year in Review — Key Themes\n")
            for theme in themes:
                lines.append(f"- {theme}")
            lines.append("")

        lines.append("## Top 10 Stories of the Year\n")
        if not items:
            lines.append("*No articles met the curation threshold for this period.*")
            lines.append("")
        else:
            for i, item in enumerate(items, start=1):
                lines.extend(self._render_item_card(i, item))

        if predictions:
            lines.append("## 10 Predictions for the Year Ahead\n")
            lines.append(
                "> *Each prediction is grounded in observed trends from the year reviewed. "
                "Reasoning is shown. (SRC-124)*\n"
            )
            for i, pred in enumerate(predictions, start=1):
                lines.append(f"**{i}.** {pred}\n")

        return lines

    # ------------------------------------------------------------------
    # Item card (shared across cadences) (SRC-048)
    # ------------------------------------------------------------------

    def _render_item_card(self, index: int, item: CuratedItem) -> list[str]:
        """
        Render a single curated item as a Markdown card.

        Includes:
        - Headline linked to primary source URL (SRC-049 — guaranteed by caller)
        - Source name, publication date, impact tag badges
        - Why-it-matters text (2–3 sentences, SRC-048, SRC-122)
        - Twitter provenance if applicable (SRC-048 — twitter_handle + tweet_url)
        - Cross-references to related items if present (SRC-048)

        Traces: SRC-048 (curated item schema), SRC-049 (URL present — enforced by caller)
        """
        badge = _impact_badge(item.impact_tags)
        twitter_note = ""
        if item.twitter_handle and item.tweet_url:
            twitter_note = f"  \n  🐦 via [@{item.twitter_handle}]({item.tweet_url})"

        lines = [
            f"### {index}. [{item.headline}]({item.url})",
            f"**{item.source_name}** · {item.pub_date} · Tier {item.tier} · {badge}{twitter_note}",
            "",
            item.why_it_matters,
            "",
        ]

        # Cross-references (SRC-048 — optional)
        if item.cross_refs:
            lines.append("**Related:** " + " · ".join(f"[link]({r})" for r in item.cross_refs))
            lines.append("")

        return lines

    # ------------------------------------------------------------------
    # Footer (SRC-129, SRC-150)
    # ------------------------------------------------------------------

    def _render_footer(self, meta: DigestMetadata) -> list[str]:
        """Render monitoring metadata footer (SRC-129, SRC-150)."""
        twitter_status = "✅ available" if meta.twitter_signal_available else "⚠️ unavailable"
        return [
            "---",
            "",
            "## Digest Metadata",
            "",
            "| Field | Value |",
            "|-------|-------|",
            f"| Agent ID | `{meta.agent_id}` |",
            f"| Cadence | `{meta.cadence}` |",
            f"| LLM Provider | `{meta.llm_provider}` |",
            f"| LLM Model | `{meta.llm_model}` |",
            f"| Prompt Version | `{meta.prompt_version}` |",
            f"| Items Considered | {meta.items_considered} |",
            f"| Items Included | {meta.items_included} |",
            f"| Twitter Signal | {twitter_status} |",
            f"| Token Usage | {meta.token_usage:,} |",
            "",
            "*Generated by AI News Curation Agent (SRC-004, SRC-145)*",
        ]

    # ------------------------------------------------------------------
    # Filename helper (SRC-145)
    # ------------------------------------------------------------------

    @staticmethod
    def filename(meta: DigestMetadata) -> str:
        """
        Return the date-stamped filename for this digest.

        Pattern: ``{YYYY-MM-DD}-{cadence}.md``

        The agent_id is embedded in the **directory path** (``outputs/{agent_id}/``),
        not the filename, so a future thin distribution layer can ingest the
        output tree without parsing filenames (SRC-140).

        Traces: SRC-145 (date-stamped, idempotent re-runs),
                SRC-140 (naming convention supports future distribution layer)
        """
        from ai_news_agent.rendering.utils import filename_stem

        return f"{filename_stem(meta.run_date, meta.cadence)}.md"
