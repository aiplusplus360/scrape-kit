"""Retry decisions that know the difference between "slow down" and "no".

Retrying a 404 costs the site a request and us latency, forever, because the
answer never changes. Retrying a 503 is right. Retrying a 429 without honouring
``Retry-After`` is how a scraper gets banned while believing it is being
polite.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import FrozenSet, Mapping, Optional

__all__ = ["RETRYABLE", "RetryPolicy", "retry_after_seconds", "should_retry"]

# 408 Request Timeout, 425 Too Early, 429 Too Many Requests, and the 5xx family
# are transient. 501 and 505 are not: they describe the request, not the mood
# of the server.
RETRYABLE: FrozenSet[int] = frozenset({408, 425, 429, 500, 502, 503, 504})


def should_retry(status: int, retryable: Optional[FrozenSet[int]] = None) -> bool:
    """Return True when ``status`` is worth another attempt."""
    pool = RETRYABLE if retryable is None else retryable
    return status in pool


def retry_after_seconds(
    headers: Mapping[str, str],
    *,
    now: Optional[datetime] = None,
) -> Optional[float]:
    """Parse a ``Retry-After`` header in either of its two legal forms."""
    raw: Optional[str] = None
    for key, value in headers.items():
        if key.lower() == "retry-after":
            raw = str(value).strip()
            break
    if not raw:
        return None
    try:
        seconds = float(raw)
    except ValueError:
        pass
    else:
        return max(0.0, seconds) if seconds == seconds and seconds != float("inf") else None

    try:
        when = parsedate_to_datetime(raw)
    except (TypeError, ValueError, IndexError):
        return None
    if when is None:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    base = now or datetime.now(timezone.utc)
    if base.tzinfo is None:
        base = base.replace(tzinfo=timezone.utc)
    return max(0.0, (when - base).total_seconds())


@dataclass(frozen=True)
class RetryPolicy:
    """Exponential backoff with jitter, capped, and bounded in attempts."""

    attempts: int = 3
    base: float = 0.5
    cap: float = 30.0
    jitter: float = 0.2
    retryable: FrozenSet[int] = RETRYABLE
    honour_retry_after: bool = True

    def __post_init__(self) -> None:
        if self.attempts < 1:
            raise ValueError("attempts must be >= 1")
        if self.base < 0 or self.cap < 0:
            raise ValueError("base and cap must be >= 0")
        if not 0.0 <= self.jitter < 1.0:
            raise ValueError("jitter must be in [0, 1)")

    def backoff(self, attempt: int, rng: Optional[random.Random] = None) -> float:
        """Delay before attempt ``attempt + 1`` (``attempt`` is 1-based)."""
        if attempt < 1:
            raise ValueError("attempt must be >= 1")
        raw = min(self.cap, self.base * (2 ** (attempt - 1)))
        if self.jitter and raw:
            spread = self.jitter
            r = (rng or random).random() * 2 - 1
            raw = raw * (1 + r * spread)
        return max(0.0, min(self.cap, raw))

    def delay(
        self,
        attempt: int,
        headers: Optional[Mapping[str, str]] = None,
        rng: Optional[random.Random] = None,
    ) -> float:
        """Delay before the next attempt, preferring the server's own answer."""
        if self.honour_retry_after and headers:
            told = retry_after_seconds(headers)
            if told is not None:
                return min(self.cap, told)
        return self.backoff(attempt, rng)
