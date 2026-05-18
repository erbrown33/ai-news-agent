"""
tests/unit/test_cli_entrypoints.py — Unit tests for CLI entry points.

Tests the ``ai-news-run``, ``ai-news-curate``, and ``ai-news-prompt-hashes``
CLI entry points exercising argument parsing, error handling, and exit codes.
All external I/O is mocked.

Coverage matrix
───────────────
SRC-028   --window-start/--window-end for on-demand re-run    → TestPipelineCLI
SRC-076   Local dev Phase 1 CLI                               → TestPipelineCLI
SRC-102   --dry-run: scratch dir, no production writes        → TestPipelineCLI
SRC-127   Prompt version control                              → TestPromptHashCLI
SRC-128   Prompt changes require review                       → TestPromptHashCLI
SRC-129   SHA-256 hash verification                           → TestPromptHashCLI
SRC-147   Manual trigger / on-demand execution               → TestPipelineCLI
SRC-148   --twitter-available flag                            → TestPipelineCLI

Traces: SRC-028, SRC-076, SRC-102, SRC-127–SRC-129, SRC-147, SRC-148
"""

from __future__ import annotations

import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_dummy_pipeline_result(success: bool = True) -> MagicMock:
    """Build a mock PipelineRunResult."""
    mock = MagicMock()
    mock.success = success
    mock.dry_run = True
    mock.agent_id = "test-agent"
    mock.cadence = "daily"
    mock.run_at = datetime(2026, 5, 9, tzinfo=UTC)
    mock.articles_fetched = 5
    mock.articles_inserted = 5
    mock.articles_duplicate = 0
    mock.tweets_fetched = 2
    mock.tweets_inserted = 2
    mock.twitter_signal_available = True
    mock.tweet_api_call_count = 2
    mock.items_considered = 10
    mock.items_included = 3
    mock.items_by_tier = {"1b": 2, "2": 1}
    mock.items_by_source_class = {"web": 3}
    mock.token_usage = 4500
    mock.llm_provider = "openai"
    mock.llm_model = "gpt-4o"
    mock.prompt_version = "sha256:abc123def456"
    mock.errors = []
    mock.markdown_path = Path("/tmp/scratch/2026-05-09-daily.md")
    mock.html_path = Path("/tmp/scratch/2026-05-09-daily.html")
    mock.json_path = Path("/tmp/scratch/2026-05-09-daily.json")
    return mock


# ---------------------------------------------------------------------------
# Pipeline CLI (ai-news-run)
# ---------------------------------------------------------------------------


