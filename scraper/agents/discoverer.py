"""Discoverer agent: crawls the course site for pages worth classifying."""

from __future__ import annotations

import json
import logging
from typing import Any, Callable

from ..config import MAX_PAGES_TO_CRAWL, MAX_STEPS_DISCOVERER
from ..models import Link
from .runtime import AgentRunner
from .tools import fetch_with_links

log = logging.getLogger(__name__)


SYSTEM = """\
You are the Discoverer agent. You are given the URL of a university course homepage.

GOAL: find every additional page on the same course site that likely contains course
resources (lecture pages, schedule subpages, readings pages, "Week N" subpages, etc.),
and return the list of all pages to be classified.

TOOL:
- fetch_page(url): fetches a page and returns its final URL, total link count, and
  a sample of links (each with text + heading context).

PROCEDURE:
1. Call fetch_page on the course URL.
2. Look at the returned links. Identify links that are NAVIGATION to other course
   subpages — usually internal to the same domain, with anchor text like
   "Schedule", "Lectures", "Readings", "Week 5", "Assignments", "Course materials".
3. For each promising subpage, call fetch_page on it.
4. DO NOT call fetch_page on links to PDFs, github repos, arxiv, youtube, google
   drive, blog posts, or external sites. Those are RESOURCES, not pages to crawl.
5. Stop when you have explored every promising internal subpage.

LIMITS:
- At most 15 fetch_page calls total. Be selective.
- If a page returns 0 links or looks like an error, do not retry it.

RETURN: a JSON object with one key, "pages", whose value is a list of all URLs you
fetched (including the original course URL). Example:
  {"pages": ["https://course.edu/", "https://course.edu/schedule"]}
No prose. No markdown fences. Just the JSON object.
"""

TOOLS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "fetch_page",
            "description": "Fetch a page and return its final URL, link count, and a sample of links.",
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "URL of the page to fetch."},
                },
                "required": ["url"],
            },
        },
    },
]


def run_discoverer(
    course_url: str,
    runner: AgentRunner,
    log_callback: Callable[[str], None],
) -> dict[str, list[Link]]:
    """Run the Discoverer; returns {final_url: list[Link]} for each crawled page."""
    crawled: dict[str, list[Link]] = {}

    def dispatch(name: str, args: dict[str, Any]) -> Any:
        if name != "fetch_page":
            return f"ERROR: unknown tool {name!r}"
        url = args.get("url", "").strip()
        if not url:
            return "ERROR: missing url"
        if url in crawled:
            return {
                "already_fetched": True,
                "num_links": len(crawled[url]),
            }
        if len(crawled) >= MAX_PAGES_TO_CRAWL:
            return f"ERROR: crawl cap of {MAX_PAGES_TO_CRAWL} pages reached"

        final_url, links = fetch_with_links(url)
        crawled[final_url] = links
        log_callback(f"  Discoverer crawled {final_url} ({len(links)} links)")
        sample = links[:60]
        return {
            "final_url": final_url,
            "num_links": len(links),
            "links_returned": len(sample),
            "links": [
                {"url": l.url, "text": l.text[:120], "context": l.context_path[:120]}
                for l in sample
            ],
        }

    user = (
        f"Course URL: {course_url}\n\n"
        "Call fetch_page on this URL first, then crawl promising internal subpages."
    )
    result = runner.run(
        agent_name="Discoverer",
        system=SYSTEM,
        user=user,
        tools=TOOLS,
        dispatch=dispatch,
        max_steps=MAX_STEPS_DISCOVERER,
        log_callback=log_callback,
    )

    # Try to parse the agent's final pages list; fall back to whatever it actually crawled.
    pages: list[str] = []
    try:
        data = json.loads(result.final_message)
        if isinstance(data, dict) and isinstance(data.get("pages"), list):
            pages = [p for p in data["pages"] if isinstance(p, str)]
    except json.JSONDecodeError:
        log.debug("Discoverer final message was not JSON: %r", result.final_message[:200])

    # Make sure every reported page is one we actually crawled (de-hallucinate).
    pages = [p for p in pages if p in crawled] or list(crawled.keys())
    log_callback(f"Discoverer done: {len(pages)} page(s), {sum(len(crawled[p]) for p in pages)} total links")
    return {p: crawled[p] for p in pages}
