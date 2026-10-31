"""Any directly downloadable file; a dead link falls back to the Internet Archive's copy."""

from __future__ import annotations

from ..state import ErrorKind
from . import archive
from .context import HandlerResult, TaskContext
from .errors import DownloadError
from .http import download


def download_file(ctx: TaskContext) -> HandlerResult:
    url = ctx.ref.download_url or ctx.ref.url
    try:
        return HandlerResult([download(url, ctx.claim, progress=ctx.progress, cancel=ctx.cancel)])
    except DownloadError as exc:
        if exc.kind != ErrorKind.NOT_FOUND or not (stamp := archive.archived_timestamp(url)):
            raise
    path = download(archive.raw_url(stamp, url), ctx.claim, progress=ctx.progress, cancel=ctx.cancel)
    return HandlerResult([path], message=archive.note(stamp))
