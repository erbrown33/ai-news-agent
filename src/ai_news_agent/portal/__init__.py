"""
portal — FastAPI + Jinja2 web portal for browsing curated digests.
Traces: SRC-004 (web portal deliverable), SRC-133–SRC-134 (portal features),
        SRC-136 (export downloads from portal)
"""

from ai_news_agent.portal.app import app, cli_main

__all__ = ["app", "cli_main"]
