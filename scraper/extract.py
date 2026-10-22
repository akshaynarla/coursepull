"""Pull every resource reference out of a page: links, embeds and media.

Each Link carries the heading path it sits under and the text of its table row,
list item or paragraph. That is what an agent needs to tell what a bare
"slides" or "video" link belongs to.
"""

from __future__ import annotations

import re
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup, Tag

from .models import Link, PageOutline

_SKIP_SCHEMES = ("javascript:", "mailto:", "tel:", "data:", "blob:")
_LINK_ATTRS: dict[str, tuple[str, ...]] = {
    "a": ("href",),
    "area": ("href",),
    "iframe": ("src", "data-src"),
    "embed": ("src",),
    "object": ("data",),
    "video": ("src",),
    "audio": ("src",),
    "source": ("src",),
}
_HEADINGS = {f"h{i}": i for i in range(1, 7)}
_ROW_TAGS = {"tr", "li", "p", "dt", "dd", "figure", "caption"}
_BOX_TAGS = {"div", "section", "article", "td"}
_SNIPPET_CHARS = 200
_HEADING_CHARS = 80

# Controls that reveal hidden content are worth clicking; these never are.
CONTROL_DENY = re.compile(
    r"log ?in|log ?out|sign ?(in|up|out)|subscribe|newsletter|cookie|accept|reject|consent|"
    r"close|dismiss|search|theme|dark|light|menu|share|copy|print|download|submit|send|delete|buy|cart",
    re.IGNORECASE,
)
_MAX_CONTROL_CHARS = 40


def _clip(text: str, limit: int) -> str:
    text = re.sub(r"\s+", " ", text).strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _snippet(el: Tag) -> str:
    """Text of the enclosing row/list item/paragraph; failing that, a small enclosing box."""
    own = el.get_text(" ", strip=True)
    box = ""
    for depth, parent in enumerate(el.parents):
        if parent.name in ("body", "html", "main") or depth > 5:
            break
        text = parent.get_text(" ", strip=True)
        if not text or text == own:
            continue  # e.g. a <td> holding nothing but the link
        if parent.name in _ROW_TAGS:
            return _clip(text, _SNIPPET_CHARS)
        if not box and parent.name in _BOX_TAGS and len(text) <= 300:
            box = text
    return _clip(box, _SNIPPET_CHARS)


def _link_text(el: Tag) -> str:
    if el.name in ("a", "area"):
        text = el.get_text(" ", strip=True)
        if not text and (img := el.find("img")):
            text = img.get("alt") or ""
        return _clip(text or el.get("title") or el.get("aria-label") or "", 160)
    return _clip(el.get("title") or el.get("aria-label") or "", 160)


def _clean_url(raw: str, base: str) -> str | None:
    raw = raw.strip()
    if not raw or raw.startswith("#") or raw.lower().startswith(_SKIP_SCHEMES):
        return None
    if raw.startswith("//"):
        raw = "https:" + raw
    parsed = urlparse(urljoin(base, raw))
    if parsed.scheme not in ("http", "https"):
        return None
    return parsed._replace(fragment="").geturl()


def extract(html: str, base_url: str) -> list[Link]:
    soup = BeautifulSoup(html, "html.parser")
    if (base_tag := soup.find("base", href=True)) is not None:
        base_url = urljoin(base_url, base_tag["href"])

    seen: dict[str, Link] = {}
    headings: list[tuple[int, str]] = []

    for el in soup.find_all(True):
        level = _HEADINGS.get(el.name)
        if level:
            text = _clip(el.get_text(" ", strip=True), _HEADING_CHARS)
            if text:
                while headings and headings[-1][0] >= level:
                    headings.pop()
                headings.append((level, text))
            continue

        for attr in _LINK_ATTRS.get(el.name, ()):
            url = _clean_url(el.get(attr) or "", base_url)
            if url is None:
                continue
            text = _link_text(el)
            if url in seen:
                if not seen[url].text and text:
                    seen[url].text = text
                continue
            source_tag = el.name
            if el.name == "source" and el.parent is not None and el.parent.name in ("video", "audio"):
                source_tag = el.parent.name
            seen[url] = Link(
                url=url,
                text=text,
                context_path=" > ".join(h for _, h in headings),
                source=source_tag,
                snippet=_snippet(el),
            )
    return list(seen.values())


def extract_views(views: list[tuple[str, str]], base_url: str) -> list[Link]:
    """Merge links from several renders of one page (e.g. one per clicked tab).

    Links first revealed by clicking a control get that control's label in
    their context path, so "[Syllabus] Week 3" says where they were hidden.
    """
    merged: dict[str, Link] = {}
    for label, html in views:
        for link in extract(html, base_url):
            if link.url in merged:
                continue
            if label:
                prefix = f"[{label}]"
                link.context_path = f"{prefix} {link.context_path}".strip()
            merged[link.url] = link
    return list(merged.values())


def find_controls(soup: BeautifulSoup) -> list[str]:
    """Labels of tabs, accordions and other in-page buttons that may hide content."""
    labels: list[str] = []
    for el in soup.select('[role="tab"], button, summary, [aria-expanded="false"]'):
        if el.find_parent("form") is not None or el.name == "a":
            continue
        label = _clip(el.get_text(" ", strip=True), 80)
        if not label or len(label) > _MAX_CONTROL_CHARS or CONTROL_DENY.search(label):
            continue
        if label not in labels:
            labels.append(label)
    return labels[:30]


def page_outline(html: str, max_text: int = 1500) -> PageOutline:
    soup = BeautifulSoup(html, "html.parser")
    title = soup.title.get_text(strip=True) if soup.title else ""
    controls = find_controls(soup)
    headings = [
        f"{'#' * _HEADINGS[h.name]} {_clip(h.get_text(' ', strip=True), _HEADING_CHARS)}"
        for h in soup.find_all(["h1", "h2", "h3"])
        if h.get_text(strip=True)
    ][:40]
    for tag in soup(["script", "style", "noscript", "svg", "template"]):
        tag.decompose()
    main = soup.find("main") or soup.body or soup
    return PageOutline(
        title=_clip(title, 200),
        headings=headings,
        text_excerpt=_clip(main.get_text(" ", strip=True), max_text),
        controls=controls,
    )
