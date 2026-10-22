"""The executor: carries out DownloadTasks in parallel and reports every step.

It makes no judgement calls. Agents decide what to download, where it goes and
what to do about failures; the engine does exactly that and streams
EngineEvents so the UI and the Examiner can watch.
"""

from __future__ import annotations

import logging
import shutil
import threading
import time
from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from ..config import (
    DEFAULT_PROVIDER_CONCURRENCY,
    DOWNLOAD_CONCURRENCY,
    PROGRESS_EVENT_INTERVAL_S,
    PROVIDER_CONCURRENCY,
    STATE_DIRNAME,
)
from ..identify import identify
from ..state import DownloadTask, EngineEvent, ErrorKind, ResourceKind, ResourceRef, TaskOutcome
from . import files, google, papers, repos, video, webpage
from .context import HandlerResult, TaskContext
from .errors import DownloadError
from .manifest import Manifest
from .paths import PathAllocator

log = logging.getLogger(__name__)

Handler = Callable[[TaskContext], HandlerResult]
EventFn = Callable[[EngineEvent], None]

HANDLERS: dict[ResourceKind, Handler] = {
    ResourceKind.GOOGLE_DOC: google.export_document,
    ResourceKind.GOOGLE_SLIDES: google.export_document,
    ResourceKind.GOOGLE_SHEET: google.export_document,
    ResourceKind.DRIVE_FILE: google.drive_file,
    ResourceKind.DRIVE_FOLDER: google.drive_folder,
    ResourceKind.VIDEO: video.download_video,
    ResourceKind.PAPER: papers.download_paper,
    ResourceKind.REPO: repos.download_repo,
    ResourceKind.FILE: files.download_file,
    ResourceKind.WEBPAGE: webpage.save_webpage,
    ResourceKind.UNKNOWN: files.download_file,
}

_FINAL = ("done", "failed", "cancelled")


def _lane(ref: ResourceRef) -> str:
    """Concurrency lane: anything rendered in Edge shares the small "browser" lane."""
    if ref.kind == ResourceKind.WEBPAGE or (ref.published and ref.kind != ResourceKind.GOOGLE_SHEET):
        return "browser"
    return ref.provider


