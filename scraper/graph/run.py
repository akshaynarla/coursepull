"""Run (or resume) the workflow for one course, streaming events to a listener.

Checkpoints go to <output_dir>/.coursepull/checkpoints.sqlite after every step.
If a run crashes or is killed, calling run_course again with the same course
and folder resumes it: finished Scouts are not re-run and finished downloads
are skipped. A run that completed starts from scratch next time.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
from collections.abc import Callable
from pathlib import Path

from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from langgraph.checkpoint.sqlite import SqliteSaver

from ..agents.toolbox import PageStore, load_pages
from ..config import MAX_PAGES_PER_RUN, RUN_TOKEN_BUDGET, STATE_DIRNAME
from ..llm import LLMRouter
from ..run_context import Budget, RunContext
from ..state import blackboard_types, make_task_id
from ..agents.tutor import plan_event
from .flow import build_graph, run_downloads, tasks_for
from .report import build_report

log = logging.getLogger(__name__)

EventFn = Callable[[dict], None]
EVENTS_FILENAME = "events.jsonl"
PAGES_FILENAME = "pages.json.gz"  # every page the agents read, for freezing a course site as an eval


class EventLog:
    """Stamps each event, appends it to events.jsonl, and passes it to the listener.

    The file is a record of what each agent said and did, for reading a run afterwards
    and for replaying it in the UI.
    """

    def __init__(self, state_dir: Path, listener: EventFn | None) -> None:
        self._file = (state_dir / EVENTS_FILENAME).open("a", encoding="utf-8")
        self._listener = listener or (lambda _event: None)

    def __call__(self, event: dict) -> None:
        stamped = {"ts": round(time.time(), 3), **event}
        self._file.write(json.dumps(stamped, ensure_ascii=False, default=str) + "\n")
        self._file.flush()
        self._listener(stamped)

    def close(self) -> None:
        self._file.close()


def checkpointer(path: Path) -> tuple[SqliteSaver, sqlite3.Connection]:
    conn = sqlite3.connect(path, check_same_thread=False)
    allowed = [(t.__module__, t.__name__) for t in blackboard_types()]
    return SqliteSaver(conn, serde=JsonPlusSerializer(allowed_msgpack_modules=allowed)), conn


def run_course(
    course_url: str,
    output_dir: Path | str,
    *,
    llm: LLMRouter | None = None,
    api_keys: dict[str, str] | None = None,
    on_event: EventFn | None = None,
    cancel: threading.Event | None = None,
    token_budget: int = RUN_TOKEN_BUDGET,
    fresh: bool = False,
    plan_only: bool = False,
    saved_pages: Path | None = None,
) -> dict:
    """Collect a course into output_dir. Returns the final graph state.

    plan_only stops after the reviewed plan: nothing is downloaded, the index lists the plan.
    saved_pages (a pages.json.gz) makes the agents read only those pages, never the live site.
    """
    output_dir = Path(output_dir).resolve()
    state_dir = output_dir / STATE_DIRNAME
    state_dir.mkdir(parents=True, exist_ok=True)
    on_event = EventLog(state_dir, on_event)

    saver, conn = checkpointer(state_dir / "checkpoints.sqlite")
    rc = None
    try:
        app = build_graph().compile(checkpointer=saver)
        thread_id = make_task_id(course_url)
        config = {"configurable": {"thread_id": thread_id}, "recursion_limit": 300}

        snapshot = app.get_state(config)
        if fresh or (snapshot.values and not snapshot.next):
            saver.delete_thread(thread_id)
            snapshot = app.get_state(config)
        resuming = bool(snapshot.next)

        rc = RunContext(
            output_dir=output_dir,
            llm=llm or LLMRouter.from_config(api_keys),
            budget=Budget(token_budget, spent=snapshot.values.get("usage") if resuming else None),
            cancel=cancel or threading.Event(),
            pages=PageStore(MAX_PAGES_PER_RUN, saved=load_pages(saved_pages) if saved_pages else None),
        )
        on_event({"type": "run", "course_url": course_url, "output_dir": str(output_dir),
                  "resuming": resuming, "models": rc.llm.describe(), "budget": token_budget})

        inputs = None if resuming else {"course_url": course_url, "phase": "discovery", "plan_only": plan_only}
        for mode, chunk in app.stream(inputs, config, context=rc, stream_mode=["custom", "updates"]):
            if mode == "custom":
                on_event(chunk)
            else:
                for node in chunk:
                    on_event({"type": "step", "node": node})
        final = app.get_state(config).values
        on_event({"type": "usage", "by_role": rc.budget.by_role(), "total": rc.budget.used})
        return final
    except BaseException as exc:
        on_event({"type": "error", "message": f"{type(exc).__name__}: {exc}"})
        raise
    finally:
        if rc is not None:
            rc.pages.save(state_dir / PAGES_FILENAME)
        conn.close()
        on_event.close()


def retry_failed(
    course_url: str,
    output_dir: Path | str,
    *,
    on_event: EventFn | None = None,
    cancel: threading.Event | None = None,
) -> dict:
    """Download again whatever failed or went missing in the folder's last finished run.

    No agents and no tokens: the plan is the one the agents made. Fixed downloaders get a
    second chance, and the course index is rewritten. Returns the updated state.
    """
    output_dir = Path(output_dir).resolve()
    state_dir = output_dir / STATE_DIRNAME
    if not (state_dir / "checkpoints.sqlite").exists():
        raise ValueError(f"no run found in {output_dir}")
    on_event = EventLog(state_dir, on_event)
    saver, conn = checkpointer(state_dir / "checkpoints.sqlite")
    try:
        app = build_graph().compile(checkpointer=saver)
        config = {"configurable": {"thread_id": make_task_id(course_url)}}
        snapshot = app.get_state(config)
        state = snapshot.values
        if not state.get("plan"):
            raise ValueError("this folder has no plan to retry; run the course first")
        if snapshot.next:
            raise ValueError("this run has not finished; start it again to resume it instead")

        outcomes = state.get("outcomes") or {}

        def needs_retry(entry) -> bool:
            outcome = outcomes.get(entry.task.task_id)
            return (outcome is None or outcome.status != "done"
                    or not all(Path(f).exists() for f in outcome.files))

        entries = [e for e in state["plan"].entries if needs_retry(e)]
        on_event({"type": "run", "course_url": course_url, "output_dir": str(output_dir), "resuming": False,
                  "retry": True, "models": {}, "budget": 0})
        on_event(plan_event(state, entries))
        on_event({"type": "phase", "phase": "download", "items": len(entries)})
        new = run_downloads(tasks_for(state, entries), output_dir, cancel or threading.Event(), relay=on_event)
        app.update_state(config, {"outcomes": new}, as_node="finish")  # stays finished: finish -> END
        updated = app.get_state(config).values
        on_event({"type": "phase", "phase": "finished"})
        on_event({"type": "done", **build_report(updated, output_dir)})
        return updated
    except BaseException as exc:
        on_event({"type": "error", "message": f"{type(exc).__name__}: {exc}"})
        raise
    finally:
        conn.close()
        on_event.close()
