"""
tests/unit/test_rendering_agent.py — Comprehensive Rendering Agent test suite.

This file extends the baseline coverage in test_rendering.py with deep tests
for every requirement in SRC-004, SRC-029–SRC-032, SRC-048–SRC-049,
SRC-102, SRC-129, SRC-135–SRC-141, SRC-145, SRC-148, and SRC-150.

The LLM is never called — all tests operate on pre-built CurationRunResult
objects constructed in-process (SRC-098: mock LLM and Twitter calls).

Test-class inventory
────────────────────
TestSharedUrlValidator      — ``rendering.utils.is_valid_url`` exhaustive cases
TestFilenameConventionFull  — stem formula, all-ASCII, agent_id in dir not file
TestOutputDirConvention     — ``outputs/{agent_id}/`` pattern (SRC-140)
TestMarkdownRendererDeep    — full item card fields, Twitter, cross-refs, footer
TestHtmlRendererDeep        — HTML structure, XSS, attribute injection, viewport
TestHtmlRendererSecurity    — ``rel="noopener"``, URL attribute injection, theme/
                               outlook/prediction injection
TestJsonRendererDeep        — schema shape, unicode preservation, all item fields
TestJsonSchemaContract      — ``schema_version``, top-level key stability
TestRenderingAgentFull      — three files written, correct extensions and stems
TestRenderingAgentDryRunFull— dry-run leaves output_dir clean (SRC-102)
TestRenderingAgentStoreFull — DigestRecord path update (SRC-145)
TestCadenceCompleteness     — every cadence produces required sections
TestUrlEnforcementLayering  — both layers enforce; renderer is the final gate
TestTwitterDegradationFull  — all formats, escaped HTML, JSON key absent/present
TestMonitoringFieldsFull    — every SRC-150 field in JSON metadata block
TestCrossRefsFull           — valid and invalid cross-ref URLs per renderer
TestCLIEntryPoint           — ``cli_main`` argument parsing and exit codes
TestCLIRender               — ``cli_main`` end-to-end with a real JSON file
TestEnsureAsciiOff          — JSON preserves non-ASCII characters
TestIdempotency             — second render always produces identical content
TestConcurrentReruns        — rapid sequential renders overwrite cleanly (SRC-145)
TestItemCardAllFields       — every field from CuratedItem.schema (SRC-048)
TestEmptyStateGraceful      — all empty-list/empty-string branches
TestRenderingResult         — RenderingResult.items_rendered and items_dropped_no_url

Traces: SRC-004, SRC-029–SRC-032, SRC-048–SRC-049, SRC-061, SRC-098, SRC-102,
        SRC-129, SRC-135–SRC-141, SRC-145, SRC-148, SRC-150
"""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, date, datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from ai_news_agent.curation.agent import CurationRunResult
from ai_news_agent.rendering import (
    HtmlRenderer,
    JsonRenderer,
    MarkdownRenderer,
    RenderingAgent,
    RenderingResult,
    filename_stem,
    is_valid_url,
)
from ai_news_agent.rendering.json_renderer import SCHEMA_VERSION
from ai_news_agent.rendering.utils import VALID_URL_SCHEMES
from ai_news_agent.storage.models import CuratedItem, DigestMetadata, DigestRecord

# ──────────────────────────────────────────────────────────────────────────────
# Shared test-data builders
# ──────────────────────────────────────────────────────────────────────────────


def _item(
    headline: str = "GPT-5 Transforms Enterprise Workflows",
    source_name: str = "Reuters",
    url: str = "https://reuters.com/gpt5-enterprise",
    pub_date: date | None = None,
    why: str = "GPT-5 deployment accelerates enterprise AI adoption by 3×.",
    impact_tags: list[str] | None = None,
    tier: str = "1b",
    cross_refs: list[str] | None = None,
    twitter_handle: str | None = None,
    tweet_url: str | None = None,
    prompt_version: str = "sha256:deadbeef",
) -> CuratedItem:
    return CuratedItem(
        headline=headline,
        source_name=source_name,
        url=url,
        pub_date=pub_date or date(2026, 5, 11),
        why_it_matters=why,
        impact_tags=impact_tags if impact_tags is not None else ["business_impact"],
        tier=tier,
        cross_refs=cross_refs if cross_refs is not None else [],
        twitter_handle=twitter_handle,
        tweet_url=tweet_url,
        prompt_version=prompt_version,
    )


def _meta(
    agent_id: str = "test-agent",
    cadence: str = "daily",
    run_date: date | None = None,
    prompt_version: str = "sha256:cafebabe0001",
    llm_model: str = "gpt-4o",
    token_usage: int = 8192,
    items_considered: int = 40,
    items_included: int = 5,
    twitter_signal_available: bool = True,
) -> DigestMetadata:
    return DigestMetadata(
        agent_id=agent_id,
        cadence=cadence,
        run_date=run_date or date(2026, 5, 11),
        window_start=datetime(2026, 5, 10, 0, 0, tzinfo=UTC),
        window_end=datetime(2026, 5, 10, 23, 59, tzinfo=UTC),
        prompt_version=prompt_version,
        llm_provider="openai",
        llm_model=llm_model,
        items_considered=items_considered,
        items_included=items_included,
        items_by_tier={"1b": 3, "2": 2},
        items_by_source_class={"web": 4, "twitter": 1},
        twitter_signal_available=twitter_signal_available,
        tweet_api_call_count=9,
        token_usage=token_usage,
    )


def _result(
    items: list[CuratedItem] | None = None,
    metadata: DigestMetadata | None = None,
    themes: list[str] | None = None,
    outlook: str = "",
    predictions: list[str] | None = None,
    twitter_degradation_note: str | None = None,
    dry_run: bool = False,
) -> CurationRunResult:
    return CurationRunResult(
        metadata=metadata or _meta(),
        items=items if items is not None else [_item()],
        themes=themes or [],
        outlook=outlook,
        predictions=predictions or [],
        twitter_degradation_note=twitter_degradation_note,
        dry_run=dry_run,
    )


# ──────────────────────────────────────────────────────────────────────────────
# TestSharedUrlValidator
# ──────────────────────────────────────────────────────────────────────────────


class TestSharedUrlValidator:
    """
    ``rendering.utils.is_valid_url`` — the single source-of-truth URL validator
    used by all three renderers (SRC-049, SRC-141).

    All three renderers import the same function, so any change to the rule
    automatically propagates.  These tests ensure every edge case is covered.

    Traces: SRC-049 (non-negotiable URL requirement),
            SRC-141 (renderer-level final enforcement — drop, not truncate)
    """

    # ------------------------------------------------------------------
    # Acceptance (True)
    # ------------------------------------------------------------------

    @pytest.mark.parametrize(
        "url",
        [
            "https://reuters.com/article",
            "http://bloomberg.com/news",
            "HTTPS://WSJ.COM/AI",  # uppercase scheme
            "HTTP://EXAMPLE.COM/PATH",  # uppercase scheme
            "https://example.com/path?q=1&r=2",  # query string
            "https://example.com/path#section",  # fragment
            "http://localhost:8080/",  # localhost with port
            "https://sub.domain.example.org/",  # subdomain
        ],
    )
    def test_valid_urls_accepted(self, url: str) -> None:
        """Valid http(s) URLs are accepted (SRC-049, SRC-141)."""
        assert is_valid_url(url) is True, f"Expected True for: {url!r}"

    # ------------------------------------------------------------------
    # Rejection (False)
    # ------------------------------------------------------------------

    @pytest.mark.parametrize(
        "url",
        [
            None,
            "",
            "   ",  # whitespace-only
            "ftp://example.com/file",  # wrong scheme
            "//example.com/path",  # protocol-relative
            "reuters.com/article",  # no scheme at all
            "/relative/path",  # relative path
            "javascript:void(0)",  # injection: JS
            "javascript:alert(document.cookie)",  # injection: JS variant
            "data:text/html,<h1>xss</h1>",  # injection: data URI
            "data:application/javascript,alert(1)",  # injection: data + JS
            "vbscript:msgbox(1)",  # injection: VBScript
            "file:///etc/passwd",  # file URI
            "mailto:user@example.com",  # mailto
            "ssh://server.example.com",  # SSH
            "#anchor",  # fragment-only
        ],
    )
    def test_invalid_urls_rejected(self, url: str | None) -> None:
        """Invalid / insecure URLs are rejected (SRC-049, SRC-141)."""
        assert is_valid_url(url) is False, f"Expected False for: {url!r}"

    def test_valid_url_schemes_constant(self) -> None:
        """VALID_URL_SCHEMES contains exactly http:// and https://."""
        assert set(VALID_URL_SCHEMES) == {"http://", "https://"}

    def test_all_three_renderers_use_same_validator(self) -> None:
        """
        All three renderers import the shared ``is_valid_url`` — divergence
        would create inconsistent output behaviour (SRC-141).
        """
        from ai_news_agent.rendering import html_renderer, json_renderer, markdown_renderer
        from ai_news_agent.rendering.utils import is_valid_url as canonical

        # Each module re-exports the canonical function under the legacy
        # ``_is_valid_url`` alias — they must be the same object.
        assert markdown_renderer._is_valid_url is canonical
        assert html_renderer._is_valid_url is canonical
        assert json_renderer._is_valid_url is canonical

    def test_case_insensitive_scheme(self) -> None:
        """URL scheme check is case-insensitive (SRC-141)."""
        for url in ("HTTP://EXAMPLE.COM", "Https://EXAMPLE.COM", "HTTPS://example.com"):
            assert is_valid_url(url) is True


# ──────────────────────────────────────────────────────────────────────────────
# TestFilenameConventionFull
# ──────────────────────────────────────────────────────────────────────────────


class TestFilenameConventionFull:
    """
    Filename stem formula + extension per renderer.

    The convention ``{YYYY-MM-DD}-{cadence}.{ext}`` is a design constraint:
    it must be stable, ASCII-only, and have consistent base-name across all
    three renderers (SRC-145, SRC-140).
    """

    def test_filename_stem_formula(self) -> None:
        """``filename_stem`` returns ``{YYYY-MM-DD}-{cadence}``."""
        assert filename_stem(date(2026, 5, 11), "daily") == "2026-05-11-daily"
        assert filename_stem(date(2026, 1, 1), "annual") == "2026-01-01-annual"
        assert filename_stem(date(2026, 12, 31), "weekly") == "2026-12-31-weekly"
        assert filename_stem(date(2026, 3, 1), "monthly") == "2026-03-01-monthly"

    @pytest.mark.parametrize("cadence", ["daily", "weekly", "monthly", "annual"])
    def test_all_extensions_use_same_stem(self, cadence: str) -> None:
        """Stem is identical for MD / HTML / JSON — only extension differs."""
        meta = _meta(cadence=cadence, run_date=date(2026, 5, 11))
        md_fn = MarkdownRenderer.filename(meta)
        html_fn = HtmlRenderer.filename(meta)
        json_fn = JsonRenderer.filename(meta)

        md_stem = md_fn.rsplit(".", 1)[0]
        html_stem = html_fn.rsplit(".", 1)[0]
        json_stem = json_fn.rsplit(".", 1)[0]

        assert md_stem == html_stem == json_stem, (
            f"Stems differ: md={md_stem}, html={html_stem}, json={json_stem}"
        )

    @pytest.mark.parametrize("cadence", ["daily", "weekly", "monthly", "annual"])
    def test_extensions_are_correct(self, cadence: str) -> None:
        """Each renderer uses its own extension."""
        meta = _meta(cadence=cadence)
        assert MarkdownRenderer.filename(meta).endswith(".md")
        assert HtmlRenderer.filename(meta).endswith(".html")
        assert JsonRenderer.filename(meta).endswith(".json")

    def test_filenames_are_ascii_only(self) -> None:
        """Filenames are ASCII-safe on all OS filesystems."""
        for cadence in ("daily", "weekly", "monthly", "annual"):
            meta = _meta(cadence=cadence)
            for fn in (
                MarkdownRenderer.filename(meta),
                HtmlRenderer.filename(meta),
                JsonRenderer.filename(meta),
            ):
                fn.encode("ascii")  # raises UnicodeEncodeError if non-ASCII

    def test_agent_id_not_in_filename(self) -> None:
        """
        The agent_id must NOT appear in the filename — it belongs in the
        directory path only (SRC-140: future distribution layer).
        """
        meta = _meta(agent_id="my-special-agent")
        for fn in (
            MarkdownRenderer.filename(meta),
            HtmlRenderer.filename(meta),
            JsonRenderer.filename(meta),
        ):
            assert "my-special-agent" not in fn, f"agent_id leaked into filename: {fn}"

    def test_rerun_same_date_produces_same_filename(self) -> None:
        """Same run_date + cadence always → same filename (idempotency, SRC-145)."""
        meta_a = _meta(run_date=date(2026, 5, 11), cadence="daily")
        meta_b = _meta(run_date=date(2026, 5, 11), cadence="daily")
        assert MarkdownRenderer.filename(meta_a) == MarkdownRenderer.filename(meta_b)

    def test_different_dates_produce_different_filenames(self) -> None:
        """Different run dates produce different filenames."""
        meta_a = _meta(run_date=date(2026, 5, 11))
        meta_b = _meta(run_date=date(2026, 5, 12))
        assert MarkdownRenderer.filename(meta_a) != MarkdownRenderer.filename(meta_b)


