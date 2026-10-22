"""Course offerings (terms): which page is which term, as far as code can tell.

Sites that host several offerings usually say so in two places a model can
overlook: page titles ("CS146S - Fall 2025") and term-switcher links (a link
labelled "Fall 2025" pointing at /fall2025). OfferingMap collects those facts
from every page read; the Orchestrator's term decision is checked against them.
"""

from __future__ import annotations

import re
from urllib.parse import urlparse

from ..identify import identify
from ..models import Link
from ..state import ResourceKind
from .loop import ToolError

_TERM = re.compile(r"\b(fall|spring|summer|winter|autumn)[\s_-]?((?:19|20)\d\d)\b", re.IGNORECASE)
_MAX_LABEL_CHARS = 40  # a term-switcher link is short: "Fall 2025", "Spring 2024 offering"
HOMEPAGE_OFFERING = "the homepage's offering"
COMPLETENESS_MARGIN = 0.8  # another offering must link clearly more material to count as more complete


def term_in(text: str) -> str | None:
    """The one term a text names, normalised ("Fall 2025"); None if it names none or several."""
    found = {f"{m.group(1).title()} {m.group(2)}" for m in _TERM.finditer(text or "")}
    return found.pop() if len(found) == 1 else None


def norm_url(url: str) -> str:
    return url.split("#")[0].rstrip("/").lower()


def material_count(links: list[Link]) -> int:
    """Links to actual course material (slides, documents, files, videos, papers, code), not web pages."""
    return sum(1 for link in links if identify(link.url).kind not in (ResourceKind.WEBPAGE, ResourceKind.UNKNOWN))


class OfferingMap:
    def __init__(self) -> None:
        self.terms: dict[str, str] = {}  # normalised page url -> term
        self.evidence: dict[str, str] = {}
        self.sizes: dict[str, int] = {}  # normalised page url -> course-material links, from complete views only

    def learn(self, page_url: str, title: str, links: list[Link], complete: bool = True) -> None:
        """complete: the whole page was seen (tabs revealed, or none to reveal), so its size is meaningful."""
        if complete:
            self.sizes[norm_url(page_url)] = material_count(links)
        if term := term_in(title):
            self._set(page_url, term, f"its title says {title!r}", strong=True)
        host = urlparse(page_url).netloc.lower()
        for link in links:
            text = " ".join(link.text.split())
            if (0 < len(text) <= _MAX_LABEL_CHARS and (term := term_in(text))
                    and urlparse(link.url).netloc.lower() == host and norm_url(link.url) != norm_url(page_url)):
                self._set(link.url, term, f"{page_url} links to it as {text!r}")

    def _set(self, url: str, term: str, evidence: str, strong: bool = False) -> None:
        key = norm_url(url)
        if strong or key not in self.terms:
            self.terms[key], self.evidence[key] = term, evidence

    def term_of(self, url: str) -> str | None:
        return self.terms.get(norm_url(url))

    def size_note(self, url: str) -> str:
        size = self.sizes.get(norm_url(url))
        return f"{size} course-material links" if size is not None else "not yet read with its tabs revealed"

    def describe(self, page_url: str, links: list[Link]) -> list[str]:
        """Lines for a page listing: what code knows about offerings on this page."""
        lines = []
        if own := self.term_of(page_url):
            lines.append(f"offering: this page is {own} ({self.evidence[norm_url(page_url)]}); "
                         f"{self.size_note(page_url)}")
        linked = {norm_url(link.url): (term, link.url) for link in links
                  if (term := self.term_of(link.url)) and norm_url(link.url) != norm_url(page_url)}
        if linked:
            others = "; ".join(f"{term} -> {url} ({self.size_note(url)})" for term, url in linked.values())
            lines.append(f"other offerings of this course linked here: {others}"
                         + ("" if own else f" (so this page is probably another offering itself; "
                                           f"{self.size_note(page_url)})"))
        return lines

    def offering_sizes(self, homepage: str) -> dict[str, int | None]:
        """Each known offering's size; the homepage counts as an offering of its own if unlabelled."""
        sizes: dict[str, int | None] = {}
        for url, term in self.terms.items():
            size = self.sizes.get(url)
            sizes[term] = max(sizes.get(term) or 0, size) if size is not None else sizes.get(term)
        home = norm_url(homepage)
        if sizes and home not in self.terms:
            sizes[HOMEPAGE_OFFERING] = self.sizes.get(home)
        return sizes


def check_term_choice(term: str, start_urls: list[str], homepage: str, offerings: OfferingMap) -> None:
    """Refuse a term decision that contradicts what the pages say. Raises ToolError with the facts."""
    known = offerings.terms
    if not term:
        others = sorted({t for url, t in known.items() if url != norm_url(homepage)})
        if others:
            raise ToolError(f"this site links other offerings of the course ({', '.join(others)}). Compare their "
                            "schedules and set term to the most complete one.")
        return
    chosen = term_in(term) or term.strip()
    wrong = [f"{u} is {known[norm_url(u)]} ({offerings.evidence[norm_url(u)]})"
             for u in start_urls if known.get(norm_url(u), chosen) != chosen]
    if wrong:
        raise ToolError(f"you chose {chosen}, but " + "; ".join(wrong) + f". Send Scouts only to {chosen} pages.")
    pages = [url for url, t in known.items() if t == chosen]
    if pages and not any(norm_url(u) in pages for u in start_urls):
        raise ToolError(f"the {chosen} offering is at {pages[0]} ({offerings.evidence[pages[0]]}), "
                        "but no Scout starts there.")

    # "Most complete" is checked against counted course-material links, not taken on trust.
    sizes = offerings.offering_sizes(homepage)
    if chosen not in sizes and HOMEPAGE_OFFERING in sizes:
        sizes[chosen] = sizes.pop(HOMEPAGE_OFFERING)  # the chosen term is the homepage's own
    unread = sorted(t for t, size in sizes.items() if size is None)
    if unread:
        pages_to_read = [url for url, t in known.items() if t in unread] or [homepage]
        raise ToolError(f"compare the offerings before choosing: {', '.join(unread)} not yet read with its tabs "
                        f"revealed ({', '.join(pages_to_read)}). Use reveal_page on it.")
    if len(sizes) > 1 and chosen in sizes:
        best, best_size = max(sizes.items(), key=lambda kv: kv[1] or 0)
        if best != chosen and (sizes[chosen] or 0) < COMPLETENESS_MARGIN * best_size:
            raise ToolError(f"{chosen} links {sizes[chosen]} course materials, {best} links {best_size}: "
                            f"the most complete offering is {best}.")
