"""
rendering/html_renderer.py — Renders curation output as standalone HTML.

Traces: SRC-004 (HTML export format), SRC-137 (email-paste-ready),
        SRC-141 (URL enforcement — items without valid URL dropped at renderer),
        SRC-145 (date-stamped filename, idempotent re-runs),
        SRC-048 (curated item schema: headline, source, URL, why-it-matters, tags),
        SRC-129 (prompt version in footer),
        SRC-150 (monitoring metadata in footer)
"""

from __future__ import annotations

import html as html_lib
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

# Re-export under the legacy private name for backwards-compatibility with tests.
# Canonical import: ``from ai_news_agent.rendering.utils import is_valid_url``
# Traces: SRC-049, SRC-141


# ---------------------------------------------------------------------------
# HTML escaping helpers
# ---------------------------------------------------------------------------


def _esc(text: str) -> str:
    """HTML-escape arbitrary text for safe insertion into HTML content."""
    return html_lib.escape(str(text), quote=True)


def _esc_attr(url: str) -> str:
    """
    Escape a URL for safe use inside an HTML attribute value (href/src).

    We escape only ``"`` → ``&quot;`` and ``&`` → ``&amp;`` to prevent
    attribute injection while keeping the URL functional.  Angle brackets
    are also escaped for completeness.
    """
    return (
        str(url)
        .replace("&", "&amp;")
        .replace('"', "&quot;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def _impact_badges_html(tags: list[str]) -> str:
    """Render impact tags as styled HTML inline badges using the shared registry."""
    from ai_news_agent.rendering.impact_tags import display_for

    badges: list[str] = []
    for tag in tags:
        d = display_for(tag)
        # Standalone-HTML export uses the "<short> Impact" form for clarity
        # in email/web destinations that show the badge without a tooltip.
        label = f"{d.label} Impact" if d.label else tag
        badges.append(
            f'<span style="background:{d.bg};color:{d.color};padding:2px 8px;'
            f'border-radius:12px;font-size:0.8em;white-space:nowrap">'
            f"{d.emoji} {_esc(label)}</span>"
        )
    if not badges:
        badges.append(
            '<span style="background:#f5f5f5;padding:2px 8px;border-radius:12px;'
            'font-size:0.8em">📌 General</span>'
        )
    return " ".join(badges)


class HtmlRenderer:
    """
    Renders a :class:`CurationRunResult` to a self-contained HTML document.

    Output format (SRC-004, SRC-137):
    - **Self-contained** — inline CSS only; no external CDN dependencies.
    - Paste-ready into an email client or web page (SRC-137).
    - Every item includes headline (linked), source, date, why-it-matters,
      impact badges, and optional Twitter provenance (SRC-048).
    - Items without a valid ``http(s)://`` URL are silently dropped —
      final URL enforcement (SRC-141, SRC-049).
    - Date-stamped filename: ``{YYYY-MM-DD}-{cadence}.html`` (SRC-145).
    - Footer includes prompt_version for regression tracing (SRC-129)
      and all SRC-150 quality-monitoring fields.

    Cadence-specific layout (SRC-029–SRC-032):
    - **daily**   → article cards
    - **weekly**  → themes + stories + outlook (SRC-030)
    - **monthly** → themes + outlook + stories (SRC-031 — bigger picture first)
    - **annual**  → year-themes + top-10 + predictions (SRC-032, SRC-124)

    Traces: SRC-004, SRC-029–SRC-032, SRC-048, SRC-049, SRC-129,
            SRC-137, SRC-141, SRC-145, SRC-148, SRC-150
    """

    def render(self, result: CurationRunResult) -> str:
        """
        Render the curation result to a standalone HTML document string.

        Args:
            result: :class:`CurationRunResult` from the Curation Agent.

        Returns:
            Full ``<!DOCTYPE html>`` document, ready to save or paste into email.

        Traces: SRC-004, SRC-048, SRC-141
        """
        meta = result.metadata
        cadence = meta.cadence

        # Final URL enforcement (SRC-141, SRC-049)
        valid_items = [item for item in result.items if _is_valid_url(item.url)]
        dropped = len(result.items) - len(valid_items)
        if dropped > 0:
            log.warning(
                "html_renderer_url_drop",
                dropped=dropped,
                cadence=cadence,
                agent_id=meta.agent_id,
            )

        body_parts: list[str] = []

        # Header (SRC-116 — concrete ISO dates)
        body_parts.append(self._render_header_html(meta, cadence))

        # Twitter degradation note (SRC-148)
        if result.twitter_degradation_note:
            body_parts.append(
                f'<div class="degradation-note">⚠️ {_esc(result.twitter_degradation_note)}</div>'
            )

        # Sparse-digest diagnostics — surface the explanation in-document so
        # readers don't have to chase logs to learn why the digest is empty.
        if result.diagnostics is not None:
            body_parts.append(self._render_diagnostics_html(result.diagnostics))

        # Cadence-specific body
        if cadence == "daily":
            body_parts.append(self._render_daily_html(valid_items))
        elif cadence == "weekly":
            body_parts.append(self._render_weekly_html(valid_items, result.themes, result.outlook))
        elif cadence == "monthly":
            body_parts.append(self._render_monthly_html(valid_items, result.themes, result.outlook))
        elif cadence == "annual":
            body_parts.append(
                self._render_annual_html(valid_items, result.themes, result.predictions)
            )

        # Footer metadata (SRC-129, SRC-150)
        body_parts.append(self._render_footer_html(meta))

        body_html = "\n".join(body_parts)
        return self._wrap_document(meta, cadence, body_html)

    # ------------------------------------------------------------------
    # Document wrapper
    # ------------------------------------------------------------------

    def _wrap_document(self, meta: DigestMetadata, cadence: str, body: str) -> str:
        """Wrap body content in a complete HTML document with inline CSS."""
        title_map = {
            "daily": "Daily AI Digest",
            "weekly": "Weekly AI Digest",
            "monthly": "Monthly AI Briefing",
            "annual": "Annual AI Review",
        }
        title = _esc(f"{title_map.get(cadence, 'AI Digest')} — {meta.run_date}")

        return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>{title}</title>
  <style>
    body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
            max-width: 720px; margin: 0 auto; padding: 24px; color: #1a1a2e; line-height: 1.6; }}
    h1 {{ color: #1a1a2e; border-bottom: 3px solid #4f46e5; padding-bottom: 8px; }}
    h2 {{ color: #4f46e5; margin-top: 32px; }}
    h3 {{ margin-bottom: 4px; }}
    a {{ color: #4f46e5; text-decoration: none; }}
    a:hover {{ text-decoration: underline; }}
    .meta-bar {{ background: #f8f8ff; padding: 12px 16px; border-radius: 8px;
                 font-size: 0.85em; color: #666; margin-bottom: 24px; }}
    .item-card {{ border-left: 4px solid #4f46e5; padding: 12px 16px; margin: 16px 0;
                  background: #fafafa; border-radius: 0 8px 8px 0; }}
    .source-line {{ font-size: 0.85em; color: #555; margin: 4px 0; }}
    .why-matters {{ margin-top: 8px; }}
    .badges {{ margin-top: 6px; }}
    .cross-refs {{ font-size: 0.82em; color: #666; margin-top: 4px; }}
    .twitter-signal {{ font-size: 0.82em; color: #1da1f2; margin-top: 4px; }}
    .themes-list {{ background: #f0f4ff; padding: 12px 20px; border-radius: 8px; }}
    .themes-list li {{ margin: 6px 0; }}
    .outlook-box {{ background: #fffde7; padding: 16px; border-radius: 8px;
                    border-left: 4px solid #f9a825; margin: 16px 0; }}
    .predictions {{ counter-reset: prediction-counter; }}
    .prediction-item {{ counter-increment: prediction-counter; padding: 12px 16px;
                         margin: 12px 0; background: #fff3e0; border-radius: 8px;
                         border-left: 4px solid #ff9800; }}
    .prediction-item::before {{ content: counter(prediction-counter) ". ";
                                  font-weight: bold; color: #ff9800; }}
    .footer-meta {{ background: #f5f5f5; padding: 12px 16px; border-radius: 8px;
                    font-size: 0.82em; color: #888; margin-top: 32px; }}
    .degradation-note {{ background: #fff8e1; border-left: 4px solid #ffc107;
                          padding: 10px 14px; border-radius: 4px; margin: 12px 0;
                          font-size: 0.9em; }}
    .empty-state {{ color: #888; font-style: italic; }}
  </style>
</head>
<body>
{body}
</body>
</html>"""

    # ------------------------------------------------------------------
    # Header
    # ------------------------------------------------------------------

    def _render_header_html(self, meta: DigestMetadata, cadence: str) -> str:
        """Render the digest header with concrete ISO window dates (SRC-116)."""
        title_map = {
            "daily": "Daily AI News Digest",
            "weekly": "Weekly AI News Digest",
            "monthly": "Monthly AI Intelligence Briefing",
            "annual": "Annual AI Year in Review &amp; Predictions",
        }
        title = title_map.get(cadence, "AI News Digest")
        window = (
            f"{meta.window_start.strftime('%Y-%m-%d')} &mdash; "
            f"{meta.window_end.strftime('%Y-%m-%d')}"
        )
        return f"""<h1>{title}</h1>
<div class="meta-bar">
  <strong>Window:</strong> {window} &nbsp;|&nbsp;
  <strong>Agent:</strong> <code>{_esc(meta.agent_id)}</code> &nbsp;|&nbsp;
  <strong>Run:</strong> {meta.run_date}
</div>"""

    # ------------------------------------------------------------------
    # Sparse-digest diagnostics
    # ------------------------------------------------------------------

    def _render_diagnostics_html(self, diag: CurationDiagnostics) -> str:
        """
        Render the empty/sparse-digest explanation block.

        Mirrors the Markdown renderer so the same information surfaces in
        every output format. Styled in-line so the HTML stays paste-ready
        for email clients that strip <style> blocks.
        """
        reason_items = "".join(f"<li>{_esc(reason)}</li>" for reason in diag.reasons)
        if diag.articles_in_window_by_tier:
            tier_breakdown = (
                " ("
                + ", ".join(
                    f"tier {_esc(k)}={_esc(str(v))}"
                    for k, v in sorted(diag.articles_in_window_by_tier.items())
                )
                + ")"
            )
        else:
            tier_breakdown = ""

        return (
            '<div class="diagnostics-note" '
            'style="background:#fff8e1;border-left:4px solid #f9a825;'
            'padding:12px 16px;margin:16px 0;border-radius:4px">'
            "<strong>ℹ️ Why this digest is sparse</strong>"
            f"<ul>{reason_items}</ul>"
            '<div style="font-size:0.85em;color:#555;margin-top:8px">'
            f"Articles in store: {_esc(str(diag.articles_in_store))} · "
            f"Candidates in window: {_esc(str(diag.articles_in_window))}"
            f"{tier_breakdown} · "
            f"Items dropped (missing URL): {_esc(str(diag.items_dropped_no_url))} · "
            f"Twitter signal available: {_esc(str(diag.twitter_signal_available))}"
            "</div>"
            "</div>"
        )

    # ------------------------------------------------------------------
    # Daily (SRC-029)
    # ------------------------------------------------------------------

    def _render_daily_html(self, items: list[CuratedItem]) -> str:
        """
        Daily layout: article cards with headline (linked), source, date,
        why-it-matters, impact badges.
        Traces: SRC-029 (daily output), SRC-048 (item schema)
        """
        if not items:
            return (
                "<h2>Today's Stories</h2>"
                '<p class="empty-state">No articles met the curation threshold.</p>'
            )
        cards = "\n".join(self._render_item_card_html(i, item) for i, item in enumerate(items, 1))
        return f"<h2>Today's Top {len(items)} Stories</h2>\n{cards}"

    # ------------------------------------------------------------------
    # Weekly (SRC-030)
    # ------------------------------------------------------------------

    def _render_weekly_html(
        self,
        items: list[CuratedItem],
        themes: list[str],
        outlook: str,
    ) -> str:
        """
        Weekly layout: themes → top stories → looking-ahead outlook.
        Traces: SRC-030 (identify themes for the week, look ahead)
        """
        parts: list[str] = []

        if themes:
            theme_items = "\n".join(f"<li>{_esc(t)}</li>" for t in themes)
            parts.append(f'<h2>This Week\'s Themes</h2><ul class="themes-list">{theme_items}</ul>')

        cards = (
            "\n".join(self._render_item_card_html(i, item) for i, item in enumerate(items, 1))
            if items
            else '<p class="empty-state">No articles met the curation threshold.</p>'
        )
        parts.append(f"<h2>Top Stories This Week</h2>\n{cards}")

        if outlook:
            parts.append(f'<h2>Looking Ahead</h2><div class="outlook-box">{_esc(outlook)}</div>')

        return "\n".join(parts)

    # ------------------------------------------------------------------
    # Monthly (SRC-031)
    # ------------------------------------------------------------------

    def _render_monthly_html(
        self,
        items: list[CuratedItem],
        themes: list[str],
        outlook: str,
    ) -> str:
        """
        Monthly layout: themes → anticipated-news outlook → top articles.

        Ordering is themes-first then outlook then detail, consistent with the
        monthly cadence's bigger-picture focus (SRC-031).

        Traces: SRC-031 (monthly: bigger-picture themes, anticipated news)
        """
        parts: list[str] = []

        if themes:
            theme_items = "\n".join(f"<li>{_esc(t)}</li>" for t in themes)
            parts.append(f'<h2>Monthly Themes</h2><ul class="themes-list">{theme_items}</ul>')

        if outlook:
            parts.append(f'<h2>What to Watch</h2><div class="outlook-box">{_esc(outlook)}</div>')

        cards = (
            "\n".join(self._render_item_card_html(i, item) for i, item in enumerate(items, 1))
            if items
            else '<p class="empty-state">No articles met the curation threshold.</p>'
        )
        parts.append(f"<h2>Top Stories This Month</h2>\n{cards}")

        return "\n".join(parts)

    # ------------------------------------------------------------------
    # Annual (SRC-032, SRC-124)
    # ------------------------------------------------------------------

    def _render_annual_html(
        self,
        items: list[CuratedItem],
        themes: list[str],
        predictions: list[str],
    ) -> str:
        """
        Annual layout: year-themes → top-10 articles → 10 predictions.
        Predictions are grounded in observed trends with reasoning shown (SRC-124).

        Traces: SRC-032 (annual output), SRC-124 (predictions section)
        """
        parts: list[str] = []

        if themes:
            theme_items = "\n".join(f"<li>{_esc(t)}</li>" for t in themes)
            parts.append(
                f'<h2>Year in Review — Key Themes</h2><ul class="themes-list">{theme_items}</ul>'
            )

        cards = (
            "\n".join(self._render_item_card_html(i, item) for i, item in enumerate(items, 1))
            if items
            else '<p class="empty-state">No articles met the curation threshold.</p>'
        )
        parts.append(f"<h2>Top 10 Stories of the Year</h2>\n{cards}")

        if predictions:
            pred_items = "\n".join(
                f'<div class="prediction-item">{_esc(p)}</div>' for p in predictions
            )
            note_text = (
                "Each prediction is grounded in observed trends from the year "
                "reviewed. Reasoning is shown. (SRC-124)"
            )
            note = f"<p><em>{_esc(note_text)}</em></p>"
            parts.append(
                f"<h2>10 Predictions for the Year Ahead</h2>{note}"
                f'<div class="predictions">{pred_items}</div>'
            )

        return "\n".join(parts)

    # ------------------------------------------------------------------
    # Item card (shared across cadences) (SRC-048)
    # ------------------------------------------------------------------

    def _render_item_card_html(self, index: int, item: CuratedItem) -> str:
        """
        Render a single curated item as an HTML card.

        Includes:
        - Headline linked to primary source URL (SRC-049 — guaranteed by caller)
        - Source name, publication date, tier
        - Why-it-matters text (2–3 sentences, SRC-048, SRC-122)
        - Impact tag badges
        - Twitter provenance if applicable (SRC-048)
        - Cross-references if present (SRC-048)

        Traces: SRC-048 (item schema), SRC-049 (URL guaranteed by caller)
        """
        badges = _impact_badges_html(item.impact_tags)
        twitter_html = ""
        if item.twitter_handle and item.tweet_url:
            twitter_html = (
                f'<div class="twitter-signal">🐦 via '
                f'<a href="{_esc_attr(item.tweet_url)}" rel="noopener">'
                f"@{_esc(item.twitter_handle)}</a></div>"
            )

        cross_refs_html = ""
        if item.cross_refs:
            links = " &middot; ".join(
                f'<a href="{_esc_attr(r)}" rel="noopener">link</a>'
                for r in item.cross_refs
                if _is_valid_url(r)
            )
            if links:
                cross_refs_html = f'<div class="cross-refs"><strong>Related:</strong> {links}</div>'

        return f"""<div class="item-card">
  <h3>{index}. <a href="{_esc_attr(item.url)}" rel="noopener">{_esc(item.headline)}</a></h3>
  <div class="source-line">
    <strong>{_esc(item.source_name)}</strong> &nbsp;&middot;&nbsp;
    {_esc(str(item.pub_date))} &nbsp;&middot;&nbsp; Tier {_esc(item.tier)}
  </div>
  {twitter_html}
  <div class="why-matters">{_esc(item.why_it_matters)}</div>
  <div class="badges">{badges}</div>
  {cross_refs_html}
</div>"""

    # ------------------------------------------------------------------
    # Footer (SRC-129, SRC-150)
    # ------------------------------------------------------------------

    def _render_footer_html(self, meta: DigestMetadata) -> str:
        """Render monitoring metadata footer (SRC-129, SRC-150)."""
        twitter_status = "✅ available" if meta.twitter_signal_available else "⚠️ unavailable"
        return f"""<div class="footer-meta">
  <strong>Agent:</strong> {_esc(meta.agent_id)} &nbsp;|&nbsp;
  <strong>Model:</strong> {_esc(meta.llm_model)} &nbsp;|&nbsp;
  <strong>Items:</strong> {meta.items_included}/{meta.items_considered} &nbsp;|&nbsp;
  <strong>Twitter signal:</strong> {twitter_status} &nbsp;|&nbsp;
  <strong>Prompt:</strong> <code>{_esc(meta.prompt_version)}</code>
</div>"""

    # ------------------------------------------------------------------
    # Filename helper (SRC-145)
    # ------------------------------------------------------------------

    @staticmethod
    def filename(meta: DigestMetadata) -> str:
        """
        Return the date-stamped filename for this digest.

        Pattern: ``{YYYY-MM-DD}-{cadence}.html``

        The agent_id is embedded in the **directory path** (``outputs/{agent_id}/``),
        not the filename, so a future thin distribution layer can ingest the
        output tree without parsing filenames (SRC-140).

        Traces: SRC-145 (date-stamped, idempotent re-runs),
                SRC-140 (naming convention supports future distribution layer)
        """
        from ai_news_agent.rendering.utils import filename_stem

        return f"{filename_stem(meta.run_date, meta.cadence)}.html"
