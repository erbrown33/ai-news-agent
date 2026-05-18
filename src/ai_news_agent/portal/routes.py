"""
portal/routes.py — FastAPI route handlers for the AI News web portal.

Routes:
  GET  /                                   → index: digest list by agent + cadence
  GET  /digest/{agent}/{date}/{cadence}    → cadence-specific digest view
  GET  /download/{agent}/{date}/{cadence}/{fmt} → serve raw digest file
  POST /api/trigger                        → authenticated manual schedule override
  GET  /api/health                         → health check for cloud scheduler probes
  GET  /api/agents                         → list available agent IDs + digest counts
  GET  /api/jobs                           → list registered scheduler jobs + next run times

Traces: SRC-004 (portal routes), SRC-028 (re-runnable curation on demand),
        SRC-029 (daily view: article cards + why-it-matters),
        SRC-030 (weekly: themes + top articles + outlook),
        SRC-031 (monthly: big-picture themes + anticipated news),
        SRC-032 (annual: top 10 + predictions),
        SRC-048 (curated item schema surfaced in portal),
        SRC-073 (secrets from env vars only), SRC-102 (smoke test via /api/health),
        SRC-133 (cadence views: daily/weekly/monthly/annual),
        SRC-134 (agent config switcher, theme visualization, no auth in v1),
        SRC-136 (export download endpoints),
        SRC-145 (date-stamped filenames), SRC-146 (non-2xx alerting),
        SRC-147 (POST /api/trigger — authenticated manual override),
        SRC-150 (quality monitoring — /api/jobs job status)
"""

from __future__ import annotations

import json
import threading
from typing import TYPE_CHECKING, Any

import structlog
from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from pydantic import BaseModel

from ai_news_agent.scheduler.auth import require_scheduler_auth

if TYPE_CHECKING:
    from pathlib import Path

    from fastapi.templating import Jinja2Templates

    from ai_news_agent.scheduler.runner import SchedulerRunner

log = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Ordered list of cadences newest-first for sorting digests on the index page.
_CADENCE_ORDER = {"annual": 0, "monthly": 1, "weekly": 2, "daily": 3}

#: Human-readable cadence labels and icons.
CADENCE_META = {
    "daily":   {"icon": "📅", "label": "Daily",   "color": "daily"},
    "weekly":  {"icon": "📆", "label": "Weekly",  "color": "weekly"},
    "monthly": {"icon": "🗓️", "label": "Monthly", "color": "monthly"},
    "annual":  {"icon": "🎯", "label": "Annual",  "color": "annual"},
}


# ---------------------------------------------------------------------------
# Request / Response models
# ---------------------------------------------------------------------------

class TriggerRequest(BaseModel):
    """
    Body for POST /api/trigger — manual schedule override (SRC-147).

    Fields:
      agent_id:  Target agent (must be loaded in scheduler).
      job_type:  "sourcing" | "curation"
      cadence:   Required when job_type == "curation".
                 One of: "daily" | "weekly" | "monthly" | "annual"

    Traces: SRC-028 (re-runnable on demand), SRC-147
    """

    agent_id: str
    job_type: str          # "sourcing" | "curation"
    cadence: str | None = None   # required if job_type == "curation"


class TriggerResponse(BaseModel):
    """Response body for POST /api/trigger."""

    status: str
    agent_id: str
    job_type: str
    cadence: str | None = None
    message: str


# ---------------------------------------------------------------------------
# Helper utilities
# ---------------------------------------------------------------------------

def _list_agents(outputs_dir: Path) -> list[str]:
    """
    Discover all agent IDs from the outputs directory.
    Each subdirectory = one agent_id.

    Traces: SRC-072 (multiple agents)
    """
    if not outputs_dir.exists():
        return []
    return sorted(
        p.name
        for p in outputs_dir.iterdir()
        if p.is_dir() and not p.name.startswith(".")
    )