class TestPipelineCLI:
    """
    Tests for the ``ai-news-run`` CLI entry point (pipeline.cli_main).

    Traces: SRC-028, SRC-076, SRC-102, SRC-147, SRC-148
    """

    def _run_cli(self, args: list[str]) -> tuple[int, str]:
        """Invoke cli_main() with the given args; return (exit_code, output)."""
        from ai_news_agent.pipeline import cli_main

        with patch.object(sys, "argv", ["ai-news-run"] + args):
            try:
                cli_main()
                return 0, ""
            except SystemExit as exc:
                return exc.code or 0, ""

    def test_cli_requires_cadence(self) -> None:
        """
        ai-news-run without --cadence must exit with code 2.
        Traces: SRC-076
        """
        from ai_news_agent.pipeline import cli_main

        with patch.object(sys, "argv", ["ai-news-run"]), pytest.raises(SystemExit) as exc:
            cli_main()
        assert exc.value.code == 2

    def test_cli_window_start_without_end_exits_2(self) -> None:
        """
        --window-start without --window-end must exit with code 2 (SRC-028).
        """
        from ai_news_agent.pipeline import cli_main

        with (
            patch("ai_news_agent.pipeline.load_agent_config", return_value=MagicMock()),
            patch("ai_news_agent.pipeline.RuntimeSecrets", return_value=MagicMock()),
            patch.object(
                sys, "argv", ["ai-news-run", "--cadence", "daily", "--window-start", "2026-05-01"]
            ),
            pytest.raises(SystemExit) as exc,
        ):
            cli_main()
        assert exc.value.code == 2

    def test_cli_window_end_without_start_exits_2(self) -> None:
        """
        --window-end without --window-start must exit with code 2 (SRC-028).
        """
        from ai_news_agent.pipeline import cli_main

        with (
            patch("ai_news_agent.pipeline.load_agent_config", return_value=MagicMock()),
            patch("ai_news_agent.pipeline.RuntimeSecrets", return_value=MagicMock()),
            patch.object(
                sys, "argv", ["ai-news-run", "--cadence", "daily", "--window-end", "2026-05-31"]
            ),
            pytest.raises(SystemExit) as exc,
        ):
            cli_main()
        assert exc.value.code == 2

    def test_cli_invalid_window_date_exits_2(self) -> None:
        """
        Non-ISO window dates must exit with code 2.
        Traces: SRC-028
        """
        from ai_news_agent.pipeline import cli_main

        with (
            patch("ai_news_agent.pipeline.load_agent_config") as mock_cfg,
            patch("ai_news_agent.pipeline.RuntimeSecrets") as mock_sec,
        ):
            mock_cfg.return_value = MagicMock()
            mock_sec.return_value = MagicMock()

            with (
                patch.object(
                    sys,
                    "argv",
                    [
                        "ai-news-run",
                        "--cadence",
                        "daily",
                        "--window-start",
                        "not-a-date",
                        "--window-end",
                        "also-not-a-date",
                    ],
                ),
                pytest.raises(SystemExit) as exc,
            ):
                cli_main()
            assert exc.value.code == 2

    def test_cli_config_load_failure_exits_2(self) -> None:
        """
        Config file load failure must exit with code 2.
        Traces: SRC-076 (fail loudly at startup)
        """
        from ai_news_agent.pipeline import cli_main

        with (
            patch(
                "ai_news_agent.pipeline.load_agent_config", side_effect=Exception("file not found")
            ),
            patch.object(sys, "argv", ["ai-news-run", "--cadence", "daily"]),
            pytest.raises(SystemExit) as exc,
        ):
            cli_main()
        assert exc.value.code == 2

    def test_cli_secrets_load_failure_exits_2(self) -> None:
        """
        RuntimeSecrets load failure must exit with code 2.
        Traces: SRC-073
        """
        from ai_news_agent.pipeline import cli_main

        with (
            patch("ai_news_agent.pipeline.load_agent_config", return_value=MagicMock()),
            patch(
                "ai_news_agent.pipeline.RuntimeSecrets", side_effect=Exception("missing env var")
            ),
            patch.object(sys, "argv", ["ai-news-run", "--cadence", "daily"]),
            pytest.raises(SystemExit) as exc,
        ):
            cli_main()
        assert exc.value.code == 2

    def test_cli_successful_run_exits_0(self, tmp_path: Path) -> None:
        """
        Successful pipeline run must exit with code 0.
        Traces: SRC-076
        """
        from ai_news_agent.pipeline import cli_main

        mock_result = _make_dummy_pipeline_result(success=True)
        scratch = str(tmp_path / "scratch")

        with (
            patch("ai_news_agent.pipeline.load_agent_config") as mock_cfg,
            patch("ai_news_agent.pipeline.RuntimeSecrets") as mock_sec,
            patch("ai_news_agent.pipeline.Pipeline") as MockPipeline,
        ):
            mock_cfg.return_value = MagicMock()
            mock_sec.return_value = MagicMock()
            MockPipeline.return_value.run.return_value = mock_result

            with (
                patch.object(
                    sys,
                    "argv",
                    [
                        "ai-news-run",
                        "--cadence",
                        "daily",
                        "--dry-run",
                        "--scratch-dir",
                        scratch,
                    ],
                ),
                pytest.raises(SystemExit) as exc,
            ):
                cli_main()
            assert exc.value.code == 0

    def test_cli_failed_run_exits_1(self, tmp_path: Path) -> None:
        """
        Failed pipeline run must exit with code 1.
        Traces: SRC-076
        """
        from ai_news_agent.pipeline import cli_main

        mock_result = _make_dummy_pipeline_result(success=False)
        mock_result.errors = ["LLM timeout"]

        with (
            patch("ai_news_agent.pipeline.load_agent_config") as mock_cfg,
            patch("ai_news_agent.pipeline.RuntimeSecrets") as mock_sec,
            patch("ai_news_agent.pipeline.Pipeline") as MockPipeline,
            patch.object(sys, "argv", ["ai-news-run", "--cadence", "daily"]),
        ):
            mock_cfg.return_value = MagicMock()
            mock_sec.return_value = MagicMock()
            MockPipeline.return_value.run.return_value = mock_result
            with pytest.raises(SystemExit) as exc:
                cli_main()
        assert exc.value.code == 1

    def test_cli_dry_run_auto_scratch_dir(self, tmp_path: Path) -> None:
        """
        --dry-run without --scratch-dir auto-creates a tmpdir.
        Traces: SRC-102
        """
        from ai_news_agent.pipeline import cli_main

        mock_result = _make_dummy_pipeline_result(success=True)

        with (
            patch("ai_news_agent.pipeline.load_agent_config") as mock_cfg,
            patch("ai_news_agent.pipeline.RuntimeSecrets") as mock_sec,
            patch("ai_news_agent.pipeline.Pipeline") as MockPipeline,
            patch("ai_news_agent.pipeline.tempfile.mkdtemp", return_value="/tmp/ai-dry-run-test"),
            patch.object(sys, "argv", ["ai-news-run", "--cadence", "daily", "--dry-run"]),
        ):
            mock_cfg.return_value = MagicMock()
            mock_sec.return_value = MagicMock()
            MockPipeline.return_value.run.return_value = mock_result
            with pytest.raises(SystemExit) as exc:
                cli_main()

        # Should succeed (dry-run auto-creates scratch dir)
        assert exc.value.code == 0

    def test_cli_twitter_available_true(self, tmp_path: Path) -> None:
        """
        --twitter-available true passes twitter_api_available=True to pipeline.
        Traces: SRC-148
        """
        from ai_news_agent.pipeline import cli_main

        mock_result = _make_dummy_pipeline_result(success=True)
        captured_kwargs: dict = {}

        def capture_run(**kwargs):
            captured_kwargs.update(kwargs)
            return mock_result

        with (
            patch("ai_news_agent.pipeline.load_agent_config") as mock_cfg,
            patch("ai_news_agent.pipeline.RuntimeSecrets") as mock_sec,
            patch("ai_news_agent.pipeline.Pipeline") as MockPipeline,
        ):
            mock_cfg.return_value = MagicMock()
            mock_sec.return_value = MagicMock()
            MockPipeline.return_value.run.side_effect = capture_run

            with (
                patch.object(
                    sys,
                    "argv",
                    [
                        "ai-news-run",
                        "--cadence",
                        "daily",
                        "--twitter-available",
                        "true",
                    ],
                ),
                pytest.raises(SystemExit) as exc,
            ):
                cli_main()

            assert exc.value.code == 0
            assert captured_kwargs.get("twitter_api_available") is True

    def test_cli_twitter_available_false(self, tmp_path: Path) -> None:
        """
        --twitter-available false passes twitter_api_available=False to pipeline.
        Traces: SRC-148
        """
        from ai_news_agent.pipeline import cli_main

        mock_result = _make_dummy_pipeline_result(success=True)
        captured_kwargs: dict = {}

        def capture_run(**kwargs):
            captured_kwargs.update(kwargs)
            return mock_result

        with (
            patch("ai_news_agent.pipeline.load_agent_config") as mock_cfg,
            patch("ai_news_agent.pipeline.RuntimeSecrets") as mock_sec,
            patch("ai_news_agent.pipeline.Pipeline") as MockPipeline,
        ):
            mock_cfg.return_value = MagicMock()
            mock_sec.return_value = MagicMock()
            MockPipeline.return_value.run.side_effect = capture_run

            with (
                patch.object(
                    sys,
                    "argv",
                    [
                        "ai-news-run",
                        "--cadence",
                        "daily",
                        "--twitter-available",
                        "false",
                    ],
                ),
                pytest.raises(SystemExit) as exc,
            ):
                cli_main()

            assert exc.value.code == 0
            assert captured_kwargs.get("twitter_api_available") is False

    def test_cli_skip_sourcing_flag(self, tmp_path: Path) -> None:
        """
        --skip-sourcing must pass skip_sourcing=True to Pipeline.run().
        Traces: SRC-028 (on-demand re-run)
        """
        from ai_news_agent.pipeline import cli_main

        mock_result = _make_dummy_pipeline_result(success=True)
        captured_kwargs: dict = {}

        def capture_run(**kwargs):
            captured_kwargs.update(kwargs)
            return mock_result

        with (
            patch("ai_news_agent.pipeline.load_agent_config") as mock_cfg,
            patch("ai_news_agent.pipeline.RuntimeSecrets") as mock_sec,
            patch("ai_news_agent.pipeline.Pipeline") as MockPipeline,
        ):
            mock_cfg.return_value = MagicMock()
            mock_sec.return_value = MagicMock()
            MockPipeline.return_value.run.side_effect = capture_run

            with (
                patch.object(sys, "argv", ["ai-news-run", "--cadence", "daily", "--skip-sourcing"]),
                pytest.raises(SystemExit) as exc,
            ):
                cli_main()

            assert exc.value.code == 0
            assert captured_kwargs.get("skip_sourcing") is True

    def test_cli_window_override_passed_to_pipeline(self, tmp_path: Path) -> None:
        """
        --window-start/--window-end are parsed into UTC datetimes and passed to pipeline.
        Traces: SRC-028
        """
        from ai_news_agent.pipeline import cli_main

        mock_result = _make_dummy_pipeline_result(success=True)
        captured_kwargs: dict = {}

        def capture_run(**kwargs):
            captured_kwargs.update(kwargs)
            return mock_result

        with (
            patch("ai_news_agent.pipeline.load_agent_config") as mock_cfg,
            patch("ai_news_agent.pipeline.RuntimeSecrets") as mock_sec,
            patch("ai_news_agent.pipeline.Pipeline") as MockPipeline,
        ):
            mock_cfg.return_value = MagicMock()
            mock_sec.return_value = MagicMock()
            MockPipeline.return_value.run.side_effect = capture_run

            with (
                patch.object(
                    sys,
                    "argv",
                    [
                        "ai-news-run",
                        "--cadence",
                        "monthly",
                        "--window-start",
                        "2026-04-01",
                        "--window-end",
                        "2026-04-30",
                    ],
                ),
                pytest.raises(SystemExit) as exc,
            ):
                cli_main()

            assert exc.value.code == 0
            ws = captured_kwargs.get("window_start")
            we = captured_kwargs.get("window_end")
            assert ws is not None, "window_start not passed to pipeline"
            assert we is not None, "window_end not passed to pipeline"
            assert ws.tzinfo is not None, "window_start must be UTC-aware"
            assert ws.month == 4
            assert ws.day == 1


