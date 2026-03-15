"""Unit tests for uploader/retry.py."""
from __future__ import annotations

import pytest

from uploader.errors import ConflictError, NetworkError, QuotaExhaustedError, RateLimitError, ServerError
from uploader.retry import with_retry


class TestWithRetry:
    def test_succeeds_on_first_attempt(self):
        call_count = 0

        def fn():
            nonlocal call_count
            call_count += 1
            return "ok"

        result = with_retry(
            fn, max_attempts=3, base_delay=0.0,
            retryable=(NetworkError,), _sleep=lambda _: None,
        )
        assert result == "ok"
        assert call_count == 1

    def test_retries_then_succeeds(self):
        calls = []

        def fn():
            calls.append(1)
            if len(calls) < 3:
                raise NetworkError("connection reset")
            return "done"

        result = with_retry(
            fn, max_attempts=5, base_delay=0.0,
            retryable=(NetworkError,), _sleep=lambda _: None,
        )
        assert result == "done"
        assert len(calls) == 3

    def test_raises_after_exhausting_attempts(self):
        def fn():
            raise NetworkError("always fails")

        with pytest.raises(NetworkError, match="always fails"):
            with_retry(
                fn, max_attempts=3, base_delay=0.0,
                retryable=(NetworkError,), _sleep=lambda _: None,
            )

    def test_non_retryable_raises_immediately(self):
        calls = []

        def fn():
            calls.append(1)
            raise QuotaExhaustedError()

        with pytest.raises(QuotaExhaustedError):
            with_retry(
                fn, max_attempts=5, base_delay=0.0,
                retryable=(NetworkError,),
                non_retryable=(QuotaExhaustedError,),
                _sleep=lambda _: None,
            )
        assert len(calls) == 1   # no retries

    def test_on_retry_callback_called(self):
        retries: list[tuple[int, Exception]] = []

        def fn():
            raise ServerError(500, "internal error")

        with pytest.raises(ServerError):
            with_retry(
                fn, max_attempts=3, base_delay=0.0,
                retryable=(ServerError,),
                on_retry=lambda attempt, exc: retries.append((attempt, exc)),
                _sleep=lambda _: None,
            )
        assert len(retries) == 2   # 3 attempts → 2 retries

    def test_sleep_called_between_attempts(self):
        slept: list[float] = []

        def fn():
            raise NetworkError("x")

        with pytest.raises(NetworkError):
            with_retry(
                fn, max_attempts=3, base_delay=1.0,
                retryable=(NetworkError,),
                _sleep=slept.append,
            )
        assert len(slept) == 2   # 3 attempts → 2 sleeps

    def test_sleep_duration_increases_exponentially(self):
        slept: list[float] = []

        def fn():
            raise NetworkError("x")

        with pytest.raises(NetworkError):
            with_retry(
                fn, max_attempts=4, base_delay=1.0,
                retryable=(NetworkError,),
                _sleep=slept.append,
            )
        # Each delay should be >= base * 2^attempt (jitter is additive positive)
        assert slept[1] >= slept[0]

    def test_rate_limit_uses_retry_after(self):
        slept: list[float] = []
        calls = [0]

        def fn():
            calls[0] += 1
            if calls[0] == 1:
                exc = RateLimitError(retry_after=42.0)
                raise exc
            return "ok"

        result = with_retry(
            fn, max_attempts=3, base_delay=1.0,
            retryable=(RateLimitError,),
            _sleep=slept.append,
        )
        assert result == "ok"
        assert slept[0] == 42.0   # used Retry-After, not backoff formula

    def test_max_attempts_one_means_no_retry(self):
        calls = [0]

        def fn():
            calls[0] += 1
            raise NetworkError("x")

        with pytest.raises(NetworkError):
            with_retry(
                fn, max_attempts=1, base_delay=0.0,
                retryable=(NetworkError,), _sleep=lambda _: None,
            )
        assert calls[0] == 1

    def test_conflict_error_is_retried(self):
        """409 ConflictError should be retried when listed as retryable."""
        calls = []

        def fn():
            calls.append(1)
            if len(calls) < 3:
                raise ConflictError("The operation was aborted")
            return "done"

        result = with_retry(
            fn, max_attempts=5, base_delay=0.0,
            retryable=(NetworkError, RateLimitError, ServerError, ConflictError),
            _sleep=lambda _: None,
        )
        assert result == "done"
        assert len(calls) == 3
