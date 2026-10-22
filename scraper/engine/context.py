"""What a handler gets to work with, and what it hands back."""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable
from urllib.parse import unquote, urlparse

from ..state import DownloadTask, ResourceRef
from .errors import Cancelled


@dataclass
class HandlerResult:
    files: list[Path]
    children: list[DownloadTask] = field(default_factory=list)  # more work discovered, e.g. folder entries
    message: str = ""


@dataclass
class TaskContext:
    task: DownloadTask
    ref: ResourceRef
    cancel: threading.Event
    tmp_dir: Path
    _allocate: Callable[[list[str], "str | None", str], Path]
    _report: Callable[[int, "int | None"], None]
    _base: Path | None = None

    def claim(self, suggested_stem: str | None = None) -> Path:
        """Destination base path (no extension). The first call decides; later calls reuse it.

        The agent-given task name wins over the server's filename, which wins over the URL.
        """
        if self._base is None:
            stem = self.task.name or suggested_stem or _stem_from_url(self.ref.url)
            self._base = self._allocate(self.task.folder, stem, self.ref.key)
        return self._base

    def progress(self, done: int, total: int | None) -> None:
        self._report(done, total)

    def check_cancelled(self) -> None:
        if self.cancel.is_set():
            raise Cancelled()


def _stem_from_url(url: str) -> str:
    parsed = urlparse(url)
    leaf = Path(unquote(parsed.path)).stem
    if leaf and leaf.lower() not in ("edit", "view", "pub", "embed", "preview", "index", "download", "pdf"):
        return leaf
    return parsed.netloc or "download"