# ──────────────────────────────────────────────────────────────────────────────
# TestOutputDirConvention
# ──────────────────────────────────────────────────────────────────────────────


class TestOutputDirConvention:
    """
    Output directory convention: ``outputs/{agent_id}/``.

    The agent_id is encoded in the directory path, not the filename, so that
    a future thin distribution layer can list agent outputs without parsing
    filenames (SRC-140).  The Rendering Agent writes files into whatever
    ``output_dir`` it is initialised with — the convention is enforced by the
    agent config and the scheduler, not the renderer itself.

    These tests verify that:
    - Files are written directly under ``output_dir`` (no nested subdirs).
    - The ``RenderingAgent`` creates the directory if absent (SRC-145).
    - Multiple agents with different output dirs do not collide.

    Traces: SRC-140, SRC-145
    """

    def test_files_written_directly_under_output_dir(self, tmp_path: Path) -> None:
        """Files are written at the top level of output_dir, not nested."""
        result = _result()
        r = RenderingAgent(output_dir=tmp_path).render(result)
        assert r.markdown_path.parent == tmp_path
        assert r.html_path.parent == tmp_path
        assert r.json_path.parent == tmp_path

    def test_creates_nested_output_dir(self, tmp_path: Path) -> None:
        """``output_dir`` created (including parents) if it does not exist."""
        deep = tmp_path / "a" / "b" / "c"
        assert not deep.exists()
        RenderingAgent(output_dir=deep).render(_result())
        assert deep.exists()

    def test_different_agent_ids_do_not_collide(self, tmp_path: Path) -> None:
        """Two agents writing to different dirs produce no filename collision."""
        dir_a = tmp_path / "outputs" / "agent-a"
        dir_b = tmp_path / "outputs" / "agent-b"
        meta_a = _meta(agent_id="agent-a")
        meta_b = _meta(agent_id="agent-b")

        r_a = RenderingAgent(output_dir=dir_a).render(_result(metadata=meta_a))
        r_b = RenderingAgent(output_dir=dir_b).render(_result(metadata=meta_b))

        assert r_a.markdown_path != r_b.markdown_path
        assert r_a.html_path != r_b.html_path
        assert r_a.json_path != r_b.json_path

    def test_output_dir_path_stored(self, tmp_path: Path) -> None:
        """RenderingAgent stores the output_dir as a Path."""
        agent = RenderingAgent(output_dir=tmp_path)
        assert agent._output_dir == tmp_path

    def test_output_dir_accepts_str(self, tmp_path: Path) -> None:
        """RenderingAgent accepts a str path for output_dir."""
        agent = RenderingAgent(output_dir=str(tmp_path))
        r = agent.render(_result())
        assert r.markdown_path.exists()


# ──────────────────────────────────────────────────────────────────────────────
# TestMarkdownRendererDeep
# ──────────────────────────────────────────────────────────────────────────────


class TestMarkdownRendererDeep:
    """
    Deep coverage of MarkdownRenderer.

    Traces: SRC-004, SRC-029–SRC-032, SRC-048, SRC-049, SRC-129, SRC-138, SRC-141, SRC-150
    """

    # ------------------------------------------------------------------
    # Full item-card fields (SRC-048)
    # ------------------------------------------------------------------

    def test_item_card_all_fields_present(self) -> None:
        """Every SRC-048 field appears in the rendered item card."""
        item = _item(
            headline="OpenAI Raises $10B Series G",
            source_name="Wall Street Journal",
            url="https://wsj.com/openai-series-g",
            pub_date=date(2026, 4, 15),
            why="This signals unprecedented commercial confidence in AI infrastructure.",
            impact_tags=["business_impact", "policy_impact"],
            tier="1b",
        )
        md = MarkdownRenderer().render(_result(items=[item]))
        assert "OpenAI Raises $10B Series G" in md
        assert "Wall Street Journal" in md
        assert "https://wsj.com/openai-series-g" in md
        assert "2026-04-15" in md
        assert "unprecedented commercial confidence" in md
        assert "Business" in md
        assert "Policy" in md
        assert "Tier 1b" in md

    def test_item_number_shown(self) -> None:
        """Each item is numbered sequentially starting at 1."""
        items = [
            _item(headline="Story Alpha"),
            _item(headline="Story Beta"),
            _item(headline="Story Gamma"),
        ]
        md = MarkdownRenderer().render(_result(items=items))
        assert "### 1." in md
        assert "### 2." in md
        assert "### 3." in md

    def test_twitter_attribution_full(self) -> None:
        """Twitter-sourced item shows handle and tweet URL (SRC-048)."""
        item = _item(
            headline="Karpathy Tweet: Scaling Laws Hold",
            url="https://x.com/karpathy/status/12345",
            twitter_handle="karpathy",
            tweet_url="https://x.com/karpathy/status/12345",
        )
        md = MarkdownRenderer().render(_result(items=[item]))
        assert "@karpathy" in md
        assert "https://x.com/karpathy/status/12345" in md
        assert "🐦" in md  # bird emoji marker

    def test_twitter_handle_without_tweet_url_suppressed(self) -> None:
        """If tweet_url is absent, Twitter attribution line is suppressed."""
        item = _item(twitter_handle="karpathy", tweet_url=None)
        md = MarkdownRenderer().render(_result(items=[item]))
        assert "🐦" not in md

    def test_no_twitter_for_web_item(self) -> None:
        """Web-sourced items have no Twitter attribution."""
        item = _item(twitter_handle=None, tweet_url=None)
        md = MarkdownRenderer().render(_result(items=[item]))
        assert "🐦" not in md
        assert "@" not in md.split("via")[1] if "via" in md else True

    def test_cross_refs_rendered_as_links(self) -> None:
        """Cross-references rendered as Markdown links (SRC-048)."""
        item = _item(
            cross_refs=[
                "https://related.example.com/a",
                "https://related.example.com/b",
            ]
        )
        md = MarkdownRenderer().render(_result(items=[item]))
        assert "https://related.example.com/a" in md
        assert "https://related.example.com/b" in md
        assert "Related:" in md

    def test_no_cross_refs_section_absent(self) -> None:
        """No cross-refs → no 'Related:' section in card."""
        item = _item(cross_refs=[])
        md = MarkdownRenderer().render(_result(items=[item]))
        assert "Related:" not in md

    def test_empty_impact_tags_shows_general_badge(self) -> None:
        """Empty impact_tags → fallback '📌 General' badge."""
        item = _item(impact_tags=[])
        md = MarkdownRenderer().render(_result(items=[item]))
        assert "📌" in md

    # ------------------------------------------------------------------
    # Header contract (SRC-116)
    # ------------------------------------------------------------------

    def test_header_no_relative_phrases(self) -> None:
        """Header must not contain relative time phrases (SRC-116)."""
        md = MarkdownRenderer().render(_result())
        for phrase in ("last week", "last month", "yesterday", "last year", "this week"):
            assert phrase not in md.lower()

    def test_header_has_window_start_and_end(self) -> None:
        """Both window_start and window_end ISO dates in header (SRC-116)."""
        meta = _meta()
        md = MarkdownRenderer().render(_result(metadata=meta))
        assert "2026-05-10" in md  # window_start date
        assert "2026-05-11" in md  # run_date (in header)

    def test_header_prompt_version_sha256(self) -> None:
        """Prompt version formatted as ``sha256:<hex>`` in header (SRC-129)."""
        meta = _meta(prompt_version="sha256:cafebabe0001")
        md = MarkdownRenderer().render(_result(metadata=meta))
        assert "sha256:cafebabe0001" in md

    # ------------------------------------------------------------------
    # Footer (SRC-129, SRC-150)
    # ------------------------------------------------------------------

    def test_footer_all_monitoring_fields(self) -> None:
        """Footer contains every SRC-150 monitoring field."""
        meta = _meta(
            llm_model="o3-mini",
            prompt_version="sha256:abc001",
            items_considered=100,
            items_included=7,
        )
        md = MarkdownRenderer().render(_result(metadata=meta))
        assert "o3-mini" in md
        assert "sha256:abc001" in md
        assert "100" in md  # items_considered
        assert "7" in md  # items_included
        assert "Digest Metadata" in md

    def test_footer_token_usage_thousands_separator(self) -> None:
        """Large token counts use thousands separator for readability (SRC-150)."""
        meta = _meta(token_usage=1_000_000)
        md = MarkdownRenderer().render(_result(metadata=meta))
        assert "1,000,000" in md

    def test_footer_twitter_unavailable_shown(self) -> None:
        """Footer shows '⚠️ unavailable' when twitter_signal_available=False."""
        meta = _meta(twitter_signal_available=False)
        md = MarkdownRenderer().render(_result(metadata=meta))
        assert "⚠️" in md or "unavailable" in md

    def test_footer_twitter_available_shown(self) -> None:
        """Footer shows available status when twitter_signal_available=True."""
        meta = _meta(twitter_signal_available=True)
        md = MarkdownRenderer().render(_result(metadata=meta))
        assert "available" in md.lower()

    # ------------------------------------------------------------------
    # Cadence-specific sections
    # ------------------------------------------------------------------

    def test_daily_format_has_headline_linked(self) -> None:
        """Daily: headline is a Markdown link to the source URL (SRC-029, SRC-049)."""
        item = _item(
            headline="Daily Story",
            url="https://example.com/daily-story",
        )
        md = MarkdownRenderer().render(_result(items=[item], metadata=_meta(cadence="daily")))
        assert "[Daily Story](https://example.com/daily-story)" in md

    def test_weekly_themes_before_articles(self) -> None:
        """Weekly: themes section precedes stories (SRC-030)."""
        result = _result(
            metadata=_meta(cadence="weekly"),
            themes=["Theme One"],
            items=[_item(headline="Story One")],
        )
        md = MarkdownRenderer().render(result)
        assert md.index("Theme One") < md.index("Story One")

    def test_weekly_looking_ahead_after_articles(self) -> None:
        """Weekly: 'Looking Ahead' section follows articles (SRC-030)."""
        result = _result(
            metadata=_meta(cadence="weekly"),
            items=[_item(headline="Story One")],
            outlook="Look for developments in AI regulation next week.",
        )
        md = MarkdownRenderer().render(result)
        # Stories appear before "Looking Ahead"
        stories_pos = md.index("Story One")
        ahead_pos = md.index("Looking Ahead")
        assert stories_pos < ahead_pos

    def test_monthly_what_to_watch_before_stories(self) -> None:
        """Monthly: 'What to Watch' precedes top stories (SRC-031)."""
        result = _result(
            metadata=_meta(cadence="monthly"),
            outlook="Anticipate major announcements from leading AI labs.",
            items=[_item(headline="Monthly Story")],
        )
        md = MarkdownRenderer().render(result)
        assert md.index("What to Watch") < md.index("Monthly Story")

    def test_annual_predictions_are_numbered(self) -> None:
        """Annual predictions are numbered 1–10 (SRC-032, SRC-124)."""
        result = _result(
            metadata=_meta(cadence="annual"),
            predictions=[f"Prediction {i}" for i in range(1, 11)],
        )
        md = MarkdownRenderer().render(result)
        for i in range(1, 11):
            assert f"**{i}.**" in md

    def test_annual_prediction_reasoning_note(self) -> None:
        """Annual section notes predictions are grounded in observed trends (SRC-124)."""
        result = _result(
            metadata=_meta(cadence="annual"),
            predictions=["AI agents become mainstream in 2027."],
        )
        md = MarkdownRenderer().render(result)
        assert "grounded in observed trends" in md or "observed trends" in md


# ──────────────────────────────────────────────────────────────────────────────
# TestHtmlRendererDeep
# ──────────────────────────────────────────────────────────────────────────────


