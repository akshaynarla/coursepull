from __future__ import annotations

import re

from scraper.agents.toolbox import LISTING_CHARS, LinkBook, PageSnapshot, describe_page
from scraper.config import AGENT_TOOL_RESULT_CHARS
from scraper.models import Link, PageOutline

LISTED = re.compile(r"^(L\d+) .*-> (\S+)$", re.MULTILINE)


def _page(n: int) -> PageSnapshot:
    links = [Link(url=f"https://c.edu/w{i}/slides.pdf", text=f"Lecture {i} slides",
                  context_path="Schedule > Fall term", snippet=f"Week {i} | Topic number {i} with a long title | " * 3)
             for i in range(n)]
    return PageSnapshot("https://c.edu/", PageOutline(title="Course", controls=["Syllabus"]), links, False)


def test_listing_never_exceeds_what_the_agent_loop_keeps():
    text = describe_page(_page(150), LinkBook(), 0)
    assert len(text) <= LISTING_CHARS < AGENT_TOOL_RESULT_CHARS


def test_paging_shows_every_link_exactly_once():
    page, book, seen, offset = _page(150), LinkBook(), [], 0
    for _ in range(20):
        text = describe_page(page, book, offset)
        seen += [url for _, url in LISTED.findall(text)]
        more = re.search(r"offset=(\d+)", text)
        if not more:
            break
        offset = int(more.group(1))
    assert seen == [link.url for link in page.links]


def test_asking_again_shows_the_links_again_with_the_same_ids():
    # Run 6: a listing masked out of the Scout's context could never be seen again, because
    # links already shown were not repeated. Asking again must always show them.
    page, book = _page(5), LinkBook()
    first = LISTED.findall(describe_page(page, book, 0))
    again = LISTED.findall(describe_page(page, book, 0))
    assert again == first and [link_id for link_id, _ in again] == ["L1", "L2", "L3", "L4", "L5"]