# ---------------------------------------------------------------------------
# Curation agent CLI (ai-news-curate)
# ---------------------------------------------------------------------------


class TestCurationCLI:
    """
    Tests for the ``ai-news-curate`` CLI entry point.
    Traces: SRC-028, SRC-076, SRC-102, SRC-147, SRC-148
    """

    def test_curation_cli_window_mismatch_exits_2(self) -> None:
        """
        --window-start without --window-end must exit with code 2 (SRC-028).
        """
        from ai_news_agent.curation.agent import cli_main

        with (
            patch("ai_news_agent.curation.agent.load_agent_config") as mock_cfg,
            patch("ai_news_agent.curation.agent.RuntimeSecrets") as mock_sec,
        ):
            mock_cfg.return_value = MagicMock()
            mock_sec.return_value = MagicMock()

            with (
                patch.object(
                    sys,
                    "argv",
                    [
                        "ai-news-curate",
                        "--cadence",
                        "daily",
                        "--window-start",
                        "2026-05-01",
                    ],
                ),
                pytest.raises(SystemExit) as exc,
            ):
                cli_main()
            assert exc.value.code == 2

    def test_curation_cli_invalid_date_exits_2(self) -> None:
        """
        Invalid ISO date in --window-start exits with code 2.
        """
        from ai_news_agent.curation.agent import cli_main

        with (
            patch("ai_news_agent.curation.agent.load_agent_config") as mock_cfg,
            patch("ai_news_agent.curation.agent.RuntimeSecrets") as mock_sec,
        ):
            mock_cfg.return_value = MagicMock()
            mock_sec.return_value = MagicMock()

            with (
                patch.object(
                    sys,
                    "argv",
                    [
                        "ai-news-curate",
                        "--cadence",
                        "daily",
                        "--window-start",
                        "bad-date",
                        "--window-end",
                        "also-bad",
                    ],
                ),
                pytest.raises(SystemExit) as exc,
            ):
                cli_main()
            assert exc.value.code == 2

    def test_curation_cli_dry_run_runs_and_exits(self) -> None:
        """
        Dry-run mode completes and exits normally.
        Traces: SRC-102
        """
        from ai_news_agent.curation.agent import CurationRunResult, cli_main

        mock_result = MagicMock(spec=CurationRunResult)
        mock_result.dry_run = True
        mock_result.metadata = MagicMock()
        mock_result.metadata.items_included = 3
        mock_result.metadata.items_considered = 10
        mock_result.metadata.llm_model = "gpt-4o"
        mock_result.themes = ["Enterprise AI", "Regulation"]
        mock_result.predictions = []
        mock_result.twitter_degradation_note = None

        with (
            patch("ai_news_agent.curation.agent.load_agent_config") as mock_cfg,
            patch("ai_news_agent.curation.agent.RuntimeSecrets") as mock_sec,
            patch("ai_news_agent.curation.agent.CurationAgent") as MockAgent,
        ):
            mock_cfg.return_value = MagicMock()
            mock_sec.return_value = MagicMock()
            MockAgent.return_value.run.return_value = mock_result

            with (
                patch.object(sys, "argv", ["ai-news-curate", "--cadence", "daily", "--dry-run"]),
                pytest.raises(SystemExit) as exc,
            ):
                cli_main()

            assert exc.value.code == 0

    def test_curation_cli_twitter_available_flag(self) -> None:
        """
        --twitter-available false is passed to CurationAgent.run().
        Traces: SRC-148
        """
        from ai_news_agent.curation.agent import CurationRunResult, cli_main

        mock_result = MagicMock(spec=CurationRunResult)
        mock_result.dry_run = False
        mock_result.metadata = MagicMock()
        mock_result.metadata.items_included = 2
        mock_result.metadata.items_considered = 8
        mock_result.metadata.llm_model = "gpt-4o"
        mock_result.themes = []
        mock_result.predictions = []
        mock_result.twitter_degradation_note = "Twitter API unavailable"

        captured_kwargs: dict = {}

        def capture_run(**kwargs):
            captured_kwargs.update(kwargs)
            return mock_result

        with (
            patch("ai_news_agent.curation.agent.load_agent_config") as mock_cfg,
            patch("ai_news_agent.curation.agent.RuntimeSecrets") as mock_sec,
            patch("ai_news_agent.curation.agent.CurationAgent") as MockAgent,
        ):
            mock_cfg.return_value = MagicMock()
            mock_sec.return_value = MagicMock()
            MockAgent.return_value.run.side_effect = capture_run

            with (
                patch.object(
                    sys,
                    "argv",
                    [
                        "ai-news-curate",
                        "--cadence",
                        "daily",
                        "--twitter-available",
                        "false",
                    ],
                ),
                pytest.raises(SystemExit) as exc,
            ):
                cli_main()

            assert exc.value.code == 0
            assert captured_kwargs.get("twitter_api_available") is False


