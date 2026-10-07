"""Minimal robots.txt support with ``*`` and ``$`` wildcards (RFC 9309).

The standard library parser only does prefix matching, so rules such as
``Disallow: /*?*das[`` would be silently ignored.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from urllib.parse import unquote, urlsplit


@dataclass
class RobotsRules:
    rules: list[tuple[bool, str, re.Pattern]] = field(default_factory=list)
    disallow_all: bool = False

    @classmethod
    def parse(cls, text: str, user_agent: str) -> RobotsRules:
        groups: list[tuple[list[str], list[tuple[bool, str]]]] = []
        agents: list[str] = []
        rules: list[tuple[bool, str]] = []
        for raw_line in text.splitlines():
            line = raw_line.split("#", 1)[0].strip()
            if ":" not in line:
                continue
            key, value = (part.strip() for part in line.split(":", 1))
            key = key.lower()
            if key == "user-agent":
                if rules:  # a user-agent line after rules starts a new group
                    groups.append((agents, rules))
                    agents, rules = [], []
                agents.append(value.lower())
            elif key in ("allow", "disallow") and agents:
                rules.append((key == "allow", value))
        if agents:
            groups.append((agents, rules))

        ua = user_agent.lower()
        specific = [r for a, r in groups if any(agent != "*" and agent in ua for agent in a)]
        chosen = specific or [r for a, r in groups if "*" in a]
        return cls(rules=[
            (allow, pattern, _compile(pattern))
            for group in chosen
            for allow, pattern in group
            if pattern  # an empty Disallow allows everything
        ])

    def can_fetch(self, url: str) -> bool:
        if self.disallow_all:
            return False
        parts = urlsplit(url)
        target = unquote(parts.path or "/") + (f"?{unquote(parts.query)}" if parts.query else "")
        best = None  # (pattern length, allow): the longest match wins, allow wins ties
        for allow, pattern, regex in self.rules:
            if regex.match(target) and (best is None or (len(pattern), allow) > best):
                best = (len(pattern), allow)
        return best is None or best[1]


def _compile(pattern: str) -> re.Pattern:
    pattern = unquote(pattern)
    anchored = pattern.endswith("$")
    if anchored:
        pattern = pattern[:-1]
    regex = ".*".join(re.escape(part) for part in pattern.split("*"))
    return re.compile(regex + ("$" if anchored else ""))
