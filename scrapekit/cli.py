"""Command line interface for scrape-kit."""

from __future__ import annotations

import argparse
import json
import sys
from typing import Dict, Optional, Sequence
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from .fetch import Fetcher, RobotsCache, TransportResponse
from .retry import RetryPolicy
from .robots import Robots
from .throttle import Throttle

DEFAULT_UA = "scrapekit/1.0 (+https://github.com/aiplusplus360/scrape-kit)"


def urllib_transport(url: str, headers: Dict[str, str], timeout: float) -> TransportResponse:
    request = Request(url, headers=headers)
    try:
        with urlopen(request, timeout=timeout) as response:  # noqa: S310 - user supplied
            body = response.read(2_000_000)
            return TransportResponse(
                status=int(getattr(response, "status", 200)),
                body=body.decode("utf-8", "replace"),
                headers={k.lower(): v for k, v in response.headers.items()},
            )
    except HTTPError as exc:
        try:
            body = exc.read().decode("utf-8", "replace")
        except Exception:  # noqa: BLE001
            body = ""
        return TransportResponse(
            status=int(exc.code),
            body=body,
            headers={k.lower(): v for k, v in (exc.headers or {}).items()},
        )
    except URLError as exc:
        raise ConnectionError(str(exc.reason)) from exc


def _path_of(url: str) -> str:
    parts = urlsplit(url)
    path = parts.path or "/"
    return f"{path}?{parts.query}" if parts.query else path


def _load_robots(target: str, ua: str, timeout: float) -> Robots:
    if "://" not in target:
        with open(target, "r", encoding="utf-8", errors="replace") as handle:
            return Robots.parse(handle.read())
    cache = RobotsCache(urllib_transport, user_agent=ua, timeout=timeout)
    return cache.get(target)


def cmd_robots(args: argparse.Namespace) -> int:
    robots = _load_robots(args.target, args.user_agent, args.timeout)
    path = args.path or (_path_of(args.target) if "://" in args.target else "/")
    verdict = {
        "path": path,
        "allowed": robots.allowed(path, args.product or args.user_agent),
        "crawl_delay": robots.delay_for(args.product or args.user_agent),
        "sitemaps": robots.sitemaps,
        "groups": sorted(robots.agents),
        "rules": len(list(robots)),
    }
    print(json.dumps(verdict, indent=2))
    return 0


def cmd_fetch(args: argparse.Namespace) -> int:
    fetcher = Fetcher(
        urllib_transport,
        user_agent=args.user_agent,
        policy=RetryPolicy(attempts=args.attempts),
        throttle=Throttle(default_interval=args.interval),
        respect_robots=not args.ignore_robots,
        timeout=args.timeout,
    )
    result = fetcher.get(args.url)
    payload = {
        "url": result.url,
        "status": result.status,
        "attempts": result.attempts,
        "bytes": len(result.body),
        "blocked_by_robots": result.blocked,
        "error": result.error,
    }
    print(json.dumps(payload, indent=2))
    if result.blocked:
        return 3
    if result.error or result.status is None:
        return 2
    return 0 if result.ok else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="scrapekit",
        description="Polite fetching: RFC 9309 robots.txt, per-host pacing, bounded retries.",
    )
    parser.add_argument("--user-agent", default=DEFAULT_UA)
    parser.add_argument("--timeout", type=float, default=15.0)
    sub = parser.add_subparsers(dest="command", required=True)

    robots = sub.add_parser("robots", help="explain the robots.txt verdict for a path")
    robots.add_argument("target", help="robots.txt URL or local file")
    robots.add_argument("--path", default=None)
    robots.add_argument("--product", default=None)
    robots.set_defaults(func=cmd_robots)

    fetch = sub.add_parser("fetch", help="perform one polite, retried GET")
    fetch.add_argument("url")
    fetch.add_argument("--attempts", type=int, default=3)
    fetch.add_argument("--interval", type=float, default=0.0)
    fetch.add_argument("--ignore-robots", action="store_true")
    fetch.set_defaults(func=cmd_fetch)
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except FileNotFoundError as exc:
        print(f"error: no such file: {exc.filename}", file=sys.stderr)
        return 2
    except (ConnectionError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