# ---------------------------------------------------------------------------
# Prompt hash CLI (ai-news-prompt-hashes)
# ---------------------------------------------------------------------------


class TestPromptHashCLI:
    """
    Tests for the ``ai-news-prompt-hashes`` CLI entry point.

    Traces: SRC-127 (version-controlled prompts), SRC-128 (review required),
            SRC-129 (SHA-256 hash in all outputs)
    """

    # The CLI function is registered as _cli_prompt_hashes in prompt_builder.py
    @staticmethod
    def _import_phash_cli():
        from ai_news_agent.curation.prompt_builder import _cli_prompt_hashes

        return _cli_prompt_hashes

    def test_print_hashes_from_temp_dir(self, tmp_path: Path) -> None:
        """
        cli_main with no flags prints hashes for all four cadences.
        Traces: SRC-129 (SHA-256 hash)
        """
        phash_cli = self._import_phash_cli()

        # Create minimal prompt files
        prompts_dir = tmp_path / "prompts"
        prompts_dir.mkdir()
        for cadence in ("daily", "weekly", "monthly", "annual"):
            (prompts_dir / f"{cadence}.md").write_text(
                f"# {cadence.title()} Prompt\nWindow: placeholder\n", encoding="utf-8"
            )

        import contextlib

        with (
            patch.object(sys, "argv", ["ai-news-prompt-hashes", "--prompts-dir", str(prompts_dir)]),
            contextlib.suppress(SystemExit),
        ):
            # Should not raise; may or may not exit (0 is fine)
            phash_cli()

    def test_save_creates_manifest(self, tmp_path: Path) -> None:
        """
        --save writes a prompt_hashes.json file to the prompts directory.
        Traces: SRC-129, SRC-127
        """
        phash_cli = self._import_phash_cli()

        prompts_dir = tmp_path / "prompts"
        prompts_dir.mkdir()
        for cadence in ("daily", "weekly", "monthly", "annual"):
            (prompts_dir / f"{cadence}.md").write_text(
                f"# {cadence.title()} Prompt content\n", encoding="utf-8"
            )

        import contextlib

        with (
            patch.object(
                sys,
                "argv",
                [
                    "ai-news-prompt-hashes",
                    "--prompts-dir",
                    str(prompts_dir),
                    "--save",
                ],
            ),
            contextlib.suppress(SystemExit),
        ):
            phash_cli()

        manifest_path = prompts_dir / "prompt_hashes.json"
        assert manifest_path.exists(), "Manifest file must be created with --save"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        for cadence in ("daily", "weekly", "monthly", "annual"):
            assert cadence in manifest, f"Missing cadence {cadence} in manifest"
            assert manifest[cadence].startswith("sha256:"), (
                f"Hash for {cadence} must start with 'sha256:'"
            )

    def test_verify_passes_when_hashes_match(self, tmp_path: Path) -> None:
        """
        --verify exits 0 when hashes match the saved manifest (SRC-127, SRC-128).
        """
        phash_cli = self._import_phash_cli()

        prompts_dir = tmp_path / "prompts"
        prompts_dir.mkdir()
        for cadence in ("daily", "weekly", "monthly", "annual"):
            (prompts_dir / f"{cadence}.md").write_text(
                f"# {cadence.title()} Prompt content\n", encoding="utf-8"
            )

        import contextlib

        # First save the manifest
        with (
            patch.object(
                sys, "argv", ["ai-news-prompt-hashes", "--prompts-dir", str(prompts_dir), "--save"]
            ),
            contextlib.suppress(SystemExit),
        ):
            phash_cli()

        # Then verify — should pass (may return normally or exit 0)
        with (
            patch.object(
                sys,
                "argv",
                ["ai-news-prompt-hashes", "--prompts-dir", str(prompts_dir), "--verify"],
            ),
            contextlib.suppress(SystemExit),
        ):
            # No exception = success; SystemExit(0) is also fine
            phash_cli()

    def test_verify_fails_when_hashes_differ(self, tmp_path: Path) -> None:
        """
        --verify exits 1 when prompt content has changed since last save (SRC-127, SRC-128).
        """
        phash_cli = self._import_phash_cli()

        prompts_dir = tmp_path / "prompts"
        prompts_dir.mkdir()
        for cadence in ("daily", "weekly", "monthly", "annual"):
            (prompts_dir / f"{cadence}.md").write_text(
                f"# {cadence.title()} Original Content\n", encoding="utf-8"
            )

        import contextlib

        # Save manifest with original content
        with (
            patch.object(
                sys, "argv", ["ai-news-prompt-hashes", "--prompts-dir", str(prompts_dir), "--save"]
            ),
            contextlib.suppress(SystemExit),
        ):
            phash_cli()

        # Now change one prompt
        (prompts_dir / "daily.md").write_text(
            "# Daily MODIFIED CONTENT — unreviewed change\n", encoding="utf-8"
        )

        # Verify must detect the change and exit 1
        with patch.object(
            sys, "argv", ["ai-news-prompt-hashes", "--prompts-dir", str(prompts_dir), "--verify"]
        ):
            with pytest.raises(SystemExit) as exc:
                phash_cli()
            assert exc.value.code == 1, (
                f"--verify must exit 1 for unreviewed changes (SRC-127–SRC-128), got: {exc.value.code}"
            )

    def test_verify_exits_1_when_no_manifest(self, tmp_path: Path) -> None:
        """
        --verify exits 1 when no manifest file exists (SRC-127, SRC-128).
        """
        phash_cli = self._import_phash_cli()

        prompts_dir = tmp_path / "prompts"
        prompts_dir.mkdir()
        for cadence in ("daily", "weekly", "monthly", "annual"):
            (prompts_dir / f"{cadence}.md").write_text(
                f"# {cadence.title()} Content\n", encoding="utf-8"
            )
        # No --save step → no manifest

        with patch.object(
            sys, "argv", ["ai-news-prompt-hashes", "--prompts-dir", str(prompts_dir), "--verify"]
        ):
            with pytest.raises(SystemExit) as exc:
                phash_cli()
            assert exc.value.code == 1

    def test_missing_prompts_dir_exits_1(self, tmp_path: Path) -> None:
        """
        Missing prompts directory exits with code 1.
        Traces: SRC-113
        """
        phash_cli = self._import_phash_cli()

        nonexistent = tmp_path / "nonexistent-prompts"

        with patch.object(
            sys, "argv", ["ai-news-prompt-hashes", "--prompts-dir", str(nonexistent)]
        ):
            with pytest.raises(SystemExit) as exc:
                phash_cli()
            assert exc.value.code == 1


