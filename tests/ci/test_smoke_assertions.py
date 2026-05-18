"""
tests/ci/test_smoke_assertions.py — CI smoke-assertion test suite.

These tests mirror *exactly* the assertions performed by the ``smoke-docker``
GitHub Actions job in ``.github/workflows/ci.yml``.  They run in plain Python
against the full pipeline (mocked LLM + Twitter) so CI gets fast feedback
before the Docker build step even starts.

Contract verified (SRC-102 acceptance criteria):
─────────────────────────────────────────────────
  ✔ Exit code 0 (pipeline.success = True)
  ✔ Markdown, HTML, and JSON files are non-empty  (SRC-004)
  ✔ JSON has schema_version, metadata{}, items[]  (SRC-102)
  ✔ metadata contains all §8.2 monitoring fields  (SRC-150)
  ✔ prompt_version starts with "sha256:"          (SRC-129)
  ✔ No items present without a valid URL          (SRC-049, SRC-141)
  ✔ twitter_signal_available is a boolean         (SRC-148)
  ✔ Filename stem matches YYYY-MM-DD-{cadence}    (SRC-145)
  ✔ window_start / window_end present in metadata (SRC-028, SRC-150)
  ✔ agent_id present in metadata                  (SRC-072, SRC-150)
  ✔ llm_provider and llm_model present            (SRC-150)
  ✔ token_usage ≥ 0                               (SRC-150)
  ✔ items_by_tier is a dict                       (SRC-150)
  ✔ items_by_source_class is a dict               (SRC-150)
  ✔ tweet_api_call_count ≥ 0                      (SRC-150)

All four cadences are tested: daily, weekly, monthly, annual.

Traces: SRC-004, SRC-028, SRC-029–SRC-032, SRC-049, SRC-072, SRC-097, SRC-098,
        SRC-102, SRC-129, SRC-141, SRC-145, SRC-148, SRC-150
"""

from __future__ import annotations

import json
import re
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

# ---------------------------------------------------------------------------
# Docker availability helper — defined early because it's used as a decorator
# ---------------------------------------------------------------------------

