"""Any directly downloadable file."""

from __future__ import annotations

from .context import HandlerResult, TaskContext
from .http import download


def download_file(ctx: TaskContext) -> HandlerResult:
    url = ctx.ref.download_url or ctx.ref.url
    return HandlerResult([download(url, ctx.claim, progress=ctx.progress, cancel=ctx.cancel)])
