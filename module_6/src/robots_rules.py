"""
robots_rules.py - robots.txt rules for the Grad Cafe scraper.

JHU EN.605.256 Modern Software Concepts in Python - Module 5 (split out of
scrape.py, Module 2).

The scraper reads robots.txt twice: with urllib.robotparser (the standard
library) and with robots_allows(), an RFC 9309 evaluation.  Grad Cafe's
robots.txt splits its "User-agent: *" rules over two groups and
RobotFileParser keeps only the first one, so the second reading is what sees
the rules in the second group.  RobotsPolicy fetches a URL only when both
readings allow it.
"""

from __future__ import annotations

import re
import urllib.robotparser
from urllib.parse import urlparse


class RobotsPolicy:
    """The robots.txt one scraper obeys, as urllib.robotparser and RFC 9309 read it."""

    def __init__(self) -> None:
        self.parser = urllib.robotparser.RobotFileParser()
        self.text = ""
        self.checked = False

    def load(self, robots_text: str) -> None:
        """Read *robots_text* and enforce it from now on."""
        self.parser.parse(robots_text.splitlines())
        self.text = robots_text
        self.checked = True

    def allows(self, agent_token: str, url: str) -> bool:
        """False when the loaded robots.txt disallows *url* under either reading.

        Before load() nothing has been read, so nothing is refused (the request
        for robots.txt itself passes through here).
        """
        if not self.checked:
            return True
        return (self.parser.can_fetch(agent_token, url)
                and robots_allows(self.text, agent_token, url))


def robots_allows(robots_text: str, agent_token: str, url: str) -> bool:
    """Evaluate robots.txt for *url* with RFC 9309 semantics.

    All groups naming *agent_token* (case-insensitive substring match) are
    merged; if none do, all "User-agent: *" groups are merged instead.  The
    rule with the longest matching path decides, and a tie goes to Allow.
    Unknown directives (Sitemap, Content-Signal, Crawl-delay...) are ignored.
    """
    path = _request_path(url)
    best_length, best_allow = -1, True
    for group_rules in _applicable_rules(_parse_groups(robots_text), agent_token):
        for allow, pattern in group_rules:
            if _pattern_matches(pattern, path):
                length = len(pattern)
                if length > best_length or (length == best_length and allow):
                    best_length, best_allow = length, allow
    return best_allow


def looks_like_robots_file(text: str) -> bool:
    """True for an empty file or one with robots directives; False for HTML or other content."""
    lowered = text.lower()
    if "<html" in lowered or "<!doctype" in lowered:
        return False
    return not text.strip() or "user-agent" in lowered


def _parse_groups(robots_text: str) -> list[tuple[list[str], list[tuple[bool, str]]]]:
    """Split robots.txt into (lower-cased user agents, [(is_allow, path pattern)]) groups.

    Comments and any directive other than User-agent / Allow / Disallow are
    skipped, and an empty Disallow ("no restriction") adds no rule.
    """
    groups: list[tuple[list[str], list[tuple[bool, str]]]] = []
    agents: list[str] = []
    rules: list[tuple[bool, str]] = []
    reading_agents = True
    for raw_line in robots_text.splitlines():
        line = raw_line.split("#", 1)[0].strip()
        if not line or ":" not in line:
            continue
        key, _, value = line.partition(":")
        key, value = key.strip().lower(), value.strip()
        if key == "user-agent":
            if not reading_agents:  # rules were read, so this starts a new group
                groups.append((agents, rules))
                agents, rules = [], []
            agents.append(value.lower())
            reading_agents = True
        elif key in ("allow", "disallow"):
            reading_agents = False
            if value:  # an empty Disallow means "no restriction"
                rules.append((key == "allow", value))
    if agents or rules:
        groups.append((agents, rules))
    return groups


def _applicable_rules(
    groups: list[tuple[list[str], list[tuple[bool, str]]]], agent_token: str
) -> list[list[tuple[bool, str]]]:
    """The rules of every group naming *agent_token*, or of every "*" group if none does."""
    token = agent_token.lower()
    specific = [r for a, r in groups if any(agent != "*" and agent in token for agent in a)]
    return specific or [r for a, r in groups if "*" in a]


def _request_path(url: str) -> str:
    """Path plus query string of *url*: what robots.txt path patterns are matched against."""
    parsed = urlparse(url)
    path = parsed.path or "/"
    if parsed.query:
        path += "?" + parsed.query
    return path


def _pattern_matches(pattern: str, path: str) -> bool:
    """RFC 9309 matching: '*' matches any characters and a trailing '$' anchors the end."""
    regex = re.escape(pattern).replace(r"\*", ".*")
    if regex.endswith(r"\$"):
        regex = regex[:-2] + "$"
    return re.match(regex, path) is not None
