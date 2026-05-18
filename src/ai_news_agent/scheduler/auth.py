"""
scheduler/auth.py — Bearer-token authentication for the manual override API.

The POST /api/trigger endpoint is an authenticated endpoint (SRC-147).  When
``SCHEDULER_API_KEY`` is set in the environment, every request to that endpoint
must include an ``Authorization: Bearer <key>`` header that matches exactly.

Design rules (SRC-073, SRC-147):
- The API key is read from the ``SCHEDULER_API_KEY`` environment variable only —
  never from YAML or any config file.
- When ``SCHEDULER_API_KEY`` is *not* set, the endpoint is effectively
  unprotected (suitable for local development).  A log warning is emitted at
  startup to make this visible.
- The dependency raises HTTP 401 for missing/wrong credentials so that cloud
  schedulers can be notified of auth failures through non-2xx alerting (SRC-146).

Traces: SRC-073 (secrets from env vars only),
        SRC-146 (non-2xx response on failure),
        SRC-147 (authenticated manual override)
"""

from __future__ import annotations

import os

import structlog
from fastapi import HTTPException, Request, status

log = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Public constants
# ---------------------------------------------------------------------------

_ENV_VAR = "SCHEDULER_API_KEY"
_HEADER  = "Authorization"
_SCHEME  = "Bearer "


# ---------------------------------------------------------------------------
# FastAPI dependency
# ---------------------------------------------------------------------------

async def require_scheduler_auth(request: Request) -> None:
    """
    FastAPI dependency that enforces Bearer-token auth on the trigger endpoint.

    Behaviour:
    - If ``SCHEDULER_API_KEY`` env var is not set: log a warning and allow the
      request through (local-dev convenience).
    - If the env var IS set: the request ``Authorization`` header must be
      ``Bearer <key>`` with an exact match.  On mismatch → HTTP 401.

    Raises:
        HTTPException(401): When the API key is configured but auth fails.

    Traces: SRC-073 (env-var secrets only), SRC-146 (non-2xx on failure),
            SRC-147 (authenticated manual override)
    """
    expected_key: str | None = os.environ.get(_ENV_VAR)

    if expected_key is None:
        # No key configured — pass through with a dev-mode warning (SRC-147)
        log.warning(
            "scheduler_auth_unprotected",
            hint=(
                f"POST /api/trigger is unprotected. "
                f"Set {_ENV_VAR} env var to enable authentication."
            ),
        )
        return

    auth_header: str = request.headers.get(_HEADER, "")
    if not auth_header.startswith(_SCHEME):
        log.warning(
            "scheduler_auth_missing_header",
            remote=request.client.host if request.client else "unknown",
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=(
                "Missing or malformed Authorization header. "
                "Expected: 'Authorization: Bearer <key>'."
            ),
            headers={"WWW-Authenticate": "Bearer"},
        )

    provided_key = auth_header[len(_SCHEME):]
    if provided_key != expected_key:
        log.warning(
            "scheduler_auth_invalid_key",
            remote=request.client.host if request.client else "unknown",
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid API key.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    log.debug(
        "scheduler_auth_ok",
        remote=request.client.host if request.client else "unknown",
    )


# ---------------------------------------------------------------------------
# Utility: validate key from raw string (used in serverless handler)
# ---------------------------------------------------------------------------

def validate_api_key(provided: str | None) -> bool:
    """
    Return True if *provided* matches the configured ``SCHEDULER_API_KEY``.

    If no key is configured, **always** returns True (open-access mode).

    Traces: SRC-073 (env var only), SRC-147 (authenticated trigger)
    """
    expected: str | None = os.environ.get(_ENV_VAR)
    if expected is None:
        return True  # Dev mode — no key configured
    if provided is None:
        return False
    return provided == expected