def _list_digests(outputs_dir: Path, agent_id: str) -> list[dict[str, Any]]:
    """
    List all available digest files for a given agent, sorted newest first.

    Scans all three output formats (.md, .html, .json) and merges them into
    digest entries keyed by (date_str, cadence).  Any of the three formats
    can be the "seed" that creates the entry — no longer requires .md to exist.

    Returns a list of dicts with keys:
      date     – YYYY-MM-DD run date string
      cadence  – "daily" | "weekly" | "monthly" | "annual"
      formats  – sorted list of available extensions, e.g. ["html", "json", "md"]

    Traces: SRC-145 (date-stamped filenames), SRC-136 (export downloads)
    """
    agent_dir = outputs_dir / agent_id
    if not agent_dir.exists():
        return []

    digests: dict[str, dict[str, Any]] = {}

    for ext in ("md", "html", "json"):
        for f in agent_dir.glob(f"*.{ext}"):
            # Skip TinyDB store file and SQLite store
            if f.stem in ("store", ".gitkeep") or f.name.startswith("."):
                continue

            # Filename pattern: {YYYY-MM-DD}-{cadence}.{ext}
            # Date part has exactly 3 hyphens; cadence has no hyphens.
            # rsplit on last hyphen to separate date from cadence.
            parts = f.stem.rsplit("-", 1)
            if len(parts) != 2:
                continue
            date_str, cadence = parts
            # Validate cadence
            if cadence not in _CADENCE_ORDER:
                continue
            # Validate date_str looks like YYYY-MM-DD (10 chars, 2 hyphens)
            if len(date_str) != 10 or date_str.count("-") != 2:
                continue

            key = f"{date_str}|{cadence}"
            if key not in digests:
                digests[key] = {"date": date_str, "cadence": cadence, "formats": []}
            if ext not in digests[key]["formats"]:
                digests[key]["formats"].append(ext)

    # Sort formats alphabetically for stable output; sort digests newest date first,
    # then by cadence weight (annual > monthly > weekly > daily) within the same date.
    result = []
    for entry in digests.values():
        entry["formats"] = sorted(entry["formats"])
        result.append(entry)

    result.sort(
        key=lambda d: (d["date"], _CADENCE_ORDER.get(d["cadence"], 99)),
        reverse=True,
    )
    return result


def _list_digests_by_cadence(
    outputs_dir: Path, agent_id: str
) -> dict[str, list[dict[str, Any]]]:
    """
    Group digests by cadence for the index page cadence-tab view.

    Returns dict mapping cadence → list of digest dicts (newest first within cadence).

    Traces: SRC-133 (cadence-specific views on index page)
    """
    all_digests = _list_digests(outputs_dir, agent_id)
    grouped: dict[str, list[dict[str, Any]]] = {c: [] for c in _CADENCE_ORDER}
    for d in all_digests:
        cadence = d["cadence"]
        if cadence in grouped:
            grouped[cadence].append(d)
    return grouped


def _load_json_digest(
    outputs_dir: Path, agent_id: str, date_str: str, cadence: str
) -> dict[str, Any] | None:
    """
    Load a JSON digest file and return its parsed content.
    Returns None if file does not exist or is not valid JSON.

    Traces: SRC-140 (JSON archive format), SRC-145 (filename pattern)
    """
    json_path = outputs_dir / agent_id / f"{date_str}-{cadence}.json"
    if not json_path.exists():
        return None
    try:
        return json.loads(json_path.read_text(encoding="utf-8"))  # type: ignore[no-any-return]
    except (json.JSONDecodeError, OSError):
        return None


def _available_formats(
    outputs_dir: Path, agent_id: str, date_str: str, cadence: str
) -> list[str]:
    """
    Return which of md/html/json exist on disk for the given digest.

    Traces: SRC-136 (export downloads)
    """
    agent_dir = outputs_dir / agent_id
    stem = f"{date_str}-{cadence}"
    return sorted(
        ext for ext in ("html", "json", "md")
        if (agent_dir / f"{stem}.{ext}").exists()
    )


def _get_scheduler_runner(request: Request) -> SchedulerRunner | None:
    """
    Retrieve the shared SchedulerRunner from app state, if available.

    The runner is stored by ``portal/app.py`` when it is initialised.
    Returns None if no runner has been registered (portal-only mode).
    """
    return getattr(request.app.state, "scheduler_runner", None)


# ---------------------------------------------------------------------------
# Route registration
# ---------------------------------------------------------------------------