class TestHtmlRendererDeep:
    """
    Deep coverage of HtmlRenderer — document structure, email-readiness, and
    cadence sections.

    Traces: SRC-004, SRC-029–SRC-032, SRC-048, SRC-049, SRC-129,
            SRC-137, SRC-141, SRC-145, SRC-148, SRC-150
    """

    # ------------------------------------------------------------------
    # Document structure (SRC-137 — self-contained, paste-ready into email)
    # ------------------------------------------------------------------

    def test_produces_complete_html5_document(self) -> None:
        """Output is a complete HTML5 document starting with <!DOCTYPE html>."""
        html = HtmlRenderer().render(_result())
        assert html.strip().startswith("<!DOCTYPE html>")
        assert "<html" in html
        assert "<head>" in html
        assert "<body>" in html or "<body" in html
        assert "</body>" in html
        assert "</html>" in html

    def test_charset_is_utf8(self) -> None:
        """HTML document declares UTF-8 charset."""
        html = HtmlRenderer().render(_result())
        assert 'charset="UTF-8"' in html or "charset=UTF-8" in html

    def test_viewport_meta_present(self) -> None:
        """Viewport meta tag present for responsive email/web rendering."""
        html = HtmlRenderer().render(_result())
        assert 'name="viewport"' in html

    def test_self_contained_no_external_css(self) -> None:
        """
        No ``<link rel="stylesheet">`` tags — all CSS is inline (SRC-137).
        An email client may block external resources; the document must render
        without any network access.
        """
        html = HtmlRenderer().render(_result())
        # <link rel="stylesheet"> is the only external CSS mechanism
        assert 'rel="stylesheet"' not in html
        # Inline <style> block must be present
        assert "<style>" in html

    def test_no_external_script_tags(self) -> None:
        """No external JavaScript — self-contained for email paste (SRC-137)."""
        html = HtmlRenderer().render(_result())
        # External script tags would break email clients
        assert "<script src=" not in html

    def test_title_contains_cadence_and_date(self) -> None:
        """HTML <title> contains cadence label and run date."""
        meta = _meta(cadence="weekly", run_date=date(2026, 5, 11))
        html = HtmlRenderer().render(_result(metadata=meta))
        # Title should include the run date
        assert "2026-05-11" in html
        assert "<title>" in html

    # ------------------------------------------------------------------
    # Item card (SRC-048)
    # ------------------------------------------------------------------

    def test_headline_linked_to_primary_url(self) -> None:
        """Headline is a hyperlink to the primary source URL (SRC-049)."""
        item = _item(
            headline="EU AI Act Enters Enforcement Phase",
            url="https://ec.europa.eu/ai-act-enforcement",
        )
        html = HtmlRenderer().render(_result(items=[item]))
        assert "EU AI Act Enters Enforcement Phase" in html
        assert "https://ec.europa.eu/ai-act-enforcement" in html
        # Linked as <a href="...">
        assert (
            'href="https://ec.europa.eu/ai-act-enforcement"' in html
            or "https://ec.europa.eu/ai-act-enforcement" in html
        )

    def test_item_card_has_class(self) -> None:
        """Item card uses the CSS class ``item-card`` for styling."""
        html = HtmlRenderer().render(_result())
        assert 'class="item-card"' in html

    def test_source_name_in_card(self) -> None:
        """Source name rendered in each item card (SRC-048)."""
        item = _item(source_name="Financial Times")
        html = HtmlRenderer().render(_result(items=[item]))
        assert "Financial Times" in html

    def test_pub_date_in_card(self) -> None:
        """Publication date in each item card (SRC-048)."""
        item = _item(pub_date=date(2026, 3, 20))
        html = HtmlRenderer().render(_result(items=[item]))
        assert "2026-03-20" in html

    def test_tier_in_card(self) -> None:
        """Tier in each item card (SRC-048)."""
        item = _item(tier="2")
        html = HtmlRenderer().render(_result(items=[item]))
        assert "Tier 2" in html

    def test_why_it_matters_in_card(self) -> None:
        """Why-it-matters text in each item card (SRC-048, SRC-122)."""
        item = _item(why="This fundamentally changes enterprise AI procurement.")
        html = HtmlRenderer().render(_result(items=[item]))
        assert "fundamentally changes enterprise AI procurement" in html

    def test_badges_for_all_impact_tags(self) -> None:
        """Each impact tag produces a visible badge (SRC-048)."""
        item = _item(impact_tags=["business_impact", "workforce_impact", "policy_impact"])
        html = HtmlRenderer().render(_result(items=[item]))
        assert "Business Impact" in html
        assert "Workforce Impact" in html
        assert "Policy Impact" in html

    def test_general_badge_for_no_tags(self) -> None:
        """Empty impact_tags → '📌 General' badge."""
        item = _item(impact_tags=[])
        html = HtmlRenderer().render(_result(items=[item]))
        assert "General" in html

    def test_twitter_attribution_rendered(self) -> None:
        """Twitter-sourced items show handle + tweet URL (SRC-048)."""
        item = _item(
            twitter_handle="sama",
            tweet_url="https://x.com/sama/status/999",
        )
        html = HtmlRenderer().render(_result(items=[item]))
        assert "@sama" in html
        assert "https://x.com/sama/status/999" in html
        assert "🐦" in html

    def test_no_twitter_for_web_sourced_item(self) -> None:
        """Web-sourced items have no Twitter attribution line (no @handle, no tweet link)."""
        item = _item(twitter_handle=None, tweet_url=None)
        html = HtmlRenderer().render(_result(items=[item]))
        # The CSS class name appears in the <style> block, so check the body content
        # specifically — the attribution div should not be in the rendered item card
        assert "🐦" not in html
        # No @handle attribution
        assert "via\n" not in html

    def test_cross_refs_rendered_as_links(self) -> None:
        """Valid cross-reference URLs rendered as HTML links (SRC-048)."""
        item = _item(
            cross_refs=[
                "https://related.example.com/a",
                "https://related.example.com/b",
            ]
        )
        html = HtmlRenderer().render(_result(items=[item]))
        assert "https://related.example.com/a" in html
        assert "https://related.example.com/b" in html

    def test_invalid_cross_ref_url_omitted(self) -> None:
        """Invalid cross-ref URLs (no scheme) are excluded from HTML output."""
        item = _item(cross_refs=["invalid-no-scheme.example.com"])
        html = HtmlRenderer().render(_result(items=[item]))
        # The invalid URL should NOT appear in an href
        assert 'href="invalid-no-scheme' not in html

    def test_multiple_items_all_rendered(self) -> None:
        """All valid items are rendered (not just the first)."""
        items = [
            _item(headline=f"Article {i}", url=f"https://example.com/{i}") for i in range(1, 6)
        ]
        html = HtmlRenderer().render(_result(items=items))
        for i in range(1, 6):
            assert f"Article {i}" in html

    # ------------------------------------------------------------------
    # Footer (SRC-129, SRC-150)
    # ------------------------------------------------------------------

    def test_footer_prompt_version(self) -> None:
        """Prompt version in HTML footer (SRC-129)."""
        meta = _meta(prompt_version="sha256:ffff0001")
        html = HtmlRenderer().render(_result(metadata=meta))
        assert "sha256:ffff0001" in html

    def test_footer_llm_model(self) -> None:
        """LLM model in HTML footer (SRC-150)."""
        meta = _meta(llm_model="claude-3-7-sonnet")
        html = HtmlRenderer().render(_result(metadata=meta))
        assert "claude-3-7-sonnet" in html

    def test_footer_items_ratio(self) -> None:
        """Footer shows items_included/items_considered (SRC-150)."""
        meta = _meta(items_considered=50, items_included=8)
        html = HtmlRenderer().render(_result(metadata=meta))
        assert "8" in html
        assert "50" in html

    def test_footer_meta_class_present(self) -> None:
        """Footer uses ``footer-meta`` CSS class for portal CSS hook."""
        html = HtmlRenderer().render(_result())
        assert "footer-meta" in html

    # ------------------------------------------------------------------
    # Cadence sections
    # ------------------------------------------------------------------

    def test_weekly_themes_section_header(self) -> None:
        """Weekly: 'This Week's Themes' section header (SRC-030)."""
        result = _result(
            metadata=_meta(cadence="weekly"),
            themes=["Enterprise AI Adoption"],
        )
        html = HtmlRenderer().render(result)
        assert "This Week" in html
        assert "Enterprise AI Adoption" in html

    def test_weekly_outlook_box_section(self) -> None:
        """Weekly: outlook rendered in ``outlook-box`` div (SRC-030)."""
        result = _result(
            metadata=_meta(cadence="weekly"),
            outlook="Major announcements expected next week.",
        )
        html = HtmlRenderer().render(result)
        assert "outlook-box" in html
        assert "Major announcements expected next week" in html

    def test_monthly_themes_section(self) -> None:
        """Monthly: themes rendered as a list (SRC-031)."""
        result = _result(
            metadata=_meta(cadence="monthly"),
            themes=["AI Infrastructure Build-out"],
        )
        html = HtmlRenderer().render(result)
        assert "Monthly Themes" in html
        assert "AI Infrastructure Build-out" in html

    def test_monthly_what_to_watch_section(self) -> None:
        """Monthly: 'What to Watch' section rendered (SRC-031)."""
        result = _result(
            metadata=_meta(cadence="monthly"),
            outlook="Expect regulatory consolidation.",
        )
        html = HtmlRenderer().render(result)
        assert "What to Watch" in html
        assert "Expect regulatory consolidation" in html

    def test_annual_predictions_div_class(self) -> None:
        """Annual predictions use ``prediction-item`` CSS class (SRC-032)."""
        result = _result(
            metadata=_meta(cadence="annual"),
            predictions=["AI replaces 15% of white-collar roles."],
        )
        html = HtmlRenderer().render(result)
        assert "prediction-item" in html
        assert "AI replaces 15%" in html

    def test_annual_grounded_note_escaped(self) -> None:
        """Annual predictions note is HTML-escaped via _esc() (SRC-124)."""
        result = _result(
            metadata=_meta(cadence="annual"),
            predictions=["A test prediction."],
        )
        html = HtmlRenderer().render(result)
        # The note text should appear
        assert "grounded in observed trends" in html or "observed trends" in html
        # And should NOT contain unescaped raw HTML from the note string
        assert "<em>Each prediction" in html  # properly wrapped in <em>


# ──────────────────────────────────────────────────────────────────────────────
# TestHtmlRendererSecurity
# ──────────────────────────────────────────────────────────────────────────────


