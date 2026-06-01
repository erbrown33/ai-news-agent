"""
curation/prompt_builder.py — Builds fully-parameterised curation prompts.

Responsibilities (SRC-112–SRC-129):
- Load the cadence-specific prompt template from prompts/ (SRC-113, SRC-127).
- Compute and record the SHA-256 hash of the template file BEFORE substitution (SRC-129).
- Inject concrete ISO date ranges — never relative phrases (SRC-116).
- Inject tier-separated candidate articles into the prompt body (SRC-016–SRC-021, SRC-027).
- Inject the Twitter influencer signal as a labeled context section (SRC-119, SRC-070).
- Inject a search budget directive calibrated to the cadence (SRC-121).
- Inject top_n and year/year+1 variables (SRC-032, SRC-124).
- Provide PromptManifest for recording all prompt hashes at build time (SRC-129).
- Provide compute_all_hashes() utility for CI/manifest generation.

Traces: SRC-059, SRC-070, SRC-112–SRC-131
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Literal

import structlog

if TYPE_CHECKING:
    from ai_news_agent.storage.models import ArticleRecord, TweetSignal

log = structlog.get_logger(__name__)

Cadence = Literal["daily", "weekly", "monthly", "annual"]

# ---------------------------------------------------------------------------
# Prompt file registry — one file per cadence (SRC-113, SRC-127)
# ---------------------------------------------------------------------------

#: Maps cadence names to prompt filenames under prompts/.
_PROMPT_FILES: dict[str, str] = {
    "daily": "daily.md",
    "weekly": "weekly.md",
    "monthly": "monthly.md",
    "annual": "annual.md",
}

# ---------------------------------------------------------------------------
# Search budget strings per cadence (SRC-121)
# ---------------------------------------------------------------------------

#: Human-readable search budget directive injected into each prompt template.
_SEARCH_BUDGET_TEXT: dict[str, str] = {
    "daily": (
        "Use a **normal search budget** — one targeted search per major topic is sufficient "
        "for daily curation. Prioritise speed and URL verification over breadth."
    ),
    "weekly": (
        "Use a **normal search budget** — focused searches to fill sourcing gaps and verify "
        "URLs. Do not conduct speculative searches; every query should have a specific goal."
    ),
    "monthly": (
        "Use a **deep search budget** — run multiple targeted searches, cross-reference "
        "primary sources, and prioritise direct reporting over aggregators. "
        "Verify the significance and status of key developments before including them."
    ),
    "annual": (
        "Use the **deepest search budget available** — run multiple targeted searches, "
        "cross-reference sources at every level, prioritise primary reporting, verify "
        "working URLs, and search for counter-evidence before finalising predictions. "
        "This is the most important output of the year; thoroughness is mandatory."
    ),
}

# ---------------------------------------------------------------------------
# Universal selection guidance — appended to EVERY prompt (SRC-118 quality bar)
# ---------------------------------------------------------------------------

#: De-duplication + variety guidance appended by ``build()`` to every curation
#: prompt — the default cadence templates *and* any custom ``curation_prompt``
#: override. Centralising it here (rather than copying it into each template
#: file) means the rule is a single, code-reviewed source of truth that cannot be
#: dropped by pointing ``curation_prompt`` at a custom prompt directory.
#:
#: It applies *on top of* the inclusion/exclusion criteria in the template above —
#: it governs how to choose among items that already qualify, never overriding the
#: template's own criteria. ``{{top_n}}`` is substituted at build time like any
#: other placeholder. Editing this text changes the runtime ``prompt_version`` hash
#: (SRC-129), so changes remain attributable for regression tracing.
_UNIVERSAL_SELECTION_GUIDANCE: str = """

---

## Universal Selection Rules — Consolidate Duplicate Coverage and Maximize Variety

These rules apply to **every** digest, on top of the inclusion and exclusion criteria
above. They do not override those criteria — they govern how you choose among the items
that already qualify.

The candidate pool routinely contains **multiple articles covering the same underlying
event** — the same announcement, deal, study, ruling, incident, release, or launch —
reported by different outlets under different headlines and wording (for example, several
separate write-ups of a single Google AI announcement). These are **not** distinct news
items. They are duplicate coverage of one story.

