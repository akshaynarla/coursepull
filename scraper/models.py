"""What a web page looks like to the agents: its links and an outline."""

from __future__ import annotations

from pydantic import BaseModel


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
