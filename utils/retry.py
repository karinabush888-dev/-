from __future__ import annotations

from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential


def with_retry(max_attempts: int = 5, min_wait: float = 0.5, max_wait: float = 8.0):
    return retry(
        reraise=True,
        stop=stop_after_attempt(max_attempts),
        wait=wait_exponential(multiplier=1, min=min_wait, max=max_wait),
        retry=retry_if_exception_type((TimeoutError, ConnectionError, OSError)),
    )
