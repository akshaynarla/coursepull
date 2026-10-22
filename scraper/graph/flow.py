"""The LangGraph workflow.

    Orchestrator ──Send──▶ Scout × N ──▶ Orchestrator (more waves?) ──▶ Tutor ⇄ Reviewer
        ▲                                                                  │
        └──────────── recovery ◀── Examiner ◀── execute (download engine) ◀┘
                                       │
                                       └──▶ finish (index, skipped.md, clean-up)

Ending early: Stop goes straight to finish. A spent token budget only stops the
thinking: a plan that already exists is still downloaded (that costs no tokens),
then the run finishes without examination. Finish always writes up what was done.
"""

from __future__ import annotations

import queue

from langgraph.graph import END, START, StateGraph
from langgraph.runtime import Runtime
from langgraph.types import Send

from ..agents.examiner import run_examiner
from ..agents.orchestrator import run_orchestrator
from ..agents.reviewer import run_reviewer
from ..agents.scout import run_scout
from ..agents.tutor import run_tutor
from ..config import MAX_REVIEW_ROUNDS
from ..engine import DownloadEngine
from ..run_context import RunContext, emit
from ..state import CourseState, EngineEvent, PlanEntry, TaskOutcome
from .report import write_report


def _phase(name: str) -> None:
    emit({"type": "phase", "phase": name})


# ---------- nodes ----------


def orchestrator_node(state: CourseState, runtime: Runtime[RunContext]) -> dict:
    rc = runtime.context
    _phase("recovery" if state.get("phase") == "recovery" else "discovery")
    if not state.get("awaiting_scouts") and (reason := rc.stop_reason()):
        return {"stop_reason": reason, "assignments": []}
    return run_orchestrator(state, rc)


def scout_node(payload: dict, runtime: Runtime[RunContext]) -> dict:
    return run_scout(payload["assignment"], payload["brief"], payload["covered"], runtime.context,
                     course_url=payload.get("course_url", ""))


def tutor_node(state: CourseState, runtime: Runtime[RunContext]) -> dict:
    rc = runtime.context
    _phase("planning")
    if reason := rc.stop_reason():
        return {"stop_reason": reason}
    recovering = state.get("phase") == "recovery"
    update = run_tutor(state, rc)
    review_next = (not recovering and state.get("review_rounds", 0) < MAX_REVIEW_ROUNDS
                   and not rc.budget.soft_exceeded())
    return {**update, "phase": "review" if review_next else "download"}


def reviewer_node(state: CourseState, runtime: Runtime[RunContext]) -> dict:
    rc = runtime.context
    _phase("review")
    if reason := rc.stop_reason():
        return {"stop_reason": reason}
    update = run_reviewer(state, rc)
    return {**update, "phase": "download" if update["review"].approved else "planning"}


def needs_run(entry: PlanEntry, outcomes: dict[str, TaskOutcome]) -> bool:
    outcome = outcomes.get(entry.task.task_id)
    if outcome is None or outcome.status == "cancelled":
        return True
    # A failed download is only retried if the plan now says to fetch it differently.
    return outcome.status == "failed" and entry.task.kind is not None and outcome.kind != entry.task.kind


def execute_node(state: CourseState, runtime: Runtime[RunContext]) -> dict:
    """The executor: carries out the plan with the download engine. No LLM involved."""
    rc = runtime.context
    outcomes = state.get("outcomes") or {}
    term = state["brief"].term if state.get("brief") else ""
    todo = [e.task.model_copy(update={"term": term}) if term else e.task
            for e in state["plan"].entries if needs_run(e, outcomes)]
    emit({"type": "phase", "phase": "download", "items": len(todo)})
    if rc.cancel.is_set():
        return {"stop_reason": "cancelled"}
    if not todo:
        return {"phase": "examination"}

    events: queue.SimpleQueue[EngineEvent] = queue.SimpleQueue()
    with DownloadEngine(rc.output_dir, cancel_event=rc.cancel, on_event=events.put) as engine:
        engine.submit(todo)
        # Engine callbacks arrive on worker threads; LangGraph's stream writer only works on
        # this node's thread, so events are relayed from here.
        while True:
            finished = engine.wait(timeout=0.25)
            _relay(events, engine)
            if finished:
                break
        new_outcomes = engine.outcomes()
    update: dict = {"outcomes": new_outcomes, "phase": "examination"}
    if rc.cancel.is_set():
        update["stop_reason"] = "cancelled"
    return update


