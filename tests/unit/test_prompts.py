"""
tests/unit/test_prompts.py — Comprehensive tests for curation prompts and PromptBuilder.

Covers:
- All four cadence prompt files (daily, weekly, monthly, annual)
- Required sections per SRC-115 through SRC-124
- ISO date injection (SRC-116)
- Disqualifier list (SRC-117)
- Inclusion criteria (SRC-118)
- Twitter signal section (SRC-119, SRC-070, SRC-046, SRC-148)
- Structured output format (SRC-120)
- Search budget per cadence (SRC-121)
- "Why it matters" requirement (SRC-122)
- Working link requirement (SRC-123)
- Annual-only sections (SRC-124): inflection points, predictions, year/year+1
- SHA-256 prompt versioning (SRC-129)
- PromptManifest generation and verification
- Tier article injection (SRC-016–SRC-021)
- Provider-agnostic plain language (SRC-059)
- Version control compliance indicators (SRC-127–SRC-128)

Traces: SRC-059, SRC-070, SRC-112–SRC-131
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, datetime
from pathlib import Path

import pytest

from ai_news_agent.curation.prompt_builder import (
    PromptBuilder,
    PromptManifest,
    _format_tier_articles,
    _format_twitter_section,
    _sha256_file,
    compute_all_hashes,
)
from ai_news_agent.storage.models import (
    ArticleRecord,
    TweetSignal,
    normalize_url,
    url_hash,
)

# ---------------------------------------------------------------------------
# Helpers — build sample data
# ---------------------------------------------------------------------------

REAL_PROMPTS_DIR = Path(__file__).parents[2] / "prompts"


def _make_article(
    tier: str,
    index: int = 1,
    with_abstract: bool = True,
    with_twitter: bool = False,
) -> ArticleRecord:
    """Factory for ArticleRecord test instances."""
    raw_url = f"https://example-{tier}.com/article-{index}"
    canon = normalize_url(raw_url)
    return ArticleRecord(
        url_hash=url_hash(canon),
        url=canon,
        headline=f"Tier {tier} Article {index}: AI Changes Everything",
        abstract=f"Abstract for tier {tier} article {index}." if with_abstract else None,
        source_name=f"Source-{tier}-{index}",
        pub_date=datetime(2026, 5, 10, 10, 0, tzinfo=UTC),
        fetched_at=datetime(2026, 5, 10, 12, 0, tzinfo=UTC),
        tier=tier,
        source_class="web" if not with_twitter else "twitter",
        agent_id="test-agent",
        twitter_handle="karpathy" if with_twitter else None,
        tweet_url=f"https://twitter.com/karpathy/status/123{index}" if with_twitter else None,
    )


def _make_tweet(handle: str = "karpathy", weight: float = 1.0) -> TweetSignal:
    """Factory for TweetSignal test instances."""
    return TweetSignal(
        tweet_id="tweet-001",
        handle=handle,
        text=f"This is a significant AI development worth investigating. @{handle}",
        created_at=datetime(2026, 5, 10, 9, 0, tzinfo=UTC),
        linked_urls=["https://reuters.com/ai-story-2026"],
        agent_id="test-agent",
        fetched_at=datetime(2026, 5, 10, 9, 5, tzinfo=UTC),
        weight=weight,
    )


def _make_builder(tmp_path: Path) -> tuple[PromptBuilder, Path]:
    """Create a PromptBuilder pointed at the real prompts directory."""
    # Always use the real prompts for content tests
    return PromptBuilder(prompts_dir=REAL_PROMPTS_DIR), REAL_PROMPTS_DIR


# ---------------------------------------------------------------------------
# TestSha256FileHash
# ---------------------------------------------------------------------------


class TestSha256FileHash:
    """SHA-256 hash computation for prompt file versioning. Traces: SRC-129."""

    def test_hash_format_is_sha256_prefixed(self, tmp_path: Path) -> None:
        """Hash string starts with 'sha256:' and is 71 chars total (SRC-129)."""
        f = tmp_path / "test.md"
        f.write_bytes(b"Hello, prompt!")
        result = _sha256_file(f)
        assert result.startswith("sha256:")
        assert len(result) == len("sha256:") + 64  # 7 + 64

    def test_hash_is_deterministic(self, tmp_path: Path) -> None:
        """Same file content always yields the same hash (SRC-129)."""
        f = tmp_path / "test.md"
        f.write_bytes(b"Stable content")
        h1 = _sha256_file(f)
        h2 = _sha256_file(f)
        assert h1 == h2

    def test_hash_changes_on_content_change(self, tmp_path: Path) -> None:
        """Different content yields a different hash (SRC-129)."""
        f = tmp_path / "test.md"
        f.write_bytes(b"Version 1")
        h1 = _sha256_file(f)
        f.write_bytes(b"Version 2")
        h2 = _sha256_file(f)
        assert h1 != h2

    def test_hash_hex_portion_matches_hashlib(self, tmp_path: Path) -> None:
        """Hash hex portion is identical to hashlib.sha256 on the same bytes."""
        content = b"Verify me carefully"
        f = tmp_path / "test.md"
        f.write_bytes(content)
        expected_hex = hashlib.sha256(content).hexdigest()
        result = _sha256_file(f)
        assert result == f"sha256:{expected_hex}"

    def test_hash_computed_on_raw_bytes_not_text(self, tmp_path: Path) -> None:
        """Hash is on raw bytes so Unicode is handled deterministically."""
        content = "AI café — résumé 🤖".encode()
        f = tmp_path / "test.md"
        f.write_bytes(content)
        result = _sha256_file(f)
        assert result.startswith("sha256:")
        assert len(result) == 71


# ---------------------------------------------------------------------------
# TestFormatTwitterSection
# ---------------------------------------------------------------------------


class TestFormatTwitterSection:
    """Twitter signal section formatting. Traces: SRC-046, SRC-047, SRC-070, SRC-119, SRC-148."""

    def test_api_unavailable_produces_warning_message(self) -> None:
        """When API is unavailable, section contains unavailability note (SRC-148)."""
        section = _format_twitter_section(signals=[], twitter_api_available=False)
        assert "unavailable" in section.lower()
        # Must not suggest the window was quiet — this was an API failure
        assert (
            "SRC-148" in section
            or "unreachable" in section.lower()
            or "unavailable" in section.lower()
        )

    def test_quiet_window_produces_different_message_than_api_down(self) -> None:
        """Empty signals with API available produces a different note than API-down (SRC-148)."""
        api_down = _format_twitter_section(signals=[], twitter_api_available=False)
        quiet_window = _format_twitter_section(signals=[], twitter_api_available=True)
        assert api_down != quiet_window

    def test_quiet_window_does_not_mention_api_error(self) -> None:
        """Quiet-window note should not say the API was unreachable (SRC-148)."""
        section = _format_twitter_section(signals=[], twitter_api_available=True)
        # Should say no posts found, not that API failed
        assert "unreachable" not in section.lower()

    def test_signals_sorted_by_weight_descending(self) -> None:
        """Highest-weight handles appear first in the section (SRC-046)."""
        low = _make_tweet(handle="low_weight", weight=0.5)
        high = _make_tweet(handle="high_weight", weight=2.0)
        section = _format_twitter_section(signals=[low, high], twitter_api_available=True)
        pos_high = section.index("high_weight")
        pos_low = section.index("low_weight")
        assert pos_high < pos_low, "High-weight handle must appear before low-weight"

    def test_section_contains_handle_text_and_tweet(self) -> None:
        """Signal section contains handle and tweet text (SRC-119)."""
        signal = _make_tweet(handle="sama", weight=1.0)
        section = _format_twitter_section(signals=[signal], twitter_api_available=True)
        assert "sama" in section
        assert "significant AI development" in section

    def test_section_contains_linked_urls(self) -> None:
        """Linked URLs from tweets are surfaced in the section (SRC-069)."""
        signal = _make_tweet()
        section = _format_twitter_section(signals=[signal], twitter_api_available=True)
        assert "reuters.com" in section

    def test_section_contains_lead_gen_instruction(self) -> None:
        """Section instructs LLM to use tweets as lead-gen only (SRC-070, SRC-119)."""
        signal = _make_tweet()
        section = _format_twitter_section(signals=[signal], twitter_api_available=True)
        lower = section.lower()
        # Must include the "lead-generation" or "not cite" instruction
        assert any(
            phrase in lower
            for phrase in [
                "lead-gen",
                "lead generation",
                "not cite",
                "do not cite",
                "primary source",
            ]
        )

    def test_section_truncates_tweet_text_at_280_chars(self) -> None:
        """Long tweet text is truncated to 280 chars to avoid context bloat."""
        long_text = "A" * 400
        signal = TweetSignal(
            tweet_id="t002",
            handle="verboseuser",
            text=long_text,
            created_at=datetime(2026, 5, 10, tzinfo=UTC),
            linked_urls=[],
            agent_id="test-agent",
            fetched_at=datetime(2026, 5, 10, tzinfo=UTC),
            weight=1.0,
        )
        section = _format_twitter_section(signals=[signal], twitter_api_available=True)
        # The text in the section should not exceed 280 chars per tweet block
        # (We look for any 280+ char contiguous run from the tweet text)
        assert "A" * 281 not in section

    def test_multiple_signals_all_appear(self) -> None:
        """All supplied signals appear in the section."""
        signals = [
            _make_tweet(handle="karpathy", weight=1.0),
            _make_tweet(handle="sama", weight=1.0),
            _make_tweet(handle="ylecun", weight=0.8),
        ]
        section = _format_twitter_section(signals=signals, twitter_api_available=True)
        assert "karpathy" in section
        assert "sama" in section
        assert "ylecun" in section


# ---------------------------------------------------------------------------
# TestFormatTierArticles
# ---------------------------------------------------------------------------


class TestFormatTierArticles:
    """Tier-separated article formatting. Traces: SRC-016–SRC-021, SRC-011, SRC-027."""

    def test_empty_tier_produces_placeholder(self) -> None:
        """Empty tier produces a readable placeholder, not a blank section."""
        result = _format_tier_articles(candidates=[], tier_key="1b")
        assert "No" in result or "none" in result.lower() or "_(No" in result

    def test_article_headline_appears(self) -> None:
        """Article headline is included in the formatted text."""
        article = _make_article("1b", index=1)
        result = _format_tier_articles(candidates=[article], tier_key="1b")
        assert "AI Changes Everything" in result

    def test_article_url_appears(self) -> None:
        """Article URL appears in the formatted text (SRC-049, SRC-123)."""
        article = _make_article("2", index=1)
        result = _format_tier_articles(candidates=[article], tier_key="2")
        assert "example-2.com" in result

    def test_article_source_name_appears(self) -> None:
        """Source name appears in formatted text (SRC-011)."""
        article = _make_article("1b", index=1)
        result = _format_tier_articles(candidates=[article], tier_key="1b")
        assert article.source_name in result

    def test_article_abstract_included_when_present(self) -> None:
        """Abstract appears when present (SRC-011)."""
        article = _make_article("3", index=1, with_abstract=True)
        result = _format_tier_articles(candidates=[article], tier_key="3")
        assert "Abstract for tier 3" in result

    def test_article_abstract_absent_when_none(self) -> None:
        """No abstract line when article has no abstract."""
        article = _make_article("3", index=1, with_abstract=False)
        result = _format_tier_articles(candidates=[article], tier_key="3")
        assert "Abstract" not in result

    def test_twitter_provenance_noted(self) -> None:
        """Twitter-sourced articles show handle reference (SRC-048)."""
        article = _make_article("2", index=1, with_twitter=True)
        result = _format_tier_articles(candidates=[article], tier_key="2")
        assert "karpathy" in result

    def test_multiple_articles_numbered_sequentially(self) -> None:
        """Multiple articles are numbered starting from 1."""
        articles = [_make_article("1b", index=i) for i in range(1, 4)]
        result = _format_tier_articles(candidates=articles, tier_key="1b")
        assert "1." in result
        assert "2." in result
        assert "3." in result

    def test_wrong_tier_articles_filtered_out(self) -> None:
        """Articles from other tiers are not included in this tier's section."""
        tier2_article = _make_article("2", index=1)
        tier3_article = _make_article("3", index=1)
        result = _format_tier_articles(candidates=[tier2_article, tier3_article], tier_key="2")
        assert "example-2.com" in result
        assert "example-3.com" not in result