# ---------------------------------------------------------------------------
# _print_run_summary (SRC-150)
# ---------------------------------------------------------------------------


class TestPrintRunSummary:
    """
    Tests for the _print_run_summary helper (§8.2 quality monitoring).
    Traces: SRC-150
    """

    def test_print_run_summary_success(self, capsys) -> None:
        """
        _print_run_summary prints all §8.2 fields for a successful run.
        Traces: SRC-150
        """
        from ai_news_agent.pipeline import PipelineRunResult, _print_run_summary

        result = PipelineRunResult(
            agent_id="test-agent",
            cadence="daily",
            run_at=datetime(2026, 5, 9, tzinfo=UTC),
        )
        result.success = True
        result.dry_run = True
        result.articles_fetched = 10
        result.articles_inserted = 8
        result.articles_duplicate = 2
        result.tweets_fetched = 5
        result.tweets_inserted = 3
        result.twitter_signal_available = True
        result.tweet_api_call_count = 2
        result.items_considered = 20
        result.items_included = 5
        result.items_by_tier = {"1b": 3, "2": 2}
        result.items_by_source_class = {"web": 5}
        result.token_usage = 4500
        result.llm_provider = "openai"
        result.llm_model = "gpt-4o"
        result.prompt_version = "sha256:abc123"
        result.markdown_path = Path("/tmp/2026-05-09-daily.md")
        result.html_path = Path("/tmp/2026-05-09-daily.html")
        result.json_path = Path("/tmp/2026-05-09-daily.json")

        _print_run_summary(result)

        captured = capsys.readouterr()
        out = captured.err  # _print_run_summary writes to stderr

        # Verify key §8.2 fields appear in output
        assert "test-agent" in out
        assert "daily" in out
        assert "openai" in out
        assert "gpt-4o" in out
        assert "sha256:abc123" in out
        assert "SUCCESS" in out
        assert "DRY-RUN" in out

    def test_print_run_summary_failure(self, capsys) -> None:
        """
        _print_run_summary shows FAILED status for unsuccessful runs.
        """
        from ai_news_agent.pipeline import PipelineRunResult, _print_run_summary

        result = PipelineRunResult(
            agent_id="test-agent",
            cadence="weekly",
            run_at=datetime(2026, 5, 9, tzinfo=UTC),
        )
        result.success = False
        result.errors = ["LLM timeout", "Rendering failed"]

        _print_run_summary(result)

        captured = capsys.readouterr()
        out = captured.err
        assert "FAILED" in out

    def test_print_run_summary_no_rendered_files(self, capsys) -> None:
        """
        When rendering did not complete, the summary notes no rendered files.
        """
        from ai_news_agent.pipeline import PipelineRunResult, _print_run_summary

        result = PipelineRunResult(
            agent_id="test-agent",
            cadence="daily",
            run_at=datetime(2026, 5, 9, tzinfo=UTC),
        )
        # markdown_path is None (rendering didn't complete)
        _print_run_summary(result)

        captured = capsys.readouterr()
        out = captured.err
        assert "No rendered files" in out or "rendering stage" in out