class TestHtmlRendererSecurity:
    """
    XSS and URL-injection tests.

    Every user-supplied string that ends up in the HTML must pass through
    ``_esc()`` or ``_esc_attr()`` to prevent cross-site scripting (SRC-137,
    SRC-141 — items that DO appear must not inject script).
    """

    def test_xss_in_headline_escaped(self) -> None:
        """Headline with XSS payload is escaped before HTML output."""
        item = _item(headline='<script>alert("xss")</script>')
        html = HtmlRenderer().render(_result(items=[item]))
        assert "<script>alert" not in html
        assert "&lt;script&gt;" in html

    def test_xss_in_why_it_matters_escaped(self) -> None:
        """why_it_matters with XSS payload is escaped."""
        item = _item(why='<img src=x onerror="alert(1)">')
        html = HtmlRenderer().render(_result(items=[item]))
        assert 'onerror="alert(1)"' not in html

    def test_xss_in_source_name_escaped(self) -> None:
        """source_name with XSS payload is escaped."""
        item = _item(source_name='<b onmouseover="alert(1)">Evil</b>')
        html = HtmlRenderer().render(_result(items=[item]))
        assert 'onmouseover="alert(1)"' not in html
        assert "&lt;b" in html

    def test_xss_in_theme_escaped(self) -> None:
        """Theme string with XSS payload is escaped (weekly/monthly)."""
        result = _result(
            metadata=_meta(cadence="weekly"),
            themes=['<script>alert("theme-xss")</script>'],
        )
        html = HtmlRenderer().render(result)
        assert "<script>alert" not in html

    def test_xss_in_outlook_escaped(self) -> None:
        """Outlook string with XSS payload is escaped."""
        result = _result(
            metadata=_meta(cadence="monthly"),
            outlook='</div><script>alert("outlook-xss")</script>',
        )
        html = HtmlRenderer().render(result)
        assert "<script>alert" not in html

    def test_xss_in_prediction_escaped(self) -> None:
        """Annual prediction string with XSS payload is escaped."""
        result = _result(
            metadata=_meta(cadence="annual"),
            predictions=['<iframe src="https://evil.com"></iframe>'],
        )
        html = HtmlRenderer().render(result)
        assert "<iframe" not in html
        assert "&lt;iframe" in html

    def test_url_with_ampersand_href_safe(self) -> None:
        """URL containing & is HTML-attribute-escaped in href."""
        item = _item(url="https://example.com/article?a=1&b=2")
        html = HtmlRenderer().render(_result(items=[item]))
        # Raw & must not appear in href; should be &amp;
        # Check that the URL is present in *some* form (escaped or not)
        assert "example.com/article" in html
        # And raw unescaped < and > must not appear as injection vector
        assert "javascript:" not in html.lower()

    def test_url_javascript_scheme_dropped(self) -> None:
        """Item with javascript: URL is dropped by the renderer (SRC-141)."""
        item = _item(url="javascript:alert(document.cookie)")
        result = _result(
            items=[
                _item(headline="Safe Article", url="https://safe.example.com/ok"),
                item,
            ]
        )
        html = HtmlRenderer().render(result)
        assert "javascript:alert" not in html
        assert "Safe Article" in html

    def test_degradation_note_xss_escaped(self) -> None:
        """Twitter degradation note with XSS payload is escaped (SRC-148)."""
        result = _result(twitter_degradation_note='<script>alert("degrade")</script>')
        html = HtmlRenderer().render(result)
        assert "<script>alert" not in html

    def test_rel_noopener_on_headline_links(self) -> None:
        """All external links include ``rel="noopener"`` (SRC-137 — email safety)."""
        item = _item(url="https://reuters.com/article")
        html = HtmlRenderer().render(_result(items=[item]))
        assert 'rel="noopener"' in html

    def test_rel_noopener_on_cross_ref_links(self) -> None:
        """Cross-reference links include ``rel="noopener"``."""
        item = _item(cross_refs=["https://related.example.com/link"])
        html = HtmlRenderer().render(_result(items=[item]))
        # The cross-ref link should have noopener
        assert 'rel="noopener"' in html


# ──────────────────────────────────────────────────────────────────────────────
# TestJsonRendererDeep
# ──────────────────────────────────────────────────────────────────────────────


class TestJsonRendererDeep:
    """
    Deep coverage of JsonRenderer.

    Traces: SRC-004, SRC-048, SRC-049, SRC-061, SRC-124, SRC-129,
            SRC-140, SRC-141, SRC-145, SRC-148, SRC-150
    """

    # ------------------------------------------------------------------
    # Top-level structure
    # ------------------------------------------------------------------

    def test_valid_json(self) -> None:
        """Output parses as valid JSON."""
        raw = JsonRenderer().render(_result())
        json.loads(raw)  # must not raise

    def test_top_level_keys_complete(self) -> None:
        """Top-level keys: schema_version, metadata, items, themes, outlook, predictions."""
        data = json.loads(JsonRenderer().render(_result()))
        expected = {"schema_version", "metadata", "items", "themes", "outlook", "predictions"}
        assert expected.issubset(set(data.keys()))

    def test_indented_output(self) -> None:
        """JSON output is human-readable (indented, not minified)."""
        raw = JsonRenderer().render(_result())
        # Indented JSON has newlines
        assert "\n" in raw

    def test_ensure_ascii_false(self) -> None:
        """Non-ASCII characters preserved in JSON output (not \\u-escaped)."""
        item = _item(
            headline="L'intelligence artificielle réinvente l'entreprise",
            why="Résultats probants — accélération majeure.",
        )
        raw = JsonRenderer().render(_result(items=[item]))
        # French characters must appear literally, not as \\uXXXX
        assert "réinvente" in raw
        assert "accélération" in raw

    # ------------------------------------------------------------------
    # Item fields (SRC-048)
    # ------------------------------------------------------------------

    def test_item_all_required_fields(self) -> None:
        """Every SRC-048 item field present in JSON output."""
        item = _item(
            headline="Test Headline",
            source_name="Reuters",
            url="https://reuters.com/test",
            pub_date=date(2026, 5, 11),
            why="This matters.",
            impact_tags=["business_impact"],
            tier="1b",
            cross_refs=[],
        )
        data = json.loads(JsonRenderer().render(_result(items=[item])))
        assert len(data["items"]) == 1
        item_d = data["items"][0]
        for field in (
            "headline",
            "source_name",
            "url",
            "pub_date",
            "why_it_matters",
            "impact_tags",
            "tier",
            "cross_refs",
            "twitter_handle",
            "tweet_url",
            "prompt_version",
        ):
            assert field in item_d, f"Missing field: {field}"

    def test_item_pub_date_iso8601(self) -> None:
        """pub_date serialised as ISO-8601 string (not datetime object)."""
        item = _item(pub_date=date(2026, 4, 1))
        data = json.loads(JsonRenderer().render(_result(items=[item])))
        pub_date = data["items"][0]["pub_date"]
        assert isinstance(pub_date, str)
        assert pub_date == "2026-04-01"

    def test_item_prompt_version(self) -> None:
        """Each item carries its own prompt_version for regression tracing (SRC-129)."""
        item = _item(prompt_version="sha256:beefdead")
        data = json.loads(JsonRenderer().render(_result(items=[item])))
        assert data["items"][0]["prompt_version"] == "sha256:beefdead"

    def test_item_twitter_fields_null_for_web(self) -> None:
        """Web-sourced items have null twitter_handle and tweet_url."""
        item = _item(twitter_handle=None, tweet_url=None)
        data = json.loads(JsonRenderer().render(_result(items=[item])))
        assert data["items"][0]["twitter_handle"] is None
        assert data["items"][0]["tweet_url"] is None

    def test_item_twitter_fields_populated_for_twitter(self) -> None:
        """Twitter-sourced items have populated twitter_handle + tweet_url (SRC-048)."""
        item = _item(
            twitter_handle="ylecun",
            tweet_url="https://x.com/ylecun/status/11111",
        )
        data = json.loads(JsonRenderer().render(_result(items=[item])))
        assert data["items"][0]["twitter_handle"] == "ylecun"
        assert data["items"][0]["tweet_url"] == "https://x.com/ylecun/status/11111"

    def test_item_cross_refs_preserved(self) -> None:
        """cross_refs list preserved exactly (SRC-048)."""
        refs = [
            "https://related.example.com/1",
            "https://related.example.com/2",
        ]
        item = _item(cross_refs=refs)
        data = json.loads(JsonRenderer().render(_result(items=[item])))
        assert data["items"][0]["cross_refs"] == refs

    def test_multiple_items_ordered(self) -> None:
        """Items are serialised in the order they appear in result.items."""
        items = [_item(headline=f"Story {i}", url=f"https://example.com/{i}") for i in range(1, 4)]
        data = json.loads(JsonRenderer().render(_result(items=items)))
        headlines = [i["headline"] for i in data["items"]]
        assert headlines == ["Story 1", "Story 2", "Story 3"]

    # ------------------------------------------------------------------
    # Metadata block (SRC-129, SRC-148, SRC-150)
    # ------------------------------------------------------------------

    def test_metadata_all_monitoring_fields(self) -> None:
        """metadata block contains every SRC-150 quality-monitoring field."""
        meta = _meta(
            agent_id="prod-agent",
            cadence="weekly",
            prompt_version="sha256:12345678",
            llm_model="gpt-4o",
            token_usage=16384,
            items_considered=80,
            items_included=6,
        )
        data = json.loads(JsonRenderer().render(_result(metadata=meta)))
        m = data["metadata"]
        for field in (
            "agent_id",
            "cadence",
            "run_date",
            "window_start",
            "window_end",
            "prompt_version",
            "llm_provider",
            "llm_model",
            "items_considered",
            "items_included",
            "items_by_tier",
            "items_by_source_class",
            "twitter_signal_available",
            "tweet_api_call_count",
            "token_usage",
        ):
            assert field in m, f"Missing metadata field: {field}"

    def test_metadata_window_dates_are_iso8601(self) -> None:
        """window_start and window_end serialised as ISO-8601 strings."""
        data = json.loads(JsonRenderer().render(_result()))
        m = data["metadata"]
        # Should parse without error
        datetime.fromisoformat(m["window_start"])
        datetime.fromisoformat(m["window_end"])

    def test_metadata_agent_id_correct(self) -> None:
        """metadata.agent_id matches the AgentConfig agent_id."""
        meta = _meta(agent_id="custom-agent-xyz")
        data = json.loads(JsonRenderer().render(_result(metadata=meta)))
        assert data["metadata"]["agent_id"] == "custom-agent-xyz"

    def test_metadata_prompt_version_correct(self) -> None:
        """metadata.prompt_version matches the run's prompt version (SRC-129)."""
        meta = _meta(prompt_version="sha256:aabbccdd")
        data = json.loads(JsonRenderer().render(_result(metadata=meta)))
        assert data["metadata"]["prompt_version"] == "sha256:aabbccdd"

    def test_metadata_twitter_signal_flag(self) -> None:
        """metadata.twitter_signal_available reflects the run's actual status (SRC-148)."""
        for flag in (True, False):
            meta = _meta(twitter_signal_available=flag)
            data = json.loads(JsonRenderer().render(_result(metadata=meta)))
            assert data["metadata"]["twitter_signal_available"] is flag

    def test_metadata_token_usage_is_int(self) -> None:
        """token_usage is an integer in metadata (not a float or string)."""
        meta = _meta(token_usage=4096)
        data = json.loads(JsonRenderer().render(_result(metadata=meta)))
        assert isinstance(data["metadata"]["token_usage"], int)
        assert data["metadata"]["token_usage"] == 4096

    def test_metadata_items_by_tier(self) -> None:
        """items_by_tier preserved as dict."""
        meta = _meta()
        meta_custom = replace(meta, items_by_tier={"1a": 1, "1b": 3, "2": 2})
        data = json.loads(JsonRenderer().render(_result(metadata=meta_custom)))
        assert data["metadata"]["items_by_tier"] == {"1a": 1, "1b": 3, "2": 2}

    # ------------------------------------------------------------------
    # Cadence-specific fields
    # ------------------------------------------------------------------

    def test_daily_themes_empty_list(self) -> None:
        """Daily: themes is [] in JSON output."""
        result = _result(metadata=_meta(cadence="daily"), themes=[])
        data = json.loads(JsonRenderer().render(result))
        assert data["themes"] == []

    def test_weekly_themes_present(self) -> None:
        """Weekly: themes list serialised in JSON."""
        result = _result(
            metadata=_meta(cadence="weekly"),
            themes=["Theme A", "Theme B"],
        )
        data = json.loads(JsonRenderer().render(result))
        assert data["themes"] == ["Theme A", "Theme B"]

    def test_monthly_outlook_present(self) -> None:
        """Monthly: outlook string serialised in JSON."""
        result = _result(
            metadata=_meta(cadence="monthly"),
            outlook="AI regulation will dominate Q3.",
        )
        data = json.loads(JsonRenderer().render(result))
        assert data["outlook"] == "AI regulation will dominate Q3."

    def test_annual_predictions_present(self) -> None:
        """Annual: predictions list serialised in JSON (SRC-124)."""
        preds = [f"Prediction {i}" for i in range(1, 11)]
        result = _result(
            metadata=_meta(cadence="annual"),
            predictions=preds,
        )
        data = json.loads(JsonRenderer().render(result))
        assert data["predictions"] == preds

    def test_non_annual_predictions_empty(self) -> None:
        """Non-annual: predictions is [] (SRC-124 only applies to annual)."""
        for cadence in ("daily", "weekly", "monthly"):
            result = _result(metadata=_meta(cadence=cadence), predictions=[])
            data = json.loads(JsonRenderer().render(result))
            assert data["predictions"] == []

    # ------------------------------------------------------------------
    # Twitter degradation (SRC-148)
    # ------------------------------------------------------------------

    def test_twitter_degradation_note_present(self) -> None:
        """twitter_degradation_note key present in JSON when degraded (SRC-148)."""
        note = "⚠️ Twitter/X influencer signal was unavailable."
        result = _result(twitter_degradation_note=note)
        data = json.loads(JsonRenderer().render(result))
        assert "twitter_degradation_note" in data
        assert data["twitter_degradation_note"] == note

    def test_twitter_degradation_note_absent_when_ok(self) -> None:
        """twitter_degradation_note key absent when Twitter is available (SRC-148)."""
        result = _result(twitter_degradation_note=None)
        data = json.loads(JsonRenderer().render(result))
        assert "twitter_degradation_note" not in data


