"""Tool implementations. Pure functions; the agent runtime calls them."""

from __future__ import annotations

import logging
from typing import Any

import requests

from ..config import INSPECT_TIMEOUT, MAX_LINKS_IN_FETCH_RESULT, USER_AGENT
from ..extract import extract
from ..fetch import fetch
from ..models import Link

log = logging.getLogger(__name__)


def tool_fetch_page(url: str) -> dict[str, Any]:
    """Fetch a page and return a summary plus a sample of its links."""
    fetched = fetch(url)
    links = extract(fetched.html, fetched.final_url)
    sample = links[:MAX_LINKS_IN_FETCH_RESULT]
    return {
        "final_url": fetched.final_url,
        "used_playwright": fetched.used_playwright,
        "num_links": len(links),
        "links_returned": len(sample),
        "links": [
            {"url": l.url, "text": l.text[:160], "context": l.context_path[:160]}
            for l in sample
        ],
    }


def tool_inspect_url(url: str) -> dict[str, Any]:
    """HEAD-style peek: status, content-type, length, final URL after redirects."""
    headers = {"User-Agent": USER_AGENT}
    try:
        resp = requests.head(url, headers=headers, timeout=INSPECT_TIMEOUT, allow_redirects=True)
    except requests.RequestException as exc:
        return {"error": f"{type(exc).__name__}: {exc}"}

    if resp.status_code in (405, 403, 501) or not resp.headers.get("Content-Type"):
        try:
            resp = requests.get(
                url,
                headers={**headers, "Range": "bytes=0-1024"},
                timeout=INSPECT_TIMEOUT,
                stream=True,
                allow_redirects=True,
            )
        except requests.RequestException as exc:
            return {"error": f"{type(exc).__name__}: {exc}"}

    return {
        "final_url": str(resp.url),
        "status": resp.status_code,
        "content_type": resp.headers.get("Content-Type", ""),
        "content_length": resp.headers.get("Content-Length", ""),
    }


# ---------- helpers used outside the agent loop ----------


def fetch_with_links(url: str) -> tuple[str, list[Link]]:
    """Same as tool_fetch_page but returns full Link objects for the pipeline to keep."""
    fetched = fetch(url)
    links = extract(fetched.html, fetched.final_url)
    return fetched.final_url, links
