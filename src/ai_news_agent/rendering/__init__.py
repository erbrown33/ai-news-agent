"""
rendering — Rendering Agent: produces Markdown, HTML, and JSON digest outputs.

Every curation run emits three date-stamped export files into
``outputs/{agent_id}/``:

- ``{YYYY-MM-DD}-{cadence}.md``   — Slack/Teams paste-ready Markdown (SRC-138)
- ``{YYYY-MM-DD}-{cadence}.html`` — Email-client paste-ready HTML (SRC-137)
- ``{YYYY-MM-DD}-{cadence}.json`` — Machine-readable archive (SRC-140)

URL enforcement (SRC-141, SRC-049):
  Every renderer independently drops items whose URL is empty or does not
  start with ``http://`` or ``https://``.  No item without a working link
  appears in any output format — non-negotiable.  This is the second safety
  layer after the Scorer in the Curation Agent.

Naming convention (SRC-145, SRC-140):
  The ``agent_id`` is embedded in the **directory path** (``outputs/{agent_id}/``),
  not in the filename.  Filenames follow ``{YYYY-MM-DD}-{cadence}.{ext}`` so
  that a future thin distribution layer can ingest the directory tree without
  parsing filenames — a design requirement (SRC-140).

Shared utilities (rendering.utils):
  ``is_valid_url``   — the single URL validator used by all three renderers
  ``filename_stem``  — the shared basename formula used by all three renderers

Traces: SRC-004 (structured export files), SRC-049 (URL required),
        SRC-135–SRC-141 (rendered export section), SRC-145 (idempotent filenames),
        SRC-140 (naming convention for future distribution layer)
"""

from ai_news_agent.rendering.agent import RenderingAgent, RenderingResult
from ai_news_agent.rendering.html_renderer import HtmlRenderer
from ai_news_agent.rendering.json_renderer import JsonRenderer
from ai_news_agent.rendering.markdown_renderer import MarkdownRenderer
from ai_news_agent.rendering.utils import VALID_URL_SCHEMES, filename_stem, is_valid_url

__all__ = [
    "RenderingAgent",
    "RenderingResult",
    "MarkdownRenderer",
    "HtmlRenderer",
    "JsonRenderer",
    # Shared utilities
    "is_valid_url",
    "filename_stem",
    "VALID_URL_SCHEMES",
]
