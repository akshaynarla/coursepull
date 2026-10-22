"""Reviewer: critiques the Tutor's plan before anything is downloaded.

Evaluator-optimizer pattern: the Reviewer never edits the plan; it raises issues
and the Tutor revises. It has no web or file tools.
"""

from __future__ import annotations

from collections import defaultdict

from ..run_context import RunContext, emit
from ..state import CourseState, Review, ReviewIssue
from .checks import doubts
from .loop import Tool, run_agent
from .tutor import describe_entry, describe_material, describe_pages, describe_term, folder_index

SYSTEM = """\
You are the Reviewer in CoursePull, a team of agents that collects the study materials of an \
open online course so a student can study it offline. The Tutor has made a download plan from \
what the Scouts found. Review it critically before anything is downloaded.

Look for:
- missing: course materials the Scouts found that were excluded or left out but a student needs \
(a required reading excluded, a found deck not planned). Judge against what the Scouts found \
and noted, not against what a course usually has: many lectures (guest talks especially) have \
no slides or recording online, and the Tutor cannot plan what nobody found. An empty module is \
only an issue if materials for it were found and left out.
- noise: planned items that are not course materials (homepages, social links, sponsors). A \
guest speaker's homepage or LinkedIn profile planned as a "lecture video" is noise.
- mislabel: wrong role, module or required flag.
- misplaced: an item filed under the wrong module.
- duplicate: the same resource planned twice.
- mixed offerings: when a term is being collected, any planned item from another offering of the \
course (another term or year) is a blocker. The planned-items-per-page check shows where items \
came from; the page titles show which offering each page belongs to.
The automatic checks below are facts established by code; each one that you agree with is at \
least a major issue. Approve only when there are no blocker or major issues. Reference material \
ids. Be concrete and brief. Call submit_review."""


def automatic_checks(state: CourseState) -> list[str]:
    """Facts code can establish without judgement; the Reviewer weighs them."""
    plan = state["plan"]
    materials = state.get("materials") or {}
    course_map = state.get("course_map")
    notes = []

    paths = defaultdict(list)
    for e in plan.entries:
        paths[(tuple(e.task.folder), (e.task.name or "").lower())].append(e.material_id)
    notes += [f"same file path planned for {ids}" for ids in paths.values() if len(ids) > 1]

    lecture_roles = {"lecture_slides", "lecture_notes", "lecture_video"}
    if course_map:
        covered = {e.module_key for e in plan.entries if e.role in lecture_roles}
        empty = [f"{m.key} {m.title}" for m in course_map.modules if m.key not in covered]
        if empty:
            notes.append(f"modules with no lecture material planned: {empty} (an issue only if lecture "
                         "material for them was found and left out)")

    flagged: dict[str, list[str]] = defaultdict(list)
    for e in plan.entries:
        for concern in doubts(e.task.url, e.role, e.task.kind):
            flagged[concern.split(" (")[0].split(";")[0]].append(e.material_id)
    notes += [f"planned items that are {concern}: {ids}" for concern, ids in flagged.items()]

    required_excluded = [x.material_id for x in plan.excluded
                         if (m := materials.get(x.material_id)) and m.required]
    if required_excluded:
        notes.append(f"excluded although a Scout marked them required: {required_excluded}")
    folders = folder_index(materials)
    planned = {e.material_id for e in plan.entries}
    planned_folders = {key for key, mid in folders.items() if mid in planned}
    inside = [e.material_id for e in plan.entries
              if (m := materials.get(e.material_id)) and set(m.within) & planned_folders]
    if inside:
        notes.append(f"planned files that sit inside a planned Drive folder (would download twice): {inside}")

    per_page: dict[str, int] = defaultdict(int)
    for e in plan.entries:
        if m := materials.get(e.material_id):
            for page in m.found_on.split(" | "):
                per_page[page or "unknown page"] += 1
    if len(per_page) > 1:
        notes.append("planned items per source page: "
                     + ", ".join(f"{page} {n}" for page, n in sorted(per_page.items(), key=lambda kv: -kv[1])))
    return notes


def run_reviewer(state: CourseState, rc: RunContext) -> dict:
    plan = state["plan"]
    materials = state.get("materials") or {}
    course_map = state.get("course_map")
    user = "\n".join([
        f"Course: {state['brief'].course_title}",
        *describe_term(state),
        "Pages the Scouts read:", *(describe_pages(state) or ["- none"]),
        "Course map: " + (course_map.model_dump_json() if course_map else "none"),
        f"Tutor's summary: {plan.summary}",
        "", f"Planned ({len(plan.entries)}):", *[describe_entry(e) for e in plan.entries],
        "", f"Excluded ({len(plan.excluded)}):",
        *[f"{x.material_id}: {x.reason} | {describe_material(materials[x.material_id], folder_index(materials))}"
          for x in plan.excluded if x.material_id in materials],
        "", "Automatic checks:", *([f"- {n}" for n in automatic_checks(state)] or ["- nothing flagged"]),
    ])
    tools = [Tool("submit_review", "Submit your review of the plan.", Review, lambda r: r, final=True)]
    result = run_agent(role="reviewer", name="Reviewer", system=SYSTEM, user=user, tools=tools, rc=rc)
    review = result.final if result.finished else Review(
        approved=True, summary=f"Review incomplete ({result.stopped}); proceeding with the plan.")
    blocking = [i for i in review.issues if i.severity in ("blocker", "major")]
    if review.approved and blocking:  # an approval cannot carry blocking issues
        review = review.model_copy(update={"approved": False})
    emit({"type": "review", "agent": "Reviewer", "approved": review.approved, "issues": len(review.issues),
          "summary": review.summary})
    return {"review": review, "review_rounds": state.get("review_rounds", 0) + 1,
            "usage": {"reviewer": result.tokens}}


__all__ = ["ReviewIssue", "run_reviewer", "automatic_checks"]
