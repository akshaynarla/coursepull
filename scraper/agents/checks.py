"""Doubts code can raise about a material, for the agents to weigh.

Nothing here decides. Scouts get these as warnings when they record a link;
the Reviewer gets them as automatic checks on the plan.
"""

from __future__ import annotations

from ..identify import identify
from ..models import Link
from ..state import ResourceKind as K

_RECORDING_KINDS = {K.VIDEO, K.FILE, K.DRIVE_FILE, K.DRIVE_FOLDER}
_GENERIC_TEXT = {
    "slides", "slide", "pdf", "pptx", "video", "videos", "recording", "youtube", "link", "here", "notes",
    "paper", "code", "download", "lecture", "handout", "reading", "transcript", "solution", "solutions",
}


def doubts(url: str, role: str, kind: K | None = None) -> list[str]:
    ref = identify(url)
    kind = kind or ref.kind
    found = []
    if ref.provider == "social":
        found.append("a social or profile page (a speaker's name usually links to one)")
    if ref.login_likely:
        found.append("probably needs a login")
    if role == "lecture_video" and kind not in _RECORDING_KINDS:
        found.append(f"labelled a recording but it is a {kind.value}, not a video")
    if role == "reading" and kind == K.REPO and "path" not in ref.params and "blob" not in ref.params:
        found.append("a whole code repository as a reading; its README page (kind=webpage) is usually enough")
    return found


def default_title(link: Link, role: str) -> str:
    """A title from the link and its row, for when the Scout doesn't write one."""
    text = " ".join(link.text.split())
    if len(text) > 3 and text.lower().strip("[]() ") not in _GENERIC_TEXT:
        return text[:120]
    context = " ".join((link.snippet or link.context_path).split())
    label = text or role.replace("_", " ")
    return f"{context[:90]} ({label})" if context else label
