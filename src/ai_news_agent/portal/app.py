"""
portal/app.py — FastAPI application factory and CLI entry point.

The portal can run in two modes:

1. **Portal-only** (default): serves existing digest files and exposes read-only
   API endpoints.  ``POST /api/trigger`` returns a 202 with a message directing
   the caller to use the CLI scheduler instead.

2. **Portal + Scheduler**: a shared :class:`SchedulerRunner` is attached to the
   FastAPI app state.  ``POST /api/trigger`` dispatches real jobs in background
   threads and ``GET /api/jobs`` shows live scheduler job statuses.

Traces: SRC-004 (web portal deliverable), SRC-052 (scheduler integration),
        SRC-072 (multi-agent via runner), SRC-102 (smoke test via /api/health),
        SRC-133 (portal features), SRC-134 (agent config switcher, no auth in v1),
        SRC-136 (export downloads), SRC-146 (non-2xx alerting), SRC-147 (trigger API)
"""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import TYPE_CHECKING

import structlog
import uvicorn
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

if TYPE_CHECKING:
    from ai_news_agent.scheduler.runner import SchedulerRunner

log = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Application factory
# ---------------------------------------------------------------------------

def create_app(
    outputs_dir: str = "outputs",
    configs_dir: str = "configs",
    scheduler_runner: SchedulerRunner | None = None,
) -> FastAPI:
    """
    Create and configure the FastAPI application.

    Args:
        outputs_dir:       Root directory containing agent output subdirectories.
                           One subdirectory per agent_id; contains date-stamped digest files.
        configs_dir:       Root directory containing per-agent YAML configurations.
        scheduler_runner:  Optional shared :class:`SchedulerRunner`.  When provided:
                           - ``POST /api/trigger`` dispatches real jobs (SRC-147).
                           - ``GET /api/jobs`` returns live job statuses (SRC-150).
                           - ``GET /api/health`` reports scheduler status.
                           When None, the portal operates in read-only mode.

    Traces: SRC-004 (portal deliverable), SRC-052 (optional scheduler attachment),
            SRC-133 (cadence-specific views), SRC-134 (agent switcher, no auth v1),
            SRC-136 (export downloads), SRC-147 (trigger endpoint)
    """
    _app = FastAPI(
        title="AI News Curation Portal",
        description=(
            "Browse curated AI news digests by agent configuration and cadence. "
            "Download Markdown, HTML, or JSON exports. "
            "Trigger on-demand sourcing or curation via POST /api/trigger. "
            "(SRC-004, SRC-133, SRC-147)"
        ),
        version="0.1.0",
        docs_url="/api/docs",
        redoc_url="/api/redoc",
    )

    # Static files (CSS, JS) — SRC-134 (tag cloud, provider filter)
    static_dir = Path(__file__).parent / "static"
    if static_dir.exists():
        _app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

    # Jinja2 templates
    templates_dir = Path(__file__).parent / "templates"
    templates = Jinja2Templates(directory=str(templates_dir))

    # Store in app state so routes can access them
    _app.state.outputs_dir = Path(outputs_dir)
    _app.state.configs_dir = Path(configs_dir)
    _app.state.templates = templates
    _app.state.scheduler_runner = scheduler_runner  # None = portal-only mode

    # Register routes
    from ai_news_agent.portal.routes import register_routes
    register_routes(_app)

    log.info(
        "portal_app_created",
        outputs_dir=outputs_dir,
        configs_dir=configs_dir,
        scheduler_attached=scheduler_runner is not None,
    )

    return _app


# ---------------------------------------------------------------------------
# Default application instance (used by uvicorn CMD)
# ---------------------------------------------------------------------------
app = create_app()


# ---------------------------------------------------------------------------
# CLI entry point (SRC-076: local dev web portal)
# ---------------------------------------------------------------------------

def cli_main() -> None:
    """
    Command-line entry point: ``ai-news-portal``.

    Usage::

        ai-news-portal
        ai-news-portal --host 0.0.0.0 --port 8080 --outputs-dir outputs
        ai-news-portal --with-scheduler    # attach SchedulerRunner for live trigger support

    When ``--with-scheduler`` is given, the portal loads the scheduler config,
    discovers all enabled agents, and attaches a :class:`SchedulerRunner` so
    that ``POST /api/trigger`` executes real jobs (SRC-147).

    Traces: SRC-076 (local dev Phase 1), SRC-133 (web portal), SRC-147 (trigger API)
    """
    parser = argparse.ArgumentParser(
        prog="ai-news-portal",
        description="Start the AI News Curation web portal.",
    )
    parser.add_argument("--host", default="0.0.0.0", help="Host to bind (default: 0.0.0.0)")
    parser.add_argument("--port", type=int, default=8080, help="Port to bind (default: 8080)")
    parser.add_argument(
        "--outputs-dir",
        default="outputs",
        help="Directory containing agent output subdirectories (default: outputs)",
    )
    parser.add_argument(
        "--configs-dir",
        default="configs",
        help="Directory containing agent YAML configs (default: configs)",
    )
    parser.add_argument(
        "--reload",
        action="store_true",
        help="Enable auto-reload for development (do not use in production)",
    )
    parser.add_argument(
        "--with-scheduler",
        action="store_true",
        help=(
            "Attach a SchedulerRunner to the portal for live job triggering. "
            "Requires configs/scheduler.yaml and all agent configs. (SRC-147)"
        ),
    )
    parser.add_argument(
        "--scheduler-config",
        default="configs/scheduler.yaml",
        help="Path to scheduler.yaml used when --with-scheduler is set.",
    )
    args = parser.parse_args()

    runner: SchedulerRunner | None = None
    if args.with_scheduler:
        from ai_news_agent.config.loader import load_scheduler_config
        from ai_news_agent.scheduler.runner import SchedulerRunner

        sched_cfg = load_scheduler_config(args.scheduler_config)
        runner = SchedulerRunner(
            scheduler_config=sched_cfg,
            scheduler_config_path=args.scheduler_config,
        )
        runner.load_agent_configs()
        log.info(
            "portal_scheduler_attached",
            agents=runner.agent_ids,
        )

    _app = create_app(
        outputs_dir=args.outputs_dir,
        configs_dir=args.configs_dir,
        scheduler_runner=runner,
    )

    uvicorn.run(
        _app,
        host=args.host,
        port=args.port,
        reload=args.reload,
        log_level="info",
    )


if __name__ == "__main__":
    cli_main()
