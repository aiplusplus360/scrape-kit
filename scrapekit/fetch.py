"""A fetcher that is polite by construction.

Ordering matters: check robots.txt _before_ spending a request on a path the
site has excluded, then wait out the host's crawl-delay, then retry only the
failures that are actually transient. Getting that order wrong is why scrapers
get banned while their logs look healthy.

The HTTP transport is injected, so the whole policy is testable without a
network and the same code can drive requests, httpx or a local fixture file.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Callable, Dict, Optional
from urllib.parse import urlsplit

from .retry import RetryPolicy, should_retry
from .robots import Robots, Rule
from .throttle import Throttle, host_of

__all__ = ["TransportResponse", "FetchResult", "Fetcher", "RobotsCache", "BLOCK_ALL"]


@dataclass
class TransportResponse:
    """What a transport must return. Keep it minimal on purpose."""

    status: int
    body: str = ""
    headers: Dict[str, str] = field(default_factory=dict)


Transport = Callable[[str, Dict[str, str], float], TransportResponse]

BLOCK_ALL = Robots(
    agents={"*": [Rule(allow=False, pattern="/")]},
    delays={},
    sitemaps=[],
)


@dataclass
class FetchResult:
    url: str
    status: Optional[int] = None
    body: str = ""
    headers: Dict[str, str] = field(default_factory=dict)
    attempts: int = 0
    blocked: bool = False
    error: Optional[str] = None

    @property
    def ok(self) -> bool:
        return self.status is not None and 200 <= self.status < 300

    @property
    def transient_failure(self) -> bool:
        return self.status is None or should_retry(self.status)


class RobotsCache:
    """Fetches each origin's robots.txt at most once.

    RFC 9309 turns on a distinction most implementations flatten:

    * ``4xx`` -- the file does not exist, so the crawler MAY fetch anything.
    * ``5xx`` or no connection -- we cannot know the policy, so assume the
      whole site is off limits (section 5.2).

    Treating those two the same is either an accidental ban or accidental
    politeness with no upside.
    """

    def __init__(
        self,
        transport: Transport,
        *,
        user_agent: str,
        timeout: float = 10.0,
        max_bytes: int = 500_000,
    ) -> None:
        self._transport = transport
        self.user_agent = user_agent
        self.timeout = timeout
        self.max_bytes = max_bytes
        self._cache: Dict[str, Robots] = {}
        self.fetched: int = 0

    def get(self, url: str) -> Robots:
        parts = urlsplit(url)
        origin = f"{parts.scheme}://{parts.netloc}"
        if origin in self._cache:
            return self._cache[origin]
        robots = self._load(origin)
        self._cache[origin] = robots
        return robots

    def _load(self, origin: str) -> Robots:
        target = origin + "/robots.txt"
        self.fetched += 1
        try:
            response = self._transport(
                target, {"User-Agent": self.user_agent}, self.timeout
            )
        except Exception:  # noqa: BLE001 - transport failures are all "unknown"
            return BLOCK_ALL
        if 200 <= response.status < 300:
            body = (response.body or "")[: self.max_bytes]
            return Robots.parse(body)
        if 400 <= response.status < 500:
            return Robots()
        return BLOCK_ALL

    def clear(self) -> None:
        self._cache.clear()


class Fetcher:
    """Composes robots, pacing and retries around a raw transport."""

    def __init__(
        self,
        transport: Transport,
        *,
        user_agent: str = "scrapekit/1.0 (+https://example.invalid/bot)",
        policy: Optional[RetryPolicy] = None,
        throttle: Optional[Throttle] = None,
        robots: Optional[RobotsCache] = None,
        product: Optional[str] = None,
        respect_robots: bool = True,
        timeout: float = 15.0,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.transport = transport
        self.user_agent = user_agent
        self.policy = policy or RetryPolicy()
        self.throttle = throttle or Throttle()
        self.product = product or user_agent
        self.respect_robots = respect_robots
        self.timeout = timeout
        self._sleep = sleep
        self.robots = robots or RobotsCache(
            transport, user_agent=user_agent, timeout=min(timeout, 10.0)
        )

    def get(self, url: str) -> FetchResult:
        parts = urlsplit(url)
        path = parts.path or "/"
        if parts.query:
            path = f"{path}?{parts.query}"

        delay: Optional[float] = None
        if self.respect_robots:
            rules = self.robots.get(url)
            if not rules.allowed(path, self.product):
                return FetchResult(url=url, blocked=True)
            delay = rules.delay_for(self.product)

        host = host_of(url)
        self.throttle.wait(host, delay)

        headers = {"User-Agent": self.user_agent, "Accept": "*/*"}
        last: Optional[TransportResponse] = None
        error: Optional[str] = None
        attempts = 0

        for attempt in range(1, self.policy.attempts + 1):
            attempts = attempt
            if attempt > 1:
                wait = self.policy.delay(attempt - 1, last.headers if last else None)
                if wait > 0:
                    self._sleep(wait)
            try:
                response = self.transport(url, dict(headers), self.timeout)
            except Exception as exc:  # noqa: BLE001 - retried below
                error = f"{type(exc).__name__}: {exc}"
                last = None
                if attempt == self.policy.attempts:
                    break
                continue
            last = response
            error = None
            if not should_retry(response.status, self.policy.retryable):
                break
            if attempt == self.policy.attempts:
                break

        if last is None:
            return FetchResult(url=url, attempts=attempts, error=error)
        return FetchResult(
            url=url,
            status=last.status,
            body=last.body,
            headers=dict(last.headers),
            attempts=attempts,
            error=error,
        )