def _relay(events: queue.SimpleQueue, engine: DownloadEngine) -> None:
    if events.empty():
        return
    tasks = engine.tasks()
    stats = engine.stats()
    while not events.empty():
        event: EngineEvent = events.get()
        task = tasks.get(event.task_id)
        payload = {"type": "download", "event": event.type, "task_id": event.task_id, "url": event.url,
                   "name": task.name if task else None, "bytes_done": event.bytes_done,
                   "bytes_total": event.bytes_total, "done": stats["done"] + stats["failed"] + stats["cancelled"],
                   "total": stats["total"]}
        if event.outcome is not None:
            payload.update(status=event.outcome.status, files=event.outcome.files,
                           error=event.outcome.error_kind.value if event.outcome.error_kind else None,
                           message=event.outcome.message)
        emit(payload)


def examiner_node(state: CourseState, runtime: Runtime[RunContext]) -> dict:
    rc = runtime.context
    _phase("examination")
    if reason := rc.stop_reason():
        return {"stop_reason": reason}
    return run_examiner(state, rc)


def finish_node(state: CourseState, runtime: Runtime[RunContext]) -> dict:
    _phase("finished")
    return write_report(state, runtime.context)


# ---------- routing ----------


def _stopped(state: CourseState) -> bool:
    return bool(state.get("stop_reason"))


def _plan_ready(state: CourseState) -> bool:
    plan = state.get("plan")
    return bool(plan and plan.entries)


def after_orchestrator(state: CourseState):
    if _stopped(state) or state.get("phase") == "finished":
        return "finish"
    if state.get("assignments"):
        covered = sorted({u for r in state.get("reports") or [] for u in r.pages_visited})
        return [Send("scout", {"assignment": a, "brief": state["brief"], "covered": covered,
                               "course_url": state["course_url"]})
                for a in state["assignments"]]
    return "tutor"


def _execute_or_stop(state: CourseState) -> str:
    return "finish" if state.get("plan_only") else "execute"


def _out_of_budget(state: CourseState) -> str:
    """No more thinking, but a plan that exists is still worth downloading."""
    return _execute_or_stop(state) if _plan_ready(state) else "finish"


def after_tutor(state: CourseState) -> str:
    if state.get("stop_reason") == "budget":
        return _out_of_budget(state)
    if _stopped(state):
        return "finish"
    return "reviewer" if state.get("phase") == "review" else _execute_or_stop(state)


def after_reviewer(state: CourseState) -> str:
    if state.get("stop_reason") == "budget":
        return _out_of_budget(state)
    if _stopped(state):
        return "finish"
    return _execute_or_stop(state) if state.get("phase") == "download" else "tutor"


def after_execute(state: CourseState) -> str:
    return "finish" if _stopped(state) else "examiner"


def after_examiner(state: CourseState) -> str:
    if _stopped(state):
        return "finish"
    return "orchestrator" if state.get("phase") == "recovery" else "finish"


def build_graph() -> StateGraph:
    graph = StateGraph(CourseState, context_schema=RunContext)
    graph.add_node("orchestrator", orchestrator_node)
    graph.add_node("scout", scout_node)
    graph.add_node("tutor", tutor_node)
    graph.add_node("reviewer", reviewer_node)
    graph.add_node("execute", execute_node)
    graph.add_node("examiner", examiner_node)
    graph.add_node("finish", finish_node)

    graph.add_edge(START, "orchestrator")
    graph.add_conditional_edges("orchestrator", after_orchestrator, ["scout", "tutor", "finish"])
    graph.add_edge("scout", "orchestrator")
    graph.add_conditional_edges("tutor", after_tutor, ["reviewer", "execute", "finish"])
    graph.add_conditional_edges("reviewer", after_reviewer, ["tutor", "execute", "finish"])
    graph.add_conditional_edges("execute", after_execute, ["examiner", "finish"])
    graph.add_conditional_edges("examiner", after_examiner, ["orchestrator", "finish"])
    graph.add_edge("finish", END)
    return graph
