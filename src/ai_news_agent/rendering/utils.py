"""
rendering/utils.py — Shared rendering utilities.

Centralising URL validation and common helpers here eliminates three-way
duplication across MarkdownRenderer, HtmlRenderer, and JsonRenderer and
ensures they all enforce the same rule — any drift becomes a compile error.

Traces: SRC-049 (URL required — non-negotiable),
        SRC-141 (URL enforcement at renderer — items missing URL are dropped,
                 not truncated or flagged),
        SRC-140 (naming convention designed for future thin distribution layer)
"""

from __future__ import annotations

__all__ = [
    "is_valid_url",
    "filename_stem",
    "VALID_URL_SCHEMES",
]

# ---------------------------------------------------------------------------
# URL validation (SRC-049, SRC-141)
# ---------------------------------------------------------------------------

VALID_URL_SCHEMES = ("http://", "https://")
"""The only accepted URL schemes for primary source links.

``ftp://``, ``//``, ``javascript:``, ``data:``, bare paths, and every
other scheme are rejected.  The check is intentionally case-insensitive.
"""


def is_valid_url(url: str | None) -> bool:
    """
    Return ``True`` only if *url* is a non-empty string that starts with
    ``http://`` or ``https://`` (case-insensitive).

    This is the **renderer-level final URL enforcement** (SRC-141, SRC-049):
    - First layer: Scorer in ``curation/scorer.py`` drops no-URL items before
      they reach ``CurationRunResult``.
    - Second layer (this function): every renderer independently re-validates
      so that any item that somehow survived the first check is still dropped.
      The two-layer approach makes the guarantee robust to future refactors.

    Rejection examples:
        - ``""``                  → False  (empty)
        - ``None``                → False  (None)
        - ``"  "``                → False  (whitespace only)
        - ``"reuters.com/news"``  → False  (no scheme)
        - ``"//reuters.com"``     → False  (protocol-relative)
        - ``"ftp://files.corp"``  → False  (wrong scheme)
        - ``"javascript:void(0)"``→ False  (injection)
        - ``"data:text/html,..."``→ False  (data URI)

    Acceptance examples:
        - ``"https://reuters.com/article"`` → True
        - ``"http://bloomberg.com/news"``   → True
        - ``"HTTPS://WSJ.COM/AI"``          → True  (case-insensitive)

    Args:
        url: The URL string to validate (may be ``None``).

    Returns:
        ``True`` if the URL is a valid ``http(s)://`` link, ``False`` otherwise.

    Traces: SRC-049 (non-negotiable URL requirement),
            SRC-141 (second safety layer — drop, not truncate)
    """
    if not url:
        return False
    lowered = url.strip().lower()
    return any(lowered.startswith(scheme) for scheme in VALID_URL_SCHEMES)


# ---------------------------------------------------------------------------
# Filename helpers (SRC-145, SRC-140)
# ---------------------------------------------------------------------------


def filename_stem(run_date: object, cadence: str) -> str:
    """
    Return the shared filename stem used by all three renderers.

    Pattern: ``{YYYY-MM-DD}-{cadence}``

    The agent_id is encoded in the **directory path** (``outputs/{agent_id}/``),
    not in the filename, so that a future thin distribution layer can ingest the
    output directory tree without parsing filenames (SRC-140).

    Example::

        filename_stem(date(2026, 5, 10), "daily")  → "2026-05-10-daily"

    Args:
        run_date: A ``datetime.date`` (or anything with a valid ``__str__``
                  that produces ``YYYY-MM-DD``).
        cadence:  One of ``"daily"``, ``"weekly"``, ``"monthly"``, ``"annual"``.

    Returns:
        Filename stem string — ASCII-only, filesystem-safe.

    Traces: SRC-145 (date-stamped, idempotent re-runs overwrite cleanly),
            SRC-140 (convention supports future distribution layer)
    """
    return f"{run_date}-{cadence}"
