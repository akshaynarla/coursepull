"""Safe, unique destination paths.

Folder and file names come from agents, which read them off untrusted web
pages. Everything is sanitised for Windows and pinned inside the output dir.
"""

from __future__ import annotations

import os
import re
import threading
import unicodedata
from pathlib import Path

_INVALID = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}
MAX_COMPONENT = 80
MAX_PATH = 240  # stay under Windows' 260-char MAX_PATH with room for extensions and ".part"


def safe_component(name: str | None, fallback: str = "untitled", max_len: int = MAX_COMPONENT) -> str:
    name = unicodedata.normalize("NFC", name or "")
    name = name.replace(":", " -")
    name = _INVALID.sub("_", name)
    name = re.sub(r"\s+", " ", name).strip().strip(".").strip()
    if not name or set(name) <= {".", "_", " "}:
        name = fallback
    if name.split(".")[0].upper() in _RESERVED:
        name = "_" + name
    if len(name) > max_len:
        name = name[:max_len].rstrip(" .")
    return name


class PathAllocator:
    """Hands out one destination stem per resource, never reusing another resource's.

    A "base" is a path without extension: a resource may write several files
    next to it (lecture.pdf + lecture.pptx, video.mp4 + video.en.vtt).
    """

    def __init__(self, root: Path, owner_of=lambda path: None) -> None:
        self.root = root.resolve()
        self._owner_of = owner_of  # path -> resource key recorded in the manifest, or None
        self._claimed: dict[Path, str] = {}
        self._lock = threading.Lock()

    def claim(self, folder: list[str], stem: str | None, key: str) -> Path:
        parts = [safe_component(p, fallback="folder") for p in folder if p and p.strip()]
        # Pure path arithmetic, no filesystem lookups: on Windows, Path.resolve() can return a
        # "\\?\C:\..." path while another download thread is creating one of these folders.
        directory = Path(os.path.normpath(self.root.joinpath(*parts)))
        if os.path.commonpath([directory, self.root]) != str(self.root):
            directory = self.root  # never escape the output dir
        stem = safe_component(stem, fallback="download")
        room = MAX_PATH - len(str(directory)) - 12
        if room < 16:
            directory, room = self.root, MAX_PATH - len(str(self.root)) - 12
        stem = stem[: max(room, 16)].rstrip(" .")

        with self._lock:
            n = 1
            while True:
                candidate = directory / (stem if n == 1 else f"{stem} ({n})")
                if self._available(candidate, key):
                    self._claimed[candidate] = key
                    return candidate
                n += 1

    def _available(self, base: Path, key: str) -> bool:
        holder = self._claimed.get(base)
        if holder is not None:
            return holder == key
        for existing in _siblings(base):
            if self._owner_of(existing) != key:
                return False
        return True


def _siblings(base: Path) -> list[Path]:
    """Existing files or dirs named <base> or <base>.<ext>."""
    if not base.parent.is_dir():
        return []
    name = os.path.normcase(base.name)  # Windows file names are case-insensitive
    prefix = name + "."
    found = []
    for entry in os.listdir(base.parent):
        norm = os.path.normcase(entry)
        if norm == name or (norm.startswith(prefix) and not norm.endswith(".part")):
            found.append(base.parent / entry)
    return found
