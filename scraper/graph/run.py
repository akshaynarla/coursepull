"""Run (or resume) the workflow for one course, streaming events to a listener.

Checkpoints go to <output_dir>/.coursepull/checkpoints.sqlite after every step.
If a run crashes or is killed, calling run_course again with the same course
and folder resumes it: finished Scouts are not re-run and finished downloads
are skipped. A run that completed starts from scratch next time.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
from collections.abc import Callable
from pathlib import Path

from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from langgraph.checkpoint.sqlite import SqliteSaver

from ..config import RUN_TOKEN_BUDGET, STATE_DIRNAME
from ..llm import LLMRouter
from ..run_context import Budget, RunContext
from ..state import blackboard_types, make_task_id
from .flow import build_graph

log = logging.getLogger(__name__)

EventFn = Callable[[dict], None]


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
) -> dict:
    """Collect a course into output_dir. Returns the final graph state.

    plan_only stops after the reviewed plan: nothing is downloaded, the index lists the plan.
    """
    output_dir = Path(output_dir).resolve()
    state_dir = output_dir / STATE_DIRNAME
    state_dir.mkdir(parents=True, exist_ok=True)
    on_event = on_event or (lambda _event: None)

    saver, conn = checkpointer(state_dir / "checkpoints.sqlite")
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
    finally:
        conn.close()
