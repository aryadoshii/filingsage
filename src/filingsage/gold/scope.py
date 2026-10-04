"""Infer which company a question is about, when the asker didn't say.

"How is Amazon doing this quarter?" searched every company's filings, so
the best-matching passages could come from anyone. If the question names
exactly one tracked company — by ticker or by name — retrieval is scoped
to it. Anything ambiguous (two companies named, or none) stays unscoped:
a wrong guess would silently hide the right answer, which is worse than a
broad search.

Deliberately simple string matching, not an LLM call: it runs before
retrieval on every question, must be instant and free, and its behavior has
to be predictable enough to test exhaustively. The spec's LangGraph planner
(roadmap L9) is where smarter scoping belongs.
"""

from __future__ import annotations

import re

# Words that start company names but don't identify one.
_GENERIC = frozenset({"the", "inc", "corp", "co", "company", "group", "holdings", "international"})

# Well-known names that aren't the legal name EDGAR uses.
ALIASES: dict[str, str] = {
    "google": "GOOGL",
    "facebook": "META",
    "instagram": "META",
    "whatsapp": "META",
}

_WORD_RE = re.compile(r"[A-Za-z][A-Za-z0-9.&-]*")


def _name_key(name: str) -> str | None:
    """First distinctive word of a company name, lower-cased:
    'AMAZON COM INC' -> 'amazon', 'JPMORGAN CHASE & CO' -> 'jpmorgan'."""
    for word in re.findall(r"[A-Za-z]+", name):
        lowered = word.lower()
        if lowered not in _GENERIC and len(lowered) >= 3:
            return lowered
    return None


def detect_ticker(question: str, companies: list[tuple[str, str]]) -> str | None:
    """The one tracked ticker the question names, else None.

    `companies` is [(ticker, name)]. A ticker matches only when written as
    a standalone word in capitals (or with a $ prefix), so "V" in a sentence
    doesn't match Visa but "$V" does.
    """
    tracked = {ticker.upper() for ticker, _ in companies}
    names = {_name_key(name): ticker.upper() for ticker, name in companies if _name_key(name)}
    found: set[str] = set()

    for match in re.finditer(r"\$?[A-Za-z][A-Za-z0-9.]*", question):
        token = match.group()
        bare = token.lstrip("$").rstrip(".")
        if bare.upper() in tracked and (token.startswith("$") or (bare.isupper() and len(bare) >= 2)):
            found.add(bare.upper())

    for word in _WORD_RE.findall(question.lower()):
        word = word.strip(".&-'")
        word = re.sub(r"'s$", "", word)
        if word in names:
            found.add(names[word])
        elif word in ALIASES and ALIASES[word] in tracked:
            found.add(ALIASES[word])

    return found.pop() if len(found) == 1 else None