**One story, one item — this is a hard rule:**

1. When two or more candidates describe the same underlying development, select the
   **single best** article to represent it. Prefer the most authoritative primary source,
   the most complete and accurate reporting, and the stronger outlet — weighing source tier,
   but a superior lower-tier report can outrank a thin higher-tier rewrite.
2. Include that one item only. Put the other URLs that cover the same story in that item's
   `cross_refs` field — do **not** list them as separate items.
3. Judge "same story" by **substance, not wording**: the same actors, the same event, the
   same time frame, and the same core facts almost always mean one story even when the
   headlines differ. Differing titles are not evidence of differing stories.

**Maximize variety within the criteria above.** A digest of {{top_n}} items covering
{{top_n}} *different* significant developments is far more valuable than {{top_n}} angles on
the same event or the same organisation. If your strongest candidates cluster around one
story, one organisation, or one theme, keep the best one or two and spend the remaining
slots on the next most significant *distinct* developments — across different organisations,
sectors, and impact types. Breadth of coverage is a primary quality goal, not an afterthought.

**Never duplicate to reach the target count.** Distinctness and quality outrank count: if
collapsing duplicates leaves fewer than {{top_n}} genuinely distinct qualifying stories,
return only those that qualify.
"""

# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _sha256_file(path: Path) -> str:
    """
    Compute the SHA-256 hex digest of a prompt file's raw bytes.

    The hash is computed on the *raw bytes* before any template substitution
    so that it uniquely identifies the prompt version, not the run output.
    Returns the canonical string ``"sha256:<64-char-hex>"``.

    Traces: SRC-129 (prompt version recorded in all digest outputs)
    """
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    return f"sha256:{digest}"


def _sha256_text(text: str) -> str:
    """
    Compute the SHA-256 hex digest of a prompt's full instructional text.

    Used for the runtime ``prompt_version`` (SRC-129), computed over the template
    plus the appended universal selection guidance, BEFORE runtime substitution —
    so the version uniquely identifies the prompt text actually sent and changes
    whenever either the template or the universal guidance changes.

    Returns the canonical string ``"sha256:<64-char-hex>"``.
    """
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


def _format_twitter_section(
    signals: list[TweetSignal],
    twitter_api_available: bool = True,
) -> str:
    """
    Format tweet signals into the labeled influencer-signal context section.

    The section clearly marks Twitter content as lead-generation only — not primary
    citation — unless the tweet itself IS the news (SRC-119, SRC-070).

    Distinguishes two empty-signal cases (SRC-148):
    - ``twitter_api_available=False``: API was unreachable — prompts the LLM to
      curate from web sources only and explicitly notes the API failure.
    - ``twitter_api_available=True`` but ``signals=[]``: API responded but the
      window produced no substantive posts — different note, no error framing.

    Signals are ordered by handle weight descending so highest-trust handles appear
    first in the LLM context window (SRC-046).

    Traces: SRC-046 (handle weights), SRC-047 (Twitter = signal not primary news),
            SRC-070 (labeled section for LLM), SRC-119 (separate labeled section),
            SRC-148 (API unavailability note)
    """
    if not signals:
        if not twitter_api_available:
            # API was unreachable — distinguish from a quiet window (SRC-148)
            return (
                "## Influencer Signal  <!-- SRC-119, SRC-148 -->\n"
                "> ⚠️ **Twitter/X API unavailable for this run.**  "
                "Influencer signal could not be collected because the API was unreachable.  "
                "Base curation entirely on web sources; do not reference any tweet "
                "as a lead or signal for this digest.  (SRC-148)\n"
            )
        # API available but window produced no substantive posts
        return (
            "## Influencer Signal  <!-- SRC-119 -->\n"
            "No substantive Twitter/X posts were found in this lookback window.  "
            "Base curation on web sources only.\n"
        )

    lines = [
        "## Influencer Signal  <!-- SRC-119 -->",
        "**IMPORTANT**: These tweets are for context and lead-generation only.",
        "Do NOT cite a tweet as a primary source unless the tweet itself IS the news",
        "(e.g., an executive announcement on X before any press coverage exists).",
        "Use these as hints about topics to verify via primary web reporting.",
        "",
    ]

    # Sort by weight descending so highest-trust handles appear first (SRC-046)
    for signal in sorted(signals, key=lambda s: s.weight, reverse=True):
        pub_date = signal.created_at.strftime("%Y-%m-%d") if signal.created_at else "unknown date"
        lines.append(f"**@{signal.handle}** (weight: {signal.weight:.1f}, {pub_date}):")
        lines.append(f"> {signal.text[:280]}")
        if signal.linked_urls:
            # Only surface the first 3 linked URLs to keep context manageable
            linked = ", ".join(signal.linked_urls[:3])
            lines.append(f"  Linked URLs: {linked}")
        lines.append("")

    return "\n".join(lines)


def _format_tier_articles(
    candidates: list[ArticleRecord],
    tier_key: str,
) -> str:
    """
    Format articles for a specific tier into the numbered list the LLM reads.

    Empty tiers produce a placeholder line so the prompt template always has
    content at each tier section — avoids confusing blank blocks.

    Traces: SRC-011 (article fields), SRC-016–SRC-021 (tier definitions),
            SRC-027 (LLM receives full candidate metadata)
    """
    tier_articles = [a for a in candidates if a.tier == tier_key]
    if not tier_articles:
        return f"_(No {tier_key} articles sourced for this window.)_"

    lines: list[str] = []
    for i, article in enumerate(tier_articles, start=1):
        pub = (
            article.pub_date.strftime("%Y-%m-%d")
            if isinstance(article.pub_date, datetime)
            else str(article.pub_date)
        )
        entry = (
            f"{i}. **{article.headline}**\n"
            f"   Source: {article.source_name} | URL: {article.url}\n"
            f"   Published: {pub}"
        )
        if article.abstract:
            entry += f"\n   Abstract: {article.abstract[:250]}"
        if article.twitter_handle:
            entry += f"\n   _(Surfaced via @{article.twitter_handle} — tweet: {article.tweet_url})_"
        lines.append(entry)
        lines.append("")  # blank line between items

    return "\n".join(lines).rstrip()


# ---------------------------------------------------------------------------
# PromptManifest — records prompt file hashes at build time (SRC-129)
# ---------------------------------------------------------------------------


@dataclass
class PromptManifest:
    """
    Records the SHA-256 hashes of all four cadence prompt files at the time
    the manifest was generated.

    Embed this in digest output metadata so every digest can be traced back to
    the exact prompt version that produced it — enabling quality regression analysis
    and prompt change attribution (SRC-129).

    Usage::

        manifest = PromptManifest.from_dir(Path("prompts"))
        print(manifest.daily)     # "sha256:<64-char-hex>"
        print(manifest.to_dict()) # {"daily": "sha256:...", ...}
        manifest.save(Path("prompts/prompt_hashes.json"))

    Traces: SRC-113 (prompts directory), SRC-125–SRC-131 (prompt ownership),
            SRC-129 (each digest records the prompt version used)
    """

    daily: str = ""
    weekly: str = ""
    monthly: str = ""
    annual: str = ""

    #: UTC datetime when this manifest was generated
    generated_at: str = ""

    @classmethod
    def from_dir(cls, prompts_dir: Path) -> PromptManifest:
        """
        Compute SHA-256 hashes for all four cadence prompt files in ``prompts_dir``.

        Raises ``FileNotFoundError`` if any cadence file is missing.

        Traces: SRC-129
        """
        hashes: dict[str, str] = {}
        for cadence, filename in _PROMPT_FILES.items():
            path = prompts_dir / filename
            if not path.exists():
                raise FileNotFoundError(
                    f"Prompt file missing: {path.absolute()} "
                    f"(cadence={cadence!r}) — ensure all four prompt files exist "
                    f"under {prompts_dir} (SRC-113)"
                )
            hashes[cadence] = _sha256_file(path)

        now_utc = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        return cls(
            daily=hashes["daily"],
            weekly=hashes["weekly"],
            monthly=hashes["monthly"],
            annual=hashes["annual"],
            generated_at=now_utc,
        )

    def to_dict(self) -> dict[str, str]:
        """Return all hashes as a plain dict (suitable for JSON serialisation)."""
        return {
            "daily": self.daily,
            "weekly": self.weekly,
            "monthly": self.monthly,
            "annual": self.annual,
            "generated_at": self.generated_at,
        }

    def save(self, output_path: Path) -> None:
        """
        Persist this manifest to ``output_path`` as pretty-printed JSON.

        The file is placed alongside the prompt templates and checked into
        version control so CI can detect unreviewed prompt changes (SRC-127–SRC-128).

        Traces: SRC-127 (version-controlled), SRC-129 (prompt hash persistence)
        """
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(
            json.dumps(self.to_dict(), indent=2) + "\n",
            encoding="utf-8",
        )
        log.info(
            "prompt_manifest_saved",
            path=str(output_path),
            daily=self.daily,
            weekly=self.weekly,
            monthly=self.monthly,
            annual=self.annual,
        )

    @classmethod
    def load(cls, manifest_path: Path) -> PromptManifest:
        """
        Load a previously-saved manifest JSON file.

        Traces: SRC-129
        """
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
        return cls(
            daily=data.get("daily", ""),
            weekly=data.get("weekly", ""),
            monthly=data.get("monthly", ""),
            annual=data.get("annual", ""),
            generated_at=data.get("generated_at", ""),
        )

    def get(self, cadence: str) -> str:
        """Return the hash for a specific cadence. Raises ``KeyError`` for unknown cadences."""
        mapping = {
            "daily": self.daily,
            "weekly": self.weekly,
            "monthly": self.monthly,
            "annual": self.annual,
        }
        if cadence not in mapping:
            raise KeyError(f"Unknown cadence: {cadence!r}")
        return mapping[cadence]


# ---------------------------------------------------------------------------
# Public utility: compute_all_hashes
# ---------------------------------------------------------------------------


def compute_all_hashes(prompts_dir: str | Path = "prompts") -> dict[str, str]:
    """
    Compute the current SHA-256 hashes for all four cadence prompt files.

    Returns a dict mapping cadence names to hash strings::

        {
            "daily":   "sha256:<hex>",
            "weekly":  "sha256:<hex>",
            "monthly": "sha256:<hex>",
            "annual":  "sha256:<hex>",
        }

    Raises ``FileNotFoundError`` if any prompt file is missing.

    Used by:
    - CI pipelines to detect unreviewed prompt changes (SRC-127–SRC-128).
    - The ``ai-news-prompt-hashes`` CLI entry point.
    - Test infrastructure to verify hashes match expected values.

    Traces: SRC-113, SRC-127, SRC-129
    """
    manifest = PromptManifest.from_dir(Path(prompts_dir))
    return {
        "daily": manifest.daily,
        "weekly": manifest.weekly,
        "monthly": manifest.monthly,
        "annual": manifest.annual,
    }


# ---------------------------------------------------------------------------
# PromptBuilder — main public class
# ---------------------------------------------------------------------------


class PromptBuilder:
    """
    Builds a fully-parameterised curation prompt for a given cadence window.

    Steps (SRC-115–SRC-124):
    1. Load the cadence-specific template from ``prompts/`` (SRC-113).
    2. Compute and record the SHA-256 hash of the template file (SRC-129).
    3. Format candidate articles by tier into ``{{tier_*_articles}}`` placeholders
       (SRC-016–SRC-021, SRC-027).
    4. Inject concrete ISO date ranges (SRC-116).
    5. Inject the Twitter signal section, distinguishing API-down from quiet-window
       (SRC-119, SRC-148).
    6. Inject the search budget directive calibrated to the cadence (SRC-121).
    7. Inject ``{{top_n}}``, ``{{year}}``, ``{{year_plus_1}}`` (SRC-032, SRC-124).

    Returns ``(prompt_text, prompt_version)`` where ``prompt_version`` is the
    ``"sha256:<hex>"`` string for embedding in all digest outputs (SRC-129).

    Traces: SRC-059, SRC-070, SRC-113, SRC-115–SRC-124, SRC-127, SRC-129, SRC-148
    """

    def __init__(self, prompts_dir: str | Path = "prompts") -> None:
        self._prompts_dir = Path(prompts_dir)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def build(
        self,
        cadence: Cadence,
        window_start: datetime,
        window_end: datetime,
        tweet_signals: list[TweetSignal],
        top_n: int,
        candidates: list[ArticleRecord] | None = None,
        curation_prompt_override: str | None = None,
        twitter_api_available: bool = True,
    ) -> tuple[str, str]:
        """
        Build and return ``(prompt_text, prompt_version)`` for the given cadence.

        All ``{{placeholder}}`` tokens in the template are replaced before returning.
        The SHA-256 hash is computed on the raw template *before* substitution so it
        uniquely identifies the prompt version regardless of runtime variable values.

        Args:
            cadence:
                ``"daily"`` | ``"weekly"`` | ``"monthly"`` | ``"annual"``
            window_start:
                Start of the lookback window (UTC).
            window_end:
                End of the lookback window (UTC).
            tweet_signals:
                Filtered, hydrated tweet signals for this window (SRC-119).
            top_n:
                How many articles to select — injected as ``{{top_n}}``.
            candidates:
                Candidate articles for the window, used to populate
                ``{{tier_1a_articles}}`` … ``{{tier_4_articles}}`` in the template
                (SRC-016–SRC-021). Pass ``None`` or an empty list when no candidates
                are available — the template sections will show placeholder text.
            curation_prompt_override:
                If set, use this *file path* instead of the default cadence prompt.
                The override file must still use the same ``{{placeholder}}`` syntax.
            twitter_api_available:
                ``False`` when the Twitter API was unreachable during sourcing —
                propagated into the influencer-signal section so the LLM knows
                signals are absent due to API failure, not window silence (SRC-148).

        Returns:
            ``(prompt_text, prompt_version)``

            - ``prompt_text``:    Fully interpolated prompt string ready for the LLM,
              including the universal selection guidance appended to every prompt.
            - ``prompt_version``: ``"sha256:<64-char-hex>"`` of the template plus the
              appended universal guidance, before substitution (SRC-129).

        Raises:
            FileNotFoundError: If the prompt file does not exist.

        Traces: SRC-016–SRC-021 (tier articles), SRC-059 (provider-agnostic plain text),
                SRC-070 (labeled Twitter section), SRC-113 (prompts dir),
                SRC-115–SRC-124 (all required sections), SRC-129 (hash), SRC-148
        """
        # ------------------------------------------------------------------
        # Step 1: Resolve prompt template file (SRC-113)
        # ------------------------------------------------------------------
        if curation_prompt_override:
            prompt_path = Path(curation_prompt_override)
        else:
            filename = _PROMPT_FILES[cadence]
            prompt_path = self._prompts_dir / filename

        if not prompt_path.exists():
            raise FileNotFoundError(
                f"Prompt file not found: {prompt_path.absolute()!r}\n"
                f"Ensure the prompts/ directory contains {_PROMPT_FILES.get(cadence, 'the override file')!r}. "
                f"(SRC-113)"
            )

        # ------------------------------------------------------------------
        # Step 2: Append the universal selection guidance, then hash the full
        #         instructional text BEFORE substitution (SRC-118, SRC-129)
        # ------------------------------------------------------------------
        # The universal de-duplication / variety guidance is appended to EVERY
        # prompt — default cadence templates and custom ``curation_prompt``
        # overrides alike — so the rule cannot be dropped by configuration.
        # The version hashes template + guidance so it uniquely identifies the
        # prompt actually sent and changes if either the template or the guidance
        # changes (SRC-129).
        template = prompt_path.read_text(encoding="utf-8") + _UNIVERSAL_SELECTION_GUIDANCE
        prompt_version = _sha256_text(template)

        log.debug(
            "prompt_builder_load",
            cadence=cadence,
            prompt_path=str(prompt_path),
            prompt_version=prompt_version,
            universal_guidance_appended=True,
            candidates=len(candidates) if candidates else 0,
            tweet_signals=len(tweet_signals),
            twitter_api_available=twitter_api_available,
        )

        # ------------------------------------------------------------------
        # Step 3: Format candidate articles by tier (SRC-016–SRC-021, SRC-027)
        # ------------------------------------------------------------------
        all_candidates: list[ArticleRecord] = candidates or []

        tier_1a_text = _format_tier_articles(all_candidates, "1a")
        tier_1b_text = _format_tier_articles(all_candidates, "1b")
        tier_2_text = _format_tier_articles(all_candidates, "2")
        tier_3_text = _format_tier_articles(all_candidates, "3")
        tier_4_text = _format_tier_articles(all_candidates, "4")

        # ------------------------------------------------------------------
        # Step 4: Format the Twitter signal section (SRC-119, SRC-148)
        # ------------------------------------------------------------------
        twitter_section = _format_twitter_section(
            tweet_signals,
            twitter_api_available=twitter_api_available,
        )

        # ------------------------------------------------------------------
        # Step 5: Search budget directive (SRC-121)
        # ------------------------------------------------------------------
        search_budget_directive = _SEARCH_BUDGET_TEXT.get(cadence, _SEARCH_BUDGET_TEXT["daily"])

        # ------------------------------------------------------------------
        # Step 6: Year variables for annual prompt (SRC-032, SRC-124)
        # ------------------------------------------------------------------
        year = window_start.year
        year_plus_1 = year + 1

        # ------------------------------------------------------------------
        # Step 7: Substitute all placeholders (SRC-116 and all injected sections)
        # ------------------------------------------------------------------
        substitutions: dict[str, str] = {
            # ISO date window (SRC-116)
            "{{window_start_iso}}": window_start.date().isoformat(),
            "{{window_end_iso}}": window_end.date().isoformat(),
            # Tier-separated article lists (SRC-016–SRC-021)
            "{{tier_1a_articles}}": tier_1a_text,
            "{{tier_1b_articles}}": tier_1b_text,
            "{{tier_2_articles}}": tier_2_text,
            "{{tier_3_articles}}": tier_3_text,
            "{{tier_4_articles}}": tier_4_text,
            # Twitter signal section (SRC-119, SRC-148)
            "{{twitter_signal_section}}": twitter_section,
            # Search budget directive (SRC-121)
            "{{search_budget_directive}}": search_budget_directive,
            # Selection size and year variables
            "{{top_n}}": str(top_n),
            "{{year}}": str(year),
            "{{year_plus_1}}": str(year_plus_1),
        }

        prompt_text = template
        for placeholder, value in substitutions.items():
            prompt_text = prompt_text.replace(placeholder, value)

        # Warn if any un-substituted placeholders remain (indicates template/code mismatch)
        remaining = [p for p in substitutions if p in prompt_text]
        if remaining:
            log.warning(
                "prompt_builder_unreplaced_placeholders",
                cadence=cadence,
                placeholders=remaining,
            )

        return prompt_text, prompt_version

    def get_prompt_version(self, cadence: Cadence) -> str:
        """
        Return the runtime ``prompt_version`` for ``cadence`` without building the
        full prompt — i.e. the SHA-256 of the template file plus the appended
        universal selection guidance, matching what :meth:`build` records.

        Used for pre-flight version checks and monitoring. Note this differs from
        :class:`PromptManifest` (and the ``--verify`` CLI), which hash the raw
        template *files* to detect unreviewed edits to the configurable templates;
        this method reflects the full prompt actually sent to the model (SRC-129).

        Traces: SRC-129
        """
        filename = _PROMPT_FILES[cadence]
        prompt_path = self._prompts_dir / filename
        if not prompt_path.exists():
            raise FileNotFoundError(f"Prompt file not found: {prompt_path!r} (SRC-113)")
        return _sha256_text(prompt_path.read_text(encoding="utf-8") + _UNIVERSAL_SELECTION_GUIDANCE)

    def get_manifest(self) -> PromptManifest:
        """
        Return a ``PromptManifest`` capturing the current SHA-256 hashes of all
        four cadence prompt files in this builder's prompts directory.

        Traces: SRC-129
        """
        return PromptManifest.from_dir(self._prompts_dir)


# ---------------------------------------------------------------------------
# CLI entry point: ai-news-prompt-hashes
# ---------------------------------------------------------------------------


def _cli_prompt_hashes() -> None:
    """
    CLI entry point: ``ai-news-prompt-hashes``

    Computes and prints the SHA-256 hashes of all four cadence prompt files and
    optionally writes/verifies ``prompts/prompt_hashes.json``.

    Usage::

        # Print current hashes
        ai-news-prompt-hashes

        # Write a new manifest (prompts/prompt_hashes.json)
        ai-news-prompt-hashes --save

        # Verify current files match the saved manifest (CI use)
        ai-news-prompt-hashes --verify

        # Use a custom prompts directory
        ai-news-prompt-hashes --prompts-dir /path/to/prompts --save

    Exit codes:
        0 — Success (or hashes match manifest on --verify)
        1 — Mismatch detected on --verify (indicates unreviewed prompt changes)

    Traces: SRC-127 (version-controlled prompts), SRC-128 (review required),
            SRC-129 (SHA-256 hash embedded in output)
    """
    import argparse
    import sys

    parser = argparse.ArgumentParser(
        prog="ai-news-prompt-hashes",
        description=(
            "Compute, save, or verify SHA-256 hashes of curation prompt files. "
            "Use --verify in CI to detect unreviewed prompt changes. (SRC-127–SRC-129)"
        ),
    )
    parser.add_argument(
        "--prompts-dir",
        default="prompts",
        help="Directory containing prompt template files (default: prompts/)",
    )
    parser.add_argument(
        "--save",
        action="store_true",
        help="Write computed hashes to prompts/prompt_hashes.json",
    )
    parser.add_argument(
        "--verify",
        action="store_true",
        help=(
            "Verify that current prompt files match prompts/prompt_hashes.json. "
            "Exits with code 1 if any hash differs (for CI enforcement). "
            "(SRC-127–SRC-128)"
        ),
    )
    args = parser.parse_args()

    prompts_path = Path(args.prompts_dir)
    manifest_path = prompts_path / "prompt_hashes.json"

    try:
        current = PromptManifest.from_dir(prompts_path)
    except FileNotFoundError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)

    # ------------------------------------------------------------------
    # Print current hashes
    # ------------------------------------------------------------------
    print("Current prompt file SHA-256 hashes:")
    for cadence in ("daily", "weekly", "monthly", "annual"):
        print(f"  {cadence:8s}: {current.get(cadence)}")
    print(f"  generated_at: {current.generated_at}")

    # ------------------------------------------------------------------
    # --save: write manifest
    # ------------------------------------------------------------------
    if args.save:
        current.save(manifest_path)
        print(f"\nManifest written to: {manifest_path}")

    # ------------------------------------------------------------------
    # --verify: compare against saved manifest
    # ------------------------------------------------------------------
    if args.verify:
        if not manifest_path.exists():
            print(
                f"\nERROR: No manifest found at {manifest_path}. "
                "Run with --save first to create it.",
                file=sys.stderr,
            )
            sys.exit(1)

        saved = PromptManifest.load(manifest_path)
        mismatches: list[str] = []
        for cadence in ("daily", "weekly", "monthly", "annual"):
            current_hash = current.get(cadence)
            saved_hash = saved.get(cadence)
            if current_hash != saved_hash:
                mismatches.append(f"  {cadence:8s}: saved={saved_hash!r}  current={current_hash!r}")

        if mismatches:
            print(
                "\nPROMPT HASH MISMATCH — unreviewed changes detected (SRC-127–SRC-128):",
                file=sys.stderr,
            )
            for line in mismatches:
                print(line, file=sys.stderr)
            print(
                "\nPrompt changes require code review before deployment. "
                "If the changes are intentional, update the manifest with --save. "
                "(SRC-128)",
                file=sys.stderr,
            )
            sys.exit(1)
        else:
            print("\nAll prompt hashes match the saved manifest. ✓")
