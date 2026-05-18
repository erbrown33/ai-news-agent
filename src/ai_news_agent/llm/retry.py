"""
llm/retry.py — Exponential-backoff retry decorator for LLM and search calls.

Retries on transient network/rate-limit errors without surfacing provider-specific
exception types to the pipeline — all are normalised to ``LLMError``.

Traces: SRC-144 (3 retries, exponential backoff — 30 → 60 → 120 s),
        SRC-056 (provider-agnostic — no provider exception types leak upward)
"""

from __future__ import annotations

import functools
import time
from collections.abc import Callable
from typing import Any, TypeVar

import structlog

log = structlog.get_logger(__name__)

F = TypeVar("F", bound=Callable[..., Any])


class LLMError(RuntimeError):
    """
    Normalised LLM/search error raised after all retries are exhausted.

    Wraps the original provider exception so callers never depend on
    provider-specific exception types. (SRC-056)
    """

    def __init__(self, message: str, cause: BaseException | None = None) -> None:
        super().__init__(message)
        self.__cause__ = cause


# ---------------------------------------------------------------------------
# Retry configuration (SRC-144)
# ---------------------------------------------------------------------------

_DEFAULT_MAX_RETRIES: int = 3
_DEFAULT_BACKOFF_BASE: float = 30.0  # seconds — doubles on each retry: 30 → 60 → 120


def _is_retryable(exc: BaseException) -> bool:
    """
    Classify whether an exception warrants a retry.

    We treat the following as retryable:
    - HTTP 429 (rate limit) from any provider
    - HTTP 500 / 502 / 503 / 504 from any provider
    - Connection / timeout errors (httpx, requests, urllib3)
    - openai.RateLimitError, openai.APIConnectionError, openai.APITimeoutError
    - anthropic.RateLimitError, anthropic.APIConnectionError (if installed)

    Non-retryable:
    - HTTP 400 Bad Request (prompt/schema error — retrying won't help)
    - HTTP 401/403 Forbidden (auth error — key wrong/missing)
    - ValueError / json.JSONDecodeError (parsing — not transient)

    Traces: SRC-144
    """
    exc_type_name = type(exc).__name__
    exc_module = type(exc).__module__ or ""
    msg_lower = str(exc).lower()

    # OpenAI SDK retryable subtypes
    retryable_names = {
        "RateLimitError",
        "APIConnectionError",
        "APITimeoutError",
        "InternalServerError",
        "ServiceUnavailableError",
    }
    if exc_type_name in retryable_names:
        return True

    # Generic HTTP-level signals in exception message
    retryable_patterns = ("429", "500", "502", "503", "504", "rate limit", "timeout", "connection")
    for pattern in retryable_patterns:
        if pattern in msg_lower:
            return True

    # httpx transport errors
    return "httpx" in exc_module and exc_type_name in {"TimeoutException", "ConnectError", "ReadError"}


def with_retry(
    max_retries: int = _DEFAULT_MAX_RETRIES,
    backoff_base: float = _DEFAULT_BACKOFF_BASE,
) -> Callable[[F], F]:
    """
    Decorator factory — wraps a function with exponential-backoff retry logic.

    Usage::

        @with_retry(max_retries=3, backoff_base=30.0)
        def my_llm_call(...) -> str:
            ...

    On non-retryable errors the original exception propagates immediately.
    After ``max_retries`` exhausted, raises :class:`LLMError`.

    Traces: SRC-144 (3 retries, exponential backoff 30 → 60 → 120 s)
    """

    def decorator(fn: F) -> F:
        @functools.wraps(fn)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            last_exc: BaseException | None = None
            for attempt in range(max_retries + 1):
                try:
                    return fn(*args, **kwargs)
                except LLMError:
                    raise  # already normalised — don't wrap again
                except Exception as exc:  # noqa: BLE001
                    if not _is_retryable(exc):
                        # Normalise to LLMError so the pipeline never sees provider types
                        raise LLMError(
                            f"{fn.__name__} failed (non-retryable): {exc}", cause=exc
                        ) from exc

                    last_exc = exc
                    if attempt < max_retries:
                        delay = backoff_base * (2**attempt)  # 30, 60, 120
                        log.warning(
                            "llm_retry",
                            fn=fn.__name__,
                            attempt=attempt + 1,
                            max_retries=max_retries,
                            delay_s=delay,
                            error=str(exc),
                        )
                        time.sleep(delay)
                    else:
                        log.error(
                            "llm_retries_exhausted",
                            fn=fn.__name__,
                            max_retries=max_retries,
                            error=str(exc),
                        )

            raise LLMError(
                f"{fn.__name__} failed after {max_retries} retries: {last_exc}",
                cause=last_exc,
            )

        return wrapper  # type: ignore[return-value]

    return decorator
