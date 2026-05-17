"""Fetch a course page; escalate to Playwright + Edge for JS-rendered pages."""

from __future__ import annotations

import logging
from dataclasses import dataclass

import requests
from bs4 import BeautifulSoup

from .config import (
    ESCALATE_MIN_ANCHORS,
    ESCALATE_MIN_BODY_CHARS,
    PLAYWRIGHT_TIMEOUT_MS,
    REQUEST_TIMEOUT,
    USER_AGENT,
)

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class FetchResult:
    final_url: str
    html: str
    used_playwright: bool


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
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch(channel="msedge", headless=True)
        try:
            context = browser.new_context(user_agent=USER_AGENT)
            page = context.new_page()
            page.goto(url, wait_until="networkidle", timeout=PLAYWRIGHT_TIMEOUT_MS)
            return page.url, page.content()
        finally:
            browser.close()


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
