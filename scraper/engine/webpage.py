"""Save an HTML page (lecture notes, articles, published Google Docs) as a PDF via Edge."""

from __future__ import annotations

import re
from pathlib import Path

from playwright.sync_api import Error as PlaywrightError

from ..browser import goto, open_page, scroll_through
from ..state import ErrorKind
from . import archive
from .context import HandlerResult, TaskContext
from .errors import DownloadError, error_for_status
from .http import looks_like_login

_GOOGLE_SUFFIX = re.compile(r"\s+-\s+Google (Docs|Slides|Sheets|Drive)$")
_UNREACHABLE = re.compile(r"ERR_NAME_NOT_RESOLVED|ERR_CONNECTION_REFUSED|ERR_ADDRESS_UNREACHABLE|"
                          r"ERR_CONNECTION_CLOSED|ERR_CERT_|ERR_SSL_")


def clean_title(title: str) -> str:
    return _GOOGLE_SUFFIX.sub("", title or "").strip()


def browser_error(exc: Exception) -> DownloadError:
    """A typed error for a failed page load, instead of a raw Playwright message."""
    first = str(exc).split("\n", 1)[0]
    if m := _UNREACHABLE.search(first):
        return DownloadError(ErrorKind.NOT_FOUND, f"site unreachable ({m.group(0).rstrip('_')})")
    if "Timeout" in type(exc).__name__ or "Timeout" in first:
        return DownloadError(ErrorKind.NETWORK, "the page took too long to load")
    return DownloadError(ErrorKind.UNKNOWN, first[:200])


def save_page_pdf(ctx: TaskContext, url: str | None = None) -> Path:
    url = url or ctx.ref.url
    ctx.check_cancelled()
    with open_page() as page:
        try:
            response = goto(page, url)
        except PlaywrightError as exc:
            raise browser_error(exc) from exc
        if response is not None and response.status >= 400:
            raise error_for_status(response.status, url)
        if looks_like_login(page.content(), page.url):
            raise DownloadError(ErrorKind.AUTH_REQUIRED, "page redirected to a login")
        scroll_through(page)  # lazily loaded images must be in place before printing
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
    try:
        return HandlerResult([save_page_pdf(ctx)])
    except DownloadError as exc:
        if exc.kind != ErrorKind.NOT_FOUND or not (stamp := archive.archived_timestamp(ctx.ref.url)):
            raise
    return HandlerResult([save_page_pdf(ctx, archive.page_url(stamp, ctx.ref.url))], message=archive.note(stamp))
