"""Extract links with a brief heading-based context path."""

from __future__ import annotations

from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup, Tag

from .models import Link

_SKIP_SCHEMES = ("javascript:", "mailto:", "tel:")


def _nearest_headings(anchor: Tag) -> list[str]:
    parts: list[str] = []
    for level in ("h1", "h2", "h3"):
        for prev in anchor.find_all_previous(level):
            text = prev.get_text(" ", strip=True)
            if text:
                parts.append(text)
                break
    return parts


def extract(html: str, base_url: str) -> list[Link]:
    soup = BeautifulSoup(html, "html.parser")
    seen: dict[str, Link] = {}

    for a in soup.find_all("a"):
        href = (a.get("href") or "").strip()
        if not href or href.startswith("#") or href.lower().startswith(_SKIP_SCHEMES):
            continue
        absolute = urljoin(base_url, href)
        parsed = urlparse(absolute)
        if parsed.scheme not in ("http", "https"):
            continue
        clean = parsed._replace(fragment="").geturl()
        if clean in seen:
            continue
        seen[clean] = Link(
            url=clean,
            text=a.get_text(" ", strip=True),
            context_path=" > ".join(_nearest_headings(a)),
        )

    return list(seen.values())