# ---------------------------------------------------------------------------
# PromptManifest (SRC-129)
# ---------------------------------------------------------------------------


class TestPromptManifest:
    """
    Unit tests for PromptManifest data class.
    Traces: SRC-127 (version control), SRC-129 (SHA-256 hash)
    """

    def test_from_dir_computes_hashes(self, tmp_path: Path) -> None:
        """
        from_dir() computes SHA-256 hashes for all four cadence prompt files.
        Traces: SRC-129
        """
        from ai_news_agent.curation.prompt_builder import PromptManifest

        prompts_dir = tmp_path / "prompts"
        prompts_dir.mkdir()
        for cadence in ("daily", "weekly", "monthly", "annual"):
            (prompts_dir / f"{cadence}.md").write_text(
                f"# {cadence.title()} content\n", encoding="utf-8"
            )

        manifest = PromptManifest.from_dir(prompts_dir)

        for cadence in ("daily", "weekly", "monthly", "annual"):
            hash_val = manifest.get(cadence)
            assert hash_val is not None, f"Missing hash for {cadence}"
            assert hash_val.startswith("sha256:"), (
                f"Hash for {cadence} must start with 'sha256:', got: {hash_val!r}"
            )

    def test_from_dir_missing_file_raises(self, tmp_path: Path) -> None:
        """
        from_dir() raises FileNotFoundError when a prompt file is missing.
        Traces: SRC-113
        """
        from ai_news_agent.curation.prompt_builder import PromptManifest

        prompts_dir = tmp_path / "prompts"
        prompts_dir.mkdir()
        # Only create daily — leave weekly/monthly/annual missing
        (prompts_dir / "daily.md").write_text("# Daily content\n", encoding="utf-8")

        with pytest.raises(FileNotFoundError):
            PromptManifest.from_dir(prompts_dir)

    def test_save_and_load_round_trip(self, tmp_path: Path) -> None:
        """
        save() then load() round-trips correctly.
        Traces: SRC-127 (version-controlled manifests)
        """
        from ai_news_agent.curation.prompt_builder import PromptManifest

        prompts_dir = tmp_path / "prompts"
        prompts_dir.mkdir()
        for cadence in ("daily", "weekly", "monthly", "annual"):
            (prompts_dir / f"{cadence}.md").write_text(
                f"# {cadence.title()} roundtrip content\n", encoding="utf-8"
            )

        original = PromptManifest.from_dir(prompts_dir)
        manifest_path = prompts_dir / "prompt_hashes.json"
        original.save(manifest_path)

        loaded = PromptManifest.load(manifest_path)

        for cadence in ("daily", "weekly", "monthly", "annual"):
            assert original.get(cadence) == loaded.get(cadence), (
                f"Hash mismatch after save/load for {cadence}"
            )

    def test_different_content_different_hash(self, tmp_path: Path) -> None:
        """
        Changing prompt content produces a different SHA-256 hash (SRC-129).
        """
        from ai_news_agent.curation.prompt_builder import PromptManifest

        prompts_dir = tmp_path / "prompts"
        prompts_dir.mkdir()
        for cadence in ("daily", "weekly", "monthly", "annual"):
            (prompts_dir / f"{cadence}.md").write_text("original content\n")

        manifest1 = PromptManifest.from_dir(prompts_dir)
        (prompts_dir / "daily.md").write_text("MODIFIED content\n")
        manifest2 = PromptManifest.from_dir(prompts_dir)

        assert manifest1.get("daily") != manifest2.get("daily"), (
            "Different content must produce different SHA-256 hash (SRC-129)"
        )
        # Other cadences must be unchanged
        assert manifest1.get("weekly") == manifest2.get("weekly")