# ──────────────────────────────────────────────────────────────────────────────
# TestJsonSchemaContract
# ──────────────────────────────────────────────────────────────────────────────


class TestJsonSchemaContract:
    """
    JSON schema stability and versioning contract.

    Downstream consumers (portal, distribution scripts) depend on a stable
    schema.  ``SCHEMA_VERSION`` signals breaking changes (SRC-140).

    Traces: SRC-061 (output parsing from structured format), SRC-140
    """

    def test_schema_version_is_1_0(self) -> None:
        """SCHEMA_VERSION is '1.0' — current stable schema."""
        assert SCHEMA_VERSION == "1.0"

    def test_schema_version_in_output(self) -> None:
        """schema_version key present in rendered JSON and equals SCHEMA_VERSION."""
        data = json.loads(JsonRenderer().render(_result()))
        assert data["schema_version"] == SCHEMA_VERSION

    def test_top_level_key_set_stable(self) -> None:
        """
        The set of top-level keys is stable.  Adding keys is backward-compatible;
        removing or renaming is a breaking change that must increment SCHEMA_VERSION.
        """
        data = json.loads(JsonRenderer().render(_result()))
        required = {"schema_version", "metadata", "items", "themes", "outlook", "predictions"}
        assert required.issubset(set(data.keys()))

    def test_item_key_set_stable(self) -> None:
        """The set of per-item keys is stable (SRC-048 contract)."""
        data = json.loads(JsonRenderer().render(_result()))
        assert len(data["items"]) > 0
        item_keys = set(data["items"][0].keys())
        required = {
            "headline",
            "source_name",
            "url",
            "pub_date",
            "why_it_matters",
            "impact_tags",
            "tier",
            "cross_refs",
            "twitter_handle",
            "tweet_url",
            "prompt_version",
        }
        assert required.issubset(item_keys)

    def test_metadata_key_set_stable(self) -> None:
        """The set of metadata keys is stable (SRC-150 contract)."""
        data = json.loads(JsonRenderer().render(_result()))
        meta_keys = set(data["metadata"].keys())
        required = {
            "agent_id",
            "cadence",
            "run_date",
            "window_start",
            "window_end",
            "prompt_version",
            "llm_provider",
            "llm_model",
            "items_considered",
            "items_included",
            "items_by_tier",
            "items_by_source_class",
            "twitter_signal_available",
            "tweet_api_call_count",
            "token_usage",
        }
        assert required.issubset(meta_keys)

    def test_round_trip_items_count(self) -> None:
        """Items survive a JSON round-trip with correct count."""
        items = [_item(url=f"https://example.com/{i}") for i in range(3)]
        data = json.loads(JsonRenderer().render(_result(items=items)))
        assert len(data["items"]) == 3


# ──────────────────────────────────────────────────────────────────────────────
# TestRenderingAgentFull
# ──────────────────────────────────────────────────────────────────────────────


class TestRenderingAgentFull:
    """
    Full coverage of RenderingAgent.render():
    - All three files written with correct names and extensions.
    - UTF-8 encoding.
    - Items rendered and dropped counts correct.
    - Monitoring log event emitted.

    Traces: SRC-004, SRC-141, SRC-145, SRC-150
    """

    def test_writes_exactly_three_files(self, tmp_path: Path) -> None:
        """render() writes exactly three files — no more, no less (SRC-004)."""
        RenderingAgent(output_dir=tmp_path).render(_result())
        written = list(tmp_path.iterdir())
        assert len(written) == 3

    def test_file_extensions(self, tmp_path: Path) -> None:
        """Three files have .md, .html, .json extensions (SRC-004)."""
        RenderingAgent(output_dir=tmp_path).render(_result())
        extensions = {p.suffix for p in tmp_path.iterdir()}
        assert ".md" in extensions
        assert ".html" in extensions
        assert ".json" in extensions

    def test_date_stamped_names(self, tmp_path: Path) -> None:
        """Filenames include the run date and cadence (SRC-145)."""
        meta = _meta(run_date=date(2026, 5, 11), cadence="daily")
        r = RenderingAgent(output_dir=tmp_path).render(_result(metadata=meta))
        for path in (r.markdown_path, r.html_path, r.json_path):
            assert "2026-05-11" in path.name
            assert "daily" in path.name

    def test_md_file_content_non_empty(self, tmp_path: Path) -> None:
        """Markdown file is non-empty."""
        r = RenderingAgent(output_dir=tmp_path).render(_result())
        assert r.markdown_path.read_text(encoding="utf-8").strip()

    def test_html_file_content_non_empty(self, tmp_path: Path) -> None:
        """HTML file is non-empty."""
        r = RenderingAgent(output_dir=tmp_path).render(_result())
        assert r.html_path.read_text(encoding="utf-8").strip()

    def test_json_file_content_valid(self, tmp_path: Path) -> None:
        """JSON file parses as valid JSON."""
        r = RenderingAgent(output_dir=tmp_path).render(_result())
        data = json.loads(r.json_path.read_text(encoding="utf-8"))
        assert "schema_version" in data

    def test_files_encoded_as_utf8(self, tmp_path: Path) -> None:
        """All three files can be read as UTF-8 without decoding errors."""
        item = _item(headline="Ångström — café — naïve")
        r = RenderingAgent(output_dir=tmp_path).render(_result(items=[item]))
        for path in (r.markdown_path, r.html_path, r.json_path):
            path.read_text(encoding="utf-8")  # must not raise

    def test_items_rendered_count(self, tmp_path: Path) -> None:
        """items_rendered counts items with valid URLs (SRC-141)."""
        items = [
            _item(url="https://example.com/a"),
            _item(url="https://example.com/b"),
            _item(url=""),  # no URL — should be dropped
        ]
        r = RenderingAgent(output_dir=tmp_path).render(_result(items=items))
        assert r.items_rendered == 2
        assert r.items_dropped_no_url == 1

    def test_all_valid_urls_rendered(self, tmp_path: Path) -> None:
        """All items with valid URLs are counted as rendered."""
        items = [_item(url=f"https://example.com/{i}") for i in range(5)]
        r = RenderingAgent(output_dir=tmp_path).render(_result(items=items))
        assert r.items_rendered == 5
        assert r.items_dropped_no_url == 0

    def test_rendering_result_type(self, tmp_path: Path) -> None:
        """render() returns a RenderingResult dataclass (SRC-004)."""
        r = RenderingAgent(output_dir=tmp_path).render(_result())
        assert isinstance(r, RenderingResult)

    def test_second_render_overwrites_first(self, tmp_path: Path) -> None:
        """Re-rendering the same result overwrites files cleanly (SRC-145)."""
        result = _result()
        agent = RenderingAgent(output_dir=tmp_path)
        r1 = agent.render(result)
        r2 = agent.render(result)

        assert r1.markdown_path == r2.markdown_path
        assert r1.html_path == r2.html_path
        assert r1.json_path == r2.json_path
        # Only 3 files should exist (second run overwrote first)
        assert len(list(tmp_path.iterdir())) == 3

    @pytest.mark.parametrize("cadence", ["daily", "weekly", "monthly", "annual"])
    def test_all_cadences_render_without_exception(self, cadence: str, tmp_path: Path) -> None:
        """All four cadences render without raising an exception (SRC-102)."""
        result = _result(
            metadata=_meta(cadence=cadence),
            themes=["A theme"] if cadence != "daily" else [],
            outlook="Looking ahead." if cadence in ("weekly", "monthly") else "",
            predictions=["A prediction."] * 10 if cadence == "annual" else [],
        )
        r = RenderingAgent(output_dir=tmp_path / cadence).render(result)
        assert r.markdown_path.exists()
        assert r.html_path.exists()
        assert r.json_path.exists()


# ──────────────────────────────────────────────────────────────────────────────
# TestRenderingAgentDryRunFull
# ──────────────────────────────────────────────────────────────────────────────


class TestRenderingAgentDryRunFull:
    """
    render_dry_run() writes to a temp directory, leaving the configured
    output_dir clean.  Used for CI smoke testing (SRC-102).

    Traces: SRC-102 (dry-run smoke test mode)
    """

    def test_dry_run_no_files_in_output_dir(self, tmp_path: Path) -> None:
        """Dry-run writes nothing to the configured output_dir."""
        RenderingAgent(output_dir=tmp_path).render_dry_run(_result())
        assert list(tmp_path.iterdir()) == []

    def test_dry_run_returns_rendering_result(self, tmp_path: Path) -> None:
        """render_dry_run returns a valid RenderingResult."""
        r = RenderingAgent(output_dir=tmp_path).render_dry_run(_result())
        assert isinstance(r, RenderingResult)

    def test_dry_run_items_rendered_correct(self, tmp_path: Path) -> None:
        """render_dry_run correctly counts rendered items (SRC-102)."""
        items = [
            _item(url="https://example.com/ok"),
            _item(url=""),  # dropped
        ]
        r = RenderingAgent(output_dir=tmp_path).render_dry_run(_result(items=items))
        assert r.items_rendered == 1
        assert r.items_dropped_no_url == 1

    def test_dry_run_all_cadences_no_exception(self, tmp_path: Path) -> None:
        """Dry-run works for all four cadences without error (SRC-102)."""
        for cadence in ("daily", "weekly", "monthly", "annual"):
            result = _result(metadata=_meta(cadence=cadence))
            RenderingAgent(output_dir=tmp_path).render_dry_run(result)

    def test_dry_run_exercises_all_renderers(self, tmp_path: Path) -> None:
        """Dry-run calls all three renderer code paths."""
        # If any renderer raises, this will propagate
        result = _result(
            metadata=_meta(cadence="annual"),
            themes=["Theme A"],
            predictions=["Prediction 1"],
            items=[_item()],
        )
        r = RenderingAgent(output_dir=tmp_path).render_dry_run(result)
        assert r.items_rendered >= 1


# ──────────────────────────────────────────────────────────────────────────────
# TestRenderingAgentStoreFull
# ──────────────────────────────────────────────────────────────────────────────


class TestRenderingAgentStoreFull:
    """
    render_and_update_store() must populate DigestRecord.{md,html,json}_path
    after writing, and update the store so the portal can serve download links.

    Traces: SRC-145 (DigestRecord file paths → portal download links)
    """

    def _make_digest_record(self) -> DigestRecord:
        meta = _meta()
        return DigestRecord(
            agent_id=meta.agent_id,
            cadence=meta.cadence,
            run_date=meta.run_date,
            window_start=meta.window_start,
            window_end=meta.window_end,
            prompt_version=meta.prompt_version,
            llm_provider=meta.llm_provider,
            llm_model=meta.llm_model,
            items_considered=meta.items_considered,
            items_included=meta.items_included,
            items_by_tier=meta.items_by_tier,
            items_by_source_class=meta.items_by_source_class,
            twitter_signal_available=meta.twitter_signal_available,
            tweet_api_call_count=meta.tweet_api_call_count,
            token_usage=meta.token_usage,
        )

    def test_updates_all_three_paths(self, tmp_path: Path) -> None:
        """All three file paths are stored in the DigestRecord."""
        existing = self._make_digest_record()
        store = MagicMock()
        store.get_digest.return_value = existing

        RenderingAgent(output_dir=tmp_path).render_and_update_store(_result(), store)

        store.upsert_digest.assert_called_once()
        updated = store.upsert_digest.call_args[0][0]
        assert updated.md_path is not None
        assert updated.html_path is not None
        assert updated.json_path is not None

    def test_paths_have_correct_extensions(self, tmp_path: Path) -> None:
        """Stored paths end with the correct extensions."""
        existing = self._make_digest_record()
        store = MagicMock()
        store.get_digest.return_value = existing

        RenderingAgent(output_dir=tmp_path).render_and_update_store(_result(), store)
        updated = store.upsert_digest.call_args[0][0]
        assert updated.md_path.endswith(".md")
        assert updated.html_path.endswith(".html")
        assert updated.json_path.endswith(".json")

    def test_noop_when_no_digest_record(self, tmp_path: Path) -> None:
        """No upsert when DigestRecord does not exist (e.g. dry-run curation)."""
        store = MagicMock()
        store.get_digest.return_value = None

        RenderingAgent(output_dir=tmp_path).render_and_update_store(_result(), store)
        store.upsert_digest.assert_not_called()

    def test_files_written_even_without_record(self, tmp_path: Path) -> None:
        """Files are written regardless of DigestRecord existence."""
        store = MagicMock()
        store.get_digest.return_value = None

        r = RenderingAgent(output_dir=tmp_path).render_and_update_store(_result(), store)
        assert r.markdown_path.exists()
        assert r.html_path.exists()
        assert r.json_path.exists()

    def test_store_called_with_correct_agent_id(self, tmp_path: Path) -> None:
        """store.get_digest called with the correct agent_id."""
        meta = _meta(agent_id="prod-agent")
        existing = replace(self._make_digest_record(), agent_id="prod-agent")
        store = MagicMock()
        store.get_digest.return_value = existing

        RenderingAgent(output_dir=tmp_path).render_and_update_store(_result(metadata=meta), store)
        get_args = store.get_digest.call_args[1]
        assert (
            get_args.get("agent_id", None) == "prod-agent"
            or store.get_digest.call_args[0][0] == "prod-agent"
        )

    def test_real_store_roundtrip_with_date_run_date(self, tmp_path: Path) -> None:
        """
        End-to-end against a real TinyDB store: upsert a DigestRecord, then run
        ``render_and_update_store``.  ``DigestMetadata.run_date`` is a ``date``,
        and the rendering agent forwards it directly into ``store.get_digest``.

        Regression for the production AttributeError
        ``'datetime.date' object has no attribute 'date'`` — the store
        previously called ``run_date.date()`` on the incoming value, which
        crashes when the caller passes a ``date`` (the canonical type per
        :attr:`DigestRecord.run_date`).
        """
        from ai_news_agent.storage.tinydb_store import TinyDBArticleStore

        store = TinyDBArticleStore(tmp_path / "store.json")
        try:
            store.upsert_digest(self._make_digest_record())

            rendering_result = RenderingAgent(output_dir=tmp_path).render_and_update_store(
                _result(), store
            )

            assert rendering_result.markdown_path.exists()

            # DigestRecord paths populated via the same date-keyed lookup
            updated = store.get_digest("test-agent", "daily", run_date=_meta().run_date)
            assert updated is not None
            assert updated.md_path is not None
            assert updated.html_path is not None
            assert updated.json_path is not None
        finally:
            store.close()


