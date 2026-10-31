"""Runs (or replays) one course run in the background and feeds its events to the view.

The run thread only ever touches a queue; the UI drains it into the RunView on its
own timer. Stop sets the run's cancel signal, so the run still finishes cleanly
and writes the course index.
"""

from __future__ import annotations

import json
import queue
import threading
import time
from pathlib import Path

import openai

from ..graph import retry_failed, run_course
from ..llm import LLMRouter, ollama_running
from .view import RunView


class RunController:
    def __init__(self) -> None:
        self.view = RunView()
        self._events: queue.SimpleQueue[dict] = queue.SimpleQueue()
        self._thread: threading.Thread | None = None
        self._cancel = threading.Event()

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self, course_url: str, output_dir: str, *, models: dict[str, dict] | None = None,
              hf_token: str = "", plan_only: bool = False, token_budget: int | None = None) -> None:
        if self.running:
            raise RuntimeError("a run is already in progress")
        self.view = RunView(status="running", course_url=course_url, output_dir=output_dir)
        self._cancel = threading.Event()
        kwargs = {"plan_only": plan_only}
        if token_budget:
            kwargs["token_budget"] = token_budget

        def work() -> None:
            try:
                llm = LLMRouter.from_config(api_keys={"hf": hf_token} if hf_token else None, models=models)
                if down := [url for url in llm.ollama_urls() if not ollama_running(url)]:
                    raise RuntimeError(f"Ollama is not running at {down[0]}; start the Ollama app first")
                run_course(course_url, output_dir, llm=llm, on_event=self._events.put, cancel=self._cancel, **kwargs)
            except openai.AuthenticationError as exc:
                self._error(f"The LLM endpoint rejected the API key ({exc.status_code}). For HuggingFace, paste a "
                            "token with the 'Make calls to Inference Providers' permission into the header's token "
                            "field. Start again to resume.")
            except Exception as exc:  # noqa: BLE001 - shown in the UI; the run can be resumed
                self._error(f"{type(exc).__name__}: {exc}. Start again with the same course and folder to resume.")

        self._thread = threading.Thread(target=work, name="course-run", daemon=True)
        self._thread.start()

    def retry(self, course_url: str, output_dir: str) -> None:
        """Re-download what failed in a finished run: no agents, no tokens."""
        if self.running:
            raise RuntimeError("a run is already in progress")
        self.view = RunView(status="running", course_url=course_url, output_dir=output_dir)
        self._cancel = threading.Event()

        def work() -> None:
            try:
                retry_failed(course_url, output_dir, on_event=self._events.put, cancel=self._cancel)
            except Exception as exc:  # noqa: BLE001 - shown in the UI
                self._error(f"{type(exc).__name__}: {exc}")

        self._thread = threading.Thread(target=work, name="retry", daemon=True)
        self._thread.start()

    def replay(self, events_file: Path, speed: float = 10.0) -> None:
        """Play back a recorded run (events.jsonl), `speed` times faster, capped at 1 s per gap."""
        if self.running:
            raise RuntimeError("a run is already in progress")
        events = [json.loads(line) for line in Path(events_file).read_text(encoding="utf-8").splitlines() if line]
        self.view = RunView()
        self._cancel = threading.Event()

        def work() -> None:
            previous = None
            for event in events:
                if self._cancel.is_set():
                    break
                if previous is not None and "ts" in event:
                    time.sleep(min(max(event["ts"] - previous, 0) / speed, 1.0))
                previous = event.get("ts", previous)
                self._events.put({**event, "ts": time.time()})  # replayed now, so times and elapsed are real

        self._thread = threading.Thread(target=work, name="replay", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        if self.running:
            self._cancel.set()
            self.view.status = "stopping"
            self.view.versions["status"] += 1

    def pump(self) -> bool:
        """Apply queued events to the view. Returns whether anything changed."""
        changed = False
        while True:
            try:
                event = self._events.get_nowait()
            except queue.Empty:
                return changed
            self.view.apply(event)
            changed = True

    def _error(self, message: str) -> None:
        self._events.put({"type": "error", "message": message, "ts": time.time()})
