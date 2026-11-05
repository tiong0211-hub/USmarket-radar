"""Shared retry for collectors: 429, 5xx, timeouts and empty responses retry; other 4xx do not."""
import random
import time
from collections.abc import Callable

import httpx


def with_retry(fn: Callable, backoff: list[float], jitter: float = 1.0):
    for delay in [*backoff, None]:
        try:
            return fn()
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code
            if delay is None or (status < 500 and status != 429):
                raise
        except Exception:
            if delay is None:
                raise
        time.sleep(delay + random.uniform(0, jitter))
