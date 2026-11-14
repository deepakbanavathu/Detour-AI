"""Text helpers shared by the pollers: HTML to text, relevance, advisory sentence splitting."""
from __future__ import annotations

import re
from html import unescape
from html.parser import HTMLParser

RELEVANT = re.compile(
    r"traffic|divert|diversion|water ?logg|flood|road closed|closure|restriction|advisory|flyover|underpass|"
    r"procession|rally|vip movement|convoy|tree fall|uprooted|road work|accident", re.I)
ACTION = re.compile(
    r"diverted|divert\b|closed|restricted|not allowed|stopped|water ?logg|flood|inundat|restored|cleared|lifted|reopened", re.I)
_SENT = re.compile(r"(?<=[.!?;])\s+|\n+|\s+[•\-\u2022]\s+")
_WS = re.compile(r"\s+")


class _P(HTMLParser):
    def __init__(self):
        super().__init__()
        self.out: list[str] = []
        self.skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style", "noscript", "nav", "footer", "header", "aside", "form"):
            self.skip += 1
        if tag in ("p", "li", "br", "h1", "h2", "h3", "div"):
            self.out.append("\n")

    def handle_endtag(self, tag):
        if tag in ("script", "style", "noscript", "nav", "footer", "header", "aside", "form") and self.skip:
            self.skip -= 1

    def handle_data(self, data):
        if not self.skip:
            self.out.append(data)


def html_to_text(html: str) -> str:
    p = _P()
    p.feed(html or "")
    text = unescape("".join(p.out))
    return "\n".join(_WS.sub(" ", line).strip() for line in text.splitlines() if line.strip())


def relevant(text: str) -> bool:
    return bool(RELEVANT.search(text or ""))


def advisory_sentences(text: str, max_sentences: int = 12) -> list[str]:
    """The sentences of an article that state an action on a road; each becomes one IR attempt."""
    out = []
    for s in _SENT.split(text or ""):
        s = s.strip()
        if 25 <= len(s) <= 400 and ACTION.search(s):
            out.append(s)
        if len(out) >= max_sentences:
            break
    return out
