"""scrape-kit: robots.txt compliance, per-host pacing and bounded retries.

>>> from scrapekit import Fetcher, TransportResponse
>>> def transport(url, headers, timeout):
...     return TransportResponse(200, "ok")
>>> Fetcher(transport).get("https://example.invalid/").status
200
"""

from .fetch import BLOCK_ALL, FetchResult, Fetcher, RobotsCache, TransportResponse
from .retry import RETRYABLE, RetryPolicy, retry_after_seconds, should_retry
from .robots import Robots, Rule, parse
from .throttle import Throttle, ThrottleStats, host_of

__all__ = [
    "BLOCK_ALL",
    "FetchResult",
    "Fetcher",
    "RETRYABLE",
    "RetryPolicy",
    "Robots",
    "RobotsCache",
    "Rule",
    "Throttle",
    "ThrottleStats",
    "TransportResponse",
    "host_of",
    "parse",
    "retry_after_seconds",
    "should_retry",
]

__version__ = "1.0.0"
