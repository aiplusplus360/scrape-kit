"""Per-host pacing.

A single global rate limit is the wrong shape: it makes us slow against the
twenty sites that would happily take a request every 100 ms, and still rude to
the one site that asked for one every three seconds. Politeness is per origin.

Time is injected so tests are deterministic and never actually sleep.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Callable, Dict, Optional

__all__ = ["Throttle", "ThrottleStats"]


@dataclass
class ThrottleStats:
    waits: int = 0
    slept: float = 0.0
    hosts: Dict[str, int] = field(default_factory=dict)

    def record(self, host: str) -> None:
        self.hosts[host] = self.hosts.get(host, 0) + 1


class Throttle:
    """Ensures a minimum interval between requests to the same host."""

    def __init__(
        self,
        *,
        default_interval: float = 0.0,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        max_wait: float = 300.0,
    ) -> None:
        if default_interval < 0:
            raise ValueError("default_interval must be >= 0")
        if max_wait < 0:
            raise ValueError("max_wait must be >= 0")
        self.default_interval = default_interval
        self.max_wait = max_wait
        self._clock = clock
        self._sleep = sleep
        self._next: Dict[str, float] = {}
        self._intervals: Dict[str, float] = {}
        self.stats = ThrottleStats()

    def interval_for(self, host: str, requested: Optional[float]) -> float:
        # A host that asked for crawl-delay 3 keeps that pacing until it says
        # otherwise; forgetting it between calls silently halves the delay.
        if requested is not None:
            self._intervals[host] = max(self.default_interval, requested)
        return max(self.default_interval, self._intervals.get(host, 0.0))

    def wait(self, host: str, interval: Optional[float] = None) -> float:
        """Block until ``host`` may be contacted again; returns seconds slept."""
        gap = self.interval_for(host, interval)
        if gap <= 0:
            self.stats.record(host)
            return 0.0
        now = self._clock()
        ready = self._next.get(host, 0.0)
        waited = 0.0
        if ready > now:
            waited = min(ready - now, self.max_wait)
            self._sleep(waited)
            now = self._clock()
        self._next[host] = now + gap
        if waited > 0:
            self.stats.waits += 1
            self.stats.slept += waited
        self.stats.record(host)
        return waited

    def reset(self) -> None:
        self._next.clear()


def host_of(url: str) -> str:
    """Cheap host extractor that avoids punting to urllib for the common case."""
    rest = url.split("://", 1)[-1]
    for sep in ("/", "?", "#"):
        idx = rest.find(sep)
        if idx >= 0:
            rest = rest[:idx]
    if "@" in rest:
        rest = rest.rsplit("@", 1)[-1]
    if ":" in rest:
        rest = rest.split(":", 1)[0]
    return rest.lower()