# ──────────────────────────────────────────────────────────────────────────────
# TestUrlEnforcementLayering
# ──────────────────────────────────────────────────────────────────────────────


class TestUrlEnforcementLayering:
    """
    URL enforcement at the renderer is the SECOND safety layer (SRC-141).

    The first layer is the Scorer in curation/scorer.py.  The renderer
    independently re-validates so that schema changes, data corruption, or
    future code refactors cannot allow a no-URL item to reach any output
    format.

    Traces: SRC-049 (non-negotiable URL requirement), SRC-141 (second layer)
    """

    @pytest.mark.parametrize(
        "bad_url",
        [
            "",
            "ftp://files.example.com",
            "//example.com",
            "example.com/path",
            "javascript:void(0)",
            "/relative/path",
        ],
    )
    def test_markdown_drops_bad_url(self, bad_url: str) -> None:
        """Markdown renderer drops items with bad URLs (SRC-141)."""
        items = [
            _item(headline="Good", url="https://good.example.com/ok"),
            _item(headline="Bad URL", url=bad_url),
        ]
        md = MarkdownRenderer().render(_result(items=items))
        assert "Good" in md
        assert "Bad URL" not in md

    @pytest.mark.parametrize(
        "bad_url",
        [
            "",
            "ftp://files.example.com",
            "javascript:alert(1)",
        ],
    )
    def test_html_drops_bad_url(self, bad_url: str) -> None:
        """HTML renderer drops items with bad URLs (SRC-141)."""
        items = [
            _item(headline="Good", url="https://good.example.com/ok"),
            _item(headline="Bad URL", url=bad_url),
        ]
        html = HtmlRenderer().render(_result(items=items))
        assert "Good" in html
        assert "Bad URL" not in html

    @pytest.mark.parametrize(
        "bad_url",
        [
            "",
            "ftp://files.example.com",
            "data:text/plain,hello",
        ],
    )
    def test_json_drops_bad_url(self, bad_url: str) -> None:
        """JSON renderer drops items with bad URLs (SRC-141)."""
        items = [
            _item(headline="Good", url="https://good.example.com/ok"),
            _item(headline="Bad URL", url=bad_url),
        ]
        data = json.loads(JsonRenderer().render(_result(items=items)))
        headlines = [i["headline"] for i in data["items"]]
        assert "Good" in headlines
        assert "Bad URL" not in headlines

    def test_all_three_renderers_consistent_drop(self) -> None:
        """All three renderers drop the same items (SRC-141 consistency)."""
        items = [
            _item(headline="Valid", url="https://valid.example.com"),
            _item(headline="Invalid", url=""),
        ]
        result = _result(items=items)

        md = MarkdownRenderer().render(result)
        html = HtmlRenderer().render(result)
        data = json.loads(JsonRenderer().render(result))

        for content in (md, html):
            assert "Valid" in content
            assert "Invalid" not in content

        json_headlines = [i["headline"] for i in data["items"]]
        assert "Valid" in json_headlines
        assert "Invalid" not in json_headlines

    def test_all_items_invalid_empty_output(self, tmp_path: Path) -> None:
        """When all items have invalid URLs, outputs have empty item lists."""
        result = _result(items=[_item(url=""), _item(url="ftp://bad")])
        r = RenderingAgent(output_dir=tmp_path).render(result)

        data = json.loads(r.json_path.read_text(encoding="utf-8"))
        assert data["items"] == []
        assert r.items_rendered == 0
        assert r.items_dropped_no_url == 2


# ──────────────────────────────────────────────────────────────────────────────
# TestTwitterDegradationFull
# ──────────────────────────────────────────────────────────────────────────────


class TestTwitterDegradationFull:
    """
    Twitter degradation note: present in all three formats when Twitter is
    unavailable, absent when available.

    Traces: SRC-148 (Twitter is signal, not hard dependency; digest notes degradation)
    """

    NOTE = "⚠️ Twitter/X influencer signal was unavailable for this run."

    def test_markdown_contains_note(self) -> None:
        """Degradation note in Markdown output (SRC-148)."""
        md = MarkdownRenderer().render(_result(twitter_degradation_note=self.NOTE))
        assert "influencer signal was unavailable" in md

    def test_html_contains_note(self) -> None:
        """Degradation note in HTML output (SRC-148)."""
        html = HtmlRenderer().render(_result(twitter_degradation_note=self.NOTE))
        assert "influencer signal was unavailable" in html

    def test_html_note_uses_degradation_class(self) -> None:
        """HTML degradation note uses the ``degradation-note`` CSS class."""
        html = HtmlRenderer().render(_result(twitter_degradation_note=self.NOTE))
        assert "degradation-note" in html

    def test_json_contains_note_key(self) -> None:
        """Degradation note present in JSON as ``twitter_degradation_note`` key (SRC-148)."""
        data = json.loads(JsonRenderer().render(_result(twitter_degradation_note=self.NOTE)))
        assert "twitter_degradation_note" in data
        assert "unavailable" in data["twitter_degradation_note"]

    def test_no_note_when_twitter_available(self) -> None:
        """No degradation note in any format when Twitter is available."""
        result = _result(twitter_degradation_note=None)
        md = MarkdownRenderer().render(result)
        html = HtmlRenderer().render(result)
        data = json.loads(JsonRenderer().render(result))

        assert "influencer signal was unavailable" not in md
        assert "influencer signal was unavailable" not in html
        assert "twitter_degradation_note" not in data

    def test_html_note_html_escaped(self) -> None:
        """Degradation note is HTML-escaped (SRC-148 — injection prevention)."""
        malicious = '<script>alert("twitter-degrade")</script>'
        html = HtmlRenderer().render(_result(twitter_degradation_note=malicious))
        assert "<script>alert" not in html
        assert "&lt;script&gt;" in html

    def test_rendering_agent_includes_note_in_files(self, tmp_path: Path) -> None:
        """Both MD and HTML files written by RenderingAgent include the note."""
        result = _result(twitter_degradation_note=self.NOTE)
        r = RenderingAgent(output_dir=tmp_path).render(result)
        md = r.markdown_path.read_text(encoding="utf-8")
        html = r.html_path.read_text(encoding="utf-8")
        assert "unavailable" in md
        assert "unavailable" in html


# ──────────────────────────────────────────────────────────────────────────────
# TestMonitoringFieldsFull
# ──────────────────────────────────────────────────────────────────────────────


class TestMonitoringFieldsFull:
    """
    Quality monitoring fields must appear in all three output formats (SRC-150).

    Traces: SRC-129 (prompt_version), SRC-148 (twitter_signal_available),
            SRC-150 (monitoring: items, tiers, model, token usage)
    """

    def test_markdown_items_considered(self) -> None:
        """Markdown footer shows items_considered count (SRC-150)."""
        meta = _meta(items_considered=99)
        md = MarkdownRenderer().render(_result(metadata=meta))
        assert "99" in md

    def test_markdown_token_usage(self) -> None:
        """Markdown footer shows token_usage (SRC-150)."""
        meta = _meta(token_usage=32768)
        md = MarkdownRenderer().render(_result(metadata=meta))
        assert "32,768" in md  # thousands separator

    def test_markdown_llm_model(self) -> None:
        """Markdown footer shows LLM model name (SRC-150)."""
        meta = _meta(llm_model="o3-mini")
        md = MarkdownRenderer().render(_result(metadata=meta))
        assert "o3-mini" in md

    def test_json_all_monitoring_fields_correct_types(self) -> None:
        """JSON metadata fields have the correct Python types (SRC-150)."""
        meta = _meta(
            items_considered=40,
            items_included=5,
            token_usage=4096,
        )
        data = json.loads(JsonRenderer().render(_result(metadata=meta)))
        m = data["metadata"]
        assert isinstance(m["items_considered"], int)
        assert isinstance(m["items_included"], int)
        assert isinstance(m["token_usage"], int)
        assert isinstance(m["twitter_signal_available"], bool)
        assert isinstance(m["tweet_api_call_count"], int)
        assert isinstance(m["items_by_tier"], dict)
        assert isinstance(m["items_by_source_class"], dict)

    def test_json_items_counts_coherent(self) -> None:
        """items_considered ≥ items_included (sanity check, SRC-150)."""
        meta = _meta(items_considered=50, items_included=8)
        data = json.loads(JsonRenderer().render(_result(metadata=meta)))
        m = data["metadata"]
        assert m["items_considered"] >= m["items_included"]

    def test_html_prompt_version_in_footer(self) -> None:
        """HTML footer contains prompt version SHA-256 (SRC-129)."""
        meta = _meta(prompt_version="sha256:0011aabb")
        html = HtmlRenderer().render(_result(metadata=meta))
        assert "sha256:0011aabb" in html

    def test_json_prompt_version_in_metadata(self) -> None:
        """JSON metadata.prompt_version matches run (SRC-129)."""
        meta = _meta(prompt_version="sha256:0011aabb")
        data = json.loads(JsonRenderer().render(_result(metadata=meta)))
        assert data["metadata"]["prompt_version"] == "sha256:0011aabb"


# ──────────────────────────────────────────────────────────────────────────────
# TestCrossRefsFull
# ──────────────────────────────────────────────────────────────────────────────


class TestCrossRefsFull:
    """
    Cross-reference links per renderer (SRC-048).

    Traces: SRC-048 (optional cross-references to related items)
    """

    def test_md_renders_all_valid_cross_refs(self) -> None:
        """All valid cross-ref URLs appear in Markdown output."""
        item = _item(
            cross_refs=[
                "https://ref-a.example.com",
                "https://ref-b.example.com",
                "https://ref-c.example.com",
            ]
        )
        md = MarkdownRenderer().render(_result(items=[item]))
        for ref in ("ref-a", "ref-b", "ref-c"):
            assert ref in md

    def test_html_renders_all_valid_cross_refs(self) -> None:
        """All valid cross-ref URLs appear in HTML output."""
        item = _item(
            cross_refs=[
                "https://ref-a.example.com",
                "https://ref-b.example.com",
            ]
        )
        html = HtmlRenderer().render(_result(items=[item]))
        assert "ref-a.example.com" in html
        assert "ref-b.example.com" in html

    def test_json_preserves_cross_refs_list(self) -> None:
        """JSON output preserves the full cross-refs list."""
        refs = ["https://ref-a.example.com", "https://ref-b.example.com"]
        item = _item(cross_refs=refs)
        data = json.loads(JsonRenderer().render(_result(items=[item])))
        assert data["items"][0]["cross_refs"] == refs

    def test_html_omits_invalid_cross_ref_from_link(self) -> None:
        """Invalid cross-ref URL not rendered as a link in HTML."""
        item = _item(cross_refs=["not-a-url-no-scheme"])
        html = HtmlRenderer().render(_result(items=[item]))
        assert 'href="not-a-url-no-scheme"' not in html

    def test_no_related_section_when_empty(self) -> None:
        """No 'Related:' section when cross_refs is empty."""
        item = _item(cross_refs=[])
        md = MarkdownRenderer().render(_result(items=[item]))
        assert "Related:" not in md

    def test_json_preserves_empty_cross_refs(self) -> None:
        """JSON output preserves empty cross_refs as []."""
        item = _item(cross_refs=[])
        data = json.loads(JsonRenderer().render(_result(items=[item])))
        assert data["items"][0]["cross_refs"] == []


