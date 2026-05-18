"""
tests/integration/test_pipeline_smoke.py — Dry-run end-to-end pipeline smoke test.
No real LLM or Twitter calls — all external I/O is mocked.

Traces: SRC-098 (unit tests — mock LLM + Twitter), SRC-102 (smoke test: dry-run mode),
        SRC-004 (MD/HTML/JSON outputs produced), SRC-049 (URL enforcement),
        SRC-141 (renderer URL drop), SRC-145 (date-stamped filenames)
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest
from tests.conftest import DummyLLMClient

from ai_news_agent.curation.agent import CurationAgent, CurationRunResult
from ai_news_agent.rendering.agent import RenderingAgent
from ai_news_agent.storage.models import (
    ArticleRecord,
    normalize_url,
    url_hash,
)
from ai_news_agent.storage.tinydb_store import TinyDBArticleStore

if TYPE_CHECKING:
    from pathlib import Path

# ---------------------------------------------------------------------------
# Smoke: Sourcing → Store → Curation → Rendering (all cadences)
# Traces: SRC-102 (smoke test), SRC-004 (non-empty output with all required fields)
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestPipelineSmoke:
    """
    Dry-run smoke test: exercises the full pipeline path (sourcing → curation → rendering)
    with mocked LLM and Twitter calls.

    Verifies (SRC-102 acceptance criteria):
    - All three output formats are written.
    - Output is non-empty.
    - All required fields populated.
    - Items without URLs are dropped.
    - Prompt version is recorded.

    Traces: SRC-004, SRC-049, SRC-098, SRC-102, SRC-141, SRC-145, SRC-150
    """

    def _make_sample_article(self, agent_id: str) -> ArticleRecord:
        raw = "https://reuters.com/pipeline-smoke-test"
        canonical = normalize_url(raw)
        return ArticleRecord(
            url_hash=url_hash(canonical),
            url=canonical,
            headline="Pipeline Smoke Test Article",
            abstract="This article is used by the smoke test.",
            source_name="Reuters",
            pub_date=datetime(2026, 5, 9, 12, 0, tzinfo=UTC),
            fetched_at=datetime(2026, 5, 9, 12, 0, tzinfo=UTC),
            tier="1b",
            source_class="web",
            agent_id=agent_id,
        )

    def test_daily_pipeline_smoke(
        self,
        sample_agent_config,
        sample_secrets,
        prompts_dir: Path,
        tmp_path: Path,
    ) -> None:
        """
        Full daily pipeline: sourcing stores article → curation selects it →
        renderer writes MD/HTML/JSON.

        Traces: SRC-004, SRC-029, SRC-049, SRC-102, SRC-141, SRC-145, SRC-150
        """
        store = TinyDBArticleStore(tmp_path / "store.json")
        article = self._make_sample_article(sample_agent_config.agent_id)
        store.insert_if_new(article)

        with patch("ai_news_agent.curation.agent.get_llm_client") as mock_factory:
            mock_factory.return_value = DummyLLMClient()

            agent = CurationAgent(
                config=sample_agent_config,
                secrets=sample_secrets,
                store=store,
                prompts_dir=str(prompts_dir),
            )
            result = agent.run(
                cadence="daily",
                window_start=datetime(2026, 5, 9, 0, 0, tzinfo=UTC),
                window_end=datetime(2026, 5, 9, 23, 59, tzinfo=UTC),
            )

        # Verify CurationRunResult structure
        assert isinstance(result, CurationRunResult)
        assert result.metadata.cadence == "daily"
        assert result.metadata.prompt_version.startswith("sha256:")  # SRC-129
        assert result.metadata.agent_id == "test-agent"

        # Render to temp directory
        output_dir = tmp_path / "outputs"
        renderer = RenderingAgent(output_dir=output_dir)
        rendering_result = renderer.render(result)

        # All three formats must exist (SRC-004)
        assert rendering_result.markdown_path.exists(), "Markdown output missing"
        assert rendering_result.html_path.exists(), "HTML output missing"
        assert rendering_result.json_path.exists(), "JSON output missing"

        # Files must not be empty
        assert rendering_result.markdown_path.stat().st_size > 0
        assert rendering_result.html_path.stat().st_size > 0
        assert rendering_result.json_path.stat().st_size > 0

        # JSON must parse and have required fields (SRC-102 — all required fields populated)
        json_data = json.loads(rendering_result.json_path.read_text())
        assert "schema_version" in json_data
        assert "metadata" in json_data
        assert "items" in json_data
        assert json_data["metadata"]["agent_id"] == "test-agent"
        assert json_data["metadata"]["prompt_version"].startswith("sha256:")

        # No items with missing URLs in JSON output (SRC-049, SRC-141)
        for item in json_data["items"]:
            assert item["url"], f"Item without URL in output: {item['headline']}"

        # Date-stamped filename (SRC-145)
        assert "2026" in rendering_result.markdown_path.name
        assert "daily" in rendering_result.markdown_path.name

        store.close()

    @pytest.mark.parametrize("cadence", ["daily", "weekly", "monthly", "annual"])
    def test_all_cadences_render_without_exception(
        self,
        cadence: str,
        sample_agent_config,
        sample_secrets,
        prompts_dir: Path,
        tmp_path: Path,
    ) -> None:
        """
        All four cadences must render without raising exceptions (SRC-004, SRC-102).
        Traces: SRC-029–SRC-032, SRC-102
        """
        store = TinyDBArticleStore(tmp_path / f"store-{cadence}.json")

        with patch("ai_news_agent.curation.agent.get_llm_client") as mock_factory:
            mock_factory.return_value = DummyLLMClient()

            agent = CurationAgent(
                config=sample_agent_config,
                secrets=sample_secrets,
                store=store,
                prompts_dir=str(prompts_dir),
            )

            ref_map = {
                "daily": datetime(2026, 5, 10, tzinfo=UTC),
                "weekly": datetime(2026, 5, 10, tzinfo=UTC),
                "monthly": datetime(2026, 5, 1, tzinfo=UTC),
                "annual": datetime(2026, 1, 1, tzinfo=UTC),
            }
            result = agent.run(
                cadence=cadence,
                reference_time=ref_map[cadence],
            )

        output_dir = tmp_path / f"outputs-{cadence}"
        renderer = RenderingAgent(output_dir=output_dir)

        # Must not raise any exception (SRC-102)
        rendering_result = renderer.render(result)
        assert rendering_result.markdown_path.exists()

        store.close()

    def test_no_url_items_absent_from_all_outputs(
        self,
        sample_agent_config,
        sample_secrets,
        prompts_dir: Path,
        tmp_path: Path,
    ) -> None:
        """
        Smoke: inject an LLM response with a no-URL item.
        All three renderers must exclude it.
        Traces: SRC-049 (non-negotiable), SRC-141 (renderer URL enforcement)
        """
        import json as json_mod

        from tests.conftest import DummyLLMClient

        no_url_response = json_mod.dumps(
            {
                "items": [
                    {
                        "headline": "No URL Item — Must Be Dropped",
                        "source_name": "Unknown",
                        "url": "",
                        "pub_date": "2026-05-09",
                        "why_it_matters": "Should not appear in output.",
                        "impact_tags": [],
                        "tier": "3",
                        "cross_refs": [],
                    }
                ],
                "themes": [],
                "outlook": "",
                "predictions": [],
            }
        )
        raw_response = f"```json\n{no_url_response}\n```"

        store = TinyDBArticleStore(tmp_path / "store.json")
        article = self._make_sample_article(sample_agent_config.agent_id)
        store.insert_if_new(article)

        with patch("ai_news_agent.curation.agent.get_llm_client") as mock_factory:
            mock_factory.return_value = DummyLLMClient(complete_response=raw_response)
            agent = CurationAgent(
                config=sample_agent_config,
                secrets=sample_secrets,
                store=store,
                prompts_dir=str(prompts_dir),
            )
            result = agent.run(
                cadence="daily",
                window_start=datetime(2026, 5, 9, 0, 0, tzinfo=UTC),
                window_end=datetime(2026, 5, 9, 23, 59, tzinfo=UTC),
            )

        output_dir = tmp_path / "outputs"
        renderer = RenderingAgent(output_dir=output_dir)
        rendering_result = renderer.render(result)

        # No-URL item must not appear in any output (SRC-049, SRC-141)
        md = rendering_result.markdown_path.read_text()
        html = rendering_result.html_path.read_text()
        json_data = json.loads(rendering_result.json_path.read_text())

        assert "No URL Item — Must Be Dropped" not in md
        assert "No URL Item — Must Be Dropped" not in html
        for item in json_data["items"]:
            assert item.get("url"), "No-URL item slipped into JSON output"

        store.close()

    def test_prompt_version_recorded_in_all_outputs(
        self,
        sample_agent_config,
        sample_secrets,
        prompts_dir: Path,
        tmp_path: Path,
    ) -> None:
        """
        Prompt SHA-256 version must appear in all three output formats (SRC-129).
        """
        store = TinyDBArticleStore(tmp_path / "store.json")

        with patch("ai_news_agent.curation.agent.get_llm_client") as mock_factory:
            mock_factory.return_value = DummyLLMClient()
            agent = CurationAgent(
                config=sample_agent_config,
                secrets=sample_secrets,
                store=store,
                prompts_dir=str(prompts_dir),
            )
            result = agent.run(
                cadence="daily",
                window_start=datetime(2026, 5, 9, 0, 0, tzinfo=UTC),
                window_end=datetime(2026, 5, 9, 23, 59, tzinfo=UTC),
            )

        output_dir = tmp_path / "outputs"
        renderer = RenderingAgent(output_dir=output_dir)
        rendering_result = renderer.render(result)

        prompt_version = result.metadata.prompt_version
        assert prompt_version.startswith("sha256:")

        md = rendering_result.markdown_path.read_text()
        html = rendering_result.html_path.read_text()
        json_data = json.loads(rendering_result.json_path.read_text())

        assert prompt_version in md, "Prompt version missing from Markdown output"
        assert prompt_version in html, "Prompt version missing from HTML output"
        assert json_data["metadata"]["prompt_version"] == prompt_version

        store.close()