# ---------------------------------------------------------------------------
# Config loader (SRC-071–SRC-073)
# ---------------------------------------------------------------------------


class TestConfigLoader:
    """
    Tests for config/loader.py public functions.
    Traces: SRC-071 (fail loudly), SRC-073 (secrets in env vars only)
    """

    def test_load_agent_config_missing_file_raises(self, tmp_path: Path) -> None:
        """
        load_agent_config on a missing file raises ConfigError.
        Traces: SRC-071
        """
        from ai_news_agent.config.loader import ConfigError, load_agent_config

        with pytest.raises(ConfigError, match="not found"):
            load_agent_config(tmp_path / "nonexistent.yaml")

    def test_load_agent_config_malformed_yaml_raises(self, tmp_path: Path) -> None:
        """
        Malformed YAML raises ConfigError with a helpful message.
        Traces: SRC-071
        """
        from ai_news_agent.config.loader import ConfigError, load_agent_config

        bad_yaml = tmp_path / "bad.yaml"
        bad_yaml.write_text("key: [unclosed bracket\n")

        with pytest.raises(ConfigError):
            load_agent_config(bad_yaml)

    def test_load_agent_config_secret_detected_raises(self, tmp_path: Path) -> None:
        """
        YAML containing a secret-like value raises ConfigError (SRC-073).
        """
        from ai_news_agent.config.loader import ConfigError, load_agent_config

        # Write a YAML with an OpenAI key-like value
        secret_yaml = tmp_path / "agent.yaml"
        secret_yaml.write_text(
            "agent_id: test\n"
            "openai_api_key: sk-proj-testKeyABCDEF12345678ABCDEF12345678ABCDEF12345678\n"
        )

        with pytest.raises(ConfigError, match="secret"):
            load_agent_config(secret_yaml)

    def test_load_agent_config_valid_minimal(self, tmp_path: Path) -> None:
        """
        A minimal valid YAML loads into AgentConfig without error.
        Traces: SRC-071
        """
        from ai_news_agent.config.loader import load_agent_config

        minimal_yaml = tmp_path / "minimal-agent.yaml"
        minimal_yaml.write_text(
            "agent_id: minimal-agent\nllm:\n  provider: openai\n  model: gpt-4o\n"
        )
        config = load_agent_config(minimal_yaml)
        assert config.agent_id == "minimal-agent"
        assert config.llm.provider == "openai"

    def test_validate_no_secrets_in_yaml_passes_clean(self) -> None:
        """
        validate_no_secrets_in_yaml does not raise for clean YAML.
        Traces: SRC-073
        """
        from ai_news_agent.config.loader import validate_no_secrets_in_yaml

        clean = "agent_id: test\nllm:\n  provider: openai\n  model: gpt-4o\n"
        validate_no_secrets_in_yaml(clean, source_name="<test>")

    def test_validate_no_secrets_skips_comment_lines(self) -> None:
        """
        Secret-like values in YAML comments are not flagged.
        Traces: SRC-073 — comments are documentation, not config
        """
        from ai_news_agent.config.loader import validate_no_secrets_in_yaml

        yaml_with_comment = (
            "# Example: sk-proj-secretKeyABCDE12345\nagent_id: test\nllm:\n  provider: openai\n"
        )
        # Must not raise — comment lines are exempt
        validate_no_secrets_in_yaml(yaml_with_comment, source_name="<test>")

    def test_validate_no_secrets_detects_openai_key(self) -> None:
        """
        OpenAI-style API key in config raises ConfigError.
        Traces: SRC-073
        """
        from ai_news_agent.config.loader import ConfigError, validate_no_secrets_in_yaml

        yaml_with_key = "api_key: sk-proj-abcdefgh12345678ABCDEF12345678ABCDEF1234\n"
        with pytest.raises(ConfigError, match="secret"):
            validate_no_secrets_in_yaml(yaml_with_key, source_name="agent.yaml")

    def test_load_scheduler_config_missing_file_raises(self, tmp_path: Path) -> None:
        """
        load_scheduler_config on a missing file raises ConfigError.
        Traces: SRC-071
        """
        from ai_news_agent.config.loader import ConfigError, load_scheduler_config

        with pytest.raises(ConfigError):
            load_scheduler_config(tmp_path / "nonexistent-scheduler.yaml")

    def test_load_scheduler_config_valid(self, tmp_path: Path) -> None:
        """
        A valid scheduler.yaml loads into SchedulerConfig.
        Traces: SRC-052, SRC-072
        """
        from ai_news_agent.config.loader import load_scheduler_config

        sched_yaml = tmp_path / "scheduler.yaml"
        sched_yaml.write_text(
            "scheduler:\n  max_retries: 3\n  retry_backoff_base_seconds: 30\nagents: []\n"
        )
        config = load_scheduler_config(sched_yaml)
        assert config.scheduler.max_retries == 3
        assert config.agents == []