# ---------------------------------------------------------------------------
# TestPromptBuilderBuild — real prompt templates
# ---------------------------------------------------------------------------


class TestPromptBuilderBuild:
    """PromptBuilder.build() with real prompt templates. Traces: SRC-115–SRC-124, SRC-129."""

    @pytest.fixture(autouse=True)
    def builder(self) -> None:
        """Use the real prompts directory for all tests in this class."""
        self._builder = PromptBuilder(prompts_dir=REAL_PROMPTS_DIR)
        self._start = datetime(2026, 5, 9, 0, 0, tzinfo=UTC)
        self._end = datetime(2026, 5, 9, 23, 59, tzinfo=UTC)

    def _build(
        self,
        cadence: str = "daily",
        candidates: list | None = None,
        signals: list | None = None,
        twitter_available: bool = True,
        top_n: int = 10,
        window_start: datetime | None = None,
        window_end: datetime | None = None,
    ) -> tuple[str, str]:
        return self._builder.build(
            cadence=cadence,
            window_start=window_start or self._start,
            window_end=window_end or self._end,
            tweet_signals=signals or [],
            top_n=top_n,
            candidates=candidates or [],
            twitter_api_available=twitter_available,
        )

    # ------ SRC-116: ISO dates ---------

    def test_iso_dates_injected_daily(self) -> None:
        """Concrete ISO dates are injected into the daily prompt (SRC-116)."""
        prompt, _ = self._build(cadence="daily")
        assert "2026-05-09" in prompt
        assert "{{window_start_iso}}" not in prompt
        assert "{{window_end_iso}}" not in prompt

    def test_no_relative_date_phrases_remain_after_injection(self) -> None:
        """No unreplaced date placeholders remain after injection (SRC-116)."""
        for cadence in ("daily", "weekly", "monthly", "annual"):
            start = datetime(2026, 5, 4, 0, 0, tzinfo=UTC)
            end = datetime(2026, 5, 10, 23, 59, tzinfo=UTC)
            prompt, _ = self._build(cadence=cadence, window_start=start, window_end=end)
            assert "{{window_start_iso}}" not in prompt, f"{cadence}: start placeholder unreplaced"
            assert "{{window_end_iso}}" not in prompt, f"{cadence}: end placeholder unreplaced"

    def test_iso_dates_correct_in_weekly_prompt(self) -> None:
        """Weekly prompt receives correct Sunday–Saturday ISO dates (SRC-116, SRC-030)."""
        start = datetime(2026, 5, 3, 0, 0, tzinfo=UTC)  # Sunday
        end = datetime(2026, 5, 9, 23, 59, tzinfo=UTC)  # Saturday
        prompt, _ = self._build(cadence="weekly", window_start=start, window_end=end)
        assert "2026-05-03" in prompt
        assert "2026-05-09" in prompt

    def test_iso_dates_correct_in_monthly_prompt(self) -> None:
        """Monthly prompt receives correct first-to-last day ISO dates (SRC-116, SRC-031)."""
        start = datetime(2026, 4, 1, 0, 0, tzinfo=UTC)
        end = datetime(2026, 4, 30, 23, 59, tzinfo=UTC)
        prompt, _ = self._build(cadence="monthly", window_start=start, window_end=end)
        assert "2026-04-01" in prompt
        assert "2026-04-30" in prompt

    def test_iso_dates_correct_in_annual_prompt(self) -> None:
        """Annual prompt receives correct Jan 1–Dec 31 ISO dates (SRC-116, SRC-032)."""
        start = datetime(2025, 1, 1, 0, 0, tzinfo=UTC)
        end = datetime(2025, 12, 31, 23, 59, tzinfo=UTC)
        prompt, _ = self._build(cadence="annual", window_start=start, window_end=end)
        assert "2025-01-01" in prompt
        assert "2025-12-31" in prompt

    # ------ SRC-117: disqualifier ---------

    def test_disqualifier_present_in_daily_prompt(self) -> None:
        """Daily prompt explicitly lists disqualified technical content (SRC-117)."""
        prompt, _ = self._build(cadence="daily")
        lower = prompt.lower()
        # Must mention implementation tutorials and/or model architecture papers
        assert any(
            phrase in lower
            for phrase in [
                "implementation tutorial",
                "architecture paper",
                "benchmark",
                "coding walkthrough",
                "how-to",
            ]
        )

    def test_disqualifier_present_in_all_cadences(self) -> None:
        """All cadence prompts contain explicit disqualifier content (SRC-117)."""
        for cadence in ("daily", "weekly", "monthly", "annual"):
            prompt, _ = self._build(cadence=cadence)
            lower = prompt.lower()
            assert any(
                phrase in lower
                for phrase in [
                    "implementation tutorial",
                    "architecture paper",
                    "benchmark",
                    "exclude",
                    "disqualif",
                ]
            ), f"{cadence} prompt missing disqualifier content (SRC-117)"

    # ------ SRC-118: inclusion criteria ---------

    def test_inclusion_criteria_business_impact_present(self) -> None:
        """All prompts include 'business impact' inclusion criteria (SRC-118)."""
        for cadence in ("daily", "weekly", "monthly", "annual"):
            prompt, _ = self._build(cadence=cadence)
            assert "business impact" in prompt.lower(), (
                f"{cadence} prompt missing 'business impact' inclusion criteria (SRC-118)"
            )

    def test_inclusion_criteria_workforce_impact_present(self) -> None:
        """All prompts include 'workforce' or 'societal impact' criteria (SRC-118)."""
        for cadence in ("daily", "weekly", "monthly", "annual"):
            prompt, _ = self._build(cadence=cadence)
            lower = prompt.lower()
            assert "workforce" in lower or "societal" in lower, (
                f"{cadence} prompt missing workforce/societal impact criteria (SRC-118)"
            )

    def test_inclusion_criteria_policy_impact_present(self) -> None:
        """All prompts include 'policy impact' or 'strategic' criteria (SRC-118)."""
        for cadence in ("daily", "weekly", "monthly", "annual"):
            prompt, _ = self._build(cadence=cadence)
            lower = prompt.lower()
            assert "policy impact" in lower or "strategic" in lower, (
                f"{cadence} prompt missing policy/strategic impact criteria (SRC-118)"
            )

    def test_inclusion_criteria_include_examples(self) -> None:
        """Inclusion criteria prompts include concrete examples (SRC-118)."""
        # Check that at least one example keyword appears per cadence
        example_keywords = ["example", "e.g.", "such as", "including"]
        for cadence in ("daily", "weekly", "monthly", "annual"):
            prompt, _ = self._build(cadence=cadence)
            lower = prompt.lower()
            assert any(kw in lower for kw in example_keywords), (
                f"{cadence} prompt missing inclusion examples (SRC-118)"
            )

    # ------ SRC-119: Twitter signal section ---------

    def test_twitter_signal_section_present_in_all_cadences(self) -> None:
        """All prompts include an influencer signal section (SRC-119)."""
        for cadence in ("daily", "weekly", "monthly", "annual"):
            prompt, _ = self._build(
                cadence=cadence, signals=[_make_tweet()], twitter_available=True
            )
            lower = prompt.lower()
            assert "influencer" in lower or "signal" in lower, (
                f"{cadence} prompt missing influencer signal section (SRC-119)"
            )

    def test_twitter_signal_not_primary_citation_instruction(self) -> None:
        """All prompts instruct LLM not to cite tweets as primary sources (SRC-047, SRC-070)."""
        for cadence in ("daily", "weekly", "monthly", "annual"):
            prompt, _ = self._build(cadence=cadence, signals=[_make_tweet()])
            lower = prompt.lower()
            # Must contain "not cite" or "do not cite" or equivalent
            has_instruction = any(
                phrase in lower
                for phrase in [
                    "not cite",
                    "do not cite",
                    "not a primary source",
                    "lead-gen",
                    "lead generation",
                ]
            )
            assert has_instruction, (
                f"{cadence} prompt missing 'do not cite tweet' instruction (SRC-070, SRC-119)"
            )

    def test_twitter_signal_handle_appears_in_prompt(self) -> None:
        """Tweet handle from signals appears in the prompt (SRC-119)."""
        signal = _make_tweet(handle="drfeifei")
        for cadence in ("daily", "weekly"):
            prompt, _ = self._build(cadence=cadence, signals=[signal])
            assert "drfeifei" in prompt, f"{cadence}: handle not injected (SRC-119)"

    def test_api_unavailable_note_in_daily_prompt(self) -> None:
        """API-unavailable note appears in prompt when twitter_available=False (SRC-148)."""
        prompt, _ = self._build(cadence="daily", signals=[], twitter_available=False)
        lower = prompt.lower()
        assert "unavailable" in lower or "unreachable" in lower, (
            "API-unavailable note missing from prompt (SRC-148)"
        )

    def test_quiet_window_note_differs_from_api_down(self) -> None:
        """Quiet window and API-down produce different prompt text (SRC-148)."""
        prompt_down, _ = self._build(cadence="daily", signals=[], twitter_available=False)
        prompt_quiet, _ = self._build(cadence="daily", signals=[], twitter_available=True)
        assert prompt_down != prompt_quiet

    # ------ SRC-120: structured output format ---------

    def test_json_block_schema_in_all_prompts(self) -> None:
        """All prompts include a JSON output schema block (SRC-120)."""
        for cadence in ("daily", "weekly", "monthly", "annual"):
            prompt, _ = self._build(cadence=cadence)
            assert "```json" in prompt, f"{cadence} missing JSON output block (SRC-120)"

    def test_json_schema_contains_required_fields(self) -> None:
        """JSON schema block contains all required fields (SRC-048, SRC-120)."""
        required_fields = [
            "headline",
            "source_name",
            "url",
            "pub_date",
            "why_it_matters",
            "impact_tags",
            "tier",
        ]
        for cadence in ("daily", "weekly", "monthly", "annual"):
            prompt, _ = self._build(cadence=cadence)
            for field_name in required_fields:
                assert f'"{field_name}"' in prompt, (
                    f'{cadence} JSON schema missing "{field_name}" field (SRC-048, SRC-120)'
                )

    def test_json_schema_has_twitter_fields(self) -> None:
        """JSON schema includes twitter_handle and tweet_url fields (SRC-048)."""
        for cadence in ("daily", "weekly", "monthly", "annual"):
            prompt, _ = self._build(cadence=cadence)
            assert "twitter_handle" in prompt, f"{cadence} missing twitter_handle field"
            assert "tweet_url" in prompt, f"{cadence} missing tweet_url field"

    def test_json_schema_has_themes_and_outlook(self) -> None:
        """JSON schema includes themes and outlook fields (SRC-030, SRC-031)."""
        for cadence in ("daily", "weekly", "monthly", "annual"):
            prompt, _ = self._build(cadence=cadence)
            assert '"themes"' in prompt, f"{cadence} missing themes field"
            assert '"outlook"' in prompt, f"{cadence} missing outlook field"

    def test_annual_json_schema_has_predictions(self) -> None:
        """Annual JSON schema includes predictions field (SRC-032, SRC-124)."""
        start = datetime(2025, 1, 1, tzinfo=UTC)
        end = datetime(2025, 12, 31, tzinfo=UTC)
        prompt, _ = self._build(cadence="annual", window_start=start, window_end=end)
        assert '"predictions"' in prompt, "Annual prompt missing predictions field (SRC-124)"

    def test_non_annual_json_schema_has_empty_predictions(self) -> None:
        """Non-annual prompts have predictions as empty array in schema (SRC-120)."""
        for cadence in ("daily", "weekly", "monthly"):
            prompt, _ = self._build(cadence=cadence)
            assert '"predictions": []' in prompt or '"predictions":[]' in prompt, (
                f"{cadence} should have empty predictions array, not populated (SRC-120)"
            )

    # ------ SRC-121: search budget ---------

    def test_daily_search_budget_is_normal(self) -> None:
        """Daily prompt specifies a normal/limited search budget (SRC-121)."""
        prompt, _ = self._build(cadence="daily")
        lower = prompt.lower()
        assert (
            "5 additional web search" in lower
            or "normal search budget" in lower
            or "up to 5" in lower
        )

    def test_weekly_search_budget_is_normal_with_10(self) -> None:
        """Weekly prompt specifies a normal search budget with 10 searches (SRC-121)."""
        prompt, _ = self._build(cadence="weekly")
        lower = prompt.lower()
        assert (
            "10 additional web search" in lower
            or "up to 10" in lower
            or "normal search budget" in lower
        )

    def test_monthly_search_budget_is_deep(self) -> None:
        """Monthly prompt specifies a deep search budget (SRC-121)."""
        prompt, _ = self._build(cadence="monthly")
        lower = prompt.lower()
        assert "deep" in lower
        assert "search budget" in lower or "web search" in lower

    def test_annual_search_budget_is_deepest(self) -> None:
        """Annual prompt specifies the deepest search budget (SRC-121)."""
        start = datetime(2025, 1, 1, tzinfo=UTC)
        end = datetime(2025, 12, 31, tzinfo=UTC)
        prompt, _ = self._build(cadence="annual", window_start=start, window_end=end)
        lower = prompt.lower()
        assert "deep" in lower
        assert "40" in prompt or "40 additional" in lower

    def test_search_budget_directive_placeholder_replaced(self) -> None:
        """{{search_budget_directive}} placeholder is fully substituted (SRC-121)."""
        for cadence in ("daily", "weekly", "monthly", "annual"):
            prompt, _ = self._build(cadence=cadence)
            assert "{{search_budget_directive}}" not in prompt, (
                f"{cadence}: search_budget_directive placeholder not replaced"
            )

    # ------ SRC-122: "why it matters" ---------

    def test_why_it_matters_required_in_all_prompts(self) -> None:
        """All prompts require a 'why it matters' justification per item (SRC-122)."""
        for cadence in ("daily", "weekly", "monthly", "annual"):
            prompt, _ = self._build(cadence=cadence)
            lower = prompt.lower()
            assert "why it matters" in lower, (
                f"{cadence} missing 'why it matters' requirement (SRC-122)"
            )

    def test_why_it_matters_specifies_2_3_sentences(self) -> None:
        """All prompts specify 2–3 sentences for 'why it matters' (SRC-122)."""
        for cadence in ("daily", "weekly", "monthly", "annual"):
            prompt, _ = self._build(cadence=cadence)
            lower = prompt.lower()
            assert "2–3 sentence" in lower or "2-3 sentence" in lower, (
                f"{cadence} missing '2–3 sentences' why_it_matters spec (SRC-122)"
            )

    # ------ SRC-123: working links ---------

    def test_working_link_required_in_all_prompts(self) -> None:
        """All prompts require working links to primary sources (SRC-123)."""
        for cadence in ("daily", "weekly", "monthly", "annual"):
            prompt, _ = self._build(cadence=cadence)
            lower = prompt.lower()
            # Must explicitly require working/verifiable URLs
            assert any(
                phrase in lower
                for phrase in [
                    "working link",
                    "working url",
                    "verifiable url",
                    "verified url",
                    "must be omitted",
                ]
            ), f"{cadence} missing working link requirement (SRC-123)"

    def test_no_url_items_dropped_instruction(self) -> None:
        """All prompts state that items without URLs must be omitted (SRC-123, SRC-049)."""
        for cadence in ("daily", "weekly", "monthly", "annual"):
            prompt, _ = self._build(cadence=cadence)
            lower = prompt.lower()
            assert "omit" in lower or "drop" in lower, (
                f"{cadence} missing 'omit items without URL' instruction (SRC-123)"
            )

    # ------ SRC-124: annual-only sections ---------

    def test_annual_prompt_has_inflection_points_section(self) -> None:
        """Annual prompt contains inflection points section (SRC-124)."""
        start = datetime(2025, 1, 1, tzinfo=UTC)
        end = datetime(2025, 12, 31, tzinfo=UTC)
        prompt, _ = self._build(cadence="annual", window_start=start, window_end=end)
        lower = prompt.lower()
        assert "inflection point" in lower, "Annual prompt missing inflection points (SRC-124)"

    def test_annual_prompt_has_predictions_section(self) -> None:
        """Annual prompt contains predictions section (SRC-032, SRC-124)."""
        start = datetime(2025, 1, 1, tzinfo=UTC)
        end = datetime(2025, 12, 31, tzinfo=UTC)
        prompt, _ = self._build(cadence="annual", window_start=start, window_end=end)
        lower = prompt.lower()
        assert "prediction" in lower, "Annual prompt missing predictions section (SRC-032)"

    def test_annual_predictions_specify_10(self) -> None:
        """Annual prompt specifies exactly 10 predictions (SRC-032)."""
        start = datetime(2025, 1, 1, tzinfo=UTC)
        end = datetime(2025, 12, 31, tzinfo=UTC)
        prompt, _ = self._build(cadence="annual", window_start=start, window_end=end)
        assert "10" in prompt
        assert "prediction" in prompt.lower()

    def test_annual_predictions_require_reasoning(self) -> None:
        """Annual predictions must show explicit reasoning, not assertions (SRC-124)."""
        start = datetime(2025, 1, 1, tzinfo=UTC)
        end = datetime(2025, 12, 31, tzinfo=UTC)
        prompt, _ = self._build(cadence="annual", window_start=start, window_end=end)
        lower = prompt.lower()
        assert "reasoning" in lower or "grounded" in lower or "evidence" in lower

    def test_annual_predictions_must_be_falsifiable(self) -> None:
        """Annual prompt requires predictions to be specific and falsifiable (SRC-124)."""
        start = datetime(2025, 1, 1, tzinfo=UTC)
        end = datetime(2025, 12, 31, tzinfo=UTC)
        prompt, _ = self._build(cadence="annual", window_start=start, window_end=end)
        lower = prompt.lower()
        assert "falsifiable" in lower or "specific" in lower

    def test_annual_prompt_has_year_and_year_plus_1(self) -> None:
        """Annual prompt contains both the year and year+1 (SRC-124)."""
        start = datetime(2025, 1, 1, tzinfo=UTC)
        end = datetime(2025, 12, 31, tzinfo=UTC)
        prompt, _ = self._build(cadence="annual", window_start=start, window_end=end)
        assert "2025" in prompt, "Annual prompt missing year (SRC-124)"
        assert "2026" in prompt, "Annual prompt missing year+1 (SRC-124)"

    def test_annual_year_placeholders_replaced(self) -> None:
        """{{year}} and {{year_plus_1}} placeholders are fully substituted in annual prompt."""
        start = datetime(2025, 1, 1, tzinfo=UTC)
        end = datetime(2025, 12, 31, tzinfo=UTC)
        prompt, _ = self._build(cadence="annual", window_start=start, window_end=end)
        assert "{{year}}" not in prompt
        assert "{{year_plus_1}}" not in prompt

    def test_annual_themes_for_signal_vs_noise(self) -> None:
        """Annual prompt includes signal vs noise analysis (SRC-124)."""
        start = datetime(2025, 1, 1, tzinfo=UTC)
        end = datetime(2025, 12, 31, tzinfo=UTC)
        prompt, _ = self._build(cadence="annual", window_start=start, window_end=end)
        lower = prompt.lower()
        assert "signal" in lower, "Annual prompt missing 'signal' (SRC-124)"
        assert "noise" in lower, "Annual prompt missing 'noise' (SRC-124)"

    def test_weekly_prompt_has_themes_section(self) -> None:
        """Weekly prompt has dominant themes section (SRC-030)."""
        start = datetime(2026, 5, 3, 0, 0, tzinfo=UTC)
        end = datetime(2026, 5, 9, 23, 59, tzinfo=UTC)
        prompt, _ = self._build(cadence="weekly", window_start=start, window_end=end)
        lower = prompt.lower()
        assert "theme" in lower, "Weekly prompt missing themes section (SRC-030)"

    def test_weekly_prompt_has_what_to_watch(self) -> None:
        """Weekly prompt has a 'what to watch' forward-looking section (SRC-030)."""
        start = datetime(2026, 5, 3, 0, 0, tzinfo=UTC)
        end = datetime(2026, 5, 9, 23, 59, tzinfo=UTC)
        prompt, _ = self._build(cadence="weekly", window_start=start, window_end=end)
        lower = prompt.lower()
        assert "what to watch" in lower or "next week" in lower, (
            "Weekly prompt missing forward-looking section (SRC-030)"
        )

    def test_monthly_prompt_has_anticipated_news(self) -> None:
        """Monthly prompt includes anticipated developments section (SRC-031)."""
        start = datetime(2026, 4, 1, 0, 0, tzinfo=UTC)
        end = datetime(2026, 4, 30, 23, 59, tzinfo=UTC)
        prompt, _ = self._build(cadence="monthly", window_start=start, window_end=end)
        lower = prompt.lower()
        assert "anticipated" in lower or "next month" in lower or "watch for" in lower, (
            "Monthly prompt missing anticipated developments section (SRC-031)"
        )

    # ------ top_n injection ---------

    def test_top_n_injected_correctly(self) -> None:
        """{{top_n}} is replaced with the correct integer value."""
        prompt, _ = self._build(cadence="daily", top_n=7)
        assert "{{top_n}}" not in prompt
        assert "7" in prompt

    def test_top_n_appears_in_output_requirements(self) -> None:
        """top_n value appears in the output requirements section."""
        prompt, _ = self._build(cadence="daily", top_n=12)
        assert "12" in prompt

    # ------ SRC-129: SHA-256 hash return ---------

    def test_build_returns_sha256_version(self) -> None:
        """build() returns a sha256-prefixed version string (SRC-129)."""
        _, version = self._build(cadence="daily")
        assert version.startswith("sha256:")
        assert len(version) == 71  # "sha256:" + 64-char hex

    def test_prompt_version_is_hash_of_raw_template(self) -> None:
        """The returned version is the SHA-256 of the raw prompt file bytes (SRC-129)."""
        daily_path = REAL_PROMPTS_DIR / "daily.md"
        expected = f"sha256:{hashlib.sha256(daily_path.read_bytes()).hexdigest()}"
        _, version = self._build(cadence="daily")
        assert version == expected

    def test_different_cadences_have_different_versions(self) -> None:
        """Each cadence has a unique prompt file and therefore a unique version (SRC-129)."""
        versions = set()
        for cadence in ("daily", "weekly", "monthly", "annual"):
            _, version = self._build(cadence=cadence)
            versions.add(version)
        assert len(versions) == 4, "All four cadences should have distinct prompt hashes"

    def test_version_is_stable_across_calls(self) -> None:
        """The same cadence always returns the same version (SRC-129)."""
        _, v1 = self._build(cadence="daily")
        _, v2 = self._build(cadence="daily")
        assert v1 == v2

    def test_version_changes_when_template_content_changes(self, tmp_path: Path) -> None:
        """Modifying a prompt file changes its version hash (SRC-129)."""
        # Copy real prompts to tmp, modify daily
        for _cadence, fname in [
            ("daily", "daily.md"),
            ("weekly", "weekly.md"),
            ("monthly", "monthly.md"),
            ("annual", "annual.md"),
        ]:
            src = REAL_PROMPTS_DIR / fname
            (tmp_path / fname).write_bytes(src.read_bytes())

        builder = PromptBuilder(prompts_dir=tmp_path)
        _, v1 = builder.build(
            cadence="daily",
            window_start=self._start,
            window_end=self._end,
            tweet_signals=[],
            top_n=10,
        )
        # Append a byte to the daily template
        daily_path = tmp_path / "daily.md"
        daily_path.write_bytes(daily_path.read_bytes() + b"\n# Modified")
        _, v2 = builder.build(
            cadence="daily",
            window_start=self._start,
            window_end=self._end,
            tweet_signals=[],
            top_n=10,
        )
        assert v1 != v2, "Version must change when template content changes (SRC-129)"

    # ------ Tier article injection ---------

    def test_tier_articles_injected_into_prompt(self) -> None:
        """Tier-separated articles appear in the built prompt (SRC-016–SRC-021)."""
        candidates = [
            _make_article("1b", index=1),
            _make_article("2", index=1),
        ]
        prompt, _ = self._build(cadence="daily", candidates=candidates)
        assert "Tier 1b Article 1" in prompt, "Tier 1b article missing from prompt"
        assert "Tier 2 Article 1" in prompt, "Tier 2 article missing from prompt"

    def test_tier_placeholders_all_replaced(self) -> None:
        """All {{tier_*_articles}} placeholders are replaced in all cadences (SRC-016–SRC-021)."""
        tier_placeholders = [
            "{{tier_1a_articles}}",
            "{{tier_1b_articles}}",
            "{{tier_2_articles}}",
            "{{tier_3_articles}}",
            "{{tier_4_articles}}",
        ]
        for cadence in ("daily", "weekly", "monthly", "annual"):
            prompt, _ = self._build(cadence=cadence)
            for ph in tier_placeholders:
                assert ph not in prompt, f"{cadence}: placeholder {ph!r} was not replaced"

    def test_empty_tier_shows_placeholder_text(self) -> None:
        """Empty tier produces placeholder text, not blank section."""
        prompt, _ = self._build(cadence="daily", candidates=[])
        # Tier sections should show placeholder, not bare blank
        assert "No" in prompt or "_(No" in prompt

    def test_tier_articles_in_correct_sections(self) -> None:
        """Articles appear under their correct tier section, not cross-contaminated."""
        tier1b = _make_article("1b", index=99)
        tier3 = _make_article("3", index=77)
        prompt, _ = self._build(cadence="daily", candidates=[tier1b, tier3])
        # Both headlines should appear
        assert "Tier 1b Article 99" in prompt
        assert "Tier 3 Article 77" in prompt

    def test_twitter_sourced_articles_note_origin(self) -> None:
        """Articles sourced via Twitter show handle reference in tier section (SRC-048)."""
        article = _make_article("2", index=1, with_twitter=True)
        prompt, _ = self._build(cadence="daily", candidates=[article])
        assert "karpathy" in prompt

    # ------ SRC-059: provider-agnostic plain language ---------

    def test_no_provider_specific_syntax_in_prompts(self) -> None:
        """Prompts use plain natural language, not provider-specific tokens (SRC-059)."""
        provider_patterns = [
            r"<\|im_start\|>",  # OpenAI legacy
            r"<\|im_end\|>",
            r"\[INST\]",  # Llama/Mistral
            r"\[/INST\]",
            r"Human:",  # Claude-specific formatting
            r"Assistant:",
        ]
        for cadence in ("daily", "weekly", "monthly", "annual"):
            prompt, _ = self._build(cadence=cadence)
            for pattern in provider_patterns:
                assert not re.search(pattern, prompt), (
                    f"{cadence} prompt contains provider-specific token {pattern!r} (SRC-059)"
                )

    # ------ SRC-127–SRC-128: version control indicators ---------

    def test_prompts_contain_version_control_notice(self) -> None:
        """Prompt files contain version control notice per SRC-127, SRC-128."""
        for fname in ("daily.md", "weekly.md", "monthly.md", "annual.md"):
            content = (REAL_PROMPTS_DIR / fname).read_text(encoding="utf-8")
            # Should have review notice
            assert "review" in content.lower() or "SRC-128" in content, (
                f"{fname} missing version control notice (SRC-127–SRC-128)"
            )

    def test_prompts_contain_src_trace_comments(self) -> None:
        """Prompt files contain SRC-* trace comments (SRC-127)."""
        for fname in ("daily.md", "weekly.md", "monthly.md", "annual.md"):
            content = (REAL_PROMPTS_DIR / fname).read_text(encoding="utf-8")
            assert "SRC-" in content, f"{fname} missing SRC-* trace comments (SRC-127)"

    def test_prompts_contain_prompt_version_comment(self) -> None:
        """Prompt files document that SHA-256 hash is computed at runtime (SRC-129)."""
        for fname in ("daily.md", "weekly.md", "monthly.md", "annual.md"):
            content = (REAL_PROMPTS_DIR / fname).read_text(encoding="utf-8")
            assert "sha256" in content.lower() or "PROMPT VERSION" in content, (
                f"{fname} missing SHA-256 / prompt version comment (SRC-129)"
            )

    # ------ Missing prompt file ---------

    def test_missing_prompt_file_raises_file_not_found_error(self, tmp_path: Path) -> None:
        """FileNotFoundError raised when prompt file is absent (SRC-113)."""
        builder = PromptBuilder(prompts_dir=tmp_path)  # empty dir
        with pytest.raises(FileNotFoundError, match="SRC-113"):
            builder.build(
                cadence="daily",
                window_start=self._start,
                window_end=self._end,
                tweet_signals=[],
                top_n=10,
            )

    def test_custom_prompt_override_loads_file(self, tmp_path: Path) -> None:
        """curation_prompt_override loads a custom prompt file."""
        custom = tmp_path / "custom.md"
        custom.write_text(
            "# Custom Prompt\n"
            "Window: {{window_start_iso}} to {{window_end_iso}}\n"
            "Budget: {{search_budget_directive}}\n"
            "Twitter: {{twitter_signal_section}}\n"
            "Top N: {{top_n}}\n"
            "Year: {{year}}\nYear+1: {{year_plus_1}}\n"
            "{{tier_1a_articles}}\n{{tier_1b_articles}}\n"
            "{{tier_2_articles}}\n{{tier_3_articles}}\n{{tier_4_articles}}\n",
            encoding="utf-8",
        )
        prompt, version = self._builder.build(
            cadence="daily",
            window_start=self._start,
            window_end=self._end,
            tweet_signals=[],
            top_n=5,
            curation_prompt_override=str(custom),
        )
        assert "2026-05-09" in prompt
        assert "Custom Prompt" in prompt
        assert version.startswith("sha256:")