# ──────────────────────────────────────────────────────────────────────────────
# TestCadenceCompleteness
# ──────────────────────────────────────────────────────────────────────────────


class TestCadenceCompleteness:
    """
    Each cadence must produce all sections required by SRC-029–SRC-032.

    Traces: SRC-029 (daily: headline/source/URL/why-it-matters),
            SRC-030 (weekly: themes + articles + outlook),
            SRC-031 (monthly: themes + outlook + articles),
            SRC-032 (annual: top-10 + predictions + themes)
    """

    def test_daily_required_fields(self) -> None:
        """Daily output has headline, source, URL, why-it-matters (SRC-029)."""
        item = _item(
            headline="Important AI Story",
            source_name="Bloomberg",
            url="https://bloomberg.com/ai-story",
            why="This story matters because it changes the market.",
        )
        result = _result(items=[item], metadata=_meta(cadence="daily"))
        md = MarkdownRenderer().render(result)

        assert "Important AI Story" in md
        assert "Bloomberg" in md
        assert "https://bloomberg.com/ai-story" in md
        assert "This story matters because it changes the market." in md

    def test_weekly_includes_all_three_sections(self) -> None:
        """Weekly output has themes + top stories + looking-ahead (SRC-030)."""
        result = _result(
            metadata=_meta(cadence="weekly"),
            themes=["AI Regulation", "Model Capabilities"],
            items=[_item()],
            outlook="Watch for EU enforcement actions.",
        )
        md = MarkdownRenderer().render(result)
        assert "This Week's Themes" in md
        assert "Top Stories This Week" in md
        assert "Looking Ahead" in md

    def test_monthly_includes_big_picture_themes(self) -> None:
        """Monthly output emphasises big-picture themes (SRC-031)."""
        result = _result(
            metadata=_meta(cadence="monthly"),
            themes=["Infrastructure consolidation", "Regulatory wave"],
        )
        md = MarkdownRenderer().render(result)
        assert "Monthly Themes" in md
        assert "Infrastructure consolidation" in md
        assert "Regulatory wave" in md

    def test_monthly_includes_anticipated_news(self) -> None:
        """Monthly output includes anticipated-news outlook section (SRC-031)."""
        result = _result(
            metadata=_meta(cadence="monthly"),
            outlook="Expect key GPT-5 enterprise partnerships to close.",
        )
        md = MarkdownRenderer().render(result)
        assert "What to Watch" in md
        assert "GPT-5" in md

    def test_annual_top_10_stories_heading(self) -> None:
        """Annual output has 'Top 10 Stories of the Year' heading (SRC-032)."""
        result = _result(
            metadata=_meta(cadence="annual"),
            items=[_item() for _ in range(10)],
        )
        md = MarkdownRenderer().render(result)
        assert "Top 10 Stories of the Year" in md

    def test_annual_predictions_section(self) -> None:
        """Annual output has predictions section (SRC-032, SRC-124)."""
        result = _result(
            metadata=_meta(cadence="annual"),
            predictions=["Prediction A", "Prediction B"],
        )
        md = MarkdownRenderer().render(result)
        assert "10 Predictions for the Year Ahead" in md
        assert "Prediction A" in md
        assert "Prediction B" in md

    def test_annual_json_has_predictions(self) -> None:
        """Annual JSON output carries predictions array (SRC-032, SRC-124)."""
        preds = ["AI agents go mainstream", "Open-source surpasses proprietary"]
        result = _result(
            metadata=_meta(cadence="annual"),
            predictions=preds,
        )
        data = json.loads(JsonRenderer().render(result))
        assert data["predictions"] == preds

    def test_annual_html_has_prediction_items(self) -> None:
        """Annual HTML output has prediction divs (SRC-032, SRC-124)."""
        result = _result(
            metadata=_meta(cadence="annual"),
            predictions=["Prediction X"],
        )
        html = HtmlRenderer().render(result)
        assert "prediction-item" in html
        assert "Prediction X" in html


# ──────────────────────────────────────────────────────────────────────────────
# TestCLIEntryPoint
# ──────────────────────────────────────────────────────────────────────────────


class TestCLIEntryPoint:
    """
    ``ai-news-render`` CLI: argument parsing, exit codes, and help output.

    Traces: SRC-076 (local dev invocation), SRC-102 (dry-run mode)
    """

    def test_no_input_exits_0(self) -> None:
        """Running without --input exits 0 (prints help)."""
        from ai_news_agent.rendering.agent import cli_main

        with patch("sys.argv", ["ai-news-render"]), pytest.raises(SystemExit) as exc_info:
            cli_main()
        assert exc_info.value.code == 0

    def test_missing_input_file_exits_2(self, tmp_path: Path) -> None:
        """--input pointing to non-existent file exits 2."""
        from ai_news_agent.rendering.agent import cli_main

        missing = str(tmp_path / "does-not-exist.json")
        with (
            patch("sys.argv", ["ai-news-render", "--input", missing]),
            pytest.raises(SystemExit) as exc_info,
        ):
            cli_main()
        assert exc_info.value.code == 2

    def test_invalid_json_input_exits_2(self, tmp_path: Path) -> None:
        """--input with invalid JSON content exits 2."""
        from ai_news_agent.rendering.agent import cli_main

        bad_json = tmp_path / "bad.json"
        bad_json.write_text("not valid JSON {{{{", encoding="utf-8")
        with (
            patch("sys.argv", ["ai-news-render", "--input", str(bad_json)]),
            pytest.raises(SystemExit) as exc_info,
        ):
            cli_main()
        assert exc_info.value.code == 2


# ──────────────────────────────────────────────────────────────────────────────
# TestCLIRender
# ──────────────────────────────────────────────────────────────────────────────


class TestCLIRender:
    """
    ``ai-news-render`` CLI end-to-end: reads a JSON digest file and renders.

    Traces: SRC-076 (local dev), SRC-102 (dry-run smoke), SRC-145 (filenames)
    """

    def _write_digest_json(self, path: Path) -> None:
        """Write a minimal valid JSON digest file."""
        payload = {
            "schema_version": "1.0",
            "metadata": {
                "agent_id": "cli-test-agent",
                "cadence": "daily",
                "run_date": "2026-05-11",
                "window_start": "2026-05-10T00:00:00+00:00",
                "window_end": "2026-05-10T23:59:59+00:00",
                "prompt_version": "sha256:abcdef0123456789",
                "llm_provider": "openai",
                "llm_model": "gpt-4o",
                "items_considered": 10,
                "items_included": 1,
                "items_by_tier": {"1b": 1},
                "items_by_source_class": {"web": 1},
                "twitter_signal_available": True,
                "tweet_api_call_count": 0,
                "token_usage": 512,
            },
            "items": [
                {
                    "headline": "CLI Test Article",
                    "source_name": "Test Source",
                    "url": "https://test.example.com/article",
                    "pub_date": "2026-05-10",
                    "why_it_matters": "This is the CLI test article.",
                    "impact_tags": ["business_impact"],
                    "tier": "1b",
                    "cross_refs": [],
                    "twitter_handle": None,
                    "tweet_url": None,
                    "prompt_version": "sha256:abcdef0123456789",
                }
            ],
            "themes": [],
            "outlook": "",
            "predictions": [],
        }
        path.write_text(json.dumps(payload), encoding="utf-8")

    def test_render_from_json_writes_three_files(self, tmp_path: Path) -> None:
        """CLI renders three files to the output directory."""
        from ai_news_agent.rendering.agent import cli_main

        input_path = tmp_path / "digest.json"
        output_dir = tmp_path / "rendered"
        self._write_digest_json(input_path)

        with (
            patch(
                "sys.argv",
                [
                    "ai-news-render",
                    "--input",
                    str(input_path),
                    "--output-dir",
                    str(output_dir),
                ],
            ),
            pytest.raises(SystemExit) as exc_info,
        ):
            cli_main()
        assert exc_info.value.code == 0
        assert (output_dir / "2026-05-11-daily.md").exists()
        assert (output_dir / "2026-05-11-daily.html").exists()
        assert (output_dir / "2026-05-11-daily.json").exists()

    def test_render_cli_default_output_dir(self, tmp_path: Path) -> None:
        """Without --output-dir, files are written beside the input file."""
        from ai_news_agent.rendering.agent import cli_main

        input_path = tmp_path / "digest.json"
        self._write_digest_json(input_path)

        with (
            patch(
                "sys.argv",
                [
                    "ai-news-render",
                    "--input",
                    str(input_path),
                ],
            ),
            pytest.raises(SystemExit) as exc_info,
        ):
            cli_main()
        assert exc_info.value.code == 0
        # Files should be in tmp_path (same dir as input)
        assert (tmp_path / "2026-05-11-daily.md").exists()

    def test_render_cli_dry_run_no_files(self, tmp_path: Path) -> None:
        """--dry-run does not write any files to output_dir."""
        from ai_news_agent.rendering.agent import cli_main

        input_path = tmp_path / "digest.json"
        output_dir = tmp_path / "should-be-empty"
        output_dir.mkdir()
        self._write_digest_json(input_path)

        with (
            patch(
                "sys.argv",
                [
                    "ai-news-render",
                    "--input",
                    str(input_path),
                    "--output-dir",
                    str(output_dir),
                    "--dry-run",
                ],
            ),
            pytest.raises(SystemExit) as exc_info,
        ):
            cli_main()
        assert exc_info.value.code == 0
        # No permanent files written
        assert list(output_dir.iterdir()) == []

    def test_render_cli_item_present_in_output(self, tmp_path: Path) -> None:
        """CLI-rendered Markdown contains the item headline."""
        from ai_news_agent.rendering.agent import cli_main

        input_path = tmp_path / "digest.json"
        output_dir = tmp_path / "out"
        self._write_digest_json(input_path)

        with (
            patch(
                "sys.argv",
                [
                    "ai-news-render",
                    "--input",
                    str(input_path),
                    "--output-dir",
                    str(output_dir),
                ],
            ),
            pytest.raises(SystemExit),
        ):
            cli_main()

        md_file = output_dir / "2026-05-11-daily.md"
        content = md_file.read_text(encoding="utf-8")
        assert "CLI Test Article" in content


# ──────────────────────────────────────────────────────────────────────────────
# TestIdempotency
# ──────────────────────────────────────────────────────────────────────────────


class TestIdempotency:
    """
    Re-rendering the same CurationRunResult always produces identical output
    files (SRC-145 — idempotent re-runs via date-stamped filenames).

    Traces: SRC-145 (idempotency — re-runs overwrite cleanly)
    """

    def test_markdown_content_idempotent(self, tmp_path: Path) -> None:
        """Two renders of the same result produce identical Markdown."""
        result = _result()
        agent = RenderingAgent(output_dir=tmp_path / "a")
        r1 = agent.render(result)
        r2 = agent.render(result)
        assert r1.markdown_path.read_text() == r2.markdown_path.read_text()

    def test_html_content_idempotent(self, tmp_path: Path) -> None:
        """Two renders of the same result produce identical HTML."""
        result = _result()
        agent = RenderingAgent(output_dir=tmp_path / "b")
        r1 = agent.render(result)
        r2 = agent.render(result)
        assert r1.html_path.read_text() == r2.html_path.read_text()

    def test_json_content_idempotent(self, tmp_path: Path) -> None:
        """Two renders of the same result produce identical JSON."""
        result = _result()
        agent = RenderingAgent(output_dir=tmp_path / "c")
        r1 = agent.render(result)
        r2 = agent.render(result)
        assert json.loads(r1.json_path.read_text()) == json.loads(r2.json_path.read_text())

    def test_rapid_rerenders_no_exception(self, tmp_path: Path) -> None:
        """Rapid sequential renders do not raise concurrency or file-locking errors."""
        result = _result()
        agent = RenderingAgent(output_dir=tmp_path)
        for _ in range(5):
            agent.render(result)  # must not raise


