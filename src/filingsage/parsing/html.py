"""HTML -> plain text line extraction.

selectolax (Lexbor bindings) over BeautifulSoup: filings run 100KB-10MB and
this pipeline parses years of filings; selectolax parses roughly an order of
magnitude faster, and here we only need clean linear text, not DOM traversal
APIs BeautifulSoup would offer for editing.
"""

from __future__ import annotations

from selectolax.lexbor import LexborHTMLParser

_DROP_TAGS = {"script", "style", "head", "noscript"}


def html_to_lines(html: str) -> list[str]:
    """Render HTML to a flat list of visually-separate text lines.

    block-level tags force a line break (matching how a browser renders
    them); everything else concatenates. This is what makes "Item 1A." and
    "Risk Factors" — sitting in adjacent <span> or <div> elements — come out
    as two distinct lines instead of one run-on string.
    """
    tree = LexborHTMLParser(html)
    for tag in _DROP_TAGS:
        for node in tree.css(tag):
            node.decompose()

    text = tree.body.text(separator="\n", deep=True) if tree.body else ""
    lines = [ln.strip() for ln in text.replace("\xa0", " ").split("\n")]
    return [ln for ln in lines if ln]