# ---------------------------------------------------------------------------
# TestGetPromptVersion
# ---------------------------------------------------------------------------


class TestGetPromptVersion:
    """PromptBuilder.get_prompt_version() method. Traces: SRC-129."""

    def test_get_prompt_version_returns_sha256_string(self) -> None:
        """get_prompt_version returns valid sha256 string for all cadences (SRC-129)."""
        builder = PromptBuilder(prompts_dir=REAL_PROMPTS_DIR)
        for cadence in ("daily", "weekly", "monthly", "annual"):
            version = builder.get_prompt_version(cadence)
            assert version.startswith("sha256:")
            assert len(version) == 71

    def test_get_prompt_version_matches_build_version(self) -> None:
        """get_prompt_version returns the same hash as build() (SRC-129)."""
        builder = PromptBuilder(prompts_dir=REAL_PROMPTS_DIR)
        for cadence in ("daily", "weekly", "monthly", "annual"):
            v_get = builder.get_prompt_version(cadence)
            _, v_build = builder.build(
                cadence=cadence,
                window_start=datetime(2026, 1, 1, tzinfo=UTC),
                window_end=datetime(2026, 1, 31, tzinfo=UTC),
                tweet_signals=[],
                top_n=10,
            )
            assert v_get == v_build, (
                f"{cadence}: get_prompt_version {v_get!r} != build version {v_build!r}"
            )

    def test_get_prompt_version_raises_for_missing_file(self, tmp_path: Path) -> None:
        """FileNotFoundError raised when prompt file is absent."""
        builder = PromptBuilder(prompts_dir=tmp_path)
        with pytest.raises(FileNotFoundError):
            builder.get_prompt_version("daily")


