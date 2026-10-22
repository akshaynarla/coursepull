"""Fetch a course page; escalate to Playwright + Edge for JS-rendered pages."""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass

import requests
from bs4 import BeautifulSoup

from .browser import open_page
from .config import (
    CONTROL_CLICK_SETTLE_MS,
    ESCALATE_MIN_ANCHORS,
    ESCALATE_MIN_BODY_CHARS,
    MAX_CONTROL_CLICKS,
    PLAYWRIGHT_TIMEOUT_MS,
    REQUEST_TIMEOUT,
    USER_AGENT,
)
from .extract import CONTROL_DENY

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class FetchResult:
    final_url: str
    html: str
    used_playwright: bool


@dataclass(frozen=True)
class PageView:
    label: str  # "" for the page as loaded, else the control clicked to reveal this view
    html: str


def _looks_empty(html: str) -> bool:
    soup = BeautifulSoup(html, "html.parser")
    body = soup.body
    if body is None:
        return True
    text_len = len(body.get_text(strip=True))
    anchor_count = len(soup.find_all("a"))
    return text_len < ESCALATE_MIN_BODY_CHARS or anchor_count < ESCALATE_MIN_ANCHORS


def _fetch_requests(url: str) -> tuple[str, str]:
    resp = requests.get(
        url,
        headers={"User-Agent": USER_AGENT},
        timeout=REQUEST_TIMEOUT,
        allow_redirects=True,
    )
    resp.raise_for_status()
    return str(resp.url), resp.text


def _fetch_playwright(url: str) -> tuple[str, str]:
    with open_page() as page:
        page.goto(url, wait_until="networkidle", timeout=PLAYWRIGHT_TIMEOUT_MS)
        return page.url, page.content()


def fetch(url: str) -> FetchResult:
    try:
        final_url, html = _fetch_requests(url)
    except requests.RequestException as exc:
        log.warning("requests failed for %s: %s; using Playwright/Edge", url, exc)
        final_url, html = _fetch_playwright(url)
        return FetchResult(final_url, html, used_playwright=True)

    if _looks_empty(html):
        log.info("Page %s looks JS-rendered; escalating to Playwright/Edge", url)
        final_url, html = _fetch_playwright(final_url)
        return FetchResult(final_url, html, used_playwright=True)

    return FetchResult(final_url, html, used_playwright=False)


# Tags every clickable control that may reveal hidden content with data-cp-ctl=<n>
# and returns their labels. Mirrors extract.find_controls.
_MARK_CONTROLS_JS = """
({deny, maxCount, maxChars}) => {
  const denyRe = new RegExp(deny, 'i');
  const seen = new Set();
  const labels = [];
  const els = document.querySelectorAll('[role="tab"], button, summary, [aria-expanded="false"]');
  for (const el of els) {
    if (labels.length >= maxCount) break;
    if (el.closest('form') || el.tagName === 'A') continue;
    const r = el.getBoundingClientRect();
    if (r.width === 0 || r.height === 0) continue;
    const label = (el.innerText || el.textContent || '').replace(/\\s+/g, ' ').trim();
    if (!label || label.length > maxChars || denyRe.test(label) || seen.has(label)) continue;
    seen.add(label);
    el.setAttribute('data-cp-ctl', String(labels.length));
    labels.push(label);
  }
  return labels;
}
"""


def render_interactive(url: str, max_clicks: int = MAX_CONTROL_CLICKS) -> tuple[str, list[PageView]]:
    """Render a page in Edge, click each tab/accordion control, and return every distinct view.

    Course sites often keep the schedule behind a client-side "Syllabus" tab that has
    no URL of its own; plain fetching never sees those links.
    """
    from playwright.sync_api import Error as PlaywrightError

    def mark(page) -> list[str]:
        return page.evaluate(
            _MARK_CONTROLS_JS,
            {"deny": CONTROL_DENY.pattern, "maxCount": max_clicks, "maxChars": 40},
        )

    def settle(page) -> None:
        page.wait_for_timeout(CONTROL_CLICK_SETTLE_MS)
        try:
            page.wait_for_load_state("networkidle", timeout=3000)
        except PlaywrightError:
            pass

    with open_page() as page:
        page.goto(url, wait_until="networkidle", timeout=PLAYWRIGHT_TIMEOUT_MS)
        final_url = page.url
        html = page.content()
        views = [PageView("", html)]
        digests = {hashlib.sha1(html.encode("utf-8")).digest()}

        labels = mark(page)
        for i, label in enumerate(labels):
            try:
                page.locator(f'[data-cp-ctl="{i}"]').first.click(timeout=3000)
            except PlaywrightError as exc:
                log.debug("could not click control %r on %s: %s", label, url, exc)
                continue
            settle(page)
            if page.url.split("#")[0] != final_url.split("#")[0]:
                # The control navigated away; what it leads to is a separate page.
                log.debug("control %r on %s navigated to %s", label, url, page.url)
                page.goto(final_url, wait_until="networkidle", timeout=PLAYWRIGHT_TIMEOUT_MS)
                mark(page)
                continue
            html = page.content()
            digest = hashlib.sha1(html.encode("utf-8")).digest()
            if digest not in digests:
                digests.add(digest)
                views.append(PageView(label, html))
        return final_url, views
