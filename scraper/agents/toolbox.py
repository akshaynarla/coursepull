"""Tools the agents work with, and the shared page cache behind them.

Agents see links as short ids ("L12") with what identify() knows about them.
They hand ids back when recording findings, so a URL is never retyped by a
model, and so never hallucinated.
"""

from __future__ import annotations

import json
import subprocess
import threading
import zipfile
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

import requests
from pydantic import BaseModel, Field
from pypdf import PdfReader

from ..config import AGENT_TOOL_RESULT_CHARS, INSPECT_TIMEOUT, LINKS_PER_PAGE_RESULT, USER_AGENT

LISTING_CHARS = AGENT_TOOL_RESULT_CHARS - 200  # a page listing must never be cut by the agent loop
from ..engine.google import list_drive_folder
from ..engine.http import sniff
from ..extract import extract, extract_views, page_outline
from ..fetch import fetch, render_interactive
from ..identify import identify
from ..models import Link, PageOutline
from ..state import ResourceKind
from ..run_context import emit
from .loop import Tool, ToolError
from .terms import OfferingMap

# ---------- shared page cache ----------


@dataclass
class PageSnapshot:
    final_url: str
    outline: PageOutline
    links: list[Link]
    revealed: bool


class PageLimitReached(ToolError):
    pass


class PageStore:
    """Pages read during a run, shared by all agents, with a run-wide page cap."""

    def __init__(self, max_pages: int) -> None:
        self.max_pages = max_pages
        self.offerings = OfferingMap()
        self._pages: dict[str, PageSnapshot] = {}
        self._reads = 0
        self._lock = threading.Lock()

    @property
    def reads(self) -> int:
        return self._reads

    def title(self, url: str) -> str:
        snapshot = self._pages.get(url.split("#")[0])
        return snapshot.outline.title if snapshot else ""

    def get(self, url: str, reveal: bool = False) -> PageSnapshot:
        key = url.split("#")[0]
        with self._lock:
            cached = self._pages.get(key)
            if cached and (cached.revealed or not reveal):
                return cached
            if self._reads >= self.max_pages:
                raise PageLimitReached(f"the run's page budget ({self.max_pages} pages) is used up")
            self._reads += 1
        if reveal:
            final_url, views = render_interactive(url)
            snapshot = PageSnapshot(final_url, page_outline(views[0].html),
                                    extract_views([(v.label, v.html) for v in views], final_url), True)
        else:
            fetched = fetch(url)
            snapshot = PageSnapshot(fetched.final_url, page_outline(fetched.html),
                                    extract(fetched.html, fetched.final_url), False)
        with self._lock:
            self._pages[key] = snapshot
            self._pages.setdefault(snapshot.final_url.split("#")[0], snapshot)
            self.offerings.learn(snapshot.final_url, snapshot.outline.title, snapshot.links,
                                 complete=snapshot.revealed or not snapshot.outline.controls)
        return snapshot


# ---------- link ids ----------


class LinkBook:
    """Short ids for the links one agent has been shown."""

    def __init__(self) -> None:
        self._by_id: dict[str, tuple[Link, str]] = {}
        self._id_of: dict[str, str] = {}
        self._page_of_key: dict[str, str] = {}  # resource key -> page the link was seen on

    def add(self, link: Link, page_url: str) -> str:
        if link.url in self._id_of:
            return self._id_of[link.url]
        link_id = f"L{len(self._by_id) + 1}"
        self._by_id[link_id] = (link, page_url)
        self._id_of[link.url] = link_id
        self._page_of_key.setdefault(identify(link.url).key, page_url)
        return link_id

    def drive_ancestors(self, page_url: str) -> list[str]:
        """Keys of the Drive folders a link seen on page_url sits in, innermost first."""
        keys: list[str] = []
        url = page_url
        while url and identify(url).kind == ResourceKind.DRIVE_FOLDER:
            key = identify(url).key
            if key in keys:
                break
            keys.append(key)
            url = self._page_of_key.get(key)  # where this folder itself was listed
        return keys

    def get(self, link_id: str) -> tuple[Link, str] | None:
        return self._by_id.get(link_id.strip().upper())

    def has(self, url: str) -> bool:
        return url in self._id_of

    def id_for(self, url: str) -> str | None:
        return self._id_of.get(url)

    def next_id(self) -> str:
        return f"L{len(self._by_id) + 1}"


