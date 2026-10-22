"""Examiner: independently checks the downloads against the plan and the course map.

It did not write the plan, so it is not grading its own work. Code inspects
every file first (type, size, pages, first-page text); the Examiner judges
whether each file is what the plan says it is, whether the course is complete,
and what is worth recovering.
"""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel

from ..config import MAX_RECOVERY_ROUNDS
from ..run_context import RunContext, emit
from ..state import CourseState, Examination, PlanEntry, TaskOutcome
from .loop import Tool, ToolError, run_agent
from .toolbox import inspect_file

SYSTEM = """\
You are the Examiner in CoursePull, a team of agents that collects the study materials of an \
open online course so a student can study it offline. You did not make the download plan; you \
check its result independently.

For each planned item you see the download outcome and what the files really contain (type, \
size, pages, first-page text). Check:
- Is each file the material it claims to be? E.g. "Lecture 5 slides" should be about lecture 5, \
not a login page, an error page or another lecture. Use inspect_file for a closer look when in \
doubt.
- Is the course complete according to the course map (every module has its lecture material)?
- Failures: which are expected (login required, paywalled) and which are worth recovering \
(a dead link that may have a mirror, a paper with an arXiv version, a page with a PDF version)?

List verdicts only for problem items. Put recovery requests only where an open alternative is \
likely to exist; give a concrete suggestion. Pass when the course is essentially complete.
Call submit_examination."""


class InspectArgs(BaseModel):
    task_id: str


def _files_of(outcome: TaskOutcome, outcomes: dict[str, TaskOutcome]) -> list[str]:
    files = list(outcome.files)
    for child_id in outcome.children:
        if child := outcomes.get(child_id):
            files += child.files
    return files


def describe_result(entry: PlanEntry, outcomes: dict[str, TaskOutcome], text_chars: int = 160) -> str:
    head = f"{entry.task.task_id} | {entry.material_id} | {entry.role} | module {entry.module_key or '-'} | {entry.task.name!r}"
    outcome = outcomes.get(entry.task.task_id)
    if outcome is None:
        return f"{head} -> NOT RUN"
    if outcome.status != "done":
        kind = outcome.error_kind.value if outcome.error_kind else "error"
        return f"{head} -> {outcome.status.upper()} {kind}: {outcome.message} ({outcome.url[:100]})"
    if outcome.children:
        children = [outcomes[c] for c in outcome.children if c in outcomes]
        ok = sum(1 for c in children if c.status == "done")
        failed = [f"{Path(c.url).name or c.url}: {c.error_kind.value if c.error_kind else c.status}"
                  for c in children if c.status != "done"]
        return f"{head} -> folder: {ok}/{len(children)} entries downloaded" + (
            f"; failed: {failed[:5]}" if failed else "")
    looks = []
    for path in outcome.files[:2]:
        info = inspect_file(Path(path), text_chars=text_chars)
        looks.append(", ".join(f"{k}={v}" for k, v in info.items() if k not in ("names",)))
    return f"{head} -> done: " + " || ".join(looks) + (f" (+{len(outcome.files) - 2} more files)" if len(outcome.files) > 2 else "")


def run_examiner(state: CourseState, rc: RunContext) -> dict:
    plan = state["plan"]
    outcomes = state.get("outcomes") or {}
    course_map = state.get("course_map")
    by_task = {e.task.task_id: e for e in plan.entries}

    def inspect(args: InspectArgs) -> dict:
        entry = by_task.get(args.task_id.strip())
        if entry is None or entry.task.task_id not in outcomes:
            raise ToolError(f"no downloaded task {args.task_id!r}")
        files = _files_of(outcomes[entry.task.task_id], outcomes)
        return {"files": [inspect_file(Path(f), text_chars=1200) for f in files[:4]]}

    user = "\n".join([
        f"Course: {state['brief'].course_title}",
        "Course map: " + (course_map.model_dump_json() if course_map else "none"),
        "", f"Download results ({len(plan.entries)} planned items):",
        *[describe_result(e, outcomes) for e in plan.entries],
    ])
    tools = [
        Tool("inspect_file", "Look closer at the files of one task: more text, all files.", InspectArgs, inspect),
        Tool("submit_examination", "Submit your examination.", Examination, lambda e: e, final=True),
    ]
    result = run_agent(role="examiner", name="Examiner", system=SYSTEM, user=user, tools=tools, rc=rc)
    if result.finished:
        exam: Examination = result.final
    else:
        failed = sum(1 for e in plan.entries if (o := outcomes.get(e.task.task_id)) and o.status != "done")
        exam = Examination(passed=failed == 0, summary=f"Examination incomplete ({result.stopped}); "
                                                       f"{failed} planned items failed.")
    emit({"type": "examination", "agent": "Examiner", "passed": exam.passed, "summary": exam.summary,
          "recovery": len(exam.recovery), "gaps": len(exam.gaps)})

    rounds = state.get("recovery_rounds", 0)
    recover = (not exam.passed and exam.recovery and rounds < MAX_RECOVERY_ROUNDS
               and not rc.budget.soft_exceeded() and not rc.stop_reason())
    update = {"examination": exam, "usage": {"examiner": result.tokens},
              "phase": "recovery" if recover else "finished"}
    if recover:
        update["recovery_rounds"] = rounds + 1
    return update
