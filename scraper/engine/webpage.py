"""Save an HTML page (lecture notes, published Google Docs) as a PDF via Edge."""

from __future__ import annotations

import re
from pathlib import Path

from ..browser import open_page
from ..config import PLAYWRIGHT_TIMEOUT_MS
from ..state import ErrorKind
from .context import HandlerResult, TaskContext
from .errors import DownloadError, error_for_status
from .http import looks_like_login

_GOOGLE_SUFFIX = re.compile(r"\s+-\s+Google (Docs|Slides|Sheets|Drive)$")


def clean_title(title: str) -> str:
    return _GOOGLE_SUFFIX.sub("", title or "").strip()


def save_page_pdf(ctx: TaskContext, url: str | None = None) -> Path:
    url = url or ctx.ref.url
    ctx.check_cancelled()
    with open_page() as page:
        response = page.goto(url, wait_until="networkidle", timeout=PLAYWRIGHT_TIMEOUT_MS)
        if response is not None and response.status >= 400:
            raise error_for_status(response.status, url)
        if looks_like_login(page.content(), page.url):
            raise DownloadError(ErrorKind.AUTH_REQUIRED, "page redirected to a login")
        base = ctx.claim(clean_title(page.title()))
        dest = base.with_name(base.name + ".pdf")
        dest.parent.mkdir(parents=True, exist_ok=True)
        part = dest.with_name(dest.name + ".part")
        page.pdf(path=str(part), format="A4", print_background=True,
                 margin={"top": "12mm", "bottom": "12mm", "left": "10mm", "right": "10mm"})
    part.replace(dest)
    size = dest.stat().st_size
    ctx.progress(size, size)
    return dest


def save_webpage(ctx: TaskContext) -> HandlerResult:
    return HandlerResult([save_page_pdf(ctx)])