# ---------------------------------------------------------------------------
# TestPromptManifest
# ---------------------------------------------------------------------------


class TestPromptManifest:
    """PromptManifest generation, persistence, and verification. Traces: SRC-129."""

    def test_from_dir_returns_manifest_with_all_cadences(self) -> None:
        """from_dir() produces a manifest with non-empty hashes for all cadences (SRC-129)."""
        manifest = PromptManifest.from_dir(REAL_PROMPTS_DIR)
        assert manifest.daily.startswith("sha256:")
        assert manifest.weekly.startswith("sha256:")
        assert manifest.monthly.startswith("sha256:")
        assert manifest.annual.startswith("sha256:")

    def test_from_dir_sets_generated_at(self) -> None:
        """from_dir() populates generated_at with a UTC timestamp."""
        manifest = PromptManifest.from_dir(REAL_PROMPTS_DIR)
        assert manifest.generated_at != ""
        # Should look like a UTC ISO timestamp
        assert "Z" in manifest.generated_at or "T" in manifest.generated_at

    def test_all_four_hashes_are_distinct(self) -> None:
        """All four cadence hashes should be distinct (different files)."""
        manifest = PromptManifest.from_dir(REAL_PROMPTS_DIR)
        hashes = [manifest.daily, manifest.weekly, manifest.monthly, manifest.annual]
        assert len(set(hashes)) == 4, "All four cadence hashes should be unique"

    def test_to_dict_contains_all_cadences(self) -> None:
        """to_dict() returns a dict with all cadence keys (SRC-129)."""
        manifest = PromptManifest.from_dir(REAL_PROMPTS_DIR)
        d = manifest.to_dict()
        assert "daily" in d
        assert "weekly" in d
        assert "monthly" in d
        assert "annual" in d
        assert "generated_at" in d

    def test_save_and_load_roundtrip(self, tmp_path: Path) -> None:
        """Saved manifest can be loaded back with identical hashes (SRC-129)."""
        manifest = PromptManifest.from_dir(REAL_PROMPTS_DIR)
        manifest_path = tmp_path / "prompt_hashes.json"
        manifest.save(manifest_path)

        loaded = PromptManifest.load(manifest_path)
        assert loaded.daily == manifest.daily
        assert loaded.weekly == manifest.weekly
        assert loaded.monthly == manifest.monthly
        assert loaded.annual == manifest.annual

    def test_saved_manifest_is_valid_json(self, tmp_path: Path) -> None:
        """Saved manifest file is valid JSON (SRC-129)."""
        manifest = PromptManifest.from_dir(REAL_PROMPTS_DIR)
        manifest_path = tmp_path / "prompt_hashes.json"
        manifest.save(manifest_path)
        data = json.loads(manifest_path.read_text())
        assert isinstance(data, dict)
        assert data["daily"].startswith("sha256:")

    def test_saved_manifest_file_exists(self, tmp_path: Path) -> None:
        """save() creates the file at the specified path."""
        manifest = PromptManifest.from_dir(REAL_PROMPTS_DIR)
        manifest_path = tmp_path / "prompt_hashes.json"
        assert not manifest_path.exists()
        manifest.save(manifest_path)
        assert manifest_path.exists()

    def test_get_method_returns_correct_hash(self) -> None:
        """get(cadence) returns the correct hash for each cadence."""
        manifest = PromptManifest.from_dir(REAL_PROMPTS_DIR)
        assert manifest.get("daily") == manifest.daily
        assert manifest.get("weekly") == manifest.weekly
        assert manifest.get("monthly") == manifest.monthly
        assert manifest.get("annual") == manifest.annual

    def test_get_method_raises_for_unknown_cadence(self) -> None:
        """get() raises KeyError for unknown cadence name."""
        manifest = PromptManifest.from_dir(REAL_PROMPTS_DIR)
        with pytest.raises(KeyError):
            manifest.get("hourly")

    def test_from_dir_raises_for_missing_file(self, tmp_path: Path) -> None:
        """from_dir() raises FileNotFoundError when a cadence file is missing."""
        with pytest.raises(FileNotFoundError, match="SRC-113"):
            PromptManifest.from_dir(tmp_path)

    def test_verify_detects_changed_prompt(self, tmp_path: Path) -> None:
        """Mismatch between saved and current hashes is detectable (SRC-128)."""
        # Copy prompts to tmp, save manifest, then modify a file
        for fname in ("daily.md", "weekly.md", "monthly.md", "annual.md"):
            (tmp_path / fname).write_bytes((REAL_PROMPTS_DIR / fname).read_bytes())

        manifest = PromptManifest.from_dir(tmp_path)
        manifest_path = tmp_path / "prompt_hashes.json"
        manifest.save(manifest_path)

        # Modify daily.md — simulating an unreviewed change
        daily = tmp_path / "daily.md"
        daily.write_bytes(daily.read_bytes() + b"\n# Unreviewed change")

        updated = PromptManifest.from_dir(tmp_path)
        saved = PromptManifest.load(manifest_path)

        assert updated.daily != saved.daily, (
            "Modified prompt file must produce a different hash (SRC-128)"
        )

    def test_prompt_manifest_matches_prompt_hashes_json(self) -> None:
        """The checked-in prompt_hashes.json matches current file hashes (SRC-129)."""
        manifest_path = REAL_PROMPTS_DIR / "prompt_hashes.json"
        if not manifest_path.exists():
            pytest.skip("prompt_hashes.json not yet generated — run ai-news-prompt-hashes --save")

        saved = PromptManifest.load(manifest_path)
        current = PromptManifest.from_dir(REAL_PROMPTS_DIR)

        assert current.daily == saved.daily, "daily.md hash mismatch with saved manifest"
        assert current.weekly == saved.weekly, "weekly.md hash mismatch with saved manifest"
        assert current.monthly == saved.monthly, "monthly.md hash mismatch with saved manifest"
        assert current.annual == saved.annual, "annual.md hash mismatch with saved manifest"

    def test_get_manifest_from_builder_matches_from_dir(self) -> None:
        """PromptBuilder.get_manifest() returns the same manifest as from_dir() (SRC-129)."""
        builder = PromptBuilder(prompts_dir=REAL_PROMPTS_DIR)
        builder_manifest = builder.get_manifest()
        direct_manifest = PromptManifest.from_dir(REAL_PROMPTS_DIR)

        assert builder_manifest.daily == direct_manifest.daily
        assert builder_manifest.weekly == direct_manifest.weekly
        assert builder_manifest.monthly == direct_manifest.monthly
        assert builder_manifest.annual == direct_manifest.annual