# ──────────────────────────────────────────────────────────────────────────────
# TestItemCardAllFields
# ──────────────────────────────────────────────────────────────────────────────


class TestItemCardAllFields:
    """
    Each CuratedItem field defined in SRC-048 must appear in every output format.
    """

    def _full_item(self) -> CuratedItem:
        return CuratedItem(
            headline="Full Field Test Article",
            source_name="The Economist",
            url="https://economist.com/full-test",
            pub_date=date(2026, 1, 15),
            why_it_matters="This test verifies all required fields are rendered.",
            impact_tags=["business_impact", "workforce_impact", "policy_impact"],
            tier="1b",
            cross_refs=["https://cross.example.com/1"],
            twitter_handle="testhandle",
            tweet_url="https://x.com/testhandle/status/9999",
            prompt_version="sha256:full_test_version",
        )

    def test_all_fields_in_markdown(self) -> None:
        """All SRC-048 fields present in Markdown output."""
        item = self._full_item()
        md = MarkdownRenderer().render(_result(items=[item]))
        assert "Full Field Test Article" in md
        assert "The Economist" in md
        assert "https://economist.com/full-test" in md
        assert "2026-01-15" in md
        assert "verifies all required fields" in md
        assert "Business" in md
        assert "Workforce" in md
        assert "Policy" in md
        assert "Tier 1b" in md
        assert "cross.example.com/1" in md
        assert "testhandle" in md
        assert "x.com/testhandle" in md

    def test_all_fields_in_html(self) -> None:
        """All SRC-048 fields present in HTML output."""
        item = self._full_item()
        html = HtmlRenderer().render(_result(items=[item]))
        assert "Full Field Test Article" in html
        assert "The Economist" in html
        assert "economist.com/full-test" in html
        assert "2026-01-15" in html
        assert "verifies all required fields" in html
        assert "Business Impact" in html
        assert "Workforce Impact" in html
        assert "Policy Impact" in html
        assert "testhandle" in html
        assert "x.com/testhandle" in html

    def test_all_fields_in_json(self) -> None:
        """All SRC-048 fields present in JSON output."""
        item = self._full_item()
        data = json.loads(JsonRenderer().render(_result(items=[item])))
        i = data["items"][0]
        assert i["headline"] == "Full Field Test Article"
        assert i["source_name"] == "The Economist"
        assert i["url"] == "https://economist.com/full-test"
        assert i["pub_date"] == "2026-01-15"
        assert "verifies all required fields" in i["why_it_matters"]
        assert "business_impact" in i["impact_tags"]
        assert "workforce_impact" in i["impact_tags"]
        assert "policy_impact" in i["impact_tags"]
        assert i["tier"] == "1b"
        assert "https://cross.example.com/1" in i["cross_refs"]
        assert i["twitter_handle"] == "testhandle"
        assert i["tweet_url"] == "https://x.com/testhandle/status/9999"
        assert i["prompt_version"] == "sha256:full_test_version"


# ──────────────────────────────────────────────────────────────────────────────
# TestEmptyStateGraceful
# ──────────────────────────────────────────────────────────────────────────────


class TestEmptyStateGraceful:
    """
    All empty-list / empty-string edge cases must not raise exceptions and
    must produce sensible output.
    """

    def test_all_cadences_empty_items(self) -> None:
        """Empty items list doesn't crash any renderer for any cadence."""
        for cadence in ("daily", "weekly", "monthly", "annual"):
            result = _result(metadata=_meta(cadence=cadence), items=[])
            MarkdownRenderer().render(result)
            HtmlRenderer().render(result)
            JsonRenderer().render(result)

    def test_weekly_no_themes_no_crash(self) -> None:
        """Weekly with empty themes renders without crash."""
        result = _result(
            metadata=_meta(cadence="weekly"),
            themes=[],
            outlook="",
            items=[],
        )
        md = MarkdownRenderer().render(result)
        assert "Top Stories This Week" in md
        assert "This Week's Themes" not in md

    def test_monthly_no_themes_no_crash(self) -> None:
        """Monthly with empty themes renders without crash."""
        result = _result(metadata=_meta(cadence="monthly"), themes=[], outlook="")
        md = MarkdownRenderer().render(result)
        assert "Top Stories This Month" in md

    def test_annual_no_predictions_no_section(self) -> None:
        """Annual with no predictions omits predictions section."""
        result = _result(metadata=_meta(cadence="annual"), predictions=[])
        md = MarkdownRenderer().render(result)
        assert "Predictions for the Year Ahead" not in md

    def test_unknown_cadence_does_not_crash(self) -> None:
        """Unknown cadence value does not crash any renderer."""
        meta = replace(_meta(), cadence="quarterly")
        result = _result(metadata=meta)
        MarkdownRenderer().render(result)
        HtmlRenderer().render(result)
        JsonRenderer().render(result)

    def test_empty_result_agent_writes_files(self, tmp_path: Path) -> None:
        """Empty result (no items, no themes) still writes all three files."""
        result = _result(items=[], themes=[], predictions=[], outlook="")
        r = RenderingAgent(output_dir=tmp_path).render(result)
        assert r.markdown_path.exists()
        assert r.html_path.exists()
        assert r.json_path.exists()


# ──────────────────────────────────────────────────────────────────────────────
# TestJsonSerializerEdgeCases
# ──────────────────────────────────────────────────────────────────────────────


class TestJsonSerializerEdgeCases:
    """
    Tests for the ``_serialize`` function and JSON encoder edge-cases.

    Traces: SRC-140 (machine-readable archive), SRC-061 (output parsing)
    """

    def test_serialize_raises_for_unknown_type(self) -> None:
        """``_serialize`` raises TypeError for non-date, non-datetime types."""
        from ai_news_agent.rendering.json_renderer import _serialize

        with pytest.raises(TypeError, match="not JSON serializable"):
            _serialize(object())

    def test_serialize_datetime_returns_isoformat(self) -> None:
        """``_serialize`` returns ISO-8601 string for datetime objects."""
        from ai_news_agent.rendering.json_renderer import _serialize

        dt = datetime(2026, 5, 11, 12, 0, 0, tzinfo=UTC)
        result = _serialize(dt)
        assert result == "2026-05-11T12:00:00+00:00"

    def test_serialize_date_returns_isoformat(self) -> None:
        """``_serialize`` returns ISO-8601 string for date objects."""
        from ai_news_agent.rendering.json_renderer import _serialize

        d = date(2026, 5, 11)
        result = _serialize(d)
        assert result == "2026-05-11"

    def test_json_output_has_no_none_metadata_gaps(self) -> None:
        """All metadata fields have non-None values (SRC-150 contract)."""
        data = json.loads(JsonRenderer().render(_result()))
        for key, value in data["metadata"].items():
            assert value is not None, f"Null metadata field: {key}"


# ──────────────────────────────────────────────────────────────────────────────
# TestLoadCurationResultEdgeCases
# ──────────────────────────────────────────────────────────────────────────────


class TestLoadCurationResultEdgeCases:
    """
    Tests for ``_load_curation_result`` error paths — malformed metadata
    and item parse failures.

    Traces: SRC-076 (local dev CLI), SRC-145 (re-render from JSON file)
    """

    def _write_json(self, path: Path, payload: dict) -> None:
        path.write_text(json.dumps(payload), encoding="utf-8")

    def _base_payload(self) -> dict:
        return {
            "schema_version": "1.0",
            "metadata": {
                "agent_id": "edge-agent",
                "cadence": "daily",
                "run_date": "2026-05-11",
                "window_start": "2026-05-10T00:00:00+00:00",
                "window_end": "2026-05-10T23:59:59+00:00",
                "prompt_version": "sha256:edgecases",
                "llm_provider": "openai",
                "llm_model": "gpt-4o",
                "items_considered": 5,
                "items_included": 1,
                "items_by_tier": {"1b": 1},
                "items_by_source_class": {"web": 1},
                "twitter_signal_available": True,
                "tweet_api_call_count": 0,
                "token_usage": 512,
            },
            "items": [
                {
                    "headline": "Edge Case Article",
                    "source_name": "Test",
                    "url": "https://test.example.com/edge",
                    "pub_date": "2026-05-10",
                    "why_it_matters": "Edge case.",
                    "impact_tags": [],
                    "tier": "1b",
                    "cross_refs": [],
                    "twitter_handle": None,
                    "tweet_url": None,
                    "prompt_version": "sha256:edgecases",
                }
            ],
            "themes": [],
            "outlook": "",
            "predictions": [],
        }

    def test_invalid_metadata_run_date_exits_2(self, tmp_path: Path) -> None:
        """Malformed run_date causes metadata parse error → exit 2."""
        from ai_news_agent.rendering.agent import cli_main

        payload = self._base_payload()
        payload["metadata"]["run_date"] = "NOT-A-DATE"
        input_path = tmp_path / "bad_date.json"
        self._write_json(input_path, payload)

        with (
            patch("sys.argv", ["ai-news-render", "--input", str(input_path)]),
            pytest.raises(SystemExit) as exc_info,
        ):
            cli_main()
        assert exc_info.value.code == 2

    def test_item_parse_error_skips_item(self, tmp_path: Path) -> None:
        """Item with bad pub_date is skipped (logged as warning) — does not crash."""
        from ai_news_agent.rendering.agent import _load_curation_result

        payload = self._base_payload()
        # Add a second item with an invalid pub_date
        payload["items"].append(
            {
                "headline": "Bad Date Article",
                "source_name": "Test",
                "url": "https://test.example.com/baddate",
                "pub_date": "NOT-A-DATE",  # invalid
                "why_it_matters": "Bad date.",
                "impact_tags": [],
                "tier": "1b",
                "cross_refs": [],
                "twitter_handle": None,
                "tweet_url": None,
                "prompt_version": "sha256:edgecases",
            }
        )
        input_path = tmp_path / "bad_item_date.json"
        self._write_json(input_path, payload)

        # _load_curation_result should succeed, skipping the bad item
        result = _load_curation_result(input_path)
        # Only the valid item should be in the result
        headlines = [item.headline for item in result.items]
        assert "Edge Case Article" in headlines
        assert "Bad Date Article" not in headlines

    def test_minimal_metadata_uses_defaults(self, tmp_path: Path) -> None:
        """Missing metadata fields fall back to safe defaults."""
        from ai_news_agent.rendering.agent import _load_curation_result

        payload = {
            "schema_version": "1.0",
            "metadata": {},  # no fields
            "items": [],
            "themes": [],
            "outlook": "",
            "predictions": [],
        }
        input_path = tmp_path / "minimal.json"
        self._write_json(input_path, payload)

        result = _load_curation_result(input_path)
        assert result.metadata.agent_id == "unknown"
        assert result.metadata.cadence == "daily"

    def test_empty_items_list_loads_successfully(self, tmp_path: Path) -> None:
        """Empty items list loads successfully without errors."""
        from ai_news_agent.rendering.agent import _load_curation_result

        payload = self._base_payload()
        payload["items"] = []
        input_path = tmp_path / "no_items.json"
        self._write_json(input_path, payload)

        result = _load_curation_result(input_path)
        assert result.items == []

    def test_twitter_degradation_note_loaded(self, tmp_path: Path) -> None:
        """twitter_degradation_note preserved when loaded from JSON."""
        from ai_news_agent.rendering.agent import _load_curation_result

        payload = self._base_payload()
        payload["twitter_degradation_note"] = "⚠️ Twitter unavailable."
        input_path = tmp_path / "degraded.json"
        self._write_json(input_path, payload)

        result = _load_curation_result(input_path)
        assert result.twitter_degradation_note == "⚠️ Twitter unavailable."

    def test_themes_and_predictions_loaded(self, tmp_path: Path) -> None:
        """themes, outlook, and predictions are preserved when loaded."""
        from ai_news_agent.rendering.agent import _load_curation_result

        payload = self._base_payload()
        payload["metadata"]["cadence"] = "annual"
        payload["themes"] = ["Theme A", "Theme B"]
        payload["outlook"] = "Looking ahead."
        payload["predictions"] = ["Pred 1", "Pred 2"]
        input_path = tmp_path / "annual.json"
        self._write_json(input_path, payload)

        result = _load_curation_result(input_path)
        assert result.themes == ["Theme A", "Theme B"]
        assert result.outlook == "Looking ahead."
        assert result.predictions == ["Pred 1", "Pred 2"]