class DownloadEngine:
    def __init__(
        self,
        output_dir: Path,
        *,
        workers: int = DOWNLOAD_CONCURRENCY,
        on_event: EventFn | None = None,
        cancel_event: threading.Event | None = None,
    ) -> None:
        self.output_dir = Path(output_dir).resolve()
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.state_dir = self.output_dir / STATE_DIRNAME
        self.tmp_dir = self.state_dir / "tmp"
        self.tmp_dir.mkdir(parents=True, exist_ok=True)

        self.cancel_event = cancel_event or threading.Event()
        self._on_event = on_event or (lambda _event: None)
        self._pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="download")
        self._lock = threading.Condition()
        self._tasks: dict[str, DownloadTask] = {}
        self._outcomes: dict[str, TaskOutcome] = {}
        self._task_for_key: dict[str, str] = {}
        self._pending = 0
        self._bytes: dict[str, tuple[int, int | None]] = {}
        self._lanes: dict[str, threading.BoundedSemaphore] = {}
        self._manifest = Manifest(self.output_dir, self.state_dir)
        self._paths = PathAllocator(self.output_dir, owner_of=self._manifest.owner_of)
        self._closed = False

    # ---------- public API ----------

    def submit(self, tasks: Iterable[DownloadTask]) -> list[str]:
        """Queue tasks; returns their task ids.

        A resource already queued, running or done is not queued twice (the existing
        id is returned). One that failed or was cancelled may be resubmitted, e.g.
        with a different kind or URL chosen by an agent.
        """
        ids = []
        for task in tasks:
            ref = identify(task.url)
            if task.kind is not None and task.kind != ref.kind:
                ref = ref.model_copy(update={"kind": task.kind})
            with self._lock:
                if self._closed:
                    raise RuntimeError("engine is closed")
                existing = self._task_for_key.get(ref.key)
                if existing is not None:
                    outcome = self._outcomes.get(existing)
                    if outcome is None or outcome.status == "done":
                        ids.append(existing)
                        continue
                if task.task_id in self._tasks:
                    task = task.model_copy(update={"task_id": f"{task.task_id}-{len(self._tasks)}"})
                self._task_for_key[ref.key] = task.task_id
                self._tasks[task.task_id] = task
                self._outcomes.pop(task.task_id, None)
                self._pending += 1
            self._emit(EngineEvent("queued", task.task_id, task.url))
            self._pool.submit(self._run, task, ref)
            ids.append(task.task_id)
        return ids

    def wait(self, timeout: float | None = None) -> bool:
        """Block until every queued task (including spawned children) has finished."""
        deadline = None if timeout is None else time.monotonic() + timeout
        with self._lock:
            while self._pending:
                remaining = None if deadline is None else deadline - time.monotonic()
                if remaining is not None and remaining <= 0:
                    return False
                self._lock.wait(remaining)
        return True

    def cancel(self) -> None:
        """Stop everything: running tasks abort at their next checkpoint, queued ones never start."""
        self.cancel_event.set()

    def close(self) -> None:
        """Finish or cancel all work, then remove partial and temporary files."""
        self.wait()  # running tasks may still queue children (Drive folders), so close only after
        with self._lock:
            self._closed = True
        self._pool.shutdown(wait=True)
        for part in self.output_dir.rglob("*.part"):
            part.unlink(missing_ok=True)
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def __enter__(self) -> DownloadEngine:
        return self

    def __exit__(self, *_exc) -> None:
        self.close()

    def outcomes(self) -> dict[str, TaskOutcome]:
        with self._lock:
            return dict(self._outcomes)

    def tasks(self) -> dict[str, DownloadTask]:
        with self._lock:
            return dict(self._tasks)

    def stats(self) -> dict[str, int]:
        with self._lock:
            counts = {"total": len(self._tasks), "pending": self._pending, "done": 0, "failed": 0, "cancelled": 0}
            for outcome in self._outcomes.values():
                counts[outcome.status] += 1
            counts["bytes_done"] = sum(done for done, _ in self._bytes.values())
            return counts

    # ---------- internals ----------

    def _emit(self, event: EngineEvent) -> None:
        try:
            self._on_event(event)
        except Exception:  # noqa: BLE001 - a broken listener must not break downloads
            log.exception("event listener failed")

    def _lane_semaphore(self, ref: ResourceRef) -> threading.BoundedSemaphore:
        lane = _lane(ref)
        with self._lock:
            if lane not in self._lanes:
                limit = PROVIDER_CONCURRENCY.get(lane, DEFAULT_PROVIDER_CONCURRENCY)
                self._lanes[lane] = threading.BoundedSemaphore(limit)
            return self._lanes[lane]

    def _run(self, task: DownloadTask, ref: ResourceRef) -> None:
        semaphore = self._lane_semaphore(ref)
        acquired = False
        try:
            while not (acquired := semaphore.acquire(timeout=0.5)):
                if self.cancel_event.is_set():
                    break
            if self.cancel_event.is_set():
                outcome = self._outcome(task, ref, "cancelled", error_kind=ErrorKind.CANCELLED, message="cancelled")
            else:
                self._emit(EngineEvent("started", task.task_id, task.url))
                outcome = self._execute(task, ref)
        except Exception as exc:  # noqa: BLE001 - one failure must not kill the run
            log.exception("unexpected engine error for %s", task.url)
            outcome = self._outcome(task, ref, "failed", error_kind=ErrorKind.UNKNOWN,
                                    message=f"{type(exc).__name__}: {exc}")
        finally:
            if acquired:
                semaphore.release()
        self._finish(outcome)

    def _execute(self, task: DownloadTask, ref: ResourceRef) -> TaskOutcome:
        if cached := self._manifest.completed_files(ref.key):
            return self._outcome(task, ref, "done", files=cached, message="already downloaded")

        last_emit = 0.0

        def report(done: int, total: int | None) -> None:
            nonlocal last_emit
            self._bytes[task.task_id] = (done, total)
            now = time.monotonic()
            if now - last_emit >= PROGRESS_EVENT_INTERVAL_S or (total is not None and done >= total):
                last_emit = now
                self._emit(EngineEvent("progress", task.task_id, task.url, done, total))

        ctx = TaskContext(
            task=task, ref=ref, cancel=self.cancel_event, tmp_dir=self.tmp_dir,
            _allocate=self._paths.claim, _report=report,
        )
        try:
            result = HANDLERS[ref.kind](ctx)
        except DownloadError as exc:
            status = "cancelled" if exc.kind == ErrorKind.CANCELLED else "failed"
            return self._outcome(task, ref, status, error_kind=exc.kind, message=str(exc))
        except Exception as exc:  # noqa: BLE001
            log.exception("handler %s failed for %s", ref.kind.value, task.url)
            kind = ErrorKind.NETWORK if "Timeout" in type(exc).__name__ else ErrorKind.UNKNOWN
            return self._outcome(task, ref, "failed", error_kind=kind, message=f"{type(exc).__name__}: {exc}")

        if result.files:
            self._manifest.record(ref.key, task.url, result.files)
        child_ids = self.submit(result.children) if result.children and not self.cancel_event.is_set() else []
        return self._outcome(task, ref, "done", files=result.files, message=result.message, children=child_ids)

    def _outcome(self, task: DownloadTask, ref: ResourceRef, status: str, *, files=(), error_kind=None,
                 message: str = "", children=()) -> TaskOutcome:
        paths = [str(Path(f)) for f in files]
        size = sum(Path(f).stat().st_size for f in paths if Path(f).is_file())
        return TaskOutcome(task_id=task.task_id, url=task.url, kind=ref.kind, status=status, files=paths,
                           bytes=size, error_kind=error_kind, message=message, children=list(children))

    def _finish(self, outcome: TaskOutcome) -> None:
        with self._lock:
            self._outcomes[outcome.task_id] = outcome
        self._emit(EngineEvent("finished", outcome.task_id, outcome.url, outcome.bytes, outcome.bytes, outcome))
        with self._lock:
            self._pending -= 1
            self._lock.notify_all()
