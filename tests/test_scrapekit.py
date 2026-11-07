"""Behavioural tests for scrape-kit. No network access required."""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from scrapekit import (
    Fetcher,
    RetryPolicy,
    Robots,
    RobotsCache,
    Throttle,
    TransportResponse,
    host_of,
    retry_after_seconds,
    should_retry,
)
from scrapekit.fetch import BLOCK_ALL

UA = "scrapekit/1.0"


class FakeClock:
    def __init__(self) -> None:
        self.t = 0.0
        self.sleeps = []

    def __call__(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.t += seconds


class ScriptedTransport:
    """Serves scripted responses per URL and records every call."""

    def __init__(self, script):
        self.script = {k: list(v) for k, v in script.items()}
        self.calls = []
        self._cursor = {k: 0 for k in self.script}

    def __call__(self, url, headers, timeout):
        self.calls.append((url, dict(headers)))
        queue = self.script.get(url)
        if queue is None:
            raise AssertionError(f"unexpected URL {url}")
        index = min(self._cursor[url], len(queue) - 1)
        self._cursor[url] += 1
        item = queue[index]
        if isinstance(item, Exception):
            raise item
        return item

    def hits(self, url):
        return sum(1 for call in self.calls if call[0] == url)


class RobotsParsingTests(unittest.TestCase):
    def test_longest_match_wins(self):
        robots = Robots.parse(
            "User-agent: *\nDisallow: /private\nAllow: /private/public\n"
        )
        self.assertFalse(robots.allowed("/private"))
        self.assertFalse(robots.allowed("/private/x"))
        self.assertTrue(robots.allowed("/private/public/a"))

    def test_tie_prefers_allow(self):
        robots = Robots.parse("User-agent: *\nDisallow: /a\nAllow: /a\n")
        self.assertTrue(robots.allowed("/a"))

    def test_unmatched_path_is_allowed(self):
        robots = Robots.parse("User-agent: *\nDisallow: /x\n")
        self.assertTrue(robots.allowed("/y"))

    def test_wildcard_and_anchor(self):
        robots = Robots.parse("User-agent: *\nDisallow: /*.pdf$\n")
        self.assertFalse(robots.allowed("/docs/report.pdf"))
        self.assertTrue(robots.allowed("/docs/report.pdfx"))
        self.assertTrue(robots.allowed("/docs/report.pdf/extra"))

    def test_anchor_only_at_end(self):
        robots = Robots.parse("User-agent: *\nDisallow: /a$b\n")
        self.assertFalse(robots.allowed("/a$b"))
        self.assertTrue(robots.allowed("/ab"))

    def test_named_group_beats_star(self):
        robots = Robots.parse(
            "User-agent: *\nDisallow: /\n\nUser-agent: scrapekit\nAllow: /\n"
        )
        self.assertTrue(robots.allowed("/anything", UA))
        self.assertFalse(robots.allowed("/anything", "OtherBot/2.0"))

    def test_star_is_fallback_only(self):
        robots = Robots.parse("User-agent: other\nDisallow: /\n")
        self.assertTrue(robots.allowed("/x", UA))

    def test_empty_disallow_allows_everything(self):
        robots = Robots.parse("User-agent: *\nDisallow:\n")
        self.assertTrue(robots.allowed("/anything"))
        self.assertFalse(robots.disallow_all())

    def test_shared_group(self):
        robots = Robots.parse("User-agent: alpha\nUser-agent: beta\nDisallow: /x\n")
        self.assertFalse(robots.allowed("/x", "alpha/1"))
        self.assertFalse(robots.allowed("/x", "beta/1"))

    def test_comments_and_blank_lines(self):
        robots = Robots.parse(
            "# top\nUser-agent: *\n\nDisallow: /x   # inline comment\n"
        )
        self.assertFalse(robots.allowed("/x"))
        # robots.txt patterns are prefixes, so "/x" covers "/xyz" too.
        self.assertFalse(robots.allowed("/xyz"))
        self.assertTrue(robots.allowed("/z"))

    def test_crawl_delay_and_sitemaps(self):
        robots = Robots.parse(
            "User-agent: *\nCrawl-delay: 2\nSitemap: https://e.test/s.xml\n"
        )
        self.assertEqual(robots.delay_for(UA), 2.0)
        self.assertEqual(robots.sitemaps, ["https://e.test/s.xml"])

    def test_crawl_delay_prefers_specific_group(self):
        robots = Robots.parse(
            "User-agent: *\nCrawl-delay: 5\n\nUser-agent: scrapekit\nCrawl-delay: 1\n"
        )
        self.assertEqual(robots.delay_for(UA), 1.0)
        self.assertEqual(robots.delay_for("OtherBot"), 5.0)

    def test_bad_crawl_delay_ignored(self):
        robots = Robots.parse("User-agent: *\nCrawl-delay: soon\nDisallow: /x\n")
        self.assertIsNone(robots.delay_for(UA))
        self.assertFalse(robots.allowed("/x"))

    def test_relative_pattern_normalised(self):
        robots = Robots.parse("User-agent: *\nDisallow: x\n")
        self.assertFalse(robots.allowed("/x"))

    def test_disallow_all(self):
        self.assertTrue(Robots.parse("User-agent: *\nDisallow: /\n").disallow_all())
        self.assertFalse(Robots.parse("User-agent: *\nDisallow: /x\n").disallow_all())
        self.assertFalse(Robots.parse("").disallow_all())


class RetryTests(unittest.TestCase):
    def test_status_classification(self):
        for status in (408, 425, 429, 500, 502, 503, 504):
            self.assertTrue(should_retry(status), status)
        for status in (200, 301, 400, 401, 403, 404, 422, 501, 505):
            self.assertFalse(should_retry(status), status)

    def test_retry_after_seconds(self):
        self.assertEqual(retry_after_seconds({"Retry-After": "7"}), 7.0)
        self.assertEqual(retry_after_seconds({"retry-after": "0"}), 0.0)
        self.assertIsNone(retry_after_seconds({}))
        self.assertIsNone(retry_after_seconds({"Retry-After": "soon"}))

    def test_retry_after_http_date(self):
        now = datetime(2026, 1, 1, tzinfo=timezone.utc)
        when = now + timedelta(seconds=42)
        header = {"Retry-After": when.strftime("%a, %d %b %Y %H:%M:%S GMT")}
        value = retry_after_seconds(header, now=now)
        self.assertIsNotNone(value)
        self.assertAlmostEqual(value, 42.0, delta=1.0)

    def test_backoff_grows_and_caps(self):
        policy = RetryPolicy(attempts=6, base=0.5, cap=3.0, jitter=0.0)
        self.assertEqual(policy.backoff(1), 0.5)
        self.assertEqual(policy.backoff(2), 1.0)
        self.assertEqual(policy.backoff(3), 2.0)
        self.assertEqual(policy.backoff(10), 3.0)

    def test_delay_prefers_server_header(self):
        policy = RetryPolicy(base=0.5, cap=30.0, jitter=0.0)
        self.assertEqual(policy.delay(1, {"Retry-After": "9"}), 9.0)
        self.assertEqual(policy.delay(1, None), 0.5)

    def test_jitter_stays_within_band(self):
        policy = RetryPolicy(base=1.0, jitter=0.5)
        import random

        rng = random.Random(7)
        for attempt in range(1, 5):
            nominal = min(policy.cap, policy.base * 2 ** (attempt - 1))
            for _ in range(50):
                value = policy.backoff(attempt, rng)
                self.assertGreaterEqual(value, nominal * 0.5 - 1e-9)
                self.assertLessEqual(value, nominal * 1.5 + 1e-9)

    def test_invalid_policy(self):
        with self.assertRaises(ValueError):
            RetryPolicy(attempts=0)
        with self.assertRaises(ValueError):
            RetryPolicy(jitter=1.0)


class ThrottleTests(unittest.TestCase):
    def test_second_call_waits(self):
        clock = FakeClock()
        throttle = Throttle(default_interval=1.0, clock=clock, sleep=clock.sleep)
        self.assertEqual(throttle.wait("a.test"), 0.0)
        self.assertEqual(throttle.wait("a.test"), 1.0)
        self.assertEqual(clock.sleeps, [1.0])

    def test_hosts_are_independent(self):
        clock = FakeClock()
        throttle = Throttle(default_interval=2.0, clock=clock, sleep=clock.sleep)
        throttle.wait("a.test")
        self.assertEqual(throttle.wait("b.test"), 0.0)
        self.assertEqual(throttle.wait("a.test"), 2.0)

    def test_requested_interval_overrides_default(self):
        clock = FakeClock()
        throttle = Throttle(default_interval=0.0, clock=clock, sleep=clock.sleep)
        throttle.wait("a.test", 3.0)
        self.assertEqual(throttle.wait("a.test"), 3.0)

    def test_max_wait_clamp(self):
        clock = FakeClock()
        throttle = Throttle(default_interval=100.0, max_wait=5.0, clock=clock, sleep=clock.sleep)
        throttle.wait("a.test")
        self.assertEqual(throttle.wait("a.test"), 5.0)

    def test_stats(self):
        clock = FakeClock()
        throttle = Throttle(default_interval=1.0, clock=clock, sleep=clock.sleep)
        throttle.wait("a.test")
        throttle.wait("a.test")
        self.assertEqual(throttle.stats.waits, 1)
        self.assertEqual(throttle.stats.hosts["a.test"], 2)

    def test_host_of(self):
        self.assertEqual(host_of("https://User@Example.COM:8443/a?b#c"), "example.com")
        self.assertEqual(host_of("http://a.test"), "a.test")


class RobotsCacheTests(unittest.TestCase):
    def test_four_xx_means_allow(self):
        transport = ScriptedTransport({
            "https://e.test/robots.txt": [TransportResponse(404)],
        })
        cache = RobotsCache(transport, user_agent=UA)
        self.assertTrue(cache.get("https://e.test/x").allowed("/x", UA))

    def test_five_xx_means_block(self):
        transport = ScriptedTransport({
            "https://e.test/robots.txt": [TransportResponse(503)],
        })
        cache = RobotsCache(transport, user_agent=UA)
        rules = cache.get("https://e.test/x")
        self.assertFalse(rules.allowed("/x", UA))
        self.assertTrue(rules.disallow_all(UA))

    def test_connection_failure_means_block(self):
        transport = ScriptedTransport({
            "https://e.test/robots.txt": [ConnectionError("boom")],
        })
        rules = RobotsCache(transport, user_agent=UA).get("https://e.test/")
        self.assertIs(rules.disallow_all(UA), True)

    def test_fetched_once_per_origin(self):
        transport = ScriptedTransport({
            "https://e.test/robots.txt": [TransportResponse(200, "User-agent: *\n")],
        })
        cache = RobotsCache(transport, user_agent=UA)
        cache.get("https://e.test/a")
        cache.get("https://e.test/b")
        self.assertEqual(transport.hits("https://e.test/robots.txt"), 1)
        self.assertEqual(cache.fetched, 1)

    def test_parsed_body_is_used(self):
        transport = ScriptedTransport({
            "https://e.test/robots.txt": [
                TransportResponse(200, "User-agent: *\nDisallow: /no\n")
            ],
        })
        rules = RobotsCache(transport, user_agent=UA).get("https://e.test/x")
        self.assertFalse(rules.allowed("/no/page", UA))

    def test_block_all_sentinel(self):
        self.assertFalse(BLOCK_ALL.allowed("/", UA))


class FetcherTests(unittest.TestCase):
    def make(self, script, **kwargs):
        transport = ScriptedTransport(script)
        clock = FakeClock()
        fetcher = Fetcher(
            transport,
            user_agent=UA,
            throttle=Throttle(
                default_interval=kwargs.pop("interval", 0.0),
                clock=clock,
                sleep=clock.sleep,
            ),
            sleep=clock.sleep,
            **kwargs,
        )
        return fetcher, transport, clock

    def test_happy_path(self):
        fetcher, transport, _ = self.make({
            "https://e.test/robots.txt": [TransportResponse(200, "")],
            "https://e.test/a": [TransportResponse(200, "body")],
        })
        result = fetcher.get("https://e.test/a")
        self.assertEqual(result.status, 200)
        self.assertEqual(result.body, "body")
        self.assertEqual(result.attempts, 1)
        self.assertTrue(result.ok)
        self.assertEqual(transport.calls[-1][1]["User-Agent"], UA)

    def test_robots_denied_costs_no_request(self):
        fetcher, transport, _ = self.make({
            "https://e.test/robots.txt": [
                TransportResponse(200, "User-agent: *\nDisallow: /secret\n")
            ],
        })
        result = fetcher.get("https://e.test/secret")
        self.assertTrue(result.blocked)
        self.assertIsNone(result.status)
        self.assertEqual(transport.hits("https://e.test/secret"), 0)

    def test_retries_transient_then_succeeds(self):
        fetcher, transport, clock = self.make({
            "https://e.test/robots.txt": [TransportResponse(200, "")],
            "https://e.test/a": [
                TransportResponse(503),
                TransportResponse(502),
                TransportResponse(200, "finally"),
            ],
        })
        result = fetcher.get("https://e.test/a")
        self.assertEqual(result.status, 200)
        self.assertEqual(result.body, "finally")
        self.assertEqual(result.attempts, 3)
        self.assertEqual(transport.hits("https://e.test/a"), 3)
        self.assertEqual(len(clock.sleeps), 2)

    def test_does_not_retry_404(self):
        fetcher, transport, _ = self.make({
            "https://e.test/robots.txt": [TransportResponse(200, "")],
            "https://e.test/missing": [TransportResponse(404)],
        })
        result = fetcher.get("https://e.test/missing")
        self.assertEqual(result.status, 404)
        self.assertEqual(result.attempts, 1)

    def test_gives_up_after_attempts(self):
        fetcher, transport, _ = self.make({
            "https://e.test/robots.txt": [TransportResponse(200, "")],
            "https://e.test/a": [TransportResponse(503)],
        }, policy=RetryPolicy(attempts=2, base=0.0, jitter=0.0))
        result = fetcher.get("https://e.test/a")
        self.assertEqual(result.status, 503)
        self.assertEqual(result.attempts, 2)
        self.assertEqual(transport.hits("https://e.test/a"), 2)

    def test_transport_exception_is_retried(self):
        fetcher, _, _ = self.make({
            "https://e.test/robots.txt": [TransportResponse(200, "")],
            "https://e.test/a": [TimeoutError("slow"), TransportResponse(200, "ok")],
        })
        result = fetcher.get("https://e.test/a")
        self.assertEqual(result.status, 200)
        self.assertEqual(result.attempts, 2)

    def test_all_exceptions_reported(self):
        fetcher, _, _ = self.make({
            "https://e.test/robots.txt": [TransportResponse(200, "")],
            "https://e.test/a": [ConnectionError("down")],
        }, policy=RetryPolicy(attempts=2, base=0.0, jitter=0.0))
        result = fetcher.get("https://e.test/a")
        self.assertIsNone(result.status)
        self.assertIn("ConnectionError", result.error or "")
        self.assertTrue(result.transient_failure)

    def test_crawl_delay_is_applied(self):
        fetcher, _, clock = self.make({
            "https://e.test/robots.txt": [
                TransportResponse(200, "User-agent: *\nCrawl-delay: 3\n")
            ],
            "https://e.test/a": [TransportResponse(200)],
            "https://e.test/b": [TransportResponse(200)],
        })
        fetcher.get("https://e.test/a")
        self.assertEqual(clock.sleeps, [])
        fetcher.get("https://e.test/b")
        self.assertEqual(clock.sleeps, [3.0])

    def test_ignore_robots_skips_fetch(self):
        transport = ScriptedTransport({"https://e.test/a": [TransportResponse(200)]})
        fetcher = Fetcher(
            transport,
            user_agent=UA,
            respect_robots=False,
            sleep=lambda _s: None,
        )
        self.assertEqual(fetcher.get("https://e.test/a").status, 200)
        self.assertEqual(transport.hits("https://e.test/robots.txt"), 0)

    def test_retry_after_header_is_honoured(self):
        fetcher, _, clock = self.make({
            "https://e.test/robots.txt": [TransportResponse(200, "")],
            "https://e.test/a": [
                TransportResponse(429, headers={"Retry-After": "4"}),
                TransportResponse(200),
            ],
        })
        result = fetcher.get("https://e.test/a")
        self.assertEqual(result.status, 200)
        self.assertEqual(clock.sleeps, [4.0])

    def test_query_string_included_in_robots_check(self):
        fetcher, transport, _ = self.make({
            "https://e.test/robots.txt": [
                TransportResponse(200, "User-agent: *\nDisallow: /*?reply=\n")
            ],
        })
        result = fetcher.get("https://e.test/post?reply=1")
        self.assertTrue(result.blocked)
        self.assertEqual(transport.hits("https://e.test/post?reply=1"), 0)


if __name__ == "__main__":
    unittest.main()
