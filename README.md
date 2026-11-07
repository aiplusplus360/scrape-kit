# scrape-kit

**Polite web fetching with none of the guesswork.** RFC 9309 robots.txt, per-host pacing, and retries that know when to stop. Zero dependencies, stdlib only, Python 3.9+.

```console
$ scrapekit fetch https://example.com/pricing
{
  "url": "https://example.com/pricing",
  "status": 200,
  "attempts": 1,
  "bytes": 48213,
  "blocked_by_robots": false,
  "error": null
}
```

---

## The three bugs this library exists to prevent

Every scraping incident I have debugged traced back to one of these. They are all invisible until a site owner notices you.

### 1. robots.txt is not first-match-wins

Most scrapers do this:

```python
for line in robots_txt:
    if line.startswith("Disallow:") and path.startswith(pattern):
        return False     # blocked
```

RFC 9309 2.2.2 says the **longest** matching rule wins, and on a tie **Allow** wins. Consider:

```
User-agent: *
Disallow: /docs
Allow: /docs/public
```

The naive loop blocks `/docs/public/report`, a page the site owner explicitly opened. `scrape-kit` returns `True`, because `Allow: /docs/public` (13 chars) beats `Disallow: /docs` (5 chars):

```python
>>> from scrapekit import Robots
>>> r = Robots.parse("User-agent: *\nDisallow: /docs\nAllow: /docs/public\n")
>>> r.allowed("/docs/public/report")
True
>>> r.allowed("/docs/internal")
False
```

### 2. `4xx` and `5xx` on robots.txt mean opposite things

RFC 9309 draws a line that almost nobody implements:

| robots.txt response | Meaning | Correct behaviour |
| --- | --- | --- |
| `200` | Rules apply | Parse and obey |
| `404` / `4xx` | No robots file exists | Crawler **MAY** access anything |
| `5xx` or no connection | Policy unknown | Assume **complete disallow** (5.2) |

Collapsing those two into "allow" gets you banned the day a site's robots.txt 500s. Collapsing them into "block" makes you refuse to crawl half the web for no reason. `RobotsCache` keeps them distinct and fetches each origin's file exactly once.

### 3. Retrying a `404` forever

Transient means transient. `scrape-kit` retries `408`, `425`, `429`, `500`, `502`, `503`, `504` and nothing else. `501 Not Implemented` and `505 HTTP Version Not Supported` describe the request, not the server's mood, so they are final. A `429` is retried at the delay the server asked for via `Retry-After` (both the seconds form and the HTTP-date form), not at whatever backoff we felt like.

---

## Installation

```bash
pip install git+https://github.com/aiplusplus360/scrape-kit
```

Or copy the `scrapekit/` directory into your project. There is nothing to compile and nothing to pin.

---

## Usage

### Tune the policy, keep your transport

`Fetcher` never opens a socket itself. You pass a transport (`requests`, `httpx`, `urllib`, or a fixture loader in tests) and the library handles everything around it.

```python
import requests
from scrapekit import Fetcher, RetryPolicy, Throttle, TransportResponse

def my_transport(url, headers, timeout):
    resp = requests.get(url, headers=headers, timeout=timeout)
    return TransportResponse(resp.status_code, resp.text, dict(resp.headers))

fetcher = Fetcher(
    my_transport,
    user_agent="mybot/2.1 (+https://mysite.example/bot)",
    policy=RetryPolicy(attempts=4, base=1.0, cap=60.0, jitter=0.2),
    throttle=Throttle(default_interval=0.5),
)

result = fetcher.get("https://example.com/blog")
if result.blocked:
    print("robots.txt disallows this path - no request was sent")
elif result.transient_failure:
    print(f"gave up after {result.attempts} attempts: {result.error}")
else:
    print(result.status, len(result.body))
```

The ordering inside `get()` is the whole point:

```
robots check  ->  per-host pacing  ->  request  ->  retry decision
    (free)        (sleep here)        (1 call)     (only if transient)
```

A robots-blocked URL costs **zero** requests: `result.blocked is True` and `result.attempts == 0`.

### Explain a robots.txt verdict

```console
$ scrapekit robots https://example.com/robots.txt --path /private/drafts
{
  "path": "/private/drafts",
  "allowed": false,
  "crawl_delay": 2.0,
  "sitemaps": ["https://example.com/sitemap.xml"],
  "groups": ["*", "adsbot-google"],
  "rules": 14
}
```

`--path` also accepts a full URL. Local files work too, which makes this usable in CI against a fixture:

```console
$ scrapekit robots ./tests/fixtures/robots.txt --path /admin
```

### Per-host pacing, not one global sleep

A global `time.sleep(1)` is simultaneously too slow for the twenty sites that would accept 10 rps and too fast for the one that asked for one request every three seconds. `Throttle` keeps state per host, and once a host declares a `Crawl-delay` it remembers it for the rest of the run.

```python
from scrapekit import Throttle

throttle = Throttle(default_interval=0.25)
throttle.wait("slow.example", interval=3.0)   # Crawl-delay: 3
throttle.wait("slow.example")                 # still waits 3s
throttle.wait("fast.example")                 # independent, 0.25s
```

Time is injected (`clock=` and `sleep=`), so the tests assert exact wait durations without sleeping.

---

## Testing your own crawler

Every behaviour is driven by injected time and an injected transport, so you can test politeness without touching the network:

```python
class FakeClock:
    def __init__(self): self.t, self.sleeps = 0.0, []
    def __call__(self): return self.t
    def sleep(self, s): self.sleeps.append(s); self.t += s

clock = FakeClock()
fetcher = Fetcher(transport, throttle=Throttle(clock=clock, sleep=clock.sleep), sleep=clock.sleep)
fetcher.get("https://example.com/a")
fetcher.get("https://example.com/b")
assert clock.sleeps == [3.0]   # Crawl-delay honoured, deterministically
```

---

## What it deliberately does not do

- **No HTML parsing.** Use `selectolax` or `lxml`; this is the layer underneath.
- **No async.** The policy is transport-agnostic, so wrapping it in `asyncio` is a transport decision, not a library one.
- **No distributed coordination.** In-process pacing only. If you run ten workers you need a shared store, and you should build it around these same rules.
- **No on-disk robots cache.** Fresh per process, which is what the spec's caching guidance permits for short-lived jobs.

---

## Architecture

```
scrapekit/
  robots.py     RFC 9309 parser: groups, longest-match, wildcards, $ anchor, crawl-delay
  throttle.py   Per-host minimum interval with injected clock
  retry.py      Status classification, Retry-After parsing, capped exponential backoff
  fetch.py      RobotsCache + Fetcher; the composition and its ordering
  cli.py        scrapekit robots | fetch
```

Each module is independent. `Robots` has no idea a network exists; `Throttle` has no idea what a retry is; `Fetcher` is the only file that knows the order of operations.

---

## Tests

```bash
python -m unittest discover -s tests -v
```

49 tests, no network, no dependencies, no flakiness, including the exact timestamp arithmetic for `Retry-After` HTTP dates, the jitter bound, and the CLI refusing a filesystem path where a URL path was expected.

---

## License

MIT (c) 2026 aiplusplus360