def _short(text: str, limit: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def describe_link(link_id: str, link: Link) -> str:
    ref = identify(link.url)
    tag = ref.kind.value
    if ref.provider not in ("generic", "direct"):
        tag += f"/{ref.provider}"
    if ref.published:
        tag += ", published"
    if ref.login_likely:
        tag += ", login?"
    parts = [f'{link_id} [{tag}] "{_short(link.text, 70)}"']
    if link.source != "a":
        parts.append(f"<{link.source}>")
    if link.context_path:
        parts.append(f"in: {_short(link.context_path, 80)}")
    if link.snippet and link.snippet != link.text:
        parts.append(f"row: {_short(link.snippet, 120)}")
    parts.append(f"-> {_short(link.url, 140)}")
    return " | ".join(parts)


def describe_page(snapshot: PageSnapshot, book: LinkBook, offset: int,
                  offerings: OfferingMap | None = None) -> str:
    outline = snapshot.outline
    lines = [f"PAGE {snapshot.final_url}", f"title: {outline.title}"]
    if outline.headings:
        lines.append("headings: " + " · ".join(_short(h, 60) for h in outline.headings[:25]))
    if outline.controls and not snapshot.revealed:
        lines.append("controls that may hide content (call reveal_page to click them): "
                     + ", ".join(outline.controls[:15]))
    if offerings is not None:
        lines += offerings.describe(snapshot.final_url, snapshot.links)
    lines.append("text: " + _short(outline.text_excerpt, 700))

    # List links until the result's size budget is used, so the agent sees every link it is
    # told about (a longer result would be cut off by the agent loop) and can page on exactly.
    # Links seen before are listed again with their old ids: older listings get masked out of
    # the agent's context, so asking again must always show them (run 6 lost them for good).
    budget = LISTING_CHARS - sum(len(line) + 1 for line in lines)
    rendered: list[str] = []
    for link in snapshot.links[offset: offset + LINKS_PER_PAGE_RESULT]:
        line = describe_link(book.id_for(link.url) or book.next_id(), link)
        if rendered and budget - len(line) - 1 < 0:
            break
        budget -= len(line) + 1
        rendered.append(describe_link(book.add(link, snapshot.final_url), link))
    end, total = offset + len(rendered), len(snapshot.links)
    header = f"links {offset + 1}-{end} of {total}"
    if end < total:
        header += f" (more: call again with offset={end})"
    return "\n".join([*lines, header + ":", *rendered])


# ---------- web tools (read-only) ----------


class PageArgs(BaseModel):
    url: str = Field(description="Absolute URL of the page")
    offset: int = Field(0, ge=0, description="Skip this many links (for long pages)")


class UrlArgs(BaseModel):
    url: str


def web_tools(rc, book: LinkBook, agent: str) -> list[Tool]:
    """fetch_page, reveal_page, inspect_url, list_drive_folder. All read-only."""

    def fetch_page(args: PageArgs) -> str:
        snapshot = rc.pages.get(args.url)
        emit({"type": "page", "agent": agent, "url": snapshot.final_url, "links": len(snapshot.links)})
        return describe_page(snapshot, book, args.offset, rc.pages.offerings)

    def reveal_page(args: PageArgs) -> str:
        snapshot = rc.pages.get(args.url, reveal=True)
        emit({"type": "page", "agent": agent, "url": snapshot.final_url, "links": len(snapshot.links),
              "revealed": True})
        return describe_page(snapshot, book, args.offset, rc.pages.offerings)

    def inspect_url(args: UrlArgs) -> dict:
        ref = identify(args.url)
        result = {"kind": ref.kind.value, "provider": ref.provider, "hint": ref.hint,
                  "login_likely": ref.login_likely}
        try:
            resp = requests.head(args.url, headers={"User-Agent": USER_AGENT},
                                 timeout=INSPECT_TIMEOUT, allow_redirects=True)
            result.update(status=resp.status_code, final_url=str(resp.url),
                          content_type=resp.headers.get("Content-Type", ""),
                          size=resp.headers.get("Content-Length", ""))
        except requests.RequestException as exc:
            result["error"] = f"{type(exc).__name__}: {exc}"
        return result

    def drive_folder(args: UrlArgs) -> str:
        ref = identify(args.url)
        if ref.kind.value != "drive_folder":
            raise ToolError("not a Google Drive folder link")
        title, entries = list_drive_folder(ref.resource_id)
        lines = [f"DRIVE FOLDER {title!r}: {len(entries)} entries"]
        for entry in entries:
            link = Link(url=entry.url, text=entry.title, context_path=f"Drive folder: {title}", source="drive")
            lines.append(describe_link(book.add(link, args.url), link))
        return "\n".join(lines)

    return [
        Tool("fetch_page", "Read a web page: outline, text excerpt and its links with ids (L1, L2, ...).",
             PageArgs, fetch_page),
        Tool("reveal_page", "Like fetch_page, but renders the page in a browser and clicks its tabs and "
             "expandable sections first. Use it when fetch_page lists controls that may hide content.",
             PageArgs, reveal_page),
        Tool("inspect_url", "Check a URL without downloading it: what it is, HTTP status, content type, size.",
             UrlArgs, inspect_url),
        Tool("list_drive_folder", "List the files in a public Google Drive folder (each gets a link id).",
             UrlArgs, drive_folder),
    ]


# ---------- local file inspection (Examiner) ----------


def inspect_file(path: Path, text_chars: int = 300) -> dict:
    """What a downloaded file actually is: type, size and a glimpse of its content."""
    info: dict = {"file": path.name}
    if path.is_dir():
        files = [p for p in path.rglob("*") if p.is_file()]
        info.update(type="folder", files=len(files), names=[p.name for p in files[:8]])
        return info
    if not path.exists():
        return {**info, "error": "missing on disk"}
    size = path.stat().st_size
    with path.open("rb") as fh:
        head = fh.read(2048)
    info.update(size=size, sniffed=sniff(head))
    suffix = path.suffix.lower()
    try:
        if info["sniffed"] == "pdf":
            reader = PdfReader(str(path))
            info["pages"] = len(reader.pages)
            meta_title = (reader.metadata or {}).get("/Title") if reader.metadata else None
            if meta_title:
                info["pdf_title"] = str(meta_title)[:120]
            first = (reader.pages[0].extract_text() or "") if reader.pages else ""
            info["first_page_text"] = " ".join(first.split())[:text_chars]
        elif info["sniffed"] == "zip" or suffix in (".zip", ".pptx", ".docx", ".xlsx"):
            with zipfile.ZipFile(path) as zf:
                names = zf.namelist()
            info.update(entries=len(names), names=names[:8])
        elif suffix in (".mp4", ".webm", ".mkv", ".mov", ".m4a", ".mp3"):
            info["duration_s"] = _media_duration(path)
        elif suffix in (".txt", ".md", ".py", ".ipynb", ".csv", ".tex", ".vtt", ".json", ".html"):
            info["text"] = " ".join(head.decode("utf-8", errors="replace").split())[:text_chars]
    except Exception as exc:  # noqa: BLE001 - a corrupt file is itself a finding
        info["error"] = f"could not read: {type(exc).__name__}: {exc}"
    return info


def _media_duration(path: Path) -> float | None:
    try:
        out = subprocess.run(["ffprobe", "-v", "quiet", "-print_format", "json", "-show_format", str(path)],
                             capture_output=True, text=True, timeout=30, check=False)
        return round(float(json.loads(out.stdout)["format"]["duration"]), 1)
    except (OSError, ValueError, KeyError, subprocess.TimeoutExpired):
        return None


def site_of(url: str) -> str:
    return urlparse(url).netloc.lower().removeprefix("www.")