# ---------------------------------------------------------------------------
# TestComputeAllHashes
# ---------------------------------------------------------------------------


class TestComputeAllHashes:
    """compute_all_hashes() utility function. Traces: SRC-113, SRC-127, SRC-129."""

    def test_returns_dict_with_four_cadences(self) -> None:
        """compute_all_hashes() returns a dict with all four cadences."""
        result = compute_all_hashes(REAL_PROMPTS_DIR)
        assert set(result.keys()) == {"daily", "weekly", "monthly", "annual"}

    def test_all_values_are_sha256_strings(self) -> None:
        """All values in the result are 'sha256:' prefixed strings."""
        result = compute_all_hashes(REAL_PROMPTS_DIR)
        for cadence, h in result.items():
            assert h.startswith("sha256:"), f"{cadence}: {h!r} not sha256-prefixed"
            assert len(h) == 71, f"{cadence}: hash has wrong length {len(h)}"

    def test_values_match_individual_file_hashes(self) -> None:
        """Each returned hash matches the direct _sha256_file() result."""
        result = compute_all_hashes(REAL_PROMPTS_DIR)
        assert result["daily"] == _sha256_file(REAL_PROMPTS_DIR / "daily.md")
        assert result["weekly"] == _sha256_file(REAL_PROMPTS_DIR / "weekly.md")
        assert result["monthly"] == _sha256_file(REAL_PROMPTS_DIR / "monthly.md")
        assert result["annual"] == _sha256_file(REAL_PROMPTS_DIR / "annual.md")

    def test_raises_for_missing_prompt_directory(self, tmp_path: Path) -> None:
        """FileNotFoundError raised when prompt files are missing."""
        with pytest.raises(FileNotFoundError):
            compute_all_hashes(tmp_path / "nonexistent")

    def test_accepts_string_path(self) -> None:
        """compute_all_hashes() accepts a string path as well as a Path object."""
        result = compute_all_hashes(str(REAL_PROMPTS_DIR))
        assert "daily" in result


