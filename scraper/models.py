"""Pipeline data models."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel

Action = Literal[
    "download_pdf",
    "download_arxiv",
    "download_github_zip",
    "download_youtube",
    "download_gdrive",
    "note_paywalled",
    "skip",
]

Subfolder = Literal["slides", "readings", "videos", "code"]


class Link(BaseModel):
    url: str
    text: str
    context_path: str = ""  # heading path, e.g. "Schedule > Week 3"
    source: str = "a"  # tag it came from: a, iframe, embed, object, video, audio, source
    snippet: str = ""  # text of the surrounding row / list item / paragraph


class PageOutline(BaseModel):
    title: str = ""
    headings: list[str] = []
    text_excerpt: str = ""
    controls: list[str] = []  # tab / accordion labels whose content may be hidden until clicked


class PlanItem(BaseModel):
    url: str
    action: Action
    chapter: str | None = None
    subfolder: Subfolder | None = None
    filename: str | None = None
    reason: str = ""


class DownloadResult(BaseModel):
    url: str
    action: Action
    ok: bool
    path: str | None = None
    message: str = ""
