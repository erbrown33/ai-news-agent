"""
rendering/agent.py — RenderingAgent orchestrator and CLI entry point.

The Rendering Agent is the final stage of the pipeline.  For every curation
run it produces three export files and updates the DigestRecord in the store
with the rendered file paths for portal download links.

Key responsibilities:
- Emit Markdown, HTML, and JSON to the configured output directory (SRC-004).
- Enforce that every item has a valid ``http(s)://`` URL at the renderer level —
  items without a URL are dropped, not truncated (SRC-049, SRC-141).
- Write date-stamped filenames that make re-runs idempotent (SRC-145).
- Update DigestRecord.md_path / html_path / json_path in the store after
  rendering (SRC-145 — portal download links stay in sync).
- Emit a structured ``rendering_complete`` log event (SRC-150).

File path convention (SRC-145, SRC-140):
    ``{output_dir}/{YYYY-MM-DD}-{cadence}.md``
    ``{output_dir}/{YYYY-MM-DD}-{cadence}.html``
    ``{output_dir}/{YYYY-MM-DD}-{cadence}.json``

where ``output_dir`` resolves to ``outputs/{agent_id}/`` from the agent config.
The ``agent_id`` is embedded in the **directory path** rather than the filename
so that a future thin distribution layer can ingest the directory tree without
parsing filenames (SRC-140).

Traces: SRC-004 (structured export files: MD/HTML/JSON),
        SRC-049 (URL required — items without valid URL dropped),
        SRC-135–SRC-141 (rendered export section),
        SRC-141 (URL enforcement — non-negotiable at renderer),
        SRC-145 (idempotent date-stamped filenames),
        SRC-150 (monitoring fields in log event)
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, replace
from datetime import date, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

import structlog

from ai_news_agent.rendering.html_renderer import HtmlRenderer
from ai_news_agent.rendering.json_renderer import JsonRenderer
from ai_news_agent.rendering.markdown_renderer import MarkdownRenderer

if TYPE_CHECKING:
    from ai_news_agent.storage.base import AbstractArticleStore

# Imported at function-call time inside _load_curation_result to avoid
# circular imports at module load.  CurationRunResult is used as a return
# type annotation; with ``from __future__ import annotations`` this is fine.
from ai_news_agent.curation.agent import CurationRunResult  # noqa: TC002

log = structlog.get_logger(__name__)


@dataclass
class RenderingResult:
    """
    Paths of the three rendered output files for a curation run.

    Traces: SRC-004 (MD/HTML/JSON), SRC-145 (date-stamped filenames),
            SRC-141 (items_dropped_no_url — auditable URL enforcement count)
    """

    markdown_path: Path
    html_path: Path
    json_path: Path
    items_rendered: int
    items_dropped_no_url: int  # SRC-141 — items dropped for missing/invalid URL


class RenderingAgent:
    """
    Rendering Agent — writes three export formats for every curation run.

    Output formats (SRC-004, SRC-136):
    - Markdown (``.md``) — Slack/Teams paste-ready (SRC-138)
    - HTML (``.html``) — Email-client paste-ready (SRC-137)
    - JSON (``.json``) — Machine-readable archive (SRC-140)

    File path pattern: ``{output_dir}/{YYYY-MM-DD}-{cadence}.{ext}`` (SRC-145)

    URL enforcement (SRC-141, SRC-049):
    - Every renderer independently validates that each item URL starts with
      ``http://`` or ``https://``.
    - Items without a valid URL are **dropped**, not truncated.
    - This is the **second** enforcement layer (after Scorer in curation agent).
    - It is non-negotiable — every item in every output file must have a URL.

    Idempotency (SRC-145):
    - Date-stamped filenames mean re-runs cleanly overwrite previous outputs.
    - The same curation result always produces the same file content.

    DigestRecord path update:
    - After writing files, ``update_digest_paths()`` populates
      ``DigestRecord.md_path``, ``.html_path``, ``.json_path`` so the portal
      can serve download links without scanning the filesystem.

    Traces: SRC-004, SRC-049, SRC-135–SRC-141, SRC-145, SRC-150
    """

    def __init__(self, output_dir: str | Path) -> None:
        """
        Args:
            output_dir: Base directory for output files.
                        Resolved from ``config.output_dir`` in the agent config.
                        Pattern: ``{output_dir}/{YYYY-MM-DD}-{cadence}.{ext}``
        """
        self._output_dir = Path(output_dir)
        self._md_renderer = MarkdownRenderer()
        self._html_renderer = HtmlRenderer()
        self._json_renderer = JsonRenderer()

    def render(self, result: CurationRunResult) -> RenderingResult:
        """
        Render all three formats and write them to the output directory.

        Steps:
        1. Ensure the output directory exists (creates parents as needed).
        2. Render Markdown, HTML, JSON (each renderer independently enforces
           URL validity — SRC-141).
        3. Write each file with the date-stamped name (SRC-145).
        4. Count items_rendered and items_dropped_no_url for audit trail.
        5. Emit ``rendering_complete`` structured log (SRC-150).

        Args:
            result: :class:`~ai_news_agent.curation.agent.CurationRunResult`
                    from the Curation Agent.

        Returns:
            :class:`RenderingResult` with paths to the three written files
            and URL-drop count.

        Traces: SRC-004, SRC-049, SRC-135–SRC-141, SRC-145, SRC-150
        """
        self._output_dir.mkdir(parents=True, exist_ok=True)

        meta = result.metadata
        initial_count = len(result.items)

        # Render all three formats
        # Each renderer applies its own URL validation (SRC-141) so content is
        # consistent.  We count once here for the monitoring log.
        md_content = self._md_renderer.render(result)
        html_content = self._html_renderer.render(result)
        json_content = self._json_renderer.render(result)

        # Build output paths (SRC-145 — date-stamped, idempotent)
        md_path = self._output_dir / MarkdownRenderer.filename(meta)
        html_path = self._output_dir / HtmlRenderer.filename(meta)
        json_path = self._output_dir / JsonRenderer.filename(meta)

        md_path.write_text(md_content, encoding="utf-8")
        html_path.write_text(html_content, encoding="utf-8")
        json_path.write_text(json_content, encoding="utf-8")

        # Count items with a valid http(s) URL — shared logic with renderers
        items_with_url = sum(
            1
            for item in result.items
            if item.url
            and (
                item.url.strip().lower().startswith("http://")
                or item.url.strip().lower().startswith("https://")
            )
        )
        items_dropped = initial_count - items_with_url

        log.info(
            "rendering_complete",
            agent_id=meta.agent_id,
            cadence=meta.cadence,
            items_rendered=items_with_url,
            items_dropped_no_url=items_dropped,
            md_path=str(md_path),
            html_path=str(html_path),
            json_path=str(json_path),
        )

        return RenderingResult(
            markdown_path=md_path,
            html_path=html_path,
            json_path=json_path,
            items_rendered=items_with_url,
            items_dropped_no_url=items_dropped,
        )

    def render_and_update_store(
        self,
        result: CurationRunResult,
        store: AbstractArticleStore,
    ) -> RenderingResult:
        """
        Render all three formats, write files, and update the DigestRecord in
        the store with the rendered file paths (for portal download links).

        This is the preferred method when the store is available (scheduled
        pipeline runs).  ``render()`` is used when no store context is present
        (e.g. dry-run mode, CI smoke tests).

        The DigestRecord is fetched by ``(agent_id, cadence, run_date)`` and
        updated with relative paths from ``output_dir`` (SRC-145).

        Args:
            result: :class:`~ai_news_agent.curation.agent.CurationRunResult`.
            store:  :class:`~ai_news_agent.storage.base.AbstractArticleStore`
                    used to upsert the updated :class:`DigestRecord`.

        Returns:
            :class:`RenderingResult` (same as ``render()``).

        Traces: SRC-145 (DigestRecord paths → portal download links)
        """
        rendering_result = self.render(result)
        self._update_digest_paths(result, rendering_result, store)
        return rendering_result

    def render_dry_run(self, result: CurationRunResult) -> RenderingResult:
        """
        Render all three formats to a temporary directory (no permanent writes).

        Used in CI smoke tests and manual validation (SRC-102 — dry-run mode).

        Verifies:
        - All three formats render without exception.
        - The return value is a valid :class:`RenderingResult`.

        Returns a :class:`RenderingResult` with temporary paths (cleaned up
        after this call returns).

        Traces: SRC-102 (smoke test dry-run mode)
        """
        import tempfile

        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_agent = RenderingAgent(output_dir=tmpdir)
            return tmp_agent.render(result)

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _update_digest_paths(
        self,
        result: CurationRunResult,
        rendering_result: RenderingResult,
        store: AbstractArticleStore,
    ) -> None:
        """
        Fetch the DigestRecord for this run and upsert it with rendered paths.

        The paths are stored as strings relative to ``output_dir`` so they
        remain valid even if the absolute filesystem path changes (e.g. in a
        container where the output volume is remounted at a different prefix).

        If no DigestRecord exists for this run (e.g. dry-run curation),
        this method is a no-op.

        Traces: SRC-145 (portal lists available digests via DigestRecord)
        """
        meta = result.metadata
        existing = store.get_digest(
            agent_id=meta.agent_id,
            cadence=meta.cadence,
            run_date=meta.run_date,
        )
        if existing is None:
            log.debug(
                "rendering_no_digest_record_to_update",
                agent_id=meta.agent_id,
                cadence=meta.cadence,
                run_date=str(meta.run_date),
            )
            return

        updated = replace(
            existing,
            md_path=str(rendering_result.markdown_path),
            html_path=str(rendering_result.html_path),
            json_path=str(rendering_result.json_path),
        )
        store.upsert_digest(updated)
        log.debug(
            "rendering_digest_paths_updated",
            agent_id=meta.agent_id,
            cadence=meta.cadence,
            run_date=str(meta.run_date),
        )


# ---------------------------------------------------------------------------
# CurationRunResult deserialiser — reconstructs the dataclass from JSON
# ---------------------------------------------------------------------------


def _load_curation_result(path: Path) -> CurationRunResult:
    """
    Load a :class:`CurationRunResult` that was serialised to JSON by the
    curation agent (``ai-news-curate --output-json <file>``).

    The serialised format is the same schema produced by :class:`JsonRenderer`
    with the additional top-level key ``"curation_result"`` carrying the
    complete result fields needed for re-rendering.

    Falls back to constructing a minimal ``CurationRunResult`` from the
    :class:`JsonRenderer` output schema if the ``"curation_result"`` key is
    absent (useful when rendering from an existing JSON digest file).

    Args:
        path: Path to the JSON file on disk.

    Returns:
        A :class:`~ai_news_agent.curation.agent.CurationRunResult` ready for
        rendering.

    Raises:
        SystemExit: When the file is missing, unreadable, or structurally
                    invalid in a way that prevents rendering.

    Traces: SRC-076 (local dev invocation), SRC-102 (CLI smoke test mode),
            SRC-145 (re-runs idempotent — JSON input is the curation output)
    """
    from ai_news_agent.curation.agent import CurationRunResult
    from ai_news_agent.storage.models import CuratedItem, DigestMetadata

    try:
        raw = path.read_text(encoding="utf-8")
        data: dict[str, Any] = json.loads(raw)
    except FileNotFoundError:
        print(f"Error: input file not found: {path}", file=sys.stderr)
        sys.exit(2)
    except json.JSONDecodeError as exc:
        print(f"Error: invalid JSON in {path}: {exc}", file=sys.stderr)
        sys.exit(2)

    # ---- Build DigestMetadata from the ``metadata`` block ----------------
    meta_raw = data.get("metadata", {})
    try:
        run_date_raw = meta_raw.get("run_date", "1970-01-01")
        run_date = (
            date.fromisoformat(run_date_raw) if isinstance(run_date_raw, str) else run_date_raw
        )
        window_start_raw = meta_raw.get("window_start", "1970-01-01T00:00:00+00:00")
        window_end_raw = meta_raw.get("window_end", "1970-01-01T23:59:59+00:00")
        window_start = datetime.fromisoformat(window_start_raw)
        window_end = datetime.fromisoformat(window_end_raw)

        metadata = DigestMetadata(
            agent_id=meta_raw.get("agent_id", "unknown"),
            cadence=meta_raw.get("cadence", "daily"),
            run_date=run_date,
            window_start=window_start,
            window_end=window_end,
            prompt_version=meta_raw.get("prompt_version", "sha256:unknown"),
            llm_provider=meta_raw.get("llm_provider", "unknown"),
            llm_model=meta_raw.get("llm_model", "unknown"),
            items_considered=int(meta_raw.get("items_considered", 0)),
            items_included=int(meta_raw.get("items_included", 0)),
            items_by_tier=meta_raw.get("items_by_tier", {}),
            items_by_source_class=meta_raw.get("items_by_source_class", {}),
            twitter_signal_available=bool(meta_raw.get("twitter_signal_available", True)),
            tweet_api_call_count=int(meta_raw.get("tweet_api_call_count", 0)),
            token_usage=int(meta_raw.get("token_usage", 0)),
        )
    except (KeyError, ValueError, TypeError) as exc:
        print(f"Error: could not parse metadata block: {exc}", file=sys.stderr)
        sys.exit(2)

    # ---- Reconstruct CuratedItems from the ``items`` list ----------------
    items: list[CuratedItem] = []
    for raw_item in data.get("items", []):
        try:
            pub_date_str = raw_item.get("pub_date", "1970-01-01")
            pub_date = (
                date.fromisoformat(pub_date_str) if isinstance(pub_date_str, str) else pub_date_str
            )
            items.append(
                CuratedItem(
                    headline=raw_item.get("headline", ""),
                    source_name=raw_item.get("source_name", ""),
                    url=raw_item.get("url", ""),
                    pub_date=pub_date,
                    why_it_matters=raw_item.get("why_it_matters", ""),
                    impact_tags=raw_item.get("impact_tags", []),
                    tier=raw_item.get("tier", "unknown"),
                    cross_refs=raw_item.get("cross_refs", []),
                    twitter_handle=raw_item.get("twitter_handle"),
                    tweet_url=raw_item.get("tweet_url"),
                    prompt_version=raw_item.get("prompt_version", metadata.prompt_version),
                )
            )
        except (KeyError, ValueError, TypeError) as exc:
            log.warning("rendering_cli_item_parse_error", error=str(exc))

    diagnostics = None
    diag_raw = data.get("diagnostics")
    if isinstance(diag_raw, dict):
        from ai_news_agent.storage.models import CurationDiagnostics

        try:
            diagnostics = CurationDiagnostics(
                threshold=int(diag_raw.get("threshold", 0)),
                articles_in_store=int(diag_raw.get("articles_in_store", 0)),
                articles_in_window=int(diag_raw.get("articles_in_window", 0)),
                articles_in_window_by_tier=dict(diag_raw.get("articles_in_window_by_tier", {})),
                items_dropped_no_url=int(diag_raw.get("items_dropped_no_url", 0)),
                twitter_signal_available=bool(diag_raw.get("twitter_signal_available", True)),
                reasons=list(diag_raw.get("reasons", [])),
            )
        except (KeyError, ValueError, TypeError) as exc:
            log.warning("rendering_cli_diagnostics_parse_error", error=str(exc))

    return CurationRunResult(
        metadata=metadata,
        items=items,
        themes=data.get("themes", []),
        outlook=data.get("outlook", ""),
        predictions=data.get("predictions", []),
        twitter_degradation_note=data.get("twitter_degradation_note"),
        dry_run=False,
        diagnostics=diagnostics,
    )


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def cli_main() -> None:
    """
    Command-line entry point: ``ai-news-render``.

    Reads a :class:`CurationRunResult` serialised as JSON (the ``.json``
    output produced by a previous ``ai-news-curate`` run) and renders all
    three output formats (Markdown, HTML, JSON) to the configured output
    directory.

    Usage examples::

        # Re-render from an existing JSON digest
        ai-news-render --input outputs/default-agent/2026-05-10-daily.json \\
            --output-dir outputs/default-agent

        # Dry-run: render to temporary directory only (CI smoke test, SRC-102)
        ai-news-render --input outputs/default-agent/2026-05-10-daily.json \\
            --dry-run

    When called without ``--input``, the agent prints usage information and
    exits cleanly.  In the scheduled pipeline the Curation Agent hands the
    result directly in-memory to the Rendering Agent; the CLI is for
    standalone re-render and debugging.

    Traces: SRC-076 (local dev invocation), SRC-102 (dry-run smoke test),
            SRC-145 (date-stamped idempotent filenames)
    """
    parser = argparse.ArgumentParser(
        prog="ai-news-render",
        description=(
            "Render a curation result (JSON digest) to Markdown, HTML, and JSON. "
            "Reads an existing JSON digest file and re-renders all three formats."
        ),
    )
    parser.add_argument(
        "--input",
        default=None,
        metavar="JSON_FILE",
        help=(
            "Path to an existing JSON digest file produced by ai-news-curate. "
            "When omitted, prints usage and exits."
        ),
    )
    parser.add_argument(
        "--output-dir",
        default=None,
        help=(
            "Output directory for rendered files. "
            "Defaults to the directory containing the input file."
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "Render to a temporary directory only — no permanent writes. "
            "Used for CI smoke testing (SRC-102)."
        ),
    )
    args = parser.parse_args()

    if args.input is None:
        parser.print_help(sys.stderr)
        sys.exit(0)

    input_path = Path(args.input)
    if not input_path.exists():
        print(f"Error: input file not found: {input_path}", file=sys.stderr)
        sys.exit(2)

    # Determine output directory
    output_dir = input_path.parent if args.output_dir is None else Path(args.output_dir)

    log.info(
        "rendering_cli_invoked",
        input=str(input_path),
        output_dir=str(output_dir),
        dry_run=args.dry_run,
    )

    result = _load_curation_result(input_path)
    agent = RenderingAgent(output_dir=output_dir)

    if args.dry_run:
        rendering_result = agent.render_dry_run(result)
        print(
            f"Dry-run complete — "
            f"{rendering_result.items_rendered} items rendered, "
            f"{rendering_result.items_dropped_no_url} dropped (no URL). "
            f"No files written to disk. (SRC-102)",
            file=sys.stderr,
        )
    else:
        rendering_result = agent.render(result)
        print(
            f"Rendering complete — "
            f"{rendering_result.items_rendered} items rendered "
            f"({rendering_result.items_dropped_no_url} dropped for missing URL).",
            file=sys.stderr,
        )
        print(f"  Markdown: {rendering_result.markdown_path}", file=sys.stderr)
        print(f"  HTML:     {rendering_result.html_path}", file=sys.stderr)
        print(f"  JSON:     {rendering_result.json_path}", file=sys.stderr)

    sys.exit(0)


if __name__ == "__main__":
    cli_main()