# ---------------------------------------------------------------------------
# TestPromptContentCompleteness
# ---------------------------------------------------------------------------


class TestPromptContentCompleteness:
    """
    Verify that the real prompt files cover all required spec content.
    These are content-level tests against the actual production files.
    Traces: SRC-115–SRC-124.
    """

    def _read(self, fname: str) -> str:
        return (REAL_PROMPTS_DIR / fname).read_text(encoding="utf-8")

    def _lower(self, fname: str) -> str:
        return self._read(fname).lower()

    # ------ Timeframe injection instructions ------

    def test_daily_prompt_documents_iso_date_injection(self) -> None:
        """daily.md documents that ISO dates are injected (SRC-116)."""
        content = self._read("daily.md")
        assert "{{window_start_iso}}" in content
        assert "{{window_end_iso}}" in content

    def test_all_prompts_have_iso_date_placeholders(self) -> None:
        """All prompt files contain ISO date placeholder tokens (SRC-116)."""
        for fname in ("daily.md", "weekly.md", "monthly.md", "annual.md"):
            content = self._read(fname)
            assert "{{window_start_iso}}" in content, f"{fname} missing window_start_iso"
            assert "{{window_end_iso}}" in content, f"{fname} missing window_end_iso"

    def test_all_prompts_have_twitter_signal_placeholder(self) -> None:
        """All prompt files contain the Twitter signal section placeholder (SRC-119)."""
        for fname in ("daily.md", "weekly.md", "monthly.md", "annual.md"):
            assert "{{twitter_signal_section}}" in self._read(fname), (
                f"{fname} missing twitter_signal_section placeholder (SRC-119)"
            )

    def test_all_prompts_have_search_budget_placeholder(self) -> None:
        """All prompt files contain search_budget_directive placeholder (SRC-121)."""
        for fname in ("daily.md", "weekly.md", "monthly.md", "annual.md"):
            assert "{{search_budget_directive}}" in self._read(fname), (
                f"{fname} missing search_budget_directive placeholder (SRC-121)"
            )

    def test_all_prompts_have_top_n_placeholder(self) -> None:
        """All prompt files contain the {{top_n}} placeholder."""
        for fname in ("daily.md", "weekly.md", "monthly.md", "annual.md"):
            assert "{{top_n}}" in self._read(fname), f"{fname} missing top_n placeholder"

    def test_annual_prompt_has_year_placeholders(self) -> None:
        """annual.md contains {{year}} and {{year_plus_1}} placeholders (SRC-124)."""
        content = self._read("annual.md")
        assert "{{year}}" in content
        assert "{{year_plus_1}}" in content

    def test_all_prompts_have_tier_article_placeholders(self) -> None:
        """All prompt files have all five tier article placeholders (SRC-016–SRC-021)."""
        tier_placeholders = [
            "{{tier_1a_articles}}",
            "{{tier_1b_articles}}",
            "{{tier_2_articles}}",
            "{{tier_3_articles}}",
            "{{tier_4_articles}}",
        ]
        for fname in ("daily.md", "weekly.md", "monthly.md", "annual.md"):
            content = self._read(fname)
            for ph in tier_placeholders:
                assert ph in content, f"{fname} missing placeholder {ph!r}"

    # ------ Tier definitions in prompts ------

    def test_daily_prompt_mentions_tier_1b_sources(self) -> None:
        """daily.md names Tier 1b sources (Reuters, Bloomberg, etc.) (SRC-018)."""
        lower = self._lower("daily.md")
        assert any(source in lower for source in ["reuters", "bloomberg", "wsj", "financial times"])

    def test_daily_prompt_mentions_tier_2_sources(self) -> None:
        """daily.md names Tier 2 sources (YCombinator, Anthropic, etc.) (SRC-019)."""
        lower = self._lower("daily.md")
        assert any(
            source in lower for source in ["ycombinator", "anthropic", "openai", "huggingface"]
        )

    def test_daily_prompt_mentions_tier_3_sources(self) -> None:
        """daily.md names Tier 3 sources (TechCrunch, Wired, etc.) (SRC-020)."""
        lower = self._lower("daily.md")
        assert any(
            source in lower for source in ["techcrunch", "wired", "mit technology", "the verge"]
        )

    def test_daily_prompt_mentions_tier_4_sources(self) -> None:
        """daily.md names Tier 4 sources (Brookings, Stanford HAI, etc.) (SRC-021)."""
        lower = self._lower("daily.md")
        assert any(source in lower for source in ["brookings", "stanford", "rand", "ai now"])

    # ------ Structured output field completeness ------

    def test_all_prompts_specify_impact_tag_values(self) -> None:
        """All prompts enumerate valid impact_tags values (SRC-048, SRC-120)."""
        valid_tags = ["business_impact", "workforce_impact", "policy_impact"]
        for fname in ("daily.md", "weekly.md", "monthly.md", "annual.md"):
            content = self._read(fname)
            for tag in valid_tags:
                assert tag in content, f"{fname} missing impact_tag value {tag!r}"

    def test_all_prompts_specify_tier_values(self) -> None:
        """All prompts enumerate valid tier values (SRC-016–SRC-021, SRC-120)."""
        for fname in ("daily.md", "weekly.md", "monthly.md", "annual.md"):
            content = self._read(fname)
            for tier_val in ['"1a"', '"1b"', '"2"', '"3"', '"4"']:
                assert tier_val in content, f"{fname} missing tier value {tier_val!r}"

    def test_annual_prompt_specifies_10_predictions(self) -> None:
        """annual.md specifically requires 10 predictions (SRC-032, SRC-124)."""
        content = self._read("annual.md")
        lower = content.lower()
        # "ten" or "10" + "prediction"
        has_10 = "ten" in lower or "10 predictions" in lower or "10 specific" in lower
        assert has_10, "annual.md must specify 10 predictions (SRC-032)"

    def test_annual_prompt_requires_failure_condition(self) -> None:
        """annual.md requires a failure condition per prediction (SRC-124)."""
        content = self._lower("annual.md")
        assert "failure condition" in content or "would invalidate" in content, (
            "annual.md should require failure conditions for predictions (SRC-124)"
        )

    def test_monthly_prompt_has_signal_vs_noise_section(self) -> None:
        """monthly.md has a signal vs noise analysis section (SRC-031)."""
        lower = self._lower("monthly.md")
        assert "signal" in lower
        assert "noise" in lower

    # ------ Content quality checks ------

    def test_prompts_are_substantial_length(self) -> None:
        """All prompt files are substantial — not empty stubs."""
        min_length = 3_000  # bytes — a real prompt should be much longer
        for fname in ("daily.md", "weekly.md", "monthly.md", "annual.md"):
            content = (REAL_PROMPTS_DIR / fname).read_bytes()
            assert len(content) >= min_length, (
                f"{fname} is only {len(content)} bytes — may be a stub"
            )

    def test_annual_prompt_is_longest(self) -> None:
        """annual.md should be the longest prompt file (most content)."""
        lengths = {
            cadence: len((REAL_PROMPTS_DIR / f"{cadence}.md").read_bytes())
            for cadence in ("daily", "weekly", "monthly", "annual")
        }
        assert lengths["annual"] >= lengths["daily"], (
            "annual.md should be at least as long as daily.md"
        )
        assert lengths["annual"] >= lengths["weekly"], (
            "annual.md should be at least as long as weekly.md"
        )

    def test_prompts_do_not_have_unreplaced_placeholders_in_template(self) -> None:
        """Template files should only have known {{placeholder}} tokens."""
        known_placeholders = {
            "{{window_start_iso}}",
            "{{window_end_iso}}",
            "{{tier_1a_articles}}",
            "{{tier_1b_articles}}",
            "{{tier_2_articles}}",
            "{{tier_3_articles}}",
            "{{tier_4_articles}}",
            "{{twitter_signal_section}}",
            "{{search_budget_directive}}",
            "{{top_n}}",
            "{{year}}",
            "{{year_plus_1}}",
        }
        placeholder_re = re.compile(r"\{\{[a-z_0-9]+\}\}")
        for fname in ("daily.md", "weekly.md", "monthly.md", "annual.md"):
            content = self._read(fname)
            found = set(placeholder_re.findall(content))
            unknown = found - known_placeholders
            assert not unknown, (
                f"{fname} contains unknown placeholders: {unknown!r}. "
                f"Add to PromptBuilder.build() substitutions or known_placeholders."
            )