def register_routes(app_instance: FastAPI) -> None:
    """
    Register all HTTP routes on the FastAPI application.

    Routes (SRC-133–SRC-134, SRC-147, SRC-150):
    - GET /                              → index: digest list by agent + cadence
    - GET /digest/{agent_id}/{date_str}/{cadence}   → cadence-specific view
    - GET /download/{agent_id}/{date_str}/{cadence}/{fmt} → serve raw file (SRC-136)
    - POST /api/trigger                  → manual schedule override (SRC-147)
    - GET /api/health                    → health check for cloud scheduler
    - GET /api/agents                    → list agent IDs + digest counts
    - GET /api/jobs                      → list scheduler job statuses (SRC-150)

    Traces: SRC-004, SRC-028, SRC-102, SRC-133–SRC-134, SRC-136, SRC-146,
            SRC-147, SRC-150
    """

    # ------------------------------------------------------------------
    # GET / — portal landing page
    # ------------------------------------------------------------------

    @app_instance.get("/", response_class=HTMLResponse, tags=["portal"])
    async def index(request: Request) -> HTMLResponse:
        """
        Landing page — lists all agents and their available digests grouped by cadence.

        Context variables for index.html:
          agents         – ordered list of agent_id strings
          agent_digests  – dict agent_id → flat list of digest dicts (newest first)
          agent_by_cadence – dict agent_id → dict cadence → list[digest]
          cadence_meta   – CADENCE_META constant for icons/labels
          total_digests  – total count across all agents

        Traces: SRC-133 (portal views), SRC-134 (agent switcher)
        """
        outputs_dir: Path = request.app.state.outputs_dir
        templates: Jinja2Templates = request.app.state.templates

        agents = _list_agents(outputs_dir)
        agent_digests: dict[str, list[dict[str, Any]]] = {}
        agent_by_cadence: dict[str, dict[str, list[dict[str, Any]]]] = {}
        total_digests = 0

        for aid in agents:
            flat = _list_digests(outputs_dir, aid)
            agent_digests[aid] = flat
            agent_by_cadence[aid] = _list_digests_by_cadence(outputs_dir, aid)
            total_digests += len(flat)

        return templates.TemplateResponse(
            request,
            "index.html",
            {
                "agents": agents,
                "agent_digests": agent_digests,
                "agent_by_cadence": agent_by_cadence,
                "cadence_meta": CADENCE_META,
                "total_digests": total_digests,
            },
        )

    # ------------------------------------------------------------------
    # GET /digest/{agent_id}/{date_str}/{cadence} — digest view
    # ------------------------------------------------------------------

    @app_instance.get(
        "/digest/{agent_id}/{date_str}/{cadence}",
        response_class=HTMLResponse,
        tags=["portal"],
    )
    async def digest_view(
        request: Request,
        agent_id: str,
        date_str: str,
        cadence: str,
    ) -> HTMLResponse:
        """
        Cadence-specific digest view.

        - daily   → article card list + why-it-matters + impact tags (SRC-029)
        - weekly  → theme word cloud + top articles + outlook (SRC-030)
        - monthly → big-picture themes + anticipated news (SRC-031)
        - annual  → top 10 + predictions + year-in-review (SRC-032, SRC-124)

        Loads digest data from the JSON export (canonical data source).
        Falls back gracefully if JSON is not present (shows "not found").

        Traces: SRC-029–SRC-032, SRC-048, SRC-133–SRC-134
        """
        outputs_dir: Path = request.app.state.outputs_dir
        templates: Jinja2Templates = request.app.state.templates

        if cadence not in _CADENCE_ORDER:
            raise HTTPException(
                status_code=400,
                detail=f"Unknown cadence: {cadence!r}. Must be one of: {list(_CADENCE_ORDER)}",
            )

        digest_data = _load_json_digest(outputs_dir, agent_id, date_str, cadence)
        if digest_data is None:
            raise HTTPException(
                status_code=404,
                detail=f"Digest not found: agent={agent_id!r}, date={date_str!r}, cadence={cadence!r}",
            )

        # Select cadence-specific template
        template_name = f"{cadence}.html"

        all_agents = _list_agents(outputs_dir)
        available_fmts = _available_formats(outputs_dir, agent_id, date_str, cadence)
        all_digests = _list_digests(outputs_dir, agent_id)

        # Extract themes for word cloud sizing (SRC-134)
        themes: list[str] = digest_data.get("themes", [])
        items: list[dict[str, Any]] = digest_data.get("items", [])

        # Build impact tag frequency map for filter bar (SRC-134)
        impact_freq: dict[str, int] = {}
        for item in items:
            for tag in item.get("impact_tags", []):
                impact_freq[tag] = impact_freq.get(tag, 0) + 1

        # Build source tier frequency for tier filter (SRC-134)
        tier_freq: dict[str, int] = {}
        for item in items:
            t = item.get("tier", "")
            if t:
                tier_freq[t] = tier_freq.get(t, 0) + 1

        # Compute theme weights for word-cloud sizing (more freq = larger) (SRC-134)
        # Themes are deduplicated strings from the LLM; count mentions across items
        theme_weights: dict[str, int] = {}
        all_text = " ".join(
            " ".join([
                item.get("headline", ""),
                item.get("why_it_matters", ""),
            ])
            for item in items
        ).lower()
        for theme in themes:
            count = all_text.count(theme.lower())
            theme_weights[theme] = max(1, count)

        return templates.TemplateResponse(
            request,
            template_name,
            {
                "agent_id": agent_id,
                "date_str": date_str,
                "cadence": cadence,
                "digest": digest_data,
                "agents": all_agents,
                "all_digests": all_digests,
                "available_fmts": available_fmts,
                "cadence_meta": CADENCE_META,
                "themes": themes,
                "theme_weights": theme_weights,
                "items": items,
                "impact_freq": impact_freq,
                "tier_freq": tier_freq,
            },
        )

    # ------------------------------------------------------------------
    # GET /download/{agent_id}/{date_str}/{cadence}/{fmt} — file download
    # ------------------------------------------------------------------

    @app_instance.get(
        "/download/{agent_id}/{date_str}/{cadence}/{fmt}",
        tags=["downloads"],
    )
    async def download_digest(
        agent_id: str,
        date_str: str,
        cadence: str,
        fmt: str,
        request: Request,
    ) -> FileResponse:
        """
        Serve a raw digest file for download (SRC-136).

        ``fmt`` must be one of: ``md``, ``html``, ``json``.
        Date-stamped filenames ensure clean idempotent downloads (SRC-145).

        Traces: SRC-136 (export downloads from portal), SRC-145
        """
        outputs_dir: Path = request.app.state.outputs_dir

        if fmt not in ("md", "html", "json"):
            raise HTTPException(
                status_code=400,
                detail=f"Invalid format: {fmt!r}. Expected 'md', 'html', or 'json'.",
            )

        filename = f"{date_str}-{cadence}.{fmt}"
        file_path = outputs_dir / agent_id / filename

        if not file_path.exists():
            raise HTTPException(
                status_code=404,
                detail=f"File not found: {file_path}",
            )

        media_types = {
            "md": "text/markdown",
            "html": "text/html",
            "json": "application/json",
        }

        return FileResponse(
            path=str(file_path),
            filename=filename,
            media_type=media_types[fmt],
        )

    # ------------------------------------------------------------------
    # POST /api/trigger — authenticated manual override (SRC-147)
    # ------------------------------------------------------------------

    @app_instance.post(
        "/api/trigger",
        response_model=TriggerResponse,
        tags=["api"],
        dependencies=[Depends(require_scheduler_auth)],
    )
    async def trigger_job(request: Request, body: TriggerRequest) -> TriggerResponse:
        """
        Manual schedule override — trigger a sourcing or curation job on demand.

        Authentication: ``Authorization: Bearer <SCHEDULER_API_KEY>`` header
        required when the ``SCHEDULER_API_KEY`` environment variable is set.
        In dev mode (key not set), the endpoint is open with a logged warning.

        The job runs in a background thread so the HTTP request returns
        immediately with HTTP 202 Accepted.  The caller can monitor the
        outcome via structured logs or by polling ``GET /api/jobs``.

        Args (JSON body):
          agent_id:  Target agent ID.
          job_type:  "sourcing" | "curation"
          cadence:   Required when job_type == "curation".

        Raises:
          HTTP 400: Unknown job_type or missing cadence.
          HTTP 401: Invalid or missing API key (when key is configured).
          HTTP 404: Unknown agent_id.

        Traces: SRC-028 (re-runnable on demand), SRC-073 (secrets env-var only),
                SRC-146 (non-2xx alerting), SRC-147 (authenticated manual override)
        """
        log.info(
            "portal_trigger_request",
            agent_id=body.agent_id,
            job_type=body.job_type,
            cadence=body.cadence,
        )

        # --- Validation ---
        if body.job_type not in ("sourcing", "curation"):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Invalid job_type: {body.job_type!r}. Expected 'sourcing' or 'curation'.",
            )
        if body.job_type == "curation" and body.cadence not in (
            "daily", "weekly", "monthly", "annual"
        ):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=(
                    f"Invalid or missing cadence: {body.cadence!r}. "
                    "Required for curation jobs. One of: daily, weekly, monthly, annual."
                ),
            )

        # --- Dispatch ---
        runner: SchedulerRunner | None = _get_scheduler_runner(request)

        if runner is None:
            # Portal running without a scheduler runner (read-only mode).
            log.warning(
                "portal_trigger_no_runner",
                agent_id=body.agent_id,
                job_type=body.job_type,
                hint="Start portal with scheduler integration to execute jobs.",
            )
            return TriggerResponse(
                status="accepted",
                agent_id=body.agent_id,
                job_type=body.job_type,
                cadence=body.cadence,
                message=(
                    "No scheduler runner available in this portal instance. "
                    "Use 'ai-news-schedule --trigger-agent' or 'ai-news-oneshot' instead."
                ),
            )

        # Verify agent exists before firing background thread
        if body.agent_id not in runner._agent_configs:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Unknown agent_id: {body.agent_id!r}. "
                       f"Loaded agents: {runner.agent_ids}",
            )

        def _background_trigger() -> None:
            try:
                runner.trigger_now(
                    agent_id=body.agent_id,
                    job_type=body.job_type,
                    cadence=body.cadence,  # type: ignore[arg-type]
                )
            except Exception as exc:  # noqa: BLE001
                log.error(
                    "portal_trigger_background_failed",
                    agent_id=body.agent_id,
                    job_type=body.job_type,
                    cadence=body.cadence,
                    error=str(exc),
                )

        t = threading.Thread(target=_background_trigger, daemon=True)
        t.start()

        log.info(
            "portal_trigger_dispatched",
            agent_id=body.agent_id,
            job_type=body.job_type,
            cadence=body.cadence,
        )

        return TriggerResponse(
            status="accepted",
            agent_id=body.agent_id,
            job_type=body.job_type,
            cadence=body.cadence,
            message=(
                f"Job '{body.job_type}' dispatched for agent '{body.agent_id}' "
                f"(cadence={body.cadence!r}). Running in background — check logs for result."
            ),
        )

    # ------------------------------------------------------------------
    # GET /api/health — health check (SRC-102, SRC-146)
    # ------------------------------------------------------------------

    @app_instance.get("/api/health", tags=["api"])
    async def health(request: Request) -> JSONResponse:
        """
        Health check endpoint for cloud scheduler and load balancer probes.
        Returns HTTP 200 with non-empty JSON to satisfy the smoke test (SRC-102).

        Includes scheduler status when a runner is available (SRC-150).

        Traces: SRC-102 (smoke test), SRC-146 (non-2xx alerting),
                SRC-150 (quality monitoring — operational status)
        """
        runner = _get_scheduler_runner(request)
        outputs_dir: Path = request.app.state.outputs_dir
        agents = _list_agents(outputs_dir)
        total_digests = sum(
            len(_list_digests(outputs_dir, aid)) for aid in agents
        )
        return JSONResponse(
            content={
                "status": "ok",
                "service": "ai-news-curation-portal",
                "agents": agents,
                "total_digests": total_digests,
                "scheduler": {
                    "running": runner.is_running if runner else False,
                    "agents": runner.agent_ids if runner else [],
                },
            },
            status_code=200,
        )

    # ------------------------------------------------------------------
    # GET /api/agents — list agents (SRC-134)
    # ------------------------------------------------------------------

    @app_instance.get("/api/agents", tags=["api"])
    async def list_agents(request: Request) -> JSONResponse:
        """
        Return all available agent IDs with digest counts per cadence.
        Used by the agent switcher in the portal UI (SRC-134).

        Traces: SRC-134 (agent config switcher)
        """
        outputs_dir: Path = request.app.state.outputs_dir
        agents = _list_agents(outputs_dir)
        agent_info = []
        for aid in agents:
            by_cadence = _list_digests_by_cadence(outputs_dir, aid)
            agent_info.append({
                "agent_id": aid,
                "digest_count": len(_list_digests(outputs_dir, aid)),
                "by_cadence": {c: len(v) for c, v in by_cadence.items()},
            })
        return JSONResponse(content={"agents": agent_info})

    # ------------------------------------------------------------------
    # GET /api/jobs — scheduler job statuses (SRC-150)
    # ------------------------------------------------------------------

    @app_instance.get("/api/jobs", tags=["api"])
    async def list_jobs(request: Request) -> JSONResponse:
        """
        Return all registered APScheduler jobs with their next scheduled run times.

        Useful for operational dashboards and verifying that the scheduler is
        running with the expected cadence triggers (SRC-150).

        Returns HTTP 503 if no scheduler runner is attached to the portal.

        Traces: SRC-052 (scheduler), SRC-150 (quality monitoring — observability)
        """
        runner = _get_scheduler_runner(request)

        if runner is None:
            return JSONResponse(
                content={
                    "status": "unavailable",
                    "message": (
                        "No scheduler runner attached to this portal instance. "
                        "Run 'ai-news-schedule' to start the background scheduler."
                    ),
                    "jobs": [],
                },
                status_code=503,
            )

        jobs = runner.get_job_statuses()
        return JSONResponse(
            content={
                "status": "ok",
                "scheduler_running": runner.is_running,
                "agent_count": len(runner.agent_ids),
                "jobs": jobs,
            }
        )
