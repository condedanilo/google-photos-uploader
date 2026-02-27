"""Exponential backoff retry utility.

Usage:
    result = with_retry(
        lambda: api_client.upload_bytes(path),
        max_attempts=3,
        base_delay=2.0,
        retryable=(NetworkError, RateLimitError, ServerError),
        non_retryable=(QuotaExhaustedError, FileTooLargeError),
        on_retry=lambda attempt, exc: print(f"Retry {attempt}: {exc}"),
    )
"""
from __future__ import annotations

import random
import time
from typing import Any, Callable, Optional, Tuple, Type

from uploader.errors import RateLimitError, UploaderError


def with_retry(
    fn: Callable[[], Any],
    *,
    max_attempts: int,
    base_delay: float,
    retryable: Tuple[Type[Exception], ...],
    non_retryable: Tuple[Type[Exception], ...] = (),
    on_retry: Optional[Callable[[int, Exception], None]] = None,
    _sleep: Callable[[float], None] = time.sleep,  # injectable for testing
) -> Any:
    """Call *fn* up to *max_attempts* times with exponential backoff.

    Args:
        fn: Zero-argument callable to retry.
        max_attempts: Maximum number of attempts (including the first call).
        base_delay: Base delay in seconds. Actual delay: base * 2^attempt + jitter.
        retryable: Exception types that trigger a retry.
        non_retryable: Exception types that are raised immediately without retry.
        on_retry: Optional callback invoked before each retry:
            ``on_retry(attempt_number, exception)``.
        _sleep: Injectable sleep function (use in tests to avoid real sleeps).

    Returns:
        The return value of *fn* on success.

    Raises:
        The last exception encountered after exhausting all attempts.
        Any non_retryable exception immediately.
    """
    last_exc: Optional[Exception] = None

    for attempt in range(max_attempts):
        try:
            return fn()
        except tuple(non_retryable) as exc:
            raise
        except tuple(retryable) as exc:
            last_exc = exc
            if attempt == max_attempts - 1:
                break   # exhausted — raise below

            # Special case: RateLimitError may carry a server-suggested delay
            if isinstance(exc, RateLimitError) and exc.retry_after is not None:
                delay = exc.retry_after
            else:
                jitter = random.uniform(0.0, 0.5)
                delay = base_delay * (2 ** attempt) + jitter

            if on_retry is not None:
                on_retry(attempt + 1, exc)

            _sleep(delay)

    assert last_exc is not None
    raise last_exc
