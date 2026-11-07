"""Print the decisions scrape-kit would make, with no network access.

Run from the repository root:

    python examples/policy_demo.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scrapekit import Fetcher, RetryPolicy, Robots, Throttle, TransportResponse  # noqa: E402

ROBOTS = """
User-agent: *
Disallow: /
Allow: /docs
Disallow: /*?session=
Crawl-delay: 2

User-agent: politebot
Allow: /api/public
"""


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0
        self.slept: list = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(round(seconds, 3))
        self.now += seconds


def main() -> int:
    robots = Robots.parse(ROBOTS)
    print("robots.txt decisions")
    print("-" * 60)
    print(f"  {'path':<24}{'curiousbot':<14}{'politebot'}")
    for path in ("/docs/a", "/docs/private/a", "/", "/docs?session=1", "/api/public/x"):
        curious = robots.allowed(path, "curiousbot/1.0")
        polite = robots.allowed(path, "politebot/2.0")
        print(f"  {path:<24}{str(curious):<14}{polite}")
    print()
    print("  curiousbot only has the wildcard group, so longest-match applies:")
    print("    /docs/a        allow (Allow: /docs beats Disallow: /)")
    print("    /              disallow")
    print("    /docs?session= disallow (13-char pattern beats the 5-char allow)")
    print("  politebot has a named group, which replaces the wildcard entirely:")
    print("    every path is allowed except the ones its own rules cover.")

    print()
    print(f"crawl-delay for curiousbot : {robots.delay_for('curiousbot/1.0')}")
    print(f"crawl-delay for politebot  : {robots.delay_for('politebot/2.0')}")

    clock = FakeClock()
    calls: list = []

    def transport(url, headers, timeout):
        calls.append(url)
        if url.endswith("/robots.txt"):
            return TransportResponse(200, ROBOTS)
        if "slow" in url:
            return TransportResponse(503)
        return TransportResponse(200, "<html>ok</html>")

    fetcher = Fetcher(
        transport,
        user_agent="politebot/2.0",
        policy=RetryPolicy(attempts=3, base=1.0, jitter=0.0),
        throttle=Throttle(clock=clock, sleep=clock.sleep),
        sleep=clock.sleep,
    )

    print()
    print("fetch sequence")
    print("-" * 60)
    for url in (
        "https://example.test/docs/report",
        "https://example.test/docs/private/hidden",
        "https://example.test/slow",
        "https://example.test/docs/fast",
    ):
        result = fetcher.get(url)
        status = "BLOCKED" if result.blocked else result.status
        print(f"  {url:<44} status={status} attempts={result.attempts}")

    print()
    print(f"transport calls     : {len(calls)}")
    print(f"sleeps recorded     : {clock.slept}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
