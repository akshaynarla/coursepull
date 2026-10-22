"""Record of what has been downloaded, so a re-run skips finished resources."""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path


class Manifest:
    FILENAME = "manifest.json"

    def __init__(self, root: Path, state_dir: Path) -> None:
        self.root = root.resolve()
        self.path = state_dir / self.FILENAME
        self._lock = threading.Lock()
        self._entries: dict[str, dict] = {}
        self._owners: dict[str, str] = {}
        if self.path.exists():
            try:
                self._entries = json.loads(self.path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                self._entries = {}
        for key, entry in self._entries.items():
            for rel in entry.get("files", []):
                self._owners[os.path.normcase(rel)] = key

    def _rel(self, path: Path) -> str:
        return os.path.relpath(Path(path).resolve(), self.root)

    def owner_of(self, path: Path) -> str | None:
        return self._owners.get(os.path.normcase(self._rel(path)))

    def completed_files(self, key: str) -> list[Path] | None:
        """Files of a finished resource, if they are all still on disk."""
        entry = self._entries.get(key)
        if not entry or not entry.get("files"):
            return None
        files = [self.root / rel for rel in entry["files"]]
        return files if all(f.exists() for f in files) else None

    def record(self, key: str, url: str, files: list[Path]) -> None:
        rels = [self._rel(f) for f in files]
        with self._lock:
            self._entries[key] = {"url": url, "files": rels, "completed_at": time.time()}
            for rel in rels:
                self._owners[os.path.normcase(rel)] = key
            self._save()

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._entries, indent=1, ensure_ascii=False), encoding="utf-8")
        tmp.replace(self.path)