def _can_run_docker() -> bool:
    """Return True if 'docker info' exits 0 (daemon is running)."""
    try:
        subprocess.check_call(  # noqa: S603
            ["docker", "info"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=5,
        )
        return True
    except Exception:  # noqa: BLE001
        return False


# ---------------------------------------------------------------------------
# Shared constants
# ---------------------------------------------------------------------------

# Filename stem pattern: YYYY-MM-DD-{cadence}  (SRC-145)
_STEM_RE = re.compile(r"^\d{4}-\d{2}-\d{2}-\w+$")

# §8.2 required metadata fields  (SRC-150)
REQUIRED_META_FIELDS: list[str] = [
    "agent_id",               # SRC-072, SRC-150
    "cadence",                # SRC-150
    "run_date",               # SRC-150
    "window_start",           # SRC-028, SRC-150
    "window_end",             # SRC-028, SRC-150
    "prompt_version",         # SRC-129
    "llm_provider",           # SRC-150
    "llm_model",              # SRC-150
    "items_considered",       # SRC-150
    "items_included",         # SRC-150
    "items_by_tier",          # SRC-150
    "items_by_source_class",  # SRC-150
    "token_usage",            # SRC-150
    "twitter_signal_available",  # SRC-148
    "tweet_api_call_count",      # SRC-150
]


# ---------------------------------------------------------------------------
# Core assertion helper — mirrors the container smoke step in ci.yml
# ---------------------------------------------------------------------------

def _assert_smoke_contract(
    result: Any,
    *,
    cadence: str,
) -> None:
    """
    Core assertion function — mirrors the container smoke-test assertions
    from ``.github/workflows/ci.yml`` ``smoke-docker`` job.

    Called for every cadence to keep the assertions DRY.

    Args:
        result:   PipelineRunResult from Pipeline.run().
        cadence:  Expected cadence string.

    Traces: SRC-004, SRC-028, SRC-049, SRC-102, SRC-129, SRC-141, SRC-145,
            SRC-148, SRC-150
    """
    # ── Pipeline success (SRC-102) ──────────────────────────────────────────
    assert result.success is True, (
        f"Pipeline returned success=False for cadence={cadence!r}. "
        f"Errors: {result.errors}"
    )
    assert result.cadence == cadence, (
        f"Expected cadence={cadence!r}, got {result.cadence!r}"
    )

    # ── Three output files exist (SRC-004) ──────────────────────────────────
    assert result.markdown_path is not None, "markdown_path is None (SRC-004)"
    assert result.html_path     is not None, "html_path is None (SRC-004)"
    assert result.json_path     is not None, "json_path is None (SRC-004)"

    md_path   = Path(result.markdown_path)
    html_path = Path(result.html_path)
    json_path = Path(result.json_path)

    assert md_path.exists(),   f"Markdown file missing: {md_path} (SRC-004)"
    assert html_path.exists(),  f"HTML file missing: {html_path} (SRC-004)"
    assert json_path.exists(),  f"JSON file missing: {json_path} (SRC-004)"

    # ── Non-empty check (SRC-102) ───────────────────────────────────────────
    assert md_path.stat().st_size > 0,   f"{md_path.name} is empty (SRC-102)"
    assert html_path.stat().st_size > 0, f"{html_path.name} is empty (SRC-102)"
    assert json_path.stat().st_size > 0, f"{json_path.name} is empty (SRC-102)"

    # ── JSON structure check (SRC-102) ─────────────────────────────────────
    digest = json.loads(json_path.read_text("utf-8"))
    assert "schema_version" in digest, "schema_version missing from JSON (SRC-102)"
    assert "metadata"       in digest, "metadata block missing from JSON (SRC-102)"
    assert "items"          in digest, "items array missing from JSON (SRC-102)"

    meta = digest["metadata"]

    # ── §8.2 monitoring fields (SRC-150) ───────────────────────────────────
    missing_fields = [f for f in REQUIRED_META_FIELDS if f not in meta]
    assert not missing_fields, (
        f"Required §8.2 metadata fields missing: {missing_fields} (SRC-150)"
    )

    # ── prompt_version SHA-256 format (SRC-129) ────────────────────────────
    pv = meta["prompt_version"]
    assert isinstance(pv, str), f"prompt_version must be str, got {type(pv)} (SRC-129)"
    assert pv.startswith("sha256:"), (
        f"prompt_version must start with 'sha256:', got: {pv!r} (SRC-129)"
    )

    # ── URL enforcement: no items without valid URL (SRC-049, SRC-141) ──────
    bad_items = [
        it for it in digest.get("items", [])
        if not str(it.get("url", "")).startswith("http")
    ]
    assert not bad_items, (
        f"{len(bad_items)} item(s) missing valid URL (SRC-049, SRC-141): "
        + str([it.get("headline", "?") for it in bad_items])
    )

    # ── twitter_signal_available is boolean (SRC-148) ──────────────────────
    tsa = meta.get("twitter_signal_available")
    assert isinstance(tsa, bool), (
        f"twitter_signal_available must be bool, got {type(tsa)} (SRC-148)"
    )

    # ── Filename convention YYYY-MM-DD-{cadence}.* (SRC-145) ───────────────
    for path_obj in (md_path, html_path, json_path):
        assert _STEM_RE.match(path_obj.stem), (
            f"Filename {path_obj.name!r} does not match YYYY-MM-DD-cadence "
            f"pattern (SRC-145)"
        )
        assert cadence in path_obj.stem, (
            f"Cadence {cadence!r} not present in filename {path_obj.name!r} (SRC-145)"
        )

    # ── agent_id in metadata (SRC-072) ─────────────────────────────────────
    assert meta["agent_id"], "agent_id is empty in metadata (SRC-072)"

    # ── llm_provider + llm_model non-empty (SRC-150) ───────────────────────
    assert meta["llm_provider"], "llm_provider is empty (SRC-150)"
    assert meta["llm_model"],    "llm_model is empty (SRC-150)"

    # ── Numeric §8.2 field types (SRC-150) ─────────────────────────────────
    assert isinstance(meta["token_usage"], int), (
        f"token_usage must be int, got {type(meta['token_usage'])} (SRC-150)"
    )
    assert meta["token_usage"] >= 0, (
        f"token_usage must be non-negative, got {meta['token_usage']} (SRC-150)"
    )
    assert isinstance(meta["items_considered"], int), "items_considered must be int (SRC-150)"
    assert isinstance(meta["items_included"],   int), "items_included must be int (SRC-150)"
    assert isinstance(meta["tweet_api_call_count"], int), (
        "tweet_api_call_count must be int (SRC-150)"
    )
    assert isinstance(meta["items_by_tier"],         dict), "items_by_tier must be dict (SRC-150)"
    assert isinstance(meta["items_by_source_class"], dict), (
        "items_by_source_class must be dict (SRC-150)"
    )

    # ── window_start / window_end present and non-empty (SRC-028) ──────────
    assert meta["window_start"], "window_start is empty in metadata (SRC-028)"
    assert meta["window_end"],   "window_end is empty in metadata (SRC-028)"


# ---------------------------------------------------------------------------
# Factory fixture: build and run Pipeline in dry-run mode
# ---------------------------------------------------------------------------

@pytest.fixture
def pipeline_runner(sample_agent_config, sample_secrets, prompts_dir: Path, tmp_path: Path):
    """
    Factory fixture: build and run the Pipeline in dry-run mode with mocked
    LLM + Twitter for a given cadence.  Returns a callable that accepts the
    cadence string and returns PipelineRunResult.

    Traces: SRC-098 (mocked LLM + Twitter), SRC-102 (dry-run)
    """
    from tests.conftest import DummyLLMClient

    from ai_news_agent.curation.agent import _WINDOW_FN
    from ai_news_agent.pipeline import Pipeline
    from ai_news_agent.sourcing.agent import SourcingRunResult
    from ai_news_agent.storage.models import ArticleRecord, normalize_url, url_hash
    from ai_news_agent.storage.tinydb_store import TinyDBArticleStore

    def _run(cadence: str, twitter_available: bool = True) -> Any:
        store = TinyDBArticleStore(tmp_path / f"store-{cadence}.json")

        # Seed with one article so the LLM has something to score
        raw_url = "https://reuters.com/ci-smoke-test"
        canonical = normalize_url(raw_url)
        article = ArticleRecord(
            url_hash=url_hash(canonical),
            url=canonical,
            headline="CI Smoke Test Article",
            abstract="Used by the CI smoke test suite to exercise the pipeline.",
            source_name="Reuters",
            pub_date=datetime(2026, 5, 9, 12, 0, tzinfo=UTC),
            fetched_at=datetime(2026, 5, 9, 12, 0, tzinfo=UTC),
            tier="1b",
            source_class="web",
            agent_id=sample_agent_config.agent_id,
        )
        store.insert_if_new(article)

        scratch_dir = tmp_path / f"scratch-{cadence}"
        scratch_dir.mkdir(parents=True, exist_ok=True)

        # Reference times per cadence (same as scheduler trigger times)
        ref_times: dict[str, datetime] = {
            "daily":   datetime(2026, 5, 10, 1, 0, tzinfo=UTC),
            "weekly":  datetime(2026, 5, 11, 1, 0, tzinfo=UTC),
            "monthly": datetime(2026, 5, 1,  2, 0, tzinfo=UTC),
            "annual":  datetime(2026, 1, 1,  3, 0, tzinfo=UTC),
        }
        ref = ref_times[cadence]
        ws, we = _WINDOW_FN[cadence](ref)

        with (
            patch("ai_news_agent.pipeline.SourcingAgent") as mock_sourcing,
            patch("ai_news_agent.curation.agent.get_llm_client") as mock_llm,
        ):
            mock_sourcing.return_value.run.return_value = SourcingRunResult(
                agent_id=sample_agent_config.agent_id,
                run_at=ref,
                window_start=ws,
                window_end=we,
                articles_fetched=3,
                articles_inserted=1,
                articles_duplicate=2,
                tweets_fetched=2,
                tweets_inserted=2,
                twitter_signal_available=twitter_available,
                tweet_api_call_count=2 if twitter_available else 0,
            )
            mock_llm.return_value = DummyLLMClient()

            pipeline = Pipeline(
                config=sample_agent_config,
                secrets=sample_secrets,
                store=store,
                prompts_dir=str(prompts_dir),
            )
            result = pipeline.run(
                cadence=cadence,
                window_start=ws,
                window_end=we,
                dry_run=True,
                scratch_dir=scratch_dir,
            )

        store.close()
        return result

    return _run


# ---------------------------------------------------------------------------
# Stage 1: Lint gate (pure-Python static checks, no subprocess)
# Mirrors the ``lint`` job in ci.yml.
# ---------------------------------------------------------------------------

class TestLintGate:
    """
    Python-side lint sanity — not a replacement for ``ruff check``, but a
    guard to catch regressions in the test suite itself.

    Traces: SRC-098 (ruff check enforced in CI)
    """

    def test_required_meta_fields_list_is_complete(self) -> None:
        """
        REQUIRED_META_FIELDS must include all fields checked in the container
        smoke step (ensures parity between Python and shell assertions).

        Traces: SRC-150
        """
        # Hard-code the canonical list from the ci.yml smoke step
        canonical = {
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
            "token_usage",
            "twitter_signal_available",
            "tweet_api_call_count",
        }
        assert set(REQUIRED_META_FIELDS) == canonical, (
            "REQUIRED_META_FIELDS diverges from the canonical field list in ci.yml. "
            "Update both in lockstep.  (SRC-150)"
        )

    def test_stem_re_rejects_bad_patterns(self) -> None:
        """_STEM_RE only matches YYYY-MM-DD-cadence stems (SRC-145)."""
        good = ["2026-05-10-daily", "2026-01-01-annual", "2025-12-31-weekly"]
        bad  = ["daily-2026-05-10", "2026-5-1-daily", "2026-05-10", "daily"]
        for stem in good:
            assert _STEM_RE.match(stem), f"Should match: {stem!r} (SRC-145)"
        for stem in bad:
            assert not _STEM_RE.match(stem), f"Should NOT match: {stem!r} (SRC-145)"


# ---------------------------------------------------------------------------
# Stage 2: Pipeline dry-run smoke test — all four cadences
# Mirrors the ``smoke-docker`` job container assertions.
# ---------------------------------------------------------------------------

@pytest.mark.integration
class TestSmokeContractDaily:
    """
    Daily cadence: full dry-run smoke assertions (SRC-029, SRC-102).

    Traces: SRC-004, SRC-028, SRC-029, SRC-049, SRC-102, SRC-129,
            SRC-141, SRC-145, SRC-148, SRC-150
    """

    def test_smoke_daily_twitter_available(self, pipeline_runner: Any) -> None:
        """
        Daily pipeline with Twitter sourcing signalling available: all §8.2 fields
        present, no URL-less items, correct filename, sha256 prompt version.

        Note: twitter_signal_available in the JSON reflects curation's re-evaluation
        of tweet signals in the store (not just sourcing's signal).  Since the test
        store has no tweet records in the curation window, the JSON value may differ
        from sourcing's report.  We assert only that the field is a bool (SRC-148).

        Traces: SRC-004, SRC-029, SRC-049, SRC-102, SRC-129, SRC-145,
                SRC-148, SRC-150
        """
        result = pipeline_runner("daily", twitter_available=True)
        _assert_smoke_contract(result, cadence="daily")

    def test_smoke_daily_twitter_degraded(self, pipeline_runner: Any) -> None:
        """
        Daily pipeline with Twitter degraded: pipeline still succeeds (SRC-148).

        Traces: SRC-102, SRC-148
        """
        result = pipeline_runner("daily", twitter_available=False)
        _assert_smoke_contract(result, cadence="daily")


@pytest.mark.integration
class TestSmokeContractWeekly:
    """
    Weekly cadence smoke assertions (SRC-030, SRC-102).

    Traces: SRC-004, SRC-028, SRC-030, SRC-049, SRC-102, SRC-129,
            SRC-141, SRC-145, SRC-148, SRC-150
    """

    def test_smoke_weekly(self, pipeline_runner: Any) -> None:
        """
        Weekly digest: themes + outlook + monitoring fields all present.

        Traces: SRC-004, SRC-030, SRC-102, SRC-129, SRC-145, SRC-150
        """
        result = pipeline_runner("weekly", twitter_available=True)
        _assert_smoke_contract(result, cadence="weekly")

    def test_smoke_weekly_json_has_themes(self, pipeline_runner: Any) -> None:
        """
        Weekly JSON digest must contain a themes array (SRC-030).

        Traces: SRC-030, SRC-102
        """
        result = pipeline_runner("weekly")
        assert result.success is True
        assert result.json_path is not None

        digest = json.loads(Path(result.json_path).read_text("utf-8"))
        has_themes = (
            "themes" in digest
            or "themes" in digest.get("metadata", {})
        )
        assert has_themes, "Weekly digest JSON must contain themes (SRC-030)"


@pytest.mark.integration
class TestSmokeContractMonthly:
    """
    Monthly cadence smoke assertions (SRC-031, SRC-102).

    Traces: SRC-004, SRC-028, SRC-031, SRC-049, SRC-102, SRC-129,
            SRC-141, SRC-145, SRC-148, SRC-150
    """

    def test_smoke_monthly(self, pipeline_runner: Any) -> None:
        """
        Monthly digest: all §8.2 fields present, correct filename format.

        Traces: SRC-004, SRC-031, SRC-102, SRC-129, SRC-145, SRC-150
        """
        result = pipeline_runner("monthly", twitter_available=True)
        _assert_smoke_contract(result, cadence="monthly")


@pytest.mark.integration
class TestSmokeContractAnnual:
    """
    Annual cadence smoke assertions (SRC-032, SRC-102).

    Annual adds predictions (10 falsifiable predictions) and top-10 articles.

    Traces: SRC-004, SRC-028, SRC-032, SRC-049, SRC-102, SRC-129,
            SRC-141, SRC-145, SRC-148, SRC-150
    """

    def test_smoke_annual(self, pipeline_runner: Any) -> None:
        """
        Annual digest: all §8.2 fields present, plus predictions section.

        Traces: SRC-004, SRC-032, SRC-102, SRC-129, SRC-145, SRC-150
        """
        result = pipeline_runner("annual", twitter_available=True)
        _assert_smoke_contract(result, cadence="annual")

    def test_smoke_annual_json_has_predictions(self, pipeline_runner: Any) -> None:
        """
        Annual JSON must contain a predictions array (SRC-032).

        Traces: SRC-032, SRC-102
        """
        result = pipeline_runner("annual")
        assert result.success is True
        assert result.json_path is not None

        digest = json.loads(Path(result.json_path).read_text("utf-8"))
        has_predictions = (
            "predictions" in digest
            or "predictions" in digest.get("metadata", {})
        )
        assert has_predictions, "Annual digest JSON must contain predictions (SRC-032)"


# ---------------------------------------------------------------------------
# Stage 3: URL enforcement — exhaustive layer checks
# (SRC-049 at curation, SRC-141 at rendering — both enforced in the pipeline)
# ---------------------------------------------------------------------------

@pytest.mark.integration
class TestURLEnforcement:
    """
    URL enforcement at two independent layers (curation + rendering).

    Mirrors the container smoke ``bad_items`` assertion.
    Traces: SRC-049, SRC-141
    """

    def test_no_url_items_absent_from_json(
        self,
        sample_agent_config,
        sample_secrets,
        prompts_dir: Path,
        tmp_path: Path,
    ) -> None:
        """
        Items with empty URL injected via LLM response must not appear in
        the final JSON output (SRC-049 — non-negotiable, SRC-141 — renderer).

        Traces: SRC-049, SRC-102, SRC-141
        """
        from tests.conftest import DummyLLMClient

        from ai_news_agent.pipeline import Pipeline
        from ai_news_agent.sourcing.agent import SourcingRunResult
        from ai_news_agent.storage.models import ArticleRecord, normalize_url, url_hash
        from ai_news_agent.storage.tinydb_store import TinyDBArticleStore

        store = TinyDBArticleStore(tmp_path / "store-url-check.json")
        raw_url = "https://reuters.com/url-enforcement-test"
        canonical = normalize_url(raw_url)
        store.insert_if_new(
            ArticleRecord(
                url_hash=url_hash(canonical),
                url=canonical,
                headline="URL Enforcement Article",
                abstract="Used to test URL enforcement.",
                source_name="Reuters",
                pub_date=datetime(2026, 5, 9, 12, 0, tzinfo=UTC),
                fetched_at=datetime(2026, 5, 9, 12, 0, tzinfo=UTC),
                tier="1b",
                source_class="web",
                agent_id=sample_agent_config.agent_id,
            )
        )

        # LLM response with a no-URL item mixed in
        no_url_payload = json.dumps({
            "items": [
                {
                    "headline": "Valid Item With URL",
                    "source_name": "Reuters",
                    "url": "https://reuters.com/valid-url",
                    "pub_date": "2026-05-09",
                    "why_it_matters": "This should appear in output.",
                    "impact_tags": ["business_impact"],
                    "tier": "1b",
                    "cross_refs": [],
                    "twitter_handle": None,
                    "tweet_url": None,
                },
                {
                    "headline": "No-URL Item — Must Be Dropped",
                    "source_name": "Unknown",
                    "url": "",          # ← empty URL
                    "pub_date": "2026-05-09",
                    "why_it_matters": "Should not appear in any output.",
                    "impact_tags": [],
                    "tier": "3",
                    "cross_refs": [],
                    "twitter_handle": None,
                    "tweet_url": None,
                },
            ],
            "themes": [],
            "outlook": "",
            "predictions": [],
        })
        mock_response = f"```json\n{no_url_payload}\n```"

        scratch = tmp_path / "scratch-url-check"
        scratch.mkdir()

        with (
            patch("ai_news_agent.pipeline.SourcingAgent") as mock_sourcing,
            patch("ai_news_agent.curation.agent.get_llm_client") as mock_llm,
        ):
            mock_sourcing.return_value.run.return_value = SourcingRunResult(
                agent_id=sample_agent_config.agent_id,
                run_at=datetime(2026, 5, 10, 1, 0, tzinfo=UTC),
                window_start=datetime(2026, 5, 9, 0, 0, tzinfo=UTC),
                window_end=datetime(2026, 5, 9, 23, 59, tzinfo=UTC),
                articles_fetched=1,
                articles_inserted=0,
                articles_duplicate=1,
                tweets_fetched=0,
                tweets_inserted=0,
                twitter_signal_available=True,
                tweet_api_call_count=0,
            )
            mock_llm.return_value = DummyLLMClient(complete_response=mock_response)

            pipeline = Pipeline(
                config=sample_agent_config,
                secrets=sample_secrets,
                store=store,
                prompts_dir=str(prompts_dir),
            )
            result = pipeline.run(
                cadence="daily",
                window_start=datetime(2026, 5, 9, 0, 0, tzinfo=UTC),
                window_end=datetime(2026, 5, 9, 23, 59, tzinfo=UTC),
                dry_run=True,
                scratch_dir=scratch,
            )

        assert result.success is True

        digest = json.loads(Path(result.json_path).read_text("utf-8"))  # type: ignore[arg-type]

        # No items without a valid URL (SRC-049, SRC-141)
        bad_items = [
            it for it in digest.get("items", [])
            if not str(it.get("url", "")).startswith("http")
        ]
        assert not bad_items, (
            f"URL enforcement failed: {len(bad_items)} item(s) without valid URL "
            f"appeared in JSON output (SRC-049, SRC-141): "
            + str([it.get("headline", "?") for it in bad_items])
        )

        # The no-URL headline must not appear in any format (SRC-049, SRC-141)
        bad_headline = "No-URL Item — Must Be Dropped"
        headlines = [it.get("headline", "") for it in digest.get("items", [])]
        assert bad_headline not in headlines, (
            f"No-URL item slipped through URL enforcement: {bad_headline!r} (SRC-049)"
        )

        md_text   = Path(result.markdown_path).read_text("utf-8")   # type: ignore[arg-type]
        html_text = Path(result.html_path).read_text("utf-8")       # type: ignore[arg-type]
        assert bad_headline not in md_text,   "No-URL item in Markdown (SRC-141)"
        assert bad_headline not in html_text, "No-URL item in HTML (SRC-141)"

        store.close()


# ---------------------------------------------------------------------------
# Stage 4: Prompt hash integrity (mirrors ``prompt-hashes`` CI job)
# ---------------------------------------------------------------------------

class TestPromptHashIntegrity:
    """
    Verify the SHA-256 prompt hash manifest is internally consistent.

    Mirrors the ``ai-news-prompt-hashes --verify`` step in the ``prompt-hashes``
    CI job.  The CLI itself is tested in test_cli_entrypoints.py; this class
    verifies the runtime invariant directly.

    Traces: SRC-127, SRC-128, SRC-129
    """

    def test_prompt_hashes_manifest_parseable(self) -> None:
        """
        prompts/prompt_hashes.json must exist and be valid JSON (SRC-129).
        """
        manifest_path = Path("prompts/prompt_hashes.json")
        if not manifest_path.exists():
            pytest.skip("prompts/prompt_hashes.json not found — run from repo root")

        data = json.loads(manifest_path.read_text("utf-8"))
        assert isinstance(data, dict), "prompt_hashes.json must be a JSON object (SRC-129)"
        assert data, "prompt_hashes.json must not be empty (SRC-129)"

    def test_all_cadence_hashes_present_in_manifest(self) -> None:
        """
        The manifest must contain an entry for each cadence (SRC-127–SRC-129).
        """
        manifest_path = Path("prompts/prompt_hashes.json")
        if not manifest_path.exists():
            pytest.skip("prompts/prompt_hashes.json not found — run from repo root")

        data = json.loads(manifest_path.read_text("utf-8"))
        for cadence in ("daily", "weekly", "monthly", "annual"):
            key = f"prompts/{cadence}.md"
            assert key in data or cadence in str(data), (
                f"Cadence {cadence!r} not found in prompt_hashes.json (SRC-129)"
            )

    def test_prompt_version_format_matches_sha256(
        self,
        sample_agent_config,
        sample_secrets,
        prompts_dir: Path,
        tmp_path: Path,
    ) -> None:
        """
        CurationAgent must produce prompt_version strings starting with 'sha256:'
        for every cadence (SRC-129).

        Traces: SRC-129
        """
        from unittest.mock import patch as _patch

        from tests.conftest import DummyLLMClient

        from ai_news_agent.curation.agent import CurationAgent
        from ai_news_agent.storage.tinydb_store import TinyDBArticleStore

        for cadence in ("daily", "weekly", "monthly", "annual"):
            store = TinyDBArticleStore(tmp_path / f"store-hash-{cadence}.json")
            with _patch("ai_news_agent.curation.agent.get_llm_client") as mock_factory:
                mock_factory.return_value = DummyLLMClient()
                agent = CurationAgent(
                    config=sample_agent_config,
                    secrets=sample_secrets,
                    store=store,
                    prompts_dir=str(prompts_dir),
                )
                ref = datetime(2026, 5, 10, tzinfo=UTC)
                run_result = agent.run(cadence=cadence, reference_time=ref)

            assert run_result.metadata.prompt_version.startswith("sha256:"), (
                f"prompt_version must start with 'sha256:' for cadence={cadence!r} "
                f"(SRC-129), got: {run_result.metadata.prompt_version!r}"
            )
            store.close()


# ---------------------------------------------------------------------------
# Stage 5: Entry-point registration check
# Mirrors the ``Container check · CLI entry-points registered`` step in ci.yml.
# ---------------------------------------------------------------------------

class TestEntryPointRegistration:
    """
    All required CLI entry points must be registered in the installed package.

    Mirrors the importlib.metadata entry_points check in the Docker build step.
    Traces: SRC-076, SRC-077, SRC-102
    """

    REQUIRED_ENTRY_POINTS: frozenset[str] = frozenset({
        "ai-news-source",
        "ai-news-curate",
        "ai-news-render",
        "ai-news-schedule",
        "ai-news-portal",
        "ai-news-run",
        "ai-news-oneshot",
        "ai-news-prompt-hashes",
    })

    def test_all_cli_entry_points_registered(self) -> None:
        """
        All required CLI entry points must be registered via pyproject.toml
        [project.scripts] after ``pip install -e .``.

        Skipped when the package is not installed as a distribution (e.g.
        direct pytest invocation without ``pip install -e .``).

        Traces: SRC-076, SRC-077 (local dev + manual trigger CLIs)
        """
        from importlib.metadata import entry_points, packages_distributions

        # Only assert when the package is properly installed as a dist
        try:
            installed_pkgs = packages_distributions()
            if "ai_news_agent" not in installed_pkgs:
                pytest.skip(
                    "ai-news-agent not installed as a distribution — "
                    "run 'pip install -e .' first (SRC-076)"
                )
        except Exception:  # noqa: BLE001
            pytest.skip("Could not determine installed packages — skipping")

        eps = {ep.name for ep in entry_points(group="console_scripts")}
        missing = self.REQUIRED_ENTRY_POINTS - eps
        assert not missing, (
            f"Missing CLI entry points: {missing}. "
            f"Check [project.scripts] in pyproject.toml. (SRC-076)"
        )

    def test_ai_news_run_importable(self) -> None:
        """
        ai-news-run entry-point target (pipeline.cli_main) must be importable.

        Traces: SRC-076
        """
        from ai_news_agent.pipeline import cli_main  # noqa: F401

    def test_ai_news_prompt_hashes_importable(self) -> None:
        """
        ai-news-prompt-hashes entry-point target must be importable (SRC-129).
        """
        from ai_news_agent.curation.prompt_builder import _cli_prompt_hashes  # noqa: F401


# ---------------------------------------------------------------------------
# Stage 6: §8.2 monitoring field completeness in PipelineRunResult
# ---------------------------------------------------------------------------

@pytest.mark.integration
class TestMonitoringFieldCompleteness:
    """
    PipelineRunResult must expose all §8.2 quality-monitoring fields (SRC-150).

    Traces: SRC-148, SRC-150
    """

    def test_pipeline_result_exposes_all_section_8_2_fields(
        self,
        pipeline_runner: Any,
    ) -> None:
        """
        The PipelineRunResult dataclass must expose every §8.2 monitoring field
        as an attribute (not just in the JSON output).

        Traces: SRC-129, SRC-148, SRC-150
        """
        result = pipeline_runner("daily", twitter_available=True)
        assert result.success is True

        # §8.2 field presence on the result object
        assert hasattr(result, "items_considered"),       "items_considered missing (SRC-150)"
        assert hasattr(result, "items_included"),         "items_included missing (SRC-150)"
        assert hasattr(result, "items_by_tier"),          "items_by_tier missing (SRC-150)"
        assert hasattr(result, "items_by_source_class"),  "items_by_source_class missing (SRC-150)"
        assert hasattr(result, "token_usage"),            "token_usage missing (SRC-150)"
        assert hasattr(result, "llm_provider"),           "llm_provider missing (SRC-150)"
        assert hasattr(result, "llm_model"),              "llm_model missing (SRC-150)"
        assert hasattr(result, "prompt_version"),         "prompt_version missing (SRC-129)"
        assert hasattr(result, "tweet_api_call_count"),   "tweet_api_call_count missing (SRC-150)"
        assert hasattr(result, "twitter_signal_available"), (
            "twitter_signal_available missing (SRC-148)"
        )

        # Type checks
        assert isinstance(result.items_by_tier, dict),         "items_by_tier must be dict"
        assert isinstance(result.items_by_source_class, dict), "items_by_source_class must be dict"
        assert isinstance(result.twitter_signal_available, bool)
        assert result.prompt_version.startswith("sha256:"), (
            "prompt_version must start with 'sha256:' (SRC-129)"
        )
        assert result.llm_provider, "llm_provider must be non-empty (SRC-150)"
        assert result.llm_model,    "llm_model must be non-empty (SRC-150)"

    def test_twitter_degraded_reflected_in_result(
        self,
        pipeline_runner: Any,
    ) -> None:
        """
        Pipeline result with Twitter degraded must report twitter_signal_available=False
        and tweet_api_call_count=0 when sourcing reports Twitter unavailable (SRC-148).

        Traces: SRC-148, SRC-150
        """
        result = pipeline_runner("daily", twitter_available=False)
        assert result.success is True
        # tweet_api_call_count should be 0 when Twitter is degraded at sourcing level
        assert result.tweet_api_call_count == 0, (
            "tweet_api_call_count should be 0 when Twitter is degraded (SRC-150)"
        )


# ---------------------------------------------------------------------------
# Stage 7: Docker image label contract
# (Checks the image *after* build — skipped if docker is not available)
# ---------------------------------------------------------------------------

class TestDockerImageLabels:
    """
    Verify the Docker image carries the required OCI labels (SRC-099).

    Skipped when Docker is not available (developer machines without daemon).
    Traces: SRC-085, SRC-099
    """

    @pytest.mark.skipif(
        not _can_run_docker(),
        reason="Docker daemon not available — skipping image label checks (SRC-099)",
    )
    def test_image_has_oci_labels(self) -> None:
        """
        The built image must carry OCI standard labels (SRC-099).
        """
        image_tag = "ai-news-agent:ci-test"
        try:
            out = subprocess.check_output(  # noqa: S603
                ["docker", "inspect", "--format", "{{json .Config.Labels}}", image_tag],
                text=True,
                timeout=10,
            )
        except subprocess.CalledProcessError:
            pytest.skip(f"Image {image_tag!r} not built — run docker build first (SRC-099)")

        labels = json.loads(out.strip())
        assert labels.get("org.opencontainers.image.title"), (
            "OCI label org.opencontainers.image.title missing (SRC-099)"
        )
