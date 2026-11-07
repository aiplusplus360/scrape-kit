"""robots.txt parser that follows RFC 9309 rather than folklore.

The single most common robots.txt bug in scrapers is "first matching rule
wins". RFC 9309 section 2.2.2 says the *longest* matching rule wins, and that
on a tie the ``allow`` rule wins. Those two sentences mean the naive
implementation silently blocks URLs the site owner explicitly permitted, which
is the difference between respecting a site and crawling it badly.

Group selection is equally easy to get wrong: the group whose ``user-agent``
token is the longest case-insensitive substring of our product token wins, and
``*`` is only used when no named group matches.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, Iterator, List, Optional, Sequence

__all__ = ["Rule", "Robots", "parse"]


def _compile(pattern: str) -> "re.Pattern[str]":
    """Translate a robots.txt path pattern into a regex.

    ``*`` matches any run of characters, ``$`` anchors the end of the path, and
    everything else is literal. A ``$`` anywhere but the last position is an
    ordinary character.
    """
    anchored = pattern.endswith("$")
    body = pattern[:-1] if anchored else pattern
    regex = "^" + ".*".join(re.escape(chunk) for chunk in body.split("*"))
    if anchored:
        regex += "$"
    return re.compile(regex, re.DOTALL)


@dataclass(frozen=True)
class Rule:
    """A single allow/disallow directive."""

    allow: bool
    pattern: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "_regex", _compile(self.pattern))

    def matches(self, path: str) -> bool:
        return bool(self._regex.match(path))

    @property
    def weight(self) -> int:
        return len(self.pattern)


def _strip_comment(line: str) -> str:
    idx = line.find("#")
    return (line[:idx] if idx >= 0 else line).strip()


def _normalise_path(path: str) -> str:
    if not path:
        return "/"
    if not path.startswith("/"):
        slash = path.find("/")
        path = path[slash:] if slash >= 0 else "/" + path
    return path


@dataclass
class Robots:
    """Parsed robots.txt content."""

    agents: Dict[str, List[Rule]] = field(default_factory=dict)
    delays: Dict[str, float] = field(default_factory=dict)
    sitemaps: List[str] = field(default_factory=list)

    # -- construction -----------------------------------------------------

    @classmethod
    def parse(cls, text: str) -> "Robots":
        agents: Dict[str, List[Rule]] = {}
        delays: Dict[str, float] = {}
        sitemaps: List[str] = []

        current: List[str] = []
        rules: List[Rule] = []
        seen_directive = False

        def flush() -> None:
            nonlocal current, rules, seen_directive
            for token in current:
                agents[token] = list(rules)
            current, rules, seen_directive = [], [], False

        for raw in text.splitlines():
            line = _strip_comment(raw)
            if not line or ":" not in line:
                continue
            key, _, value = line.partition(":")
            key, value = key.strip().lower(), value.strip()

            if key == "user-agent":
                if seen_directive:
                    flush()
                if value:
                    current.append(value.lower())
                continue

            if not current:
                # Directives before the first user-agent line are only valid
                # for sitemap, which is global.
                if key == "sitemap" and value:
                    sitemaps.append(value)
                continue

            if key in ("allow", "disallow"):
                seen_directive = True
                if key == "disallow" and not value:
                    # "Disallow:" (empty) means allow everything and must not
                    # become a rule that matches every path.
                    continue
                if value:
                    rules.append(Rule(allow=key == "allow",
                                      pattern=_normalise_path(value)))
                continue

            if key == "crawl-delay":
                seen_directive = True
                try:
                    delay = float(value)
                except ValueError:
                    continue
                if delay >= 0:
                    for token in current:
                        delays[token] = delay
                continue

            if key == "sitemap":
                if value:
                    sitemaps.append(value)
                seen_directive = True
                continue

            # Unknown directive: ignored, and it does not break the group.
            seen_directive = True

        flush()
        return cls(agents=agents, delays=delays, sitemaps=sitemaps)

    # -- queries ----------------------------------------------------------

    def _select(self, product: str) -> List[Rule]:
        product = product.lower()
        best_len = -1
        best: List[Rule] = []
        star: Optional[List[Rule]] = None
        for token, rules in self.agents.items():
            if token == "*":
                if star is None:
                    star = rules
                continue
            if token in product and len(token) > best_len:
                best_len, best = len(token), rules
        if best_len >= 0:
            return best
        return star if star is not None else []

    def allowed(self, path: str, product: str = "*") -> bool:
        """True when ``product`` may fetch ``path``.

        The longest matching pattern wins; on a tie Allow beats Disallow.
        """
        path = _normalise_path(path)
        winner: Optional[Rule] = None
        for rule in self._select(product):
            if not rule.matches(path):
                continue
            if winner is None or rule.weight > winner.weight:
                winner = rule
            elif rule.weight == winner.weight and rule.allow and not winner.allow:
                winner = rule
        return True if winner is None else winner.allow

    def delay_for(self, product: str) -> Optional[float]:
        product = product.lower()
        best_len = -1
        found: Optional[float] = None
        for token, delay in self.delays.items():
            if token == "*":
                if found is None:
                    found = delay
                continue
            if token in product and len(token) > best_len:
                best_len, found = len(token), delay
        return found

    def disallow_all(self, product: str = "*") -> bool:
        """True when the site disallows the root path, i.e. do not crawl."""
        return not self.allowed("/", product)

    def rules_for(self, product: str = "*") -> Sequence[Rule]:
        return tuple(self._select(product))

    def __iter__(self) -> Iterator[Rule]:
        for rules in self.agents.values():
            yield from rules


def parse(text: str) -> Robots:
    return Robots.parse(text)