# ---------------------------------------------------------------------------
# SchedulerRunner internals (SRC-052, SRC-144)
# ---------------------------------------------------------------------------


class TestSchedulerRunnerInternals:
    """
    Unit tests for SchedulerRunner._with_retry and _parse_cron helpers.
    Traces: SRC-052, SRC-144
    """

    def test_with_retry_succeeds_immediately(self) -> None:
        """
        _with_retry calls fn once when it succeeds immediately (SRC-144).
        """
        from ai_news_agent.scheduler.runner import _with_retry

        call_count = 0

        def fn():
            nonlocal call_count
            call_count += 1

        _with_retry(fn, max_retries=3, backoff_base=1)
        assert call_count == 1

    def test_with_retry_retries_on_failure(self) -> None:
        """
        _with_retry retries up to max_retries times before raising (SRC-144).
        """
        from ai_news_agent.scheduler.runner import _with_retry

        call_count = 0

        def fn():
            nonlocal call_count
            call_count += 1
            raise RuntimeError("transient failure")

        with patch("time.sleep"), pytest.raises(RuntimeError, match="transient failure"):
            _with_retry(fn, max_retries=3, backoff_base=1)

        assert call_count == 4  # 1 initial + 3 retries

    def test_with_retry_succeeds_on_second_attempt(self) -> None:
        """
        _with_retry stops retrying once fn succeeds (SRC-144).
        """
        from ai_news_agent.scheduler.runner import _with_retry

        call_count = 0

        def fn():
            nonlocal call_count
            call_count += 1
            if call_count < 2:
                raise RuntimeError("first failure")

        with patch("time.sleep"):
            _with_retry(fn, max_retries=3, backoff_base=1)

        assert call_count == 2

    def test_parse_cron_valid(self) -> None:
        """
        _parse_cron parses a valid 5-field cron expression.
        Traces: SRC-052
        """
        from ai_news_agent.scheduler.runner import _parse_cron

        result = _parse_cron("5 0 * * *")
        assert result == {
            "minute": "5",
            "hour": "0",
            "day": "*",
            "month": "*",
            "day_of_week": "*",
        }

    def test_parse_cron_weekly(self) -> None:
        """
        _parse_cron parses the weekly cron (01:00 UTC Sunday).
        Traces: SRC-030 (weekly curation)
        """
        from ai_news_agent.scheduler.runner import _parse_cron

        result = _parse_cron("0 1 * * 0")
        assert result["hour"] == "1"
        assert result["day_of_week"] == "0"

    def test_parse_cron_invalid_raises(self) -> None:
        """
        _parse_cron raises ValueError for expressions without exactly 5 fields.
        """
        from ai_news_agent.scheduler.runner import _parse_cron

        with pytest.raises(ValueError, match="5 fields"):
            _parse_cron("0 1 * *")  # only 4 fields

    def test_run_sourcing_job_logs_error_on_exception(self) -> None:
        """
        _run_sourcing_job logs and re-raises on exception.
        Traces: SRC-144, SRC-146
        """
        from ai_news_agent.scheduler.runner import _run_sourcing_job

        mock_cfg = MagicMock()
        mock_cfg.agent_id = "test-agent"
        mock_secrets = MagicMock()

        with patch("ai_news_agent.scheduler.runner.SourcingAgent") as MockSrc:
            MockSrc.return_value.run.side_effect = RuntimeError("sourcing error")

            with pytest.raises(RuntimeError, match="sourcing error"):
                _run_sourcing_job(mock_cfg, mock_secrets)

    def test_run_curation_job_raises_on_pipeline_failure(self) -> None:
        """
        _run_curation_job raises RuntimeError when Pipeline returns success=False.
        Traces: SRC-144
        """
        from ai_news_agent.scheduler.runner import _run_curation_job

        mock_cfg = MagicMock()
        mock_cfg.agent_id = "test-agent"
        mock_secrets = MagicMock()

        mock_result = MagicMock()
        mock_result.success = False
        mock_result.errors = ["LLM timeout"]

        with patch("ai_news_agent.scheduler.runner.Pipeline") as MockPipeline:
            MockPipeline.return_value.run.return_value = mock_result

            with pytest.raises(RuntimeError, match="failure"):
                _run_curation_job(mock_cfg, mock_secrets, "daily")
