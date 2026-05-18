"""
rendering/json_renderer.py — Renders curation output as JSON.

Traces: SRC-004 (JSON export format), SRC-140 (machine-readable / archive-ready),
        SRC-141 (URL enforcement — items without valid URL dropped at renderer),
        SRC-145 (date-stamped filename, idempotent re-runs),
        SRC-048 (curated item schema), SRC-049 (URL required),
        SRC-129 (prompt_version in every item for regression tracing),
        SRC-150 (all quality-monitoring fields in ``metadata`` block)
"""

from __future__ import annotations

import json
from datetime import date, datetime
from typing import TYPE_CHECKING, Any

import structlog

from ai_news_agent.rendering.utils import is_valid_url as _is_valid_url  # noqa: F401

if TYPE_CHECKING:
    from ai_news_agent.curation.agent import CurationRunResult
    from ai_news_agent.storage.models import CuratedItem, DigestMetadata

log = structlog.get_logger(__name__)

# Current schema version — increment when the JSON structure changes incompatibly.
# Consumers should gate on this field before parsing item fields.
SCHEMA_VERSION = "1.0"

# Re-export the shared URL validator under its legacy private name for
# backwards-compatibility with tests that import it directly.
# Canonical import: ``from ai_news_agent.rendering.utils import is_valid_url``
# Traces: SRC-049, SRC-141


# ---------------------------------------------------------------------------
# Serialisation helpers
# ---------------------------------------------------------------------------


def _serialize(obj: Any) -> Any:
    """Custom JSON serialiser for :class:`date` and :class:`datetime` objects."""
    if isinstance(obj, (date, datetime)):
        return obj.isoformat()
    raise TypeError(f"Object of type {type(obj).__name__} is not JSON serializable")


def _item_to_dict(item: CuratedItem) -> dict[str, Any]:
    """
    Convert a :class:`CuratedItem` to a JSON-serialisable dict.

    All fields from SRC-048 are included:
    - headline, source_name, url, pub_date
    - why_it_matters, impact_tags, tier
    - cross_refs (SRC-048 — related item URLs)
    - twitter_handle, tweet_url (SRC-048 — null if web-sourced)
    - prompt_version (SRC-129 — per-item traceability)
    """
    return {
        "headline": item.headline,
        "source_name": item.source_name,
        "url": item.url,
        "pub_date": (
            item.pub_date.isoformat() if isinstance(item.pub_date, date) else str(item.pub_date)
        ),
        "why_it_matters": item.why_it_matters,
        "impact_tags": item.impact_tags,
        "tier": item.tier,
        "cross_refs": item.cross_refs,
        "twitter_handle": item.twitter_handle,
        "tweet_url": item.tweet_url,
        "prompt_version": item.prompt_version,
    }


def _metadata_to_dict(meta: DigestMetadata) -> dict[str, Any]:
    """
    Convert a :class:`DigestMetadata` to a JSON-serialisable dict.

    Includes every SRC-150 quality-monitoring field plus SRC-129
    (prompt_version) and SRC-148 (twitter_signal_available).
    """
    return {
        "agent_id": meta.agent_id,
        "cadence": meta.cadence,
        "run_date": (
            meta.run_date.isoformat() if isinstance(meta.run_date, date) else str(meta.run_date)
        ),
        "window_start": meta.window_start.isoformat(),
        "window_end": meta.window_end.isoformat(),
        "prompt_version": meta.prompt_version,  # SRC-129
        "llm_provider": meta.llm_provider,  # SRC-150
        "llm_model": meta.llm_model,  # SRC-150
        "items_considered": meta.items_considered,  # SRC-150
        "items_included": meta.items_included,  # SRC-150
        "items_by_tier": meta.items_by_tier,  # SRC-150
        "items_by_source_class": meta.items_by_source_class,  # SRC-150
        "twitter_signal_available": meta.twitter_signal_available,  # SRC-148
        "tweet_api_call_count": meta.tweet_api_call_count,  # SRC-150
        "token_usage": meta.token_usage,  # SRC-150
    }


class JsonRenderer:
    """
    Renders a :class:`CurationRunResult` to a JSON string.

    Output format (SRC-004, SRC-140):
    - Machine-readable, archive-ready JSON.
    - ``schema_version`` field enables future schema evolution.
    - ``metadata`` block contains all SRC-150 quality-monitoring fields.
    - Each item carries ``prompt_version`` for per-item regression tracing (SRC-129).
    - Items without a valid ``http(s)://`` URL are silently dropped —
      final URL enforcement (SRC-141, SRC-049).
    - Date-stamped filename: ``{YYYY-MM-DD}-{cadence}.json`` (SRC-145).

    JSON schema (top-level keys):
    - ``schema_version`` — semver string (currently "1.0")
    - ``metadata``       — :func:`_metadata_to_dict` output
    - ``items``          — list of :func:`_item_to_dict` dicts (URL-validated)
    - ``themes``         — list of str (weekly/monthly/annual; [] for daily)
    - ``outlook``        — str (weekly/monthly look-ahead; "" for daily/annual)
    - ``predictions``    — list of str (annual only, SRC-124; [] otherwise)
    - ``twitter_degradation_note`` — str | absent (SRC-148)

    Traces: SRC-004, SRC-048, SRC-049, SRC-061, SRC-124, SRC-129,
            SRC-140, SRC-141, SRC-145, SRC-148, SRC-150
    """

    def render(self, result: CurationRunResult) -> str:
        """
        Render the curation result to a JSON string.

        Args:
            result: :class:`CurationRunResult` from the Curation Agent.

        Returns:
            Indented JSON string ready to write to disk.

        Traces: SRC-004, SRC-048, SRC-141, SRC-150
        """
        meta = result.metadata

        # Final URL enforcement (SRC-141, SRC-049) — second safety layer
        valid_items = [item for item in result.items if _is_valid_url(item.url)]
        dropped = len(result.items) - len(valid_items)
        if dropped > 0:
            log.warning(
                "json_renderer_url_drop",
                dropped=dropped,
                cadence=meta.cadence,
                agent_id=meta.agent_id,
            )

        payload: dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "metadata": _metadata_to_dict(meta),
            "items": [_item_to_dict(item) for item in valid_items],
            "themes": result.themes,
            "outlook": result.outlook,
            "predictions": result.predictions,  # annual only (SRC-124)
        }

        if result.twitter_degradation_note:
            payload["twitter_degradation_note"] = result.twitter_degradation_note

        if result.diagnostics is not None:
            diag = result.diagnostics
            payload["diagnostics"] = {
                "threshold": diag.threshold,
                "articles_in_store": diag.articles_in_store,
                "articles_in_window": diag.articles_in_window,
                "articles_in_window_by_tier": diag.articles_in_window_by_tier,
                "items_dropped_no_url": diag.items_dropped_no_url,
                "twitter_signal_available": diag.twitter_signal_available,
                "reasons": diag.reasons,
            }

        return json.dumps(payload, indent=2, default=_serialize, ensure_ascii=False)

    @staticmethod
    def filename(meta: DigestMetadata) -> str:
        """
        Return the date-stamped filename for this digest.

        Pattern: ``{YYYY-MM-DD}-{cadence}.json``

        The agent_id is embedded in the **directory path** (``outputs/{agent_id}/``),
        not the filename, so a future thin distribution layer can ingest the
        output tree without parsing filenames (SRC-140).

        Traces: SRC-145 (date-stamped, idempotent re-runs),
                SRC-140 (naming convention supports future distribution layer)
        """
        from ai_news_agent.rendering.utils import filename_stem

        return f"{filename_stem(meta.run_date, meta.cadence)}.json"